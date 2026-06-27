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
SLEEP_BATCH = 3          # sequential pacing — concurrency triggers Yahoo throttling
FETCH_ATTEMPTS = 3       # per-batch retries on transient download errors


def _safe(val, decimals=4):
    try:
        if val is None:
            return None
        f = float(val)
        if np.isnan(f) or np.isinf(f):   # inf (e.g. debt/equity on zero equity) breaks JSON upsert
            return None
        return round(f, decimals)
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
    # Paginate — PostgREST caps a plain select at 1000 rows, which silently
    # truncated the ~1,900-name universe to 1000.
    tickers, seen = [], set()
    page, frm = 1000, 0
    while True:
        resp = (sb.table('us_stock_sectors').select('ticker')
                  .range(frm, frm + page - 1).execute())
        for r in (resp.data or []):
            t = r.get('ticker')
            if t and t not in seen:
                seen.add(t)
                tickers.append(t)
        if not resp.data or len(resp.data) < page:
            break
        frm += page
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


def _download_max(batch, attempts=FETCH_ATTEMPTS):
    """yf.download(period='max') with retry/backoff so a transient error doesn't
    silently drop a whole batch of price metrics."""
    for a in range(1, attempts + 1):
        try:
            hist = yf.download(batch, period='max', interval='1d', auto_adjust=True,
                               progress=False, threads=True)
            if hist is not None and not hist.empty:
                return hist
        except Exception as e:
            if a < attempts:
                time.sleep(2 * a)
                continue
            print(f'    price download error: {str(e)[:60]}')
    return None


def fetch_price_metrics_batch(batch):
    results = {}
    hist = _download_max(batch)
    if hist is None or hist.empty:
        return results
    try:
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


def fetch_quarterly_batch(batch):
    """Pull last ~8 quarters of revenue + net_income from yfinance.

    Returns ticker -> [{quarter_end, revenue, net_income}, ...] newest-first,
    matching the JSON shape the paper trader's earnings filter expects.
    """
    REVENUE_ALIASES = ['Total Revenue', 'TotalRevenue', 'Revenue', 'Operating Revenue']
    NET_INCOME_ALIASES = ['Net Income', 'NetIncome', 'Net Income Common Stockholders',
                          'Net Income From Continuing Operations']
    results = {}
    try:
        tks = yf.Tickers(' '.join(batch))
        for ticker in batch:
            try:
                df = tks.tickers[ticker].quarterly_income_stmt
                if df is None or df.empty:
                    continue

                def _row(aliases):
                    for a in aliases:
                        if a in df.index:
                            return df.loc[a]
                    return None

                rev = _row(REVENUE_ALIASES)
                ni  = _row(NET_INCOME_ALIASES)
                if rev is None or ni is None:
                    continue

                # Columns are Timestamps; iterate newest-first (DataFrame is already newest-first)
                quarters = []
                for col in df.columns:
                    r = rev.get(col)
                    n = ni.get(col)
                    if r is None or n is None:
                        continue
                    try:
                        r_f = float(r) if not (isinstance(r, float) and np.isnan(r)) else None
                        n_f = float(n) if not (isinstance(n, float) and np.isnan(n)) else None
                    except (TypeError, ValueError):
                        r_f, n_f = None, None
                    if r_f is None or n_f is None:
                        continue
                    quarter_end = col.strftime('%Y-%m-%d') if hasattr(col, 'strftime') else str(col)[:10]
                    quarters.append({'quarter_end': quarter_end, 'revenue': r_f, 'net_income': n_f})

                # Ensure newest-first sort even if yfinance order changes
                quarters.sort(key=lambda q: q['quarter_end'], reverse=True)
                if quarters:
                    results[ticker] = quarters[:8]  # cap at 8 quarters
            except Exception:
                pass
    except Exception as e:
        print(f'    quarterly batch error: {e}')
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


def run_pass(label, batches, fn):
    """Run one fetch pass over all batches SEQUENTIALLY (concurrency triggers
    Yahoo rate-limiting — the OHLC fetcher proved sequential + threads=True +
    sleep gets full coverage). Per-batch retry lives inside the fetch fns."""
    out = {}
    for i, b in enumerate(batches, 1):
        try:
            out.update(fn(b) or {})
        except Exception as e:
            print(f'  {label} batch {i} failed: {str(e)[:60]}')
        if i % 5 == 0 or i == len(batches):
            print(f'  {label}: {i}/{len(batches)} batches | {len(out)} tickers')
        if i < len(batches):
            time.sleep(SLEEP_BATCH)
    return out


def main():
    print('=' * 70)
    print('US YFINANCE METRICS FETCHER  ',
          datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 70)
    sb = create_client(URL, KEY)
    tickers = get_universe(sb)
    if not tickers:
        sys.exit('No tickers found')

    batches = [tickers[i:i + BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
    print(f'{len(tickers)} tickers in {len(batches)} batches (sequential)')
    price_metrics = run_pass('price', batches, fetch_price_metrics_batch)
    metadata = run_pass('metadata', batches, fetch_metadata_batch)
    quarterly = run_pass('quarterly', batches, fetch_quarterly_batch)

    records = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for ticker in tickers:
        pm, md = price_metrics.get(ticker, {}), metadata.get(ticker, {})
        qf = quarterly.get(ticker)
        if not pm and not md and not qf:
            continue
        rec = {'ticker': ticker, 'last_updated': now_iso, 'source_yfinance': True}
        rec.update(pm)
        rec.update(md)
        if qf:
            rec['quarterly_financials'] = qf
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
    print(f'  price metrics: {len(price_metrics)} | metadata: {len(metadata)} | quarterly: {len(quarterly)}')


if __name__ == '__main__':
    main()
