#!/usr/bin/env python3
"""Yahoo Finance Live Price Updater — Railway worker.

Always-on worker (Procfile `worker`). During the US regular session it polls
the Yahoo v8 chart endpoint for every held ticker plus the index/benchmark
symbols and upserts them into `live_prices`. This replaces the old Google
Sheets + proxy-server price path.

Port of the India `zerodha_rest_updater_railway.py`, minus the Zerodha auth
layer (Yahoo needs no token) and the paper-trading fill/stop logic (Phase 4).

Scheduling is ET-aware via `America/New_York`, so US daylight-saving shifts
are handled automatically.

Env vars (a .env file in the repo root is auto-loaded for local runs;
on Railway set them as service variables):
  SUPABASE_URL
  SUPABASE_SERVICE_KEY
"""

import os
import time
from datetime import datetime, timezone

import requests
import pytz
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise SystemExit('SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
et = pytz.timezone('America/New_York')

POLL_INTERVAL = 15      # seconds between price cycles
TICKER_REFRESH = 300    # re-check holdings/transactions every 5 min

# Index / benchmark symbols streamed alongside holdings (Yahoo symbols).
INDEX_TICKERS = ['^GSPC', '^IXIC', '^DJI', '^VIX']

YAHOO_HOSTS = ['https://query1.finance.yahoo.com',
               'https://query2.finance.yahoo.com']
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def is_market_open():
    """US equity regular session: 09:30-16:00 ET, Mon-Fri.
    Market holidays are not special-cased — on a holiday prices simply
    don't move, which is harmless."""
    now = datetime.now(et)
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return start <= now <= end


def get_streaming_tickers():
    """Holdings + transaction-ledger tickers + index symbols."""
    tickers = set(INDEX_TICKERS)
    try:
        for h in (supabase.table('holdings').select('ticker').execute().data or []):
            if h.get('ticker'):
                tickers.add(h['ticker'])
        for t in (supabase.table('transactions').select('ticker').execute().data or []):
            if t.get('ticker'):
                tickers.add(t['ticker'])
    except Exception as e:
        print(f'WARN: could not load tickers from Supabase: {e}')
    return tickers


def fetch_quote(session, symbol):
    """Fetch one quote from the Yahoo v8 chart endpoint.
    Returns dict(price, prev_close, volume) or None on failure."""
    for host in YAHOO_HOSTS:
        try:
            url = f'{host}/v8/finance/chart/{symbol}?interval=1d&range=1d'
            r = session.get(url, timeout=10)
            if r.status_code != 200:
                continue
            meta = r.json()['chart']['result'][0]['meta']
            price = meta.get('regularMarketPrice')
            prev = meta.get('chartPreviousClose') or meta.get('previousClose')
            if price is None or prev is None:
                continue
            return {
                'price': float(price),
                'prev_close': float(prev),
                'volume': int(meta.get('regularMarketVolume') or 0),
            }
        except Exception:
            continue
    return None


def fetch_and_update_prices(session, tickers):
    """Fetch every ticker, upsert the batch into live_prices."""
    updates = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for symbol in tickers:
        q = fetch_quote(session, symbol)
        if not q:
            continue
        day_change = q['price'] - q['prev_close']
        day_change_pct = (day_change / q['prev_close'] * 100) if q['prev_close'] else 0
        updates.append({
            'ticker': symbol,
            'price': round(q['price'], 4),
            'day_change': round(day_change, 4),
            'day_change_pct': round(day_change_pct, 4),
            'prev_close': round(q['prev_close'], 4),
            'volume': q['volume'],
            'updated_at': now_iso,
        })
    if updates:
        supabase.table('live_prices').upsert(updates, on_conflict='ticker').execute()
    return len(updates)


def main():
    print('=' * 60)
    print('YAHOO LIVE PRICE UPDATER - Railway worker')
    print('=' * 60)
    session = requests.Session()
    session.headers.update({'User-Agent': UA})

    tickers = None
    last_session_date = None
    last_ticker_refresh = 0.0

    while True:
        try:
            now = datetime.now(et)

            if not is_market_open():
                print(f'Market closed - {now:%Y-%m-%d %H:%M %Z} - waiting...')
                time.sleep(60)
                continue

            # New session — (re)load the ticker set.
            if last_session_date != now.date():
                print(f'\n=== New market session: {now.date()} ===')
                tickers = get_streaming_tickers()
                print(f'Streaming {len(tickers)} tickers')
                last_session_date = now.date()
                last_ticker_refresh = time.time()

            # Periodic ticker refresh — picks up new holdings intraday.
            if time.time() - last_ticker_refresh > TICKER_REFRESH:
                refreshed = get_streaming_tickers()
                if refreshed != tickers:
                    added = sorted(refreshed - tickers)
                    dropped = sorted(tickers - refreshed)
                    if added:
                        print(f'+ added tickers: {added}')
                    if dropped:
                        print(f'- dropped tickers: {dropped}')
                    tickers = refreshed
                last_ticker_refresh = time.time()

            n = fetch_and_update_prices(session, tickers)
            print(f'{now:%H:%M:%S} ET - updated {n}/{len(tickers)} prices')
            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print('\nShutting down.')
            break
        except Exception as e:
            print(f'ERROR in main loop: {e} - retrying in 30s')
            time.sleep(30)


if __name__ == '__main__':
    main()
