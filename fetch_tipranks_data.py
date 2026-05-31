#!/usr/bin/env python3
"""Pull TipRanks-proprietary signals for the holdings + open paper-trade
universe and merge into analyst_ratings.

Uses TipRanks' MCP server (https://mcp.tipranks.com/mcp/) — REST-callable via
JSON-RPC 2.0 over HTTP+SSE. One get_assets_data call accepts a comma-separated
ticker list and returns smartScore, bestAnalystConsensus, hedge-fund / insider /
blogger sentiment, next earnings date, and more — all in a single billed call.

Quota note: free tier = 5 req/min, 25 req/day. We batch all portfolio + paper
tickers into ONE call, leaving 24 calls/day free for ad-hoc deep-dive lookups.

Env: TIPRANKS_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY (.env auto-loaded).
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests
from supabase import create_client
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

SB_URL = os.environ.get('SUPABASE_URL')
SB_KEY = os.environ.get('SUPABASE_SERVICE_KEY') or os.environ.get('SUPABASE_KEY')
TR_KEY = os.environ.get('TIPRANKS_API_KEY')
if not SB_URL or not SB_KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')
if not TR_KEY:
    sys.exit('ERROR: TIPRANKS_API_KEY must be set')

MCP_URL = 'https://mcp.tipranks.com/mcp/'
HEADERS = {
    'Authorization': f'Bearer {TR_KEY}',
    'Accept': 'application/json, text/event-stream',
    'Content-Type': 'application/json',
}


def sse_extract_json(text):
    """SSE response is `event: message\\ndata: {json}\\n\\n`. Pull the first data line."""
    for line in (text or '').split('\n'):
        if line.startswith('data: '):
            return json.loads(line[6:])
    return None


def mcp_call(session, sid, method, params=None, req_id=2):
    """Send a JSON-RPC request to the MCP server. Returns the parsed `result`."""
    headers = dict(HEADERS)
    if sid:
        headers['mcp-session-id'] = sid
    payload = {'jsonrpc': '2.0', 'id': req_id, 'method': method}
    if params is not None:
        payload['params'] = params
    r = session.post(MCP_URL, headers=headers, json=payload, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f'MCP {method} -> HTTP {r.status_code}: {r.text[:300]}')
    parsed = sse_extract_json(r.text)
    if not parsed:
        return None
    if parsed.get('error'):
        raise RuntimeError(f'MCP {method} error: {parsed["error"]}')
    return parsed.get('result')


def mcp_notify(session, sid, method, params=None):
    """Notifications don't expect a response body."""
    headers = dict(HEADERS)
    if sid:
        headers['mcp-session-id'] = sid
    payload = {'jsonrpc': '2.0', 'method': method}
    if params is not None:
        payload['params'] = params
    session.post(MCP_URL, headers=headers, json=payload, timeout=15)


def open_mcp_session():
    """Initialize an MCP session. Returns (session, session_id)."""
    session = requests.Session()
    init_params = {
        'protocolVersion': '2024-11-05',
        'capabilities': {},
        'clientInfo': {'name': 'us-dashboard', 'version': '0.1'},
    }
    r = session.post(MCP_URL, headers=HEADERS,
                     json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                           'params': init_params}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f'MCP initialize -> HTTP {r.status_code}: {r.text[:300]}')
    sid = r.headers.get('mcp-session-id') or r.headers.get('Mcp-Session-Id')
    mcp_notify(session, sid, 'notifications/initialized')
    return session, sid


# TipRanks get_assets_data caps responses at 100 tickers per request. Confirmed
# by passing 200 and getting only 101 back. So we chunk the universe accordingly.
TICKERS_PER_CALL = 100
# Rate limit: free tier = 5 req/min. Sleep between batches to stay under.
SLEEP_BETWEEN_BATCHES = 15  # seconds


def get_universe(sb):
    """Full scan universe (S&P 500 + NASDAQ 100 + held tickers ~ 518 names).
    Chunked into batches of TICKERS_PER_CALL for the get_assets_data call.
    Total calls: ceil(518 / 100) = 6 per day, well within the 25/day free tier."""
    if len(sys.argv) > 1:
        return sorted({t.strip().upper() for t in sys.argv[1:]})
    tickers = set()
    # us_stock_sectors covers the full scan universe
    page, frm = 1000, 0
    while True:
        resp = sb.table('us_stock_sectors').select('ticker').range(frm, frm + page - 1).execute()
        for r in (resp.data or []):
            if r.get('ticker'):
                tickers.add(r['ticker'])
        if not resp.data or len(resp.data) < page:
            break
        frm += page
    # Defensive: union holdings + open/pending paper in case anything's missing
    for h in (sb.table('holdings').select('ticker').execute().data or []):
        if h.get('ticker'):
            tickers.add(h['ticker'])
    for p in (sb.table('paper_trades').select('ticker')
                .in_('status', ['pending', 'open']).execute().data or []):
        if p.get('ticker'):
            tickers.add(p['ticker'])
    return sorted(tickers)


def chunks(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def _to_date(v):
    """TipRanks returns dates as '2026-08-26T00:00:00'. Keep only the date part."""
    if not v:
        return None
    return str(v)[:10]


def _pct(v):
    """Decimal -> percent (0.46 -> 46.00)."""
    if v is None:
        return None
    try:
        return round(float(v) * 100, 2)
    except (TypeError, ValueError):
        return None


def _num(v, digits=4):
    if v is None:
        return None
    try:
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


def _int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def upsert_ticker_row(sb, ad, now_iso):
    """Map one assetsData entry -> partial analyst_ratings update."""
    ticker = ad.get('ticker')
    if not ticker:
        return False
    best_c = ad.get('bestAnalystConsensus') or {}
    hf     = ad.get('hedgeFundSentimentData') or {}
    ins    = ad.get('insiderSentimentData') or {}
    blog   = ad.get('bloggerSentimentData') or {}
    cal    = ad.get('calendarEarningsData') or {}

    patch = {
        'ticker': ticker,
        'smart_score':             _num(ad.get('smartScore'), 2),
        'best_consensus':          best_c.get('consensus'),
        'best_target_mean':        _num(ad.get('bestPriceTarget'), 4),
        'best_target_upside_pct':  _pct(ad.get('bestPriceTargetUpside')),
        'best_distribution':       best_c.get('distribution'),
        'price_target_upside_pct': _pct(ad.get('priceTargetUpside')),
        'hedge_fund_score':        _num(ad.get('hedgeFundsScore'), 4),
        'hedge_fund_rating':       hf.get('rating'),
        'insider_score':           _num(ad.get('insiderScore'), 4),
        'insider_rating':          ins.get('rating'),
        'blogger_bullish_count':   _int(blog.get('bullishCount')),
        'blogger_bearish_count':   _int(blog.get('bearishCount')),
        'blogger_rating':          blog.get('rating'),
        'news_sentiment':          _num(ad.get('newsSentiment'), 4),
        'investor_score':          _num(ad.get('investorScore'), 4),
        'buzz':                    _num(ad.get('buzz'), 4),
        'next_earnings_date':      _to_date(cal.get('nextEarningsDate')),
        'last_earnings_date':      _to_date(cal.get('lastEarningsDate')),
        'tipranks_last_updated':   now_iso,
    }
    sb.table('analyst_ratings').upsert(patch, on_conflict='ticker').execute()
    return True


def main():
    print('=' * 60)
    print('TIPRANKS FETCH  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 60)
    sb = create_client(SB_URL, SB_KEY)
    tickers = get_universe(sb)
    if not tickers:
        sys.exit('No tickers in universe')
    n_batches = (len(tickers) + TICKERS_PER_CALL - 1) // TICKERS_PER_CALL
    print(f'Universe: {len(tickers)} tickers in {n_batches} batches of up to {TICKERS_PER_CALL}')

    try:
        session, sid = open_mcp_session()
    except Exception as e:
        sys.exit(f'Failed to open MCP session: {e}')

    import time
    now_iso = datetime.now(timezone.utc).isoformat()
    total_ok = 0
    total_rows = 0
    counts_by_consensus = {}

    for i, batch in enumerate(chunks(tickers, TICKERS_PER_CALL), start=1):
        try:
            result = mcp_call(session, sid, 'tools/call', {
                'name': 'get_assets_data',
                'arguments': {'tickers': ','.join(batch)},
            }, req_id=10 + i)
        except Exception as e:
            print(f'  batch {i}/{n_batches}: FAILED -- {e}')
            continue

        try:
            data = json.loads(result['content'][0]['text'])
            rows = data.get('assetsData', [])
        except Exception as e:
            print(f'  batch {i}/{n_batches}: parse failed -- {e}')
            continue

        total_rows += len(rows)
        batch_ok = 0
        for ad in rows:
            try:
                if upsert_ticker_row(sb, ad, now_iso):
                    batch_ok += 1
                    bc = (ad.get('bestAnalystConsensus') or {}).get('consensus', '?')
                    counts_by_consensus[bc] = counts_by_consensus.get(bc, 0) + 1
            except Exception as e:
                print(f'    {ad.get("ticker", "?")}: upsert failed: {e}')
        total_ok += batch_ok
        print(f'  batch {i}/{n_batches}: requested {len(batch)} -> got {len(rows)} -> upserted {batch_ok}')

        # Respect rate limit: 5 req/min = 12s between calls minimum
        if i < n_batches:
            time.sleep(SLEEP_BETWEEN_BATCHES)

    print('=' * 60)
    print(f'Done. {total_ok}/{total_rows} tickers updated (universe size {len(tickers)})')
    if counts_by_consensus:
        print('Best-analyst consensus distribution:')
        for k in ('StrongBuy', 'Buy', 'Hold', 'Sell', 'StrongSell'):
            if counts_by_consensus.get(k):
                print(f'  {k:11s}: {counts_by_consensus[k]}')


if __name__ == '__main__':
    main()
