#!/usr/bin/env python3
"""ETF momentum board — sector SPDRs + thematic ETFs, ranked in their OWN set.

Fetches daily OHLC for the ETFs in `etfs`, computes the SAME indicator stack and
`alkalyme_rs` as the stock pipeline (imported from fetch_ohlc so the math is
identical), then ranks them CROSS-SECTIONALLY WITHIN THE ETF SET — not against the
~1,900 stocks. That isolation matters: a diversified ETF would read mid-pack forever
inside the stock universe, so "which sector/theme is hottest" only means something
when ETFs are scored against other ETFs.

`momentum_score` is the identical 5-factor composite used everywhere else on the
site (RS level, RS improvement ~20d, EMA-9 slope ~10d, EMA-20 slope ~20d, price vs
EMA-50), each factor turned into a cross-sectional percentile — here over the ETF
cross-section — and averaged to 0-100. Same number, same meaning, different universe.

Writes etf_snapshots (upsert on symbol+snapshot_date). Run nightly after fetch_ohlc.
Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (.env auto-loaded).
Usage: python compute_etf_momentum.py
"""

import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from supabase import create_client

# Reuse the canonical indicator math + benchmark so ETF EMAs / alkalyme_rs match
# the stock pipeline exactly (import-safe: fetch_ohlc guards sb/main under __main__).
from fetch_ohlc import get_ticker_df, compute_records, BENCHMARK, HISTORY_PERIOD

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

sb = create_client(URL, KEY)

print('=' * 66)
print('ETF MOMENTUM BOARD  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
print('=' * 66)


def _num(df, *cols):
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors='coerce')


def _f(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if pd.isna(v):
        return None
    return v.item() if hasattr(v, 'item') else v


def main():
    etfs = sb.table('etfs').select('symbol').execute().data or []
    symbols = sorted({r['symbol'] for r in etfs})
    if not symbols:
        sys.exit('etfs table empty — run seed_etfs.py first')
    print(f'{len(symbols)} ETFs: {", ".join(symbols)}\n')

    # Benchmark closes for alkalyme_rs (same reference index as the stock pipeline).
    print(f'Downloading benchmark {BENCHMARK} ...')
    bench = yf.download(BENCHMARK, period=HISTORY_PERIOD, interval='1d',
                        auto_adjust=True, progress=False, threads=False)
    bdf = get_ticker_df(bench, BENCHMARK)
    index_closes = bdf['Close'] if bdf is not None else None
    if index_closes is None or len(index_closes) < 25:
        sys.exit('benchmark history unavailable — cannot compute alkalyme_rs')

    print('Downloading ETF OHLC ...')
    data = yf.download(symbols, period=HISTORY_PERIOD, interval='1d',
                       auto_adjust=True, progress=False, threads=False, group_by='ticker')
    rows, missed = [], []
    for sym in symbols:
        edf = get_ticker_df(data, sym)
        recs = compute_records(sym, edf, index_closes)
        if recs:
            rows.extend(recs)
        else:
            missed.append(sym)
    if missed:
        print(f'  (no data, skipped: {", ".join(missed)})')
    if not rows:
        sys.exit('no ETF OHLC fetched — aborting')

    df = pd.DataFrame(rows).rename(columns={'ticker': 'symbol'})
    _num(df, 'alkalyme_rs', 'close', 'ema_9', 'ema_20', 'ema_50')

    # ── etf_rank: RS rank WITHIN the ETF set per date (1 = strongest) ──────────
    print('Ranking ETF cross-section ...')
    df['etf_rank'] = (df.groupby('snapshot_date')['alkalyme_rs']
                        .rank(ascending=False, method='min').astype('Int64'))

    # ── momentum_score: identical 5-factor composite, over the ETF cross-section ─
    df = df.sort_values(['symbol', 'snapshot_date']).reset_index(drop=True)
    g = df.groupby('symbol', group_keys=False)
    rank_20 = g['etf_rank'].shift(20).astype('float')
    ema9_10 = g['ema_9'].shift(10)
    ema20_20 = g['ema_20'].shift(20)
    factors = pd.DataFrame({
        'f_level':   -df['etf_rank'].astype('float'),
        'f_improve': rank_20 - df['etf_rank'].astype('float'),
        'f_e9':      df['ema_9'] / ema9_10 - 1,
        'f_e20':     df['ema_20'] / ema20_20 - 1,
        'f_px':      df['close'] / df['ema_50'] - 1,
    })
    pct = pd.DataFrame({c: factors[c].groupby(df['snapshot_date']).rank(pct=True) for c in factors})
    present = pct.notna().sum(axis=1)
    mom = (pct.mean(axis=1) * 100).round()
    mom[present < 3] = np.nan                       # need >=3 of 5 factors
    df['momentum_score'] = mom.astype('Int64')

    # ── Upsert ────────────────────────────────────────────────────────────────
    print('Pushing etf_snapshots ...')
    updates = [{
        'symbol': r['symbol'], 'snapshot_date': r['snapshot_date'],
        'open': _f(r.get('open')), 'high': _f(r.get('high')),
        'low': _f(r.get('low')), 'close': _f(r.get('close')),
        'ema_9': _f(r.get('ema_9')), 'ema_20': _f(r.get('ema_20')),
        'ema_50': _f(r.get('ema_50')), 'alkalyme_rs': _f(r.get('alkalyme_rs')),
        'etf_rank': _f(r.get('etf_rank')), 'momentum_score': _f(r.get('momentum_score')),
    } for _, r in df.iterrows()]
    for i in range(0, len(updates), 500):
        sb.table('etf_snapshots').upsert(
            updates[i:i + 500], on_conflict='symbol,snapshot_date').execute()
    print(f'  upserted {len(updates)} rows')

    latest = df['snapshot_date'].max()
    board = (df[df['snapshot_date'] == latest]
             .dropna(subset=['momentum_score'])
             .sort_values('momentum_score', ascending=False))
    print(f'\nETF momentum board — {latest}:')
    for _, r in board.iterrows():
        print(f"  {int(r['momentum_score']):>3}  {r['symbol']:<5} (etf_rank {int(r['etf_rank'])})")
    print('\nDone.')


if __name__ == '__main__':
    main()
