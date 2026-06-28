#!/usr/bin/env python3
"""Pull analyst consensus + price targets + recent upgrades-downgrades from
yfinance for the full us_stock_sectors universe (~1,900 names). Upserts into
analyst_ratings keyed by ticker.

Runs daily via GitHub Actions. Each ticker is 3 per-ticker yfinance calls
(.info + recommendations_summary + upgrades_downgrades), fetched SEQUENTIALLY
with per-ticker retry/backoff — concurrency triggers Yahoo rate-limiting at this
scale (observed only 1256/1906 at 6 workers). Batch-upserted at the end.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (or local .env).
"""

import os
import sys
import time
from datetime import datetime, timezone

import yfinance as yf
import pandas as pd
from supabase import create_client
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY') or os.environ.get('SUPABASE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')


RECENT_ACTIONS_LIMIT = 30  # last N analyst actions per ticker
FETCH_ATTEMPTS = 3         # per-ticker retries on transient Yahoo errors


def _safe_float(v):
    try:
        if v is None:
            return None
        f = float(v)
        if pd.isna(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_int(v):
    try:
        if v is None:
            return None
        i = int(v)
        return i
    except (TypeError, ValueError):
        return None


def get_universe(sb):
    """Full scan universe (S&P 500 + NASDAQ 100 + held tickers ~ 518 names).
    Runtime ~4 minutes — well within yfinance polite-use limits and far below
    any GH Actions timeout. The deep dive needs analyst data for any ticker
    the user looks at, not just held names."""
    if len(sys.argv) > 1:
        return [t.strip().upper() for t in sys.argv[1:]]
    tickers = set()
    # us_stock_sectors covers the full universe (S&P 500 + NASDAQ adds + holdings)
    page = 1000
    frm = 0
    while True:
        resp = sb.table('us_stock_sectors').select('ticker').range(frm, frm + page - 1).execute()
        for r in (resp.data or []):
            if r.get('ticker'):
                tickers.add(r['ticker'])
        if not resp.data or len(resp.data) < page:
            break
        frm += page
    # Defensive: also include any holdings or open paper trades not in us_stock_sectors
    for h in (sb.table('holdings').select('ticker').execute().data or []):
        if h.get('ticker'):
            tickers.add(h['ticker'])
    for p in (sb.table('paper_trades').select('ticker')
                .in_('status', ['pending', 'open']).execute().data or []):
        if p.get('ticker'):
            tickers.add(p['ticker'])
    return sorted(tickers)


def fetch_one(ticker, attempts=FETCH_ATTEMPTS):
    """Returns a dict ready to upsert, or None. Retries on transient Yahoo
    errors (throttling) with backoff; a clean no-data result is not retried."""
    for attempt in range(1, attempts + 1):
        try:
            return _fetch_one_inner(ticker)
        except Exception as e:
            if attempt < attempts:
                time.sleep(1.5 * attempt)
                continue
            print(f'  {ticker}: ERROR {e}')
            return None


def _fetch_one_inner(ticker):
    """Core fetch — raises on transient errors so fetch_one can retry."""
    tk = yf.Ticker(ticker)
    info = tk.info or {}

    consensus = info.get('recommendationKey')
    consensus_score = _safe_float(info.get('recommendationMean'))
    num_analysts = _safe_int(info.get('numberOfAnalystOpinions'))
    target_mean = _safe_float(info.get('targetMeanPrice'))
    target_high = _safe_float(info.get('targetHighPrice'))
    target_low = _safe_float(info.get('targetLowPrice'))

    # Recommendations summary — current month row only (most relevant snapshot)
    rating_counts = None
    try:
        rs = tk.recommendations_summary
        if rs is not None and not rs.empty:
            row = rs.iloc[0]
            rating_counts = {
                'strongBuy': _safe_int(row.get('strongBuy')),
                'buy': _safe_int(row.get('buy')),
                'hold': _safe_int(row.get('hold')),
                'sell': _safe_int(row.get('sell')),
                'strongSell': _safe_int(row.get('strongSell')),
            }
    except Exception:
        pass

    # Upgrades / downgrades — last N
    recent_actions = []
    try:
        ud = tk.upgrades_downgrades
        if ud is not None and not ud.empty:
            # ud is indexed by datetime; head() gives the most recent
            for ts, row in ud.head(RECENT_ACTIONS_LIMIT).iterrows():
                recent_actions.append({
                    'date': ts.isoformat() if hasattr(ts, 'isoformat') else str(ts),
                    'firm': str(row.get('Firm') or ''),
                    'from_grade': str(row.get('FromGrade') or ''),
                    'to_grade': str(row.get('ToGrade') or ''),
                    'action': str(row.get('Action') or ''),
                })
    except Exception:
        pass

    if consensus is None and target_mean is None and not recent_actions:
        return None  # nothing useful

    return {
        'ticker': ticker,
        'consensus': consensus,
        'consensus_score': consensus_score,
        'target_mean': target_mean,
        'target_high': target_high,
        'target_low': target_low,
        'num_analysts': num_analysts,
        'rating_counts': rating_counts,
        'recent_actions': recent_actions or None,
        'last_updated': datetime.now(timezone.utc).isoformat(),
    }


def main():
    print('=' * 60)
    print('ANALYST RATINGS FETCHER  ',
          datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 60)
    sb = create_client(URL, KEY)
    universe = get_universe(sb)
    if not universe:
        sys.exit('No tickers in universe')
    print(f'Universe: {len(universe)} tickers (sequential)')

    # Sequential — concurrency triggers Yahoo rate-limiting at this scale.
    # Per-ticker retry/backoff lives in fetch_one.
    records, no_data = [], 0
    for i, tk in enumerate(universe, 1):
        rec = fetch_one(tk)
        if rec:
            records.append(rec)
        else:
            no_data += 1
        if i % 200 == 0 or i == len(universe):
            print(f'  fetched {i}/{len(universe)} ({len(records)} with data)')
        time.sleep(0.15)

    success = 0
    for i in range(0, len(records), 100):
        chunk = records[i:i + 100]
        try:
            sb.table('analyst_ratings').upsert(chunk, on_conflict='ticker').execute()
            success += len(chunk)
        except Exception as e:
            print(f'  upsert chunk {i}-{i + len(chunk)} failed: {e}')

    print('=' * 60)
    print(f'Done. {success}/{len(universe)} upserted | {len(records)} had data | {no_data} no-data')


if __name__ == '__main__':
    main()
