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


STABILITY_WINDOW = 60     # trailing sessions for the durability read
TOP_DECILE = 0.10         # rank_pct at or below this counts as "leading"


def main():
    etfs = sb.table('etfs').select('symbol,benchmark_only').execute().data or []
    symbols = sorted({r['symbol'] for r in etfs})
    # SPY/QQQ are the yardstick the checklist measures against, so they are fetched
    # and stored but kept OUT of the percentile cross-section — leaving them in
    # would have the market competing with its own sectors for rank.
    bench_only = {r['symbol'] for r in etfs if r.get('benchmark_only')}
    if not symbols:
        sys.exit('etfs table empty — run seed_etfs.py first')
    print(f'{len(symbols)} ETFs ({len(symbols) - len(bench_only)} ranked, '
          f'{len(bench_only)} benchmark-only: {", ".join(sorted(bench_only)) or "none"})\n')

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
    _num(df, 'alkalyme_rs', 'close', 'ema_9', 'ema_20', 'ema_50', 'high_52w')
    df = df.sort_values(['symbol', 'snapshot_date']).reset_index(drop=True)

    # ── Weekend-checklist measures ────────────────────────────────────────────
    # "What is making new highs?" — distance below the rolling 52-week high.
    df['pct_from_high'] = df['close'] / df['high_52w'] - 1

    # "What is holding its 8/21 trend?" — computed here rather than reusing the
    # shared 9/20 EMAs, because 8/21 is the specific pair the method names and
    # the stock pipeline's columns are not ours to repurpose.
    g_sym = df.groupby('symbol', group_keys=False)['close']
    ema8 = g_sym.transform(lambda s: s.ewm(span=8, adjust=False, min_periods=8).mean())
    ema21 = g_sym.transform(lambda s: s.ewm(span=21, adjust=False, min_periods=21).mean())
    # object dtype, not bool: the first 21 sessions per symbol have no EMA-21 yet
    # and must stay None rather than silently reading as False.
    df['above_8_21'] = ((df['close'] > ema8) & (ema8 > ema21)).astype('object')
    df.loc[ema21.isna(), 'above_8_21'] = None

    # "What is outperforming SPY / QQQ?" — N-session return less the benchmark's
    # over the same window, so it reads directly as excess return.
    #
    # Measured over two windows, because a fixed lookback is fragile at its
    # boundary. On 2026-09-14 the whole cyber complex repriced 6-16% in one
    # session; that day sat exactly on the edge of the 20-day window, so HACK
    # read +19% one day and would read +10% the next on no price change at all.
    # A 60-session window straddles different events, so when the two disagree
    # sharply the number is tracking a single day rather than a trend.
    g_close = df.groupby('symbol', group_keys=False)['close']
    for n in (20, 60):
        df[f'ret_{n}'] = g_close.transform(lambda s, n=n: s / s.shift(n) - 1)
        for bsym, pre in (('SPY', 'vs_spy'), ('QQQ', 'vs_qqq')):
            bench_ret = (df.loc[df['symbol'] == bsym, ['snapshot_date', f'ret_{n}']]
                           .set_index('snapshot_date')[f'ret_{n}'])
            df[f'{pre}_{n}'] = df[f'ret_{n}'] - df['snapshot_date'].map(bench_ret)

    # ── etf_rank: RS rank WITHIN the ranked ETF set per date (1 = strongest) ───
    print('Ranking ETF cross-section ...')
    ranked = ~df['symbol'].isin(bench_only)
    rs_for_rank = df['alkalyme_rs'].where(ranked)      # benchmarks excluded
    df['etf_rank'] = (rs_for_rank.groupby(df['snapshot_date'])
                                 .rank(ascending=False, method='min').astype('Int64'))
    # Rank as a share of the ranked set. Absolute rank is not comparable once the
    # universe grows (#5 of 28 and #5 of 55 are different things); this is.
    df['rank_pct'] = df['etf_rank'].astype('float') / df['snapshot_date'].map(
        df[ranked].groupby('snapshot_date')['symbol'].count())

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
    # Benchmarks are masked out before percentiling, not just excluded from the
    # rank: leaving them in would let the market set the percentile its own
    # sectors are measured against, and would hand SPY/QQQ a score computed
    # against a pool they are not part of.
    factors = factors.where(ranked, np.nan)
    pct = pd.DataFrame({c: factors[c].groupby(df['snapshot_date']).rank(pct=True) for c in factors})
    present = pct.notna().sum(axis=1)
    mom = (pct.mean(axis=1) * 100).round()
    mom[present < 3] = np.nan                       # need >=3 of 5 factors
    df['momentum_score'] = mom.astype('Int64')

    # ── Leadership durability ─────────────────────────────────────────────────
    # Momentum says "strong today"; it cannot tell a sector that has led for
    # three months from one that popped this week. Over the trailing 60 sessions:
    # how much of the time was this fund in the top decile, and how much did its
    # rank move around. High momentum with low durability is a whipsaw to fade,
    # not a trend to join. Measured on rank_pct so it survives universe changes.
    print('Computing leadership durability ...')
    gp = df.groupby('symbol', group_keys=False)['rank_pct']
    df['top_decile_60d'] = gp.transform(
        lambda s: s.le(TOP_DECILE).rolling(STABILITY_WINDOW, min_periods=20).mean())
    df['rank_vol_60d'] = gp.transform(
        lambda s: s.rolling(STABILITY_WINDOW, min_periods=20).std())
    df.loc[df['symbol'].isin(bench_only),
           ['top_decile_60d', 'rank_vol_60d']] = np.nan

    # ── Upsert ────────────────────────────────────────────────────────────────
    print('Pushing etf_snapshots ...')
    updates = [{
        'symbol': r['symbol'], 'snapshot_date': r['snapshot_date'],
        'open': _f(r.get('open')), 'high': _f(r.get('high')),
        'low': _f(r.get('low')), 'close': _f(r.get('close')),
        'ema_9': _f(r.get('ema_9')), 'ema_20': _f(r.get('ema_20')),
        'ema_50': _f(r.get('ema_50')), 'alkalyme_rs': _f(r.get('alkalyme_rs')),
        'etf_rank': _f(r.get('etf_rank')), 'momentum_score': _f(r.get('momentum_score')),
        'high_52w': _f(r.get('high_52w')), 'pct_from_high': _f(r.get('pct_from_high')),
        'above_8_21': (None if pd.isna(r.get('above_8_21'))
                       else bool(r.get('above_8_21'))),
        'ret_20': _f(r.get('ret_20')), 'ret_60': _f(r.get('ret_60')),
        'vs_spy_20': _f(r.get('vs_spy_20')), 'vs_qqq_20': _f(r.get('vs_qqq_20')),
        'vs_spy_60': _f(r.get('vs_spy_60')), 'vs_qqq_60': _f(r.get('vs_qqq_60')),
        'rank_pct': _f(r.get('rank_pct')),
        'top_decile_60d': _f(r.get('top_decile_60d')),
        'rank_vol_60d': _f(r.get('rank_vol_60d')),
    } for _, r in df.iterrows()]
    for i in range(0, len(updates), 500):
        sb.table('etf_snapshots').upsert(
            updates[i:i + 500], on_conflict='symbol,snapshot_date').execute()
    print(f'  upserted {len(updates)} rows')

    latest = df['snapshot_date'].max()
    board = (df[(df['snapshot_date'] == latest) & ~df['symbol'].isin(bench_only)]
             .dropna(subset=['momentum_score'])
             .sort_values('momentum_score', ascending=False))
    print(f'\nETF board — {latest}   (NH = % below 52w high, 8/21 = trend held, '
          f'vQQQ 20d/60d = excess return over each window, Top10 = share of last '
          f'{STABILITY_WINDOW}d in the top decile)')
    print(f'{"mom":>4} {"sym":<6}{"rank":>5}{"NH":>8}{"8/21":>6}'
          f'{"vQQQ20":>8}{"vQQQ60":>8}{"Top10":>7}')
    for _, r in board.iterrows():
        pct = lambda v, d=1: '  –  ' if pd.isna(v) else f'{v * 100:+.{d}f}%'
        td = r.get('top_decile_60d')
        print(f"{int(r['momentum_score']):>4} {r['symbol']:<6}{int(r['etf_rank']):>5}"
              f"{pct(r.get('pct_from_high')):>8}"
              f"{('yes' if r.get('above_8_21') else 'no'):>6}"
              f"{pct(r.get('vs_qqq_20')):>8}{pct(r.get('vs_qqq_60')):>8}"
              f"{('  –  ' if pd.isna(td) else f'{td*100:.0f}%'):>7}")
    print('\nDone.')


if __name__ == '__main__':
    main()
