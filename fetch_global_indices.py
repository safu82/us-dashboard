#!/usr/bin/env python3
"""Weekly GLOBAL TAPE snapshot -> global_indices.

The US market doesn't trade in a vacuum. A handful of foreign equity indices and
cross-asset prices lead or co-move with US indices, and — because Asia closes
before the US opens — they're a same-day tell. This pulls a compact set for the
newsletter's macro read:

  Asian equities  ^TWII  Taiwan (TAIEX)   — TSMC / the AI + semiconductor supply chain
                  ^KS11  KOSPI (Korea)    — Samsung + SK Hynix / the memory cycle
                  ^N225  Nikkei 225       — global risk appetite + the yen carry trade
                  ^HSI   Hang Seng        — China demand (materials, industrials, luxury)
  FX              DX-Y.NYB  US Dollar Index — a rising dollar is an earnings headwind
                  JPY=X     USD/JPY        — the carry-trade gauge (yen strength = risk-off)
  Commodities     HG=F   Copper           — "Dr. Copper", the global-growth barometer
                  BZ=F   Brent crude      — the direct oil / supply-shock gauge
  Crypto          BTC-USD Bitcoin         — 24/7 risk-sentiment proxy

For each symbol we pull ~2 months of daily history from Yahoo (yfinance), take the
latest close and the last close of the PRIOR ISO week (this market's own calendar —
the same week-over-week baseline the data pack uses), compute the weekly % change,
and upsert one row per symbol. Optional enrichment: a yfinance/network outage must
degrade (leave last week's rows) rather than block the newsletter.

Run weekly, after compute_market_health.py (for the snapshot_date stamp) and before
build_data_pack.py. Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (.env auto-loaded).
"""

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

HISTORY_PERIOD = '2mo'      # enough to reach back past the prior ISO week + holidays
ATTEMPTS = 3
SLEEP = 2

# (symbol, display name, category, display_order). Order groups asia → fx → commodity → crypto.
INSTRUMENTS = [
    ('^TWII',    'Taiwan (TAIEX)',    'asia_equity', 1),
    ('^KS11',    'KOSPI (Korea)',     'asia_equity', 2),
    ('^N225',    'Nikkei 225 (Japan)', 'asia_equity', 3),
    ('^HSI',     'Hang Seng (HK/China)', 'asia_equity', 4),
    ('DX-Y.NYB', 'US Dollar Index',   'fx',          5),
    ('JPY=X',    'USD/JPY',           'fx',          6),
    ('HG=F',     'Copper',            'commodity',   7),
    ('BZ=F',     'Brent crude',       'commodity',   8),
    ('BTC-USD',  'Bitcoin',           'crypto',      9),
]


def us_latest_date():
    """The US market's last trading date (from market_health) to stamp the rows with,
    so build_data_pack can align the global tape with the rest of the issue. Falls back
    to today's UTC date if market_health is empty."""
    try:
        r = (sb.table('market_health').select('snapshot_date')
             .order('snapshot_date', desc=True).limit(1).execute().data)
        if r:
            return r[0]['snapshot_date']
    except Exception as e:
        print(f'  WARN could not read market_health for snapshot_date: {str(e)[:60]}')
    return datetime.now(timezone.utc).date().isoformat()


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


def prior_week_close(series):
    """Last close of the most recent EARLIER ISO week than the latest bar — the
    week-over-week baseline (mirrors build_data_pack.previous_week_close)."""
    latest_wk = date.fromisoformat(series[-1][0]).isocalendar()[:2]
    prior = [(d, v) for d, v in series if date.fromisoformat(d).isocalendar()[:2] < latest_wk]
    return prior[-1] if prior else None


def main():
    print('=' * 60)
    print('FETCH GLOBAL INDICES  ',
          datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 60)
    snap = us_latest_date()
    print(f'Stamping rows with snapshot_date {snap}')

    rows, missed = [], []
    for symbol, name, category, order in INSTRUMENTS:
        series = closes_for(symbol)
        if not series:
            missed.append(symbol)
            continue
        as_of, last = series[-1]
        pw = prior_week_close(series)
        prev = pw[1] if pw else None
        pct = ((last / prev) - 1) * 100 if prev else None
        rows.append({
            'snapshot_date': snap,
            'symbol': symbol,
            'name': name,
            'category': category,
            'display_order': order,
            'last_close': round(last, 4),
            'prev_close': round(prev, 4) if prev is not None else None,
            'pct_chg_wk': round(pct, 2) if pct is not None else None,
            'as_of': as_of,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })
        pct_txt = f'{pct:+.2f}%' if pct is not None else 'n/a'
        print(f'  {symbol:<10} {name:<22} {last:>12,.2f}  wk {pct_txt}  (as of {as_of})')
        time.sleep(SLEEP)

    if rows:
        sb.table('global_indices').upsert(rows, on_conflict='snapshot_date,symbol').execute()
        print(f'\nUpserted {len(rows)} rows for {snap}.')
    else:
        print('\nNo rows fetched — leaving prior global_indices data in place.')
    if missed:
        print(f'Missed (degraded): {", ".join(missed)}')
    print('Done.')


if __name__ == '__main__':
    main()
