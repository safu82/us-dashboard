#!/usr/bin/env python3
"""Append the latest QQQ daily close into benchmark_history.

daily_stock_snapshots is pruned to a rolling 90-day window for the universe,
but the Custom Period card on the Overview tab needs QQQ closes all the way
back to portfolio inception (Nov 2021). benchmark_history is the long-lived
table that holds those closes.

This script picks up where benchmark_history left off and appends every QQQ
row from daily_stock_snapshots that's newer than the latest stored date.
Idempotent — re-runs are no-ops.

Runs nightly inside the GitHub Actions workflow after fetch_ohlc.py.
"""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

BENCHMARK_TICKER = 'QQQ'

sb = create_client(URL, KEY)

print(f'sync_benchmark_history.py  {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}')

# Latest QQQ date already in benchmark_history
existing = (sb.table('benchmark_history')
              .select('snapshot_date')
              .eq('ticker', BENCHMARK_TICKER)
              .order('snapshot_date', desc=True)
              .limit(1).execute())
last_date = existing.data[0]['snapshot_date'] if existing.data else '1970-01-01'
print(f'  benchmark_history latest {BENCHMARK_TICKER} date: {last_date}')

# Pull newer QQQ rows from daily_stock_snapshots
new = (sb.table('daily_stock_snapshots')
         .select('ticker, snapshot_date, close')
         .eq('ticker', BENCHMARK_TICKER)
         .gt('snapshot_date', last_date)
         .order('snapshot_date')
         .execute())
rows = new.data or []
if not rows:
    print('  nothing to sync.')
    sys.exit(0)

print(f'  appending {len(rows)} new rows to benchmark_history')
sb.table('benchmark_history').upsert(rows, on_conflict='ticker,snapshot_date').execute()
print('Done.')
