#!/usr/bin/env python3
"""yfinance fundamentals/metrics fetcher for the US universe.

Pulls all-time high, beta, market cap, valuation ratios, margins, growth and
multi-period returns from Yahoo and upserts them into stock_fundamentals.

Port of the India fetch_yfinance_metrics.py — same batch design, US tickers,
market cap stored in raw USD.

Usage:
  python fetch_yfinance_metrics.py            # full universe
  python fetch_yfinance_metrics.py NVDA MSFT  # only the named tickers

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (.env in repo root auto-loaded).
"""

import os
import sys
import time
from datetime import datetime, date, timezone

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

BATCH_SIZE = 50
SLEEP_BATCH = 3


def _safe(val, decimals=4):
    try:
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return None
        return round(float(val), decimals)
    except Exception:
        return None


def pct(v):
    """yfinance margins/ROE/growth are fractions (0.34 -> 34%). US mega-caps
    can have ROE or growth above 100%, so always scale by 100 — no <=1 guard
    (that guard, carried from the India code, left such values unconverted)."""
    s = _safe(v)
    return round(s * 100, 2) if s is not None else None


def get_universe(sb):
    if len(sys.argv) > 1:
        return [t.strip().upper() for t in sys.argv[1:]]
    rows = sb.table('us_stock_sectors').select('ticker').execute().data or []
    tickers = [r['ticker'] for r in rows if r.get('ticker')]
    print(f'{len(tickers)} tickers from us_stock_sectors')
    return tickers


def _compute_price_metrics(highs, closes):
    today_price = float(closes.iloc[-1])
    ath_price = float(highs.max())
    ath_idx = highs.idxmax()
    ath_date = ath_idx.date().isoformat() if hasattr(ath_idx, 'date') else str(ath_idx)[:10]

    def ret(n):
        if len(closes) > n:
            past = float(closes.iloc[-n])
            return round((today_price - past) / past * 100, 2) if past > 0 else None
        return None

    year_start = f'{date.today().year}-01-01'
    ytd_slice = closes[closes.index >= year_start]
    ytd = (round((today_price - float(ytd_slice.iloc[0])) / float(ytd_slice.iloc[0]) * 100, 2)
           if len(ytd_slice) > 1 else None)

    return {
        'ath_price': round(ath_price, 2),
        'ath_date': ath_date,
        'return_1w': ret(5), 'return_1m': ret(21), 'return_3m': ret(63),
        'return_6m': ret(126), 'return_ytd': ytd, 'return_1y': ret(252),
        'return_3y': ret(756), 'return_5y': ret(1260),
    }


def fetch_price_metrics_batch(batch):
    results = {}
    try:
        hist = yf.download(batch, period='max', interval='1d', auto_adjust=True,
                           progress=False, threads=True)
        if hist.empty:
            return results
        if len(batch) == 1:
            closes = hist['Close'] if 'Close' in hist.columns else None
            highs = hist['High'] if 'High' in hist.columns else None
            if closes is not None and not closes.empty:
                results[batch[0]] = _compute_price_metrics(highs, closes)
        else:
            closes = hist['Close'] if 'Close' in hist else None
            highs = hist['High'] if 'High' in hist else None
            if closes is not None:
                for ticker in batch:
                    if ticker in closes.columns:
                        c = closes[ticker].dropna()
                        h = (highs[ticker].dropna()
                             if highs is not None and ticker in highs.columns else c)
                        if len(c) > 10:
                            results[ticker] = _compute_price_metrics(h, c)
    except Exception as e:
        print(f'    batch download error: {e}')
    return results


def fetch_metadata_batch(batch):
    results = {}
    try:
        tks = yf.Tickers(' '.join(batch))
        for ticker in batch:
            try:
                info = tks.tickers[ticker].info or {}
                mcap = info.get('marketCap')

                rec = {
                    'stock_name': info.get('longName') or info.get('shortName'),
                    'beta': _safe(info.get('beta')),
                    'market_cap_usd': round(mcap, 0) if mcap else None,
                    'pe_ttm': _safe(info.get('trailingPE')),
                    'eps_ttm': _safe(info.get('trailingEps')),
                    'price_to_book': _safe(info.get('priceToBook')),
                    'price_to_sales': _safe(info.get('priceToSalesTrailing12Months')),
                    'ev_to_ebitda': _safe(info.get('enterpriseToEbitda')),
                    'roe': pct(info.get('returnOnEquity')),
                    'net_margin': pct(info.get('profitMargins')),
                    'ebitda_margin': pct(info.get('ebitdaMargins')),
                    'gross_margin': pct(info.get('grossMargins')),
                    'debt_to_equity': _safe(info.get('debtToEquity')),
                    'dividend_yield': _safe(info.get('dividendYield'), 2),
                    'book_value_per_share': _safe(info.get('bookValue')),
                    'revenue_growth_yoy': pct(info.get('revenueGrowth')),
                    'earnings_growth_yoy': pct(info.get('earningsGrowth')),
                }
                results[ticker] = {k: v for k, v in rec.items() if v is not None}
            except Exception:
                pass
    except Exception as e:
        print(f'    metadata batch error: {e}')
    return results


def main():
    print('=' * 70)
    print('US YFINANCE METRICS FETCHER  ',
          datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 70)
    sb = create_client(URL, KEY)
    tickers = get_universe(sb)
    if not tickers:
        sys.exit('No tickers found')

    n_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE
    price_metrics, metadata = {}, {}

    print(f'Fetching price history in {n_batches} batches ...')
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        print(f'  price batch {i // BATCH_SIZE + 1}/{n_batches} ...', end=' ', flush=True)
        m = fetch_price_metrics_batch(batch)
        price_metrics.update(m)
        print(f'{len(m)}/{len(batch)}')
        if i + BATCH_SIZE < len(tickers):
            time.sleep(SLEEP_BATCH)

    print(f'Fetching metadata in {n_batches} batches ...')
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        print(f'  meta batch {i // BATCH_SIZE + 1}/{n_batches} ...', end=' ', flush=True)
        m = fetch_metadata_batch(batch)
        metadata.update(m)
        print(f'{len(m)}/{len(batch)}')
        if i + BATCH_SIZE < len(tickers):
            time.sleep(SLEEP_BATCH)

    records = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for ticker in tickers:
        pm, md = price_metrics.get(ticker, {}), metadata.get(ticker, {})
        if not pm and not md:
            continue
        rec = {'ticker': ticker, 'last_updated': now_iso, 'source_yfinance': True}
        rec.update(pm)
        rec.update(md)
        records.append(rec)

    success = 0
    for i in range(0, len(records), 50):
        for rec in records[i:i + 50]:
            try:
                sb.table('stock_fundamentals').upsert(rec, on_conflict='ticker').execute()
                success += 1
            except Exception as e:
                print(f'    save error {rec.get("ticker")}: {e}')

    print('=' * 70)
    print(f'Done. {success}/{len(records)} rows upserted into stock_fundamentals')
    print(f'  price metrics: {len(price_metrics)} | metadata: {len(metadata)}')


if __name__ == '__main__':
    main()
