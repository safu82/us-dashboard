#!/usr/bin/env python3
"""Weekly GLOBAL TAPE snapshot -> global_indices (+ global_news.json).

The US market doesn't trade in a vacuum. A handful of foreign equity indices and
cross-asset prices lead or co-move with US indices, and — because Asia closes
before the US opens — they're a same-day tell. This pulls a compact set:

  Asian equities  ^TWII  Taiwan (TAIEX)   — TSMC / the AI + semiconductor supply chain
                  ^KS11  KOSPI (Korea)    — Samsung + SK Hynix / the memory cycle (Micron)
                  ^N225  Nikkei 225       — global risk appetite + the yen carry trade
                  ^HSI   Hang Seng        — China demand (materials, industrials, luxury)
  FX              DX-Y.NYB  US Dollar Index — a rising dollar is an earnings headwind
                  JPY=X     USD/JPY        — the carry-trade gauge (yen strength = risk-off)
  Commodities     HG=F   Copper           — "Dr. Copper", the global-growth barometer
                  BZ=F   Brent crude      — the direct oil / supply-shock gauge
  Crypto          BTC-USD Bitcoin         — 24/7 risk-sentiment proxy

TWO numbers per market, so a backward-looking weekly figure never hides the path:

  1. WEEK (US-aligned): this market's close on/before the US latest date vs its close
     on/before the prior US-week close. Same window as the US RS/breadth numbers, so
     it's apples-to-apples (not each market's own latest bar, which mixed windows and
     once made a market that ROSE ~5% on its US week read as slightly down).
  2. SINCE US CLOSE: the cumulative move in this market's sessions that traded AFTER
     the US last print — e.g. an Asian Friday when the US was shut for a holiday. That
     move is un-priced into the US close the newsletter is built on, so it's the forward
     tell for the next US session. Plus the latest single-session move to show direction.

Then, for the notable movers, we fetch explanatory GDELT headlines (keyless) into
global_news.json so the synthesis layer can say WHY a market moved and what it means
for US stocks (a KOSPI memory-cycle plunge is a Micron read), not just report the %.

Optional enrichment: a yfinance/GDELT outage degrades (keeps last week's rows / skips
news) rather than blocking the newsletter. Run weekly, after compute_market_health.py
(for the US anchor dates) and before build_data_pack.py.
Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (.env auto-loaded).
"""

import json
import os
import sys
import time
from datetime import date, datetime, timezone

import pandas as pd
import yfinance as yf
from supabase import create_client
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

sb = create_client(URL, KEY)

HISTORY_PERIOD = '2mo'      # enough to reach back past the prior US week + holidays
ATTEMPTS = 3
SLEEP = 2

# Notability thresholds (any one trips it) — a mover gets news fetched + flagged.
WK_NOTABLE_PCT = 4.0        # |US-week move|
SINCE_US_NOTABLE_PCT = 2.0  # |move since the US close| (needs sessions after it)
DAY_NOTABLE_PCT = 3.0       # |latest single-session move|

# (symbol, display name, category, order, GDELT news query, US linkage for the writer).
# The query is data-first: only spent when the move is notable. It MUST be a flat OR-chain
# — the provider wraps it in (...) and GDELT only allows parentheses around OR'd statements,
# so AND-ing parenthesised groups is rejected. Multi-word phrases keep each chain topical.
INSTRUMENTS = [
    ('^TWII', 'Taiwan (TAIEX)', 'asia_equity', 1,
     '"Taiwan stocks" OR "Taiwan shares" OR "Taiwan semiconductor" OR "TSMC earnings" OR "Taiwan chipmakers"',
     'TSMC & the AI / semiconductor supply chain — the lead tell for US chips (NVDA/AMD/AVGO)'),
    ('^KS11', 'KOSPI (Korea)', 'asia_equity', 2,
     'KOSPI OR "SK Hynix" OR "Samsung Electronics" OR "Korean stocks" OR "Korea chipmakers" OR "memory chip"',
     'Samsung + SK Hynix — the global memory cycle; a direct read on US memory (Micron/MU) and the semis'),
    ('^N225', 'Nikkei 225 (Japan)', 'asia_equity', 3,
     'Nikkei OR "Japanese stocks" OR "Tokyo stocks" OR "Bank of Japan" OR "yen carry"',
     'global risk appetite + the yen carry trade (a sharp drop precedes US risk-off)'),
    ('^HSI', 'Hang Seng (HK/China)', 'asia_equity', 4,
     '"Hang Seng" OR "Hong Kong stocks" OR "China stocks" OR "China stimulus" OR "Chinese equities"',
     'China demand — materials, industrials, copper, US multinationals\' China revenue'),
    ('DX-Y.NYB', 'US Dollar Index', 'fx', 5,
     '"US dollar" OR "dollar index" OR DXY OR "dollar rally" OR "dollar weakness"',
     'the US dollar — a rising dollar is a headwind for US multinationals & commodities'),
    ('JPY=X', 'USD/JPY', 'fx', 6,
     '"Japanese yen" OR "USD/JPY" OR "yen carry trade" OR "Bank of Japan" OR "yen intervention"',
     'USD/JPY, the carry-trade gauge — fast yen strength = a carry unwind = global risk-off'),
    ('HG=F', 'Copper', 'commodity', 7,
     '"copper price" OR "copper prices" OR "copper demand" OR "copper futures" OR "Dr Copper"',
     '"Dr. Copper", the global-growth barometer (ties to the copper theme when it runs)'),
    ('BZ=F', 'Brent crude', 'commodity', 8,
     '"Brent crude" OR "oil prices" OR "crude oil" OR OPEC OR "oil supply"',
     'Brent crude — the direct oil / supply-shock gauge behind any Middle-East headline'),
    ('BTC-USD', 'Bitcoin', 'crypto', 9,
     'bitcoin OR cryptocurrency OR "crypto rally" OR "crypto selloff" OR "bitcoin ETF"',
     '24/7 risk-sentiment proxy — trades through the weekend when equities are shut'),
]


# ── US anchor dates (mirror build_data_pack) ─────────────────────────────────
def previous_week_close(dates):
    latest_wk = date.fromisoformat(dates[-1]).isocalendar()[:2]
    prior = [d for d in dates if date.fromisoformat(d).isocalendar()[:2] < latest_wk]
    return prior[-1] if prior else dates[0]


def us_anchor():
    """(us_latest, us_week_ago) from market_health — the SAME window the data pack uses,
    so every market's weekly number is comparable to the US RS/breadth numbers. Falls
    back to today / 7 days ago if market_health is empty."""
    try:
        rows = (sb.table('market_health').select('snapshot_date')
                .order('snapshot_date').execute().data or [])
        dates = [r['snapshot_date'] for r in rows]
        if dates:
            return dates[-1], previous_week_close(dates)
    except Exception as e:
        print(f'  WARN could not read market_health: {str(e)[:60]}')
    today = datetime.now(timezone.utc).date()
    return today.isoformat(), (today.fromordinal(today.toordinal() - 7)).isoformat()


def on_or_before(series, target):
    chosen = None
    for d, v in series:
        if d <= target:
            chosen = (d, v)
        else:
            break
    return chosen


def closes_for(symbol):
    """Ascending [(iso_date, close)] for one symbol, tolerating yfinance's single- vs
    MultiIndex column shapes. Returns [] on failure (caller degrades)."""
    for a in range(1, ATTEMPTS + 1):
        try:
            hist = yf.download(symbol, period=HISTORY_PERIOD, interval='1d',
                               auto_adjust=True, progress=False, threads=False)
            if hist is not None and not hist.empty:
                cols = hist.columns
                if isinstance(cols, pd.MultiIndex):
                    close = (hist['Close'][symbol] if symbol in hist['Close'].columns
                             else hist['Close'].iloc[:, 0])
                else:
                    close = hist['Close']
                out = [(d.strftime('%Y-%m-%d'), float(v))
                       for d, v in close.dropna().items()]
                out.sort(key=lambda x: x[0])
                if out:
                    return out
            print(f'  ({symbol} empty, retry {a})')
        except Exception as e:
            print(f'  ({symbol} err {a}: {str(e)[:50]})')
        time.sleep(SLEEP * a)
    return []


def compute(symbol, series, us_latest, us_week_ago):
    """The two-window read for one market. Returns a dict or None."""
    if len(series) < 2:
        return None
    wf = on_or_before(series, us_week_ago)          # window start (prior US-week close)
    wt = on_or_before(series, us_latest)            # window end (US latest close)
    weekly = ((wt[1] / wf[1]) - 1) * 100 if (wf and wt and wf[1]) else None

    as_of, last = series[-1]
    prev = series[-2][1]
    latest_session = ((last / prev) - 1) * 100 if prev else None

    after = [(d, v) for d, v in series if wt and d > wt[0]]   # sessions after the US close
    since_us = ((last / wt[1]) - 1) * 100 if (after and wt and wt[1]) else None

    def notable(x, thr):
        return x is not None and abs(x) >= thr
    is_notable = (notable(weekly, WK_NOTABLE_PCT)
                  or (after and notable(since_us, SINCE_US_NOTABLE_PCT))
                  or notable(latest_session, DAY_NOTABLE_PCT))

    return {
        'last_close': round(last, 4), 'as_of': as_of,
        'week_from': wf[0] if wf else None, 'week_to': wt[0] if wt else None,
        'prev_close': round(wf[1], 4) if wf else None,
        'pct_chg_wk': round(weekly, 2) if weekly is not None else None,
        'latest_session_pct': round(latest_session, 2) if latest_session is not None else None,
        'since_us_pct': round(since_us, 2) if since_us is not None else None,
        'since_us_days': len(after),
        'is_notable': bool(is_notable),
    }


def move_summary(name, c, tell):
    """A crisp deterministic description of the move for the news artifact / logs."""
    bits = []
    if c['pct_chg_wk'] is not None:
        bits.append(f"{c['pct_chg_wk']:+.1f}% over the US week ({c['week_from']}→{c['week_to']})")
    if c['since_us_days'] and c['since_us_pct'] is not None:
        bits.append(f"then {c['since_us_pct']:+.1f}% in {c['since_us_days']} session(s) "
                    f"after the US close (through {c['as_of']}, un-priced into the US tape)")
    return f"{name}: " + ", ".join(bits) + f". Linkage: {tell}"


def fetch_news(movers):
    """GDELT explanatory headlines for the notable movers -> list of news blocks.
    Keyless; a failure/omission just drops that block (synth degrades)."""
    try:
        from news_providers import get_provider
        provider = get_provider('gdelt')
    except Exception as e:
        print(f'  news provider unavailable ({str(e)[:60]}) — skipping global news')
        return []
    blocks = []
    for symbol, name, query, tell, c in movers:
        arts = provider.fetch_articles(query, max_articles=50, timespan='7d')
        seen, top = set(), []
        for a in arts:
            if a.domain in seen:
                continue
            seen.add(a.domain)
            top.append({'title': a.title, 'domain': a.domain, 'date': a.date, 'url': a.url})
            if len(top) >= 4:
                break
        blocks.append({
            'symbol': symbol, 'name': name,
            'weekly_pct': c['pct_chg_wk'], 'since_us_pct': c['since_us_pct'],
            'since_us_days': c['since_us_days'], 'latest_date': c['as_of'],
            'us_linkage': tell, 'summary': move_summary(name, c, tell),
            'query': query, 'top_headlines': top,
        })
        print(f'    news {symbol:<9} {len(arts):>3} arts, {len(top)} headlines')
        time.sleep(5)          # GDELT throttles bursts
    return blocks


def main():
    print('=' * 64)
    print('FETCH GLOBAL INDICES  ',
          datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 64)
    us_latest, us_week_ago = us_anchor()
    print(f'US anchor window: {us_week_ago} (prior-week close) -> {us_latest} (latest)\n')

    rows, movers, missed = [], [], []
    for symbol, name, category, order, query, tell in INSTRUMENTS:
        series = closes_for(symbol)
        if not series:
            missed.append(symbol)
            continue
        c = compute(symbol, series, us_latest, us_week_ago)
        if not c:
            missed.append(symbol)
            continue
        rows.append({'snapshot_date': us_latest, 'symbol': symbol, 'name': name,
                     'category': category, 'display_order': order,
                     'updated_at': datetime.now(timezone.utc).isoformat(), **c})
        wk = f"{c['pct_chg_wk']:+.2f}%" if c['pct_chg_wk'] is not None else 'n/a'
        su = (f", since US {c['since_us_pct']:+.2f}% ({c['since_us_days']}d)"
              if c['since_us_days'] else '')
        flag = '  *NOTABLE' if c['is_notable'] else ''
        print(f"  {symbol:<9} {name:<22} {c['last_close']:>11,.2f}  wk {wk}{su}{flag}")
        if c['is_notable']:
            movers.append((symbol, name, query, tell, c))
        time.sleep(SLEEP)

    if rows:
        sb.table('global_indices').upsert(rows, on_conflict='snapshot_date,symbol').execute()
        print(f'\nUpserted {len(rows)} rows for {us_latest}.')
    else:
        print('\nNo rows fetched — leaving prior global_indices data in place.')
    if missed:
        print(f'Missed (degraded): {", ".join(missed)}')

    # Explanatory news for the notable movers (data-first: only when something moved).
    news_blocks = []
    if movers:
        print(f'\nFetching explanatory news for {len(movers)} notable mover(s):')
        news_blocks = fetch_news(movers)
    out = {'generated': datetime.now(timezone.utc).isoformat(),
           'us_latest': us_latest, 'us_week_ago': us_week_ago,
           'provider': 'gdelt', 'movers': news_blocks}
    open(os.path.join(BASE, 'global_news.json'), 'w', encoding='utf-8').write(
        json.dumps(out, indent=2))
    print(f"[global_news.json written - {len(news_blocks)} mover block(s)]")
    print('Done.')


if __name__ == '__main__':
    main()
