#!/usr/bin/env python3
"""Daily market-health rollup -> market_health table.

Computes the Athena-style "Where We Stand" 5-point regime check for every
snapshot_date in the stored window (so we get the TREND, not just today):
  - pct_above_200d / pct_above_50d : market breadth (regime vs short-term)
  - new_highs / new_lows           : 52-week new-high vs new-low count
  - advancers / decliners          : day-over-day participation
  - sp_close / sp_ema_200 / sp_above_200d : the index vs its own 200-day
  - vix_close                      : fear gauge (today only, from live_prices)
  - credit_spread                  : left null here; filled by a separate FRED fetch

Pure DB read + compute (no yfinance), so it's fast and runs at the end of the
nightly chain after fetch_ohlc + compute_cross_sectional.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (.env in repo root auto-loaded).
"""

import os
import sys
from datetime import datetime, timezone

import pandas as pd
from supabase import create_client
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

sb = create_client(URL, KEY)

SP = '^GSPC'                       # index whose own 200-day we track
NON_STOCKS = {'^GSPC', '^VIX', 'QQQ'}   # excluded from breadth/NH-NL universe


def paginate(table, columns, page=1000):
    # MUST order by a stable key — .range() pagination without an explicit sort
    # returns rows in inconsistent order across pages, causing duplicates/misses.
    out, frm = [], 0
    while True:
        res = (sb.table(table).select(columns)
                 .order('snapshot_date').order('ticker')
                 .range(frm, frm + page - 1).execute())
        if not res.data:
            break
        out.extend(res.data)
        if len(res.data) < page:
            break
        frm += page
    return out


def main():
    print('=' * 60)
    print('COMPUTE MARKET HEALTH  ',
          datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 60)

    rows = paginate('daily_stock_snapshots',
                    'ticker, snapshot_date, close, ema_50, ema_200, high_52w, low_52w')
    df = pd.DataFrame(rows)
    if df.empty:
        sys.exit('No snapshots found')
    for c in ('close', 'ema_50', 'ema_200', 'high_52w', 'low_52w'):
        df[c] = pd.to_numeric(df[c], errors='coerce')

    # Index series (S&P vs its own 200-day). Dedupe defensively on date.
    sp = (df[df.ticker == SP].drop_duplicates('snapshot_date', keep='last')
          .set_index('snapshot_date')[['close', 'ema_200']].to_dict('index'))

    # Day-over-day for advancers/decliners.
    df = df.sort_values(['ticker', 'snapshot_date'])
    df['prev_close'] = df.groupby('ticker')['close'].shift(1)

    # Breadth / NH-NL universe = real stocks only (drop the index + ETF symbols).
    stocks = df[~df.ticker.isin(NON_STOCKS) & ~df.ticker.str.startswith('^')]

    # VIX: latest close from the live worker (stamped only on the newest date).
    vix_latest = None
    try:
        r = (sb.table('live_prices').select('price')
               .eq('ticker', '^VIX').limit(1).execute().data)
        if r and r[0].get('price'):
            vix_latest = round(float(r[0]['price']), 2)
    except Exception as e:
        print(f'  WARN: could not read VIX from live_prices: {e}')

    latest_date = df.snapshot_date.max()
    out = []
    for d, g in stocks.groupby('snapshot_date'):
        e200 = g.dropna(subset=['ema_200'])
        e50 = g.dropna(subset=['ema_50'])
        gp = g.dropna(subset=['prev_close'])
        hi = g.dropna(subset=['high_52w'])
        lo = g.dropna(subset=['low_52w'])
        spd = sp.get(d) or {}
        sp_c = spd.get('close')
        sp_e = spd.get('ema_200')
        out.append({
            'snapshot_date': d,
            'universe_count': int(len(g)),
            'pct_above_50d': round(float((e50['close'] > e50['ema_50']).mean() * 100), 2) if len(e50) else None,
            'pct_above_200d': round(float((e200['close'] > e200['ema_200']).mean() * 100), 2) if len(e200) else None,
            'new_highs': int((hi['close'] >= hi['high_52w'] * 0.9999).sum()),
            'new_lows': int((lo['close'] <= lo['low_52w'] * 1.0001).sum()),
            'advancers': int((gp['close'] > gp['prev_close']).sum()),
            'decliners': int((gp['close'] < gp['prev_close']).sum()),
            'sp_close': round(float(sp_c), 2) if sp_c is not None else None,
            'sp_ema_200': round(float(sp_e), 2) if sp_e is not None else None,
            'sp_above_200d': bool(sp_c > sp_e) if (sp_c is not None and sp_e is not None) else None,
            'vix_close': vix_latest if d == latest_date else None,
        })

    for i in range(0, len(out), 200):
        sb.table('market_health').upsert(out[i:i + 200], on_conflict='snapshot_date').execute()

    print(f'Upserted {len(out)} market_health rows ({out[0]["snapshot_date"]} .. {latest_date})')
    last = out[-1]
    print('\nLatest "Where We Stand":')
    print(f'  S&P vs 200d        : {"ABOVE" if last["sp_above_200d"] else "below"} '
          f'({last["sp_close"]} vs {last["sp_ema_200"]})')
    print(f'  % above 200-day    : {last["pct_above_200d"]}%')
    print(f'  % above 50-day     : {last["pct_above_50d"]}%')
    print(f'  New highs vs lows  : {last["new_highs"]} vs {last["new_lows"]}')
    print(f'  Advancers/decliners: {last["advancers"]}/{last["decliners"]}')
    print(f'  VIX                : {last["vix_close"]}')
    print(f'  Universe counted   : {last["universe_count"]}')


if __name__ == '__main__':
    main()
