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


def get_universe(sb):
    """Holdings + open/pending paper trades only (small set, single batched call).
    The whole-universe expansion would require a paid TipRanks tier."""
    if len(sys.argv) > 1:
        return sorted({t.strip().upper() for t in sys.argv[1:]})
    tickers = set()
    for h in (sb.table('holdings').select('ticker').execute().data or []):
        if h.get('ticker'):
            tickers.add(h['ticker'])
    for p in (sb.table('paper_trades').select('ticker')
                .in_('status', ['pending', 'open']).execute().data or []):
        if p.get('ticker'):
            tickers.add(p['ticker'])
    return sorted(tickers)


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
    print(f'Universe ({len(tickers)} tickers): {", ".join(tickers)}')

    try:
        session, sid = open_mcp_session()
    except Exception as e:
        sys.exit(f'Failed to open MCP session: {e}')

    # One batched call covers everything — keeps us within the 25/day free tier
    try:
        result = mcp_call(session, sid, 'tools/call', {
            'name': 'get_assets_data',
            'arguments': {'tickers': ','.join(tickers)},
        })
    except Exception as e:
        sys.exit(f'get_assets_data failed: {e}')

    # Tool responses come back as text content (JSON-encoded inside)
    try:
        content_text = result['content'][0]['text']
        data = json.loads(content_text)
        rows = data.get('assetsData', [])
    except Exception as e:
        sys.exit(f'Failed to parse tool response: {e}\nRaw: {str(result)[:500]}')

    print(f'Got {len(rows)} ticker rows back')
    now_iso = datetime.now(timezone.utc).isoformat()
    ok = 0
    for ad in rows:
        try:
            if upsert_ticker_row(sb, ad, now_iso):
                ok += 1
                ss = ad.get('smartScore', '?')
                bc = (ad.get('bestAnalystConsensus') or {}).get('consensus', '?')
                bt = ad.get('bestPriceTarget')
                up = _pct(ad.get('bestPriceTargetUpside'))
                hf = (ad.get('hedgeFundSentimentData') or {}).get('rating', '?')
                ins = (ad.get('insiderSentimentData') or {}).get('rating', '?')
                bt_str = f'${bt:.0f}' if bt else '—'
                up_str = f'{up:+.1f}%' if up is not None else '—'
                print(f'  {ad["ticker"]:6s} SS={ss:>2}  best={bc:10s} target={bt_str:>7s} ({up_str})  HF={hf:6s} INS={ins:6s}')
        except Exception as e:
            print(f'  {ad.get("ticker", "?")}: upsert failed: {e}')
    print('=' * 60)
    print(f'Done. {ok}/{len(rows)} tickers updated')


if __name__ == '__main__':
    main()
