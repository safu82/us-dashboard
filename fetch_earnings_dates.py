#!/usr/bin/env python3
"""yfinance earnings-date calendar -> stock_fundamentals.{last,next}_earnings_date.

Replaces the stale/sparse TipRanks earnings dates as the gate for the newsletter's
"earnings reactions" section. For each ticker we pull yfinance's earnings-date
table and derive:
  - last_earnings_date : most recent announcement on/before today
  - next_earnings_date : soonest announcement after today

detect_market_events.py gates earnings events on last_earnings_date (a real report
this week) and surfaces next_earnings_date for the Key-Events calendar.

Sequential (yfinance throttles concurrency at scale — same lesson as OHLC/metrics),
with per-ticker retry + a short sleep.

Usage:
  python fetch_earnings_dates.py            # full us_stock_sectors universe
  python fetch_earnings_dates.py NVDA MU    # only named tickers (smoke test)

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (.env auto-loaded).
"""

import os
import sys
import time
from datetime import datetime, timezone

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

SLEEP = 0.3
ATTEMPTS = 3


def get_universe():
    if len(sys.argv) > 1:
        return [t.strip().upper() for t in sys.argv[1:]]
    tickers, seen, page, frm = [], set(), 1000, 0
    while True:
        resp = (sb.table('us_stock_sectors').select('ticker')
                  .range(frm, frm + page - 1).execute())
        for r in (resp.data or []):
            t = r.get('ticker')
            if t and t not in seen:
                seen.add(t)
                tickers.append(t)
        if not resp.data or len(resp.data) < page:
            break
        frm += page
    return tickers


def earnings_dates_for(tk):
    """Return (last_earnings_date, next_earnings_date) as ISO strings or None.

    Primary source is .info (earningsTimestamp / earningsTimestampStart/End),
    which yfinance keeps CURRENT. The historical .earnings_dates table is often
    stale (e.g. MU's most-recent row was 2025-06-25 even after its 2026-06-24
    report), so we use it only as supplementary deep history for past dates.
    """
    today = datetime.now(timezone.utc).date()
    t = yf.Ticker(tk)
    past, future = [], []

    def add_ts(ts):
        if isinstance(ts, (int, float)) and ts > 0:
            d = datetime.fromtimestamp(ts, timezone.utc).date()
            (past if d <= today else future).append(d)

    try:
        info = t.info or {}
    except Exception:
        info = {}
    add_ts(info.get('earningsTimestamp'))         # most recent (or imminent) report
    add_ts(info.get('earningsTimestampStart'))    # next estimated window
    add_ts(info.get('earningsTimestampEnd'))

    try:                                          # deep history (may be stale) — past only
        df = t.earnings_dates
        if df is not None and not df.empty:
            for ix in df.index:
                d = ix.date()
                (past if d <= today else future).append(d)
    except Exception:
        pass

    last = max(past) if past else None
    nxt = min(future) if future else None
    return (last.isoformat() if last else None,
            nxt.isoformat() if nxt else None)


def main():
    print('=' * 60)
    print('FETCH EARNINGS DATES  ',
          datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 60)
    universe = get_universe()
    print(f'{len(universe)} tickers (sequential)')

    now_iso = datetime.now(timezone.utc).isoformat()
    ok = with_last = 0
    for i, tk in enumerate(universe, 1):
        last = nxt = None
        for a in range(1, ATTEMPTS + 1):
            try:
                last, nxt = earnings_dates_for(tk)
                break
            except Exception as e:
                if a < ATTEMPTS:
                    time.sleep(1.5 * a)
                    continue
                print(f'  {tk}: ERROR {str(e)[:50]}')
        if last or nxt:
            try:
                sb.table('stock_fundamentals').upsert(
                    {'ticker': tk, 'last_earnings_date': last,
                     'next_earnings_date': nxt, 'last_updated': now_iso},
                    on_conflict='ticker').execute()
                ok += 1
                if last:
                    with_last += 1
            except Exception as e:
                print(f'  {tk}: upsert failed: {e}')
        if i % 200 == 0 or i == len(universe):
            print(f'  {i}/{len(universe)} processed ({ok} stored)')
        time.sleep(SLEEP)

    print('=' * 60)
    print(f'Done. {ok}/{len(universe)} stored, {with_last} with a last_earnings_date')


if __name__ == '__main__':
    main()
