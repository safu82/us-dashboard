#!/usr/bin/env python3
"""Daily OHLC fetcher for the US universe (S&P 500 + portfolio holdings).

Downloads ~2 years of daily OHLC from Yahoo (yfinance), computes the technical
indicator stack — EMA(9/20/50/200), RSI(14) + RSI EMA(9), weekly RSI + EMA(9),
ATR(14, Wilder), 52-week high, volume ratio, and Alkalyme RS vs the S&P 500 —
and upserts the most recent rows into daily_stock_snapshots.

Port of the India PRODUCTION_fetch_ohlc_with_rsi.py: identical indicator math,
yfinance in place of Zerodha, S&P 500 (^GSPC) in place of NIFTY 50.

OHLC is split/dividend-adjusted (yfinance auto_adjust) so the series is
continuous across corporate actions — correct for technicals. Portfolio
valuation uses raw prices from live_prices, so this adjustment never touches
NAV or XIRR.

Usage:
  python fetch_ohlc.py              # full universe
  python fetch_ohlc.py NVDA MSFT    # only the named tickers (smoke test)

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (.env in repo root auto-loaded).
"""

import os
import sys
import time
from datetime import datetime, timezone

import numpy as np
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

HISTORY_PERIOD = '2y'      # enough to seed EMA(200) + weekly RSI
STORE_DAYS = 90            # most recent N daily rows upserted per ticker
BATCH_SIZE = 50            # tickers per yf.download call
SLEEP_BATCH = 2            # seconds between batches
BENCHMARK = '^GSPC'        # S&P 500 — Alkalyme RS reference index


# ── Indicator math (ported verbatim from the India fetcher) ────────────────
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period, min_periods=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period, min_periods=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def calculate_wilder_rsi(series, period=14):
    values = series.tolist()
    n = len(values)
    rsi_values = [None] * n
    if n < period + 1:
        return rsi_values
    gains, losses = [], []
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rsi_values[period] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    for i in range(period + 1, n):
        delta = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0)) / period
        rsi_values[i] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    return rsi_values


def calculate_alkalyme_rs(stock_closes, index_closes):
    combined = pd.DataFrame({'stock': stock_closes, 'index': index_closes}).dropna()
    if len(combined) < 25:
        return [None] * len(stock_closes)
    rs_ratio = (combined['stock'] / combined['index']) * 1000
    rsi_values = calculate_wilder_rsi(rs_ratio, period=14)
    non_none = [(i, v) for i, v in enumerate(rsi_values) if v is not None]
    if len(non_none) < 9:
        ema_values = [None] * len(rsi_values)
    else:
        ema_values = [None] * len(rsi_values)
        seed_indices = [i for i, v in non_none[:9]]
        seed_vals = [v for i, v in non_none[:9]]
        seed_ema = sum(seed_vals) / 9
        ema_values[seed_indices[-1]] = seed_ema
        k = 2 / (9 + 1)
        prev_ema = seed_ema
        for i in range(seed_indices[-1] + 1, len(rsi_values)):
            if rsi_values[i] is not None:
                prev_ema = rsi_values[i] * k + prev_ema * (1 - k)
                ema_values[i] = prev_ema
    result = [None] * len(stock_closes)
    combined_dates = list(combined.index)
    stock_dates = list(stock_closes.index)
    for i, ema_val in enumerate(ema_values):
        if i < len(combined_dates) and ema_val is not None:
            d = combined_dates[i]
            if d in stock_dates:
                result[stock_dates.index(d)] = ema_val
    return result


def calculate_ema(series, period):
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def calculate_atr(df, period=14):
    """ATR with Wilder smoothing (TradingView default)."""
    high, low = df['High'], df['Low']
    prev_close = df['Close'].shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def resample_to_weekly(df):
    weekly = df.resample('W-FRI').agg({'Open': 'first', 'High': 'max',
                                       'Low': 'min', 'Close': 'last',
                                       'Volume': 'sum'})
    return weekly.dropna()


# ── yfinance helpers ───────────────────────────────────────────────────────
def get_ticker_df(hist, ticker):
    """Extract a clean OHLCV frame for one ticker from a yf.download result,
    tolerating both single-level and MultiIndex column shapes."""
    if hist is None or hist.empty:
        return None
    cols = hist.columns
    if isinstance(cols, pd.MultiIndex):
        if ticker in cols.get_level_values(0):
            df = hist[ticker]
        elif ticker in cols.get_level_values(1):
            df = hist.xs(ticker, axis=1, level=1)
        else:
            return None
    else:
        df = hist
    need = ['Open', 'High', 'Low', 'Close', 'Volume']
    if not all(c in df.columns for c in need):
        return None
    df = df[need].dropna(how='all')
    return df if len(df) else None


def compute_records(ticker, df, index_closes):
    """Compute the indicator stack and return the last STORE_DAYS snapshot rows."""
    if df is None or len(df) < 20:
        return []
    df = df.copy()
    df['rsi_14'] = calculate_rsi(df['Close'], 14)
    df['rsi_ema_9'] = calculate_ema(df['rsi_14'], 9)
    df['ema_9'] = calculate_ema(df['Close'], 9)
    df['ema_20'] = calculate_ema(df['Close'], 20)
    df['ema_50'] = calculate_ema(df['Close'], 50)
    df['ema_200'] = calculate_ema(df['Close'], 200)
    df['atr_14'] = calculate_atr(df, 14)
    df['high_52w'] = df['High'].rolling(window=252, min_periods=1).max()
    df['vol_20_avg'] = df['Volume'].shift(1).rolling(window=20, min_periods=5).mean()
    df['vol_ratio'] = (df['Volume'] / df['vol_20_avg']).round(2)

    if index_closes is not None and len(index_closes) >= 25:
        df['alkalyme_rs'] = calculate_alkalyme_rs(df['Close'], index_closes)
    else:
        df['alkalyme_rs'] = None

    df['weekly_rsi_14'] = np.nan
    df['weekly_rsi_ema_9'] = np.nan
    weekly = resample_to_weekly(df)
    if len(weekly) >= 14:
        weekly['weekly_rsi_14'] = calculate_rsi(weekly['Close'], 14)
        weekly['weekly_rsi_ema_9'] = calculate_ema(weekly['weekly_rsi_14'], 9)
        for date, row in weekly.iterrows():
            mask = (df.index >= date - pd.Timedelta(days=6)) & (df.index <= date)
            df.loc[mask, 'weekly_rsi_14'] = row['weekly_rsi_14']
            df.loc[mask, 'weekly_rsi_ema_9'] = row['weekly_rsi_ema_9']

    def f(v):
        return float(v) if pd.notna(v) else None

    records = []
    for date, row in df.tail(STORE_DAYS).iterrows():
        records.append({
            'ticker': ticker,
            'snapshot_date': date.strftime('%Y-%m-%d'),
            'open': f(row['Open']), 'high': f(row['High']),
            'low': f(row['Low']), 'close': f(row['Close']),
            'adj_close': f(row['Close']),
            'volume': int(row['Volume']) if pd.notna(row['Volume']) else None,
            'rsi_14': f(row['rsi_14']), 'rsi_ema_9': f(row['rsi_ema_9']),
            'ema_9': f(row['ema_9']), 'ema_20': f(row['ema_20']),
            'ema_50': f(row['ema_50']), 'ema_200': f(row['ema_200']),
            'atr_14': f(row['atr_14']),
            'weekly_rsi_14': f(row['weekly_rsi_14']),
            'weekly_rsi_ema_9': f(row['weekly_rsi_ema_9']),
            'high_52w': f(row['high_52w']),
            'vol_ratio': f(row['vol_ratio']),
            'alkalyme_rs': f(row['alkalyme_rs']),
        })
    return records


def benchmark_ohlc_records(ticker, df):
    """OHLC-only rows for an index — no indicators, no RS."""
    if df is None or df.empty:
        return []

    def f(v):
        return float(v) if pd.notna(v) else None

    return [{
        'ticker': ticker,
        'snapshot_date': date.strftime('%Y-%m-%d'),
        'open': f(row['Open']), 'high': f(row['High']),
        'low': f(row['Low']), 'close': f(row['Close']),
        'adj_close': f(row['Close']),
        'volume': int(row['Volume']) if pd.notna(row['Volume']) else None,
    } for date, row in df.tail(STORE_DAYS).iterrows()]


def load_universe(sb):
    """CLI args override; otherwise S&P 500 file + held tickers not in it."""
    if len(sys.argv) > 1:
        return [t.strip().upper() for t in sys.argv[1:]]
    with open(os.path.join(BASE, 'tickers_sp500.txt'), encoding='utf-8') as fh:
        tickers = [ln.strip() for ln in fh if ln.strip()]
    held = {h['ticker'] for h in (sb.table('holdings').select('ticker')
                                  .execute().data or []) if h.get('ticker')}
    extras = sorted(held - set(tickers))
    if extras:
        print(f'  + {len(extras)} held extras outside the S&P 500: {extras}')
    return tickers + extras


def cleanup_old(sb):
    cutoff = (datetime.now(timezone.utc) - pd.Timedelta(days=STORE_DAYS)).strftime('%Y-%m-%d')
    try:
        sb.table('daily_stock_snapshots').delete().lt('snapshot_date', cutoff).execute()
        print(f'Cleaned daily_stock_snapshots rows before {cutoff}')
    except Exception as e:
        print(f'WARN cleanup: {e}')


def main():
    print('=' * 70)
    print('US OHLC FETCHER  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 70)
    sb = create_client(URL, KEY)
    universe = load_universe(sb)
    print(f'Universe: {len(universe)} tickers')

    print(f'Downloading benchmark {BENCHMARK} ...')
    bench_hist = yf.download(BENCHMARK, period=HISTORY_PERIOD, interval='1d',
                             auto_adjust=True, progress=False)
    bench_df = get_ticker_df(bench_hist, BENCHMARK)
    index_closes = bench_df['Close'].dropna() if bench_df is not None else None
    if index_closes is None or len(index_closes) < 25:
        print('WARN: benchmark history unavailable — Alkalyme RS will be null')
    else:
        bench_rows = benchmark_ohlc_records(BENCHMARK, bench_df)
        if bench_rows:
            sb.table('daily_stock_snapshots').upsert(
                bench_rows, on_conflict='ticker,snapshot_date').execute()
            print(f'  stored {len(bench_rows)} {BENCHMARK} OHLC rows')

    ok = fail = total = 0
    for i in range(0, len(universe), BATCH_SIZE):
        batch = universe[i:i + BATCH_SIZE]
        bnum = i // BATCH_SIZE + 1
        print(f'  batch {bnum} ({len(batch)} tickers) ...', end=' ', flush=True)
        try:
            hist = yf.download(batch, period=HISTORY_PERIOD, interval='1d',
                               auto_adjust=True, progress=False, threads=True,
                               group_by='ticker')
        except Exception as e:
            print(f'download error: {e}')
            fail += len(batch)
            continue

        batch_records = []
        for ticker in batch:
            recs = compute_records(ticker, get_ticker_df(hist, ticker), index_closes)
            if recs:
                batch_records.extend(recs)
                ok += 1
            else:
                fail += 1

        for j in range(0, len(batch_records), 500):
            sb.table('daily_stock_snapshots').upsert(
                batch_records[j:j + 500], on_conflict='ticker,snapshot_date').execute()
        total += len(batch_records)
        print(f'{len(batch_records)} rows')
        if i + BATCH_SIZE < len(universe):
            time.sleep(SLEEP_BATCH)

    cleanup_old(sb)
    print('=' * 70)
    print(f'Done. {ok} tickers OK | {fail} failed | {total:,} rows upserted')
    print(f'Completed: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}')


if __name__ == '__main__':
    main()
