#!/usr/bin/env python3
"""Cross-sectional post-pass for daily_stock_snapshots.

Run after fetch_ohlc.py. For every snapshot_date in the rolling window:
  - rs_rank              — rank of alkalyme_rs across the universe (1 = highest)
  - sector_percentile    — alkalyme_rs percentile within the ticker's GICS sector
  - sector_composite_pct — composite percentile = mean(rs_pct, near-52WH pct) within sector
  - peer_group           — GICS industry if >= MIN_PEER_SIZE tickers, else GICS sector
  - peer_percentile      — alkalyme_rs percentile within peer_group
  - peer_composite_pct   — composite percentile = mean(rs_pct, near-52WH pct) within peer_group
Per-ticker (across dates):
  - rank_slope           — 5-day OLS slope of rs_rank (negative = improving rank)

Sector + industry mapping comes from us_stock_sectors.
"""

import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from supabase import create_client

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

sb = create_client(URL, KEY)

# An industry must have at least this many tickers in the universe to be used
# as a peer group on its own. Below this, tickers in that industry roll up to
# their GICS sector for RS ranking. Tuning knob.
MIN_PEER_SIZE = 5

print('=' * 70)
print('CROSS-SECTIONAL POST-PASS  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
print('=' * 70)


# ── Pull every snapshot row (paginated, stable ORDER for correct pagination) ──
def pull_all(table, columns, order_cols):
    rows, page, frm = [], 1000, 0
    while True:
        q = sb.table(table).select(columns)
        for c in order_cols:
            q = q.order(c)
        res = q.range(frm, frm + page - 1).execute()
        if not res.data:
            break
        rows.extend(res.data)
        if len(res.data) < page:
            break
        frm += page
    return rows


print('Pulling snapshots ...')
# OHLC included only so the upsert payload satisfies NOT NULL constraints;
# the INSERT-side validation fires before ON CONFLICT resolves to UPDATE.
snap = pull_all('daily_stock_snapshots',
                'ticker, snapshot_date, alkalyme_rs, open, high, low, close, high_52w',
                ['snapshot_date', 'ticker'])
print(f'  {len(snap)} rows')

print('Pulling sector + industry map ...')
sectors = pull_all('us_stock_sectors', 'ticker, sector, industry', ['ticker'])
sec_map = {r['ticker']: r['sector'] for r in sectors if r.get('sector')}
ind_map = {r['ticker']: r['industry'] for r in sectors if r.get('industry')}
print(f'  {len(sec_map)} ticker->sector entries, {len(ind_map)} ticker->industry entries')

# Build peer_group map: industry if industry has >= MIN_PEER_SIZE tickers in
# the universe, otherwise roll up to GICS sector.
from collections import Counter
ind_counts = Counter(ind_map.values())
peer_map = {}
peer_type_map = {}  # 'industry' or 'sector' — for sector_rankings
parent_sector_map = {}  # peer_group -> parent GICS sector
big_industries = {ind for ind, n in ind_counts.items() if n >= MIN_PEER_SIZE}
for tk, sec in sec_map.items():
    ind = ind_map.get(tk)
    if ind and ind in big_industries:
        peer_map[tk] = ind
        peer_type_map[ind] = 'industry'
        parent_sector_map[ind] = sec
    else:
        peer_map[tk] = sec
        peer_type_map[sec] = 'sector'
        parent_sector_map[sec] = sec
print(f'  {len(big_industries)} industries qualify as peer groups (n>={MIN_PEER_SIZE})')
print(f'  remainder ({len(ind_counts) - len(big_industries)} industries) rolls up to sector')

df = pd.DataFrame(snap)
if df.empty:
    sys.exit('No snapshot data — nothing to do')

df['alkalyme_rs'] = pd.to_numeric(df['alkalyme_rs'], errors='coerce')
df['close'] = pd.to_numeric(df['close'], errors='coerce')
df['high_52w'] = pd.to_numeric(df['high_52w'], errors='coerce')
df['sector'] = df['ticker'].map(sec_map).fillna('Unknown')
df['peer_group'] = df['ticker'].map(peer_map).fillna('Unknown')
# Distance from 52WH as a "near-high" strength score (0 = at high, large = far below)
df['near_high'] = df['close'] / df['high_52w']  # 1.0 = at the 52WH

# ── rs_rank (cross-sectional, descending by alkalyme_rs) ───────────────────
print('Computing rs_rank ...')
df['rs_rank'] = (df.groupby('snapshot_date')['alkalyme_rs']
                   .rank(ascending=False, method='min').astype('Int64'))

# ── sector_percentile (within sector, by alkalyme_rs; column is integer) ───
print('Computing sector_percentile ...')
df['sector_percentile'] = (df.groupby(['snapshot_date', 'sector'])['alkalyme_rs']
                             .rank(pct=True) * 100).round().astype('Int64')

# ── sector_composite_pct: mean of (RS pct, near-high pct) within sector ────
print('Computing sector_composite_pct ...')
rs_pct = df.groupby(['snapshot_date', 'sector'])['alkalyme_rs'].rank(pct=True)
nh_pct = df.groupby(['snapshot_date', 'sector'])['near_high'].rank(pct=True)
df['sector_composite_pct'] = ((rs_pct + nh_pct) / 2 * 100).round(2)

# ── peer_percentile & peer_composite_pct (within peer_group) ───────────────
print('Computing peer_percentile + peer_composite_pct ...')
df['peer_percentile'] = (df.groupby(['snapshot_date', 'peer_group'])['alkalyme_rs']
                           .rank(pct=True) * 100).round().astype('Int64')
rs_pct_pg = df.groupby(['snapshot_date', 'peer_group'])['alkalyme_rs'].rank(pct=True)
nh_pct_pg = df.groupby(['snapshot_date', 'peer_group'])['near_high'].rank(pct=True)
df['peer_composite_pct'] = ((rs_pct_pg + nh_pct_pg) / 2 * 100).round(2)

# ── rank_slope: 5-day OLS slope of rs_rank per ticker ──────────────────────
print('Computing rank_slope ...')
df = df.sort_values(['ticker', 'snapshot_date']).reset_index(drop=True)
def _slope(s):
    n = len(s)
    if n < 2 or s.isna().any():
        return np.nan
    x = np.arange(n)
    y = s.astype(float).values
    return np.polyfit(x, y, 1)[0]
df['rank_slope'] = (df.groupby('ticker')['rs_rank']
                      .rolling(window=5, min_periods=5).apply(_slope, raw=False)
                      .reset_index(level=0, drop=True))

# ── Push back (UPDATE per row, batched) ────────────────────────────────────
print('Pushing updates ...')

def _f(v):
    if pd.isna(v):
        return None
    if hasattr(v, 'item'):
        return v.item()
    return v

updates = []
for _, r in df.iterrows():
    updates.append({
        'ticker': r['ticker'],
        'snapshot_date': r['snapshot_date'],
        # NOT NULL pass-throughs (unchanged from existing row, kept so the
        # upsert's INSERT side passes constraint checks).
        'open':  _f(r['open']),  'high': _f(r['high']),
        'low':   _f(r['low']),   'close': _f(r['close']),
        # The cross-sectional fields we actually want to update.
        'rs_rank': _f(r['rs_rank']),
        'sector_percentile': _f(r['sector_percentile']),
        'sector_composite_pct': _f(r['sector_composite_pct']),
        'peer_group': _f(r['peer_group']),
        'peer_percentile': _f(r['peer_percentile']),
        'peer_composite_pct': _f(r['peer_composite_pct']),
        'rank_slope': _f(r['rank_slope']),
    })

# upsert (PK = ticker + snapshot_date) so we update existing rows in place
inserted = 0
for i in range(0, len(updates), 500):
    chunk = updates[i:i + 500]
    sb.table('daily_stock_snapshots').upsert(
        chunk, on_conflict='ticker,snapshot_date').execute()
    inserted += len(chunk)
    print(f'  {inserted} / {len(updates)}')

print('Done.')
