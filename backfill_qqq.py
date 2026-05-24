#!/usr/bin/env python3
"""One-time backfill: QQQ daily closes from 2021-11-01 into benchmark_history.

Used by the Custom Period card on the Overview tab to compute QQQ XIRR for
any user-picked start month. Separate table from daily_stock_snapshots so
fetch_ohlc.py's 90-day pruning doesn't wipe the history.

Run once: `python backfill_qqq.py`
Re-running is idempotent (upsert on PK).
"""

import os
import sys

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from supabase import create_client

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

sb = create_client(URL, KEY)

print('Downloading QQQ daily OHLC since 2021-11-01...')
df = yf.download('QQQ', start='2021-11-01', interval='1d',
                 auto_adjust=True, progress=False)
if df is None or df.empty:
    sys.exit('No data returned from yfinance')

# yfinance now returns MultiIndex columns even for a single ticker — flatten.
if isinstance(df.columns, pd.MultiIndex):
    df.columns = df.columns.get_level_values(0)

rows = [{
    'ticker': 'QQQ',
    'snapshot_date': date.strftime('%Y-%m-%d'),
    'close': float(row['Close']),
} for date, row in df.iterrows() if pd.notna(row['Close'])]

print(f'Got {len(rows)} daily rows ({rows[0]["snapshot_date"]} -> {rows[-1]["snapshot_date"]})')

inserted = 0
for i in range(0, len(rows), 500):
    chunk = rows[i:i + 500]
    sb.table('benchmark_history').upsert(
        chunk, on_conflict='ticker,snapshot_date').execute()
    inserted += len(chunk)
    print(f'  upserted {inserted} / {len(rows)}')

print('Done.')
