#!/usr/bin/env python3
"""Aggregate sector_rankings rows per peer_group per snapshot_date.

Run after compute_cross_sectional.py.

For every snapshot_date in the lookback window, and for every peer_group:
  composite        — percentile blend of (avg_alkalyme_rs, breadth)
  composite_rank   — 1 = strongest peer_group for that date
  breadth          — % of tickers in peer_group with close > ema_50
  avg_alkalyme_rs  — simple mean
  n_tickers        — peer_group size for that date
  rrg_quadrant     — Leading / Improving / Weakening / Lagging  (RS level × RS momentum)
  peer_group_type  — 'industry' or 'sector'
  parent_sector    — GICS sector this peer_group rolls under
"""

import os
import sys
from collections import Counter
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

MIN_PEER_SIZE = 5    # mirrors compute_cross_sectional.py
SLOPE_WINDOW = 5     # days

print('=' * 70)
print('SECTOR_RANKINGS (peer_group aggregates) ',
      datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
print('=' * 70)


def pull_all(table, columns, order_cols, filters=None):
    rows, page, frm = [], 1000, 0
    while True:
        q = sb.table(table).select(columns)
        for c in order_cols:
            q = q.order(c)
        if filters:
            for f, val in filters:
                q = f(q, val)
        res = q.range(frm, frm + page - 1).execute()
        if not res.data:
            break
        rows.extend(res.data)
        if len(res.data) < page:
            break
        frm += page
    return rows


print('Pulling sector + industry map ...')
meta = pull_all('us_stock_sectors', 'ticker, sector, industry', ['ticker'])
sec_map = {r['ticker']: r['sector'] for r in meta if r.get('sector')}
ind_map = {r['ticker']: r['industry'] for r in meta if r.get('industry')}

ind_counts = Counter(ind_map.values())
big_industries = {ind for ind, n in ind_counts.items() if n >= MIN_PEER_SIZE}

peer_map = {}
peer_type = {}
parent_sec = {}
for tk, sec in sec_map.items():
    ind = ind_map.get(tk)
    if ind and ind in big_industries:
        peer_map[tk] = ind
        peer_type[ind] = 'industry'
        parent_sec[ind] = sec
    else:
        peer_map[tk] = sec
        peer_type[sec] = 'sector'
        parent_sec[sec] = sec
print(f'  {len(big_industries)} industries qualify as peer groups')

print('Pulling snapshots (window-bounded) ...')
snap = pull_all(
    'daily_stock_snapshots',
    'ticker, snapshot_date, alkalyme_rs, close, ema_50, high_52w',
    ['snapshot_date', 'ticker'],
)
print(f'  {len(snap)} rows')

df = pd.DataFrame(snap)
df['alkalyme_rs'] = pd.to_numeric(df['alkalyme_rs'], errors='coerce')
df['close'] = pd.to_numeric(df['close'], errors='coerce')
df['ema_50'] = pd.to_numeric(df['ema_50'], errors='coerce')
df['high_52w'] = pd.to_numeric(df['high_52w'], errors='coerce')
df['peer_group'] = df['ticker'].map(peer_map)
df = df.dropna(subset=['peer_group', 'alkalyme_rs'])
# Benchmarks/ETFs are not peer-group members.
df = df[df['peer_group'] != 'ETF']
df['above_ema50'] = (df['close'] > df['ema_50']).astype(int)

# ── Per (snapshot_date, peer_group) aggregates ─────────────────────────────
print('Aggregating peer_group rows ...')
agg = df.groupby(['snapshot_date', 'peer_group']).agg(
    n_tickers=('ticker', 'count'),
    avg_alkalyme_rs=('alkalyme_rs', 'mean'),
    breadth=('above_ema50', 'mean'),
).reset_index()
agg['breadth'] = (agg['breadth'] * 100).round(2)
agg['avg_alkalyme_rs'] = agg['avg_alkalyme_rs'].round(4)

# ── composite: blend two percentiles within each snapshot_date ─────────────
print('Computing composite + rank ...')
agg['rs_pct'] = agg.groupby('snapshot_date')['avg_alkalyme_rs'].rank(pct=True) * 100
agg['br_pct'] = agg.groupby('snapshot_date')['breadth'].rank(pct=True) * 100
agg['composite'] = ((agg['rs_pct'] + agg['br_pct']) / 2).round(2)
agg['composite_rank'] = (agg.groupby('snapshot_date')['composite']
                          .rank(ascending=False, method='min').astype('Int64'))

# ── RRG: classify each peer_group day by (RS level × RS momentum) ──────────
print('Classifying RRG quadrants ...')
agg = agg.sort_values(['peer_group', 'snapshot_date']).reset_index(drop=True)

def _slope(s):
    s = s.dropna()
    if len(s) < SLOPE_WINDOW:
        return np.nan
    y = s.astype(float).values
    x = np.arange(len(y))
    return np.polyfit(x, y, 1)[0]

agg['rs_slope'] = (agg.groupby('peer_group')['avg_alkalyme_rs']
                     .rolling(window=SLOPE_WINDOW, min_periods=SLOPE_WINDOW)
                     .apply(_slope, raw=False)
                     .reset_index(level=0, drop=True))

# High vs Low RS = above/below the median avg_alkalyme_rs across peer groups
# for that date. Improving vs Deteriorating = sign of the slope.
def _quadrant(row, medians):
    rs = row['avg_alkalyme_rs']
    sl = row['rs_slope']
    if pd.isna(rs) or pd.isna(sl):
        return None
    high_rs = rs >= medians[row['snapshot_date']]
    improving = sl >= 0
    if high_rs and improving:   return 'Leading'
    if high_rs and not improving: return 'Weakening'
    if not high_rs and improving: return 'Improving'
    return 'Lagging'

medians = agg.groupby('snapshot_date')['avg_alkalyme_rs'].median().to_dict()
agg['rrg_quadrant'] = agg.apply(lambda r: _quadrant(r, medians), axis=1)

# ── Build upsert payload ───────────────────────────────────────────────────
print('Pushing aggregates ...')

def _f(v):
    if pd.isna(v):
        return None
    if hasattr(v, 'item'):
        return v.item()
    return v

rows = []
for _, r in agg.iterrows():
    pg = r['peer_group']
    rows.append({
        'peer_group':       pg,
        'snapshot_date':    str(r['snapshot_date']),
        'composite':        _f(r['composite']),
        'composite_rank':   _f(r['composite_rank']),
        'breadth':          _f(r['breadth']),
        'avg_alkalyme_rs':  _f(r['avg_alkalyme_rs']),
        'n_tickers':        _f(r['n_tickers']),
        'rrg_quadrant':     _f(r['rrg_quadrant']),
        'peer_group_type':  peer_type.get(pg),
        'parent_sector':    parent_sec.get(pg),
    })

inserted = 0
for i in range(0, len(rows), 500):
    chunk = rows[i:i + 500]
    sb.table('sector_rankings').upsert(
        chunk, on_conflict='peer_group,snapshot_date').execute()
    inserted += len(chunk)
    print(f'  {inserted} / {len(rows)}')

print(f'Done. {len(rows)} peer_group/day rows written.')
