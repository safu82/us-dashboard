#!/usr/bin/env python3
"""Seed live_prices from the EOD close for the WHOLE universe.

The Railway live worker only streams a small set (holdings + open/pending paper
trades + indices), so live_prices freezes for every other ticker — which made the
dashboard show weeks-old prices for names like AMD. This nightly step backfills the
latest daily close into live_prices for any ticker whose live row is missing or
STALE (older than the latest snapshot date), so live_prices is never staler than
the last close for ANY ticker. Every UI that reads live_prices is then correct
without per-page guards.

It deliberately SKIPS tickers whose live row is already fresh (the streamed names),
so it never clobbers the worker's intraday data. Run in the nightly scan right
AFTER fetch_ohlc.py (snapshots must exist), in the post-/pre-market dead zone.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY.
"""

import os
import sys
from datetime import datetime, timezone

from supabase import create_client
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

sb = create_client(URL, KEY)


def paginate(table, columns, order_cols, filters=None, page=1000):
    out, frm = [], 0
    while True:
        q = sb.table(table).select(columns)
        for c in order_cols:
            q = q.order(c)
        for f in (filters or []):
            q = f(q)
        res = q.range(frm, frm + page - 1).execute()
        if not res.data:
            break
        out.extend(res.data)
        if len(res.data) < page:
            break
        frm += page
    return out


def main():
    print('=' * 60)
    print('SEED LIVE_PRICES (EOD) ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 60)

    # Two most recent snapshot dates (for close + day change).
    recent = (sb.table('daily_stock_snapshots').select('snapshot_date')
                .order('snapshot_date', desc=True).limit(1).execute().data)
    if not recent:
        sys.exit('daily_stock_snapshots empty')
    latest = recent[0]['snapshot_date']
    prior_rows = (sb.table('daily_stock_snapshots').select('snapshot_date')
                    .lt('snapshot_date', latest)
                    .order('snapshot_date', desc=True).limit(1).execute().data)
    prior = prior_rows[0]['snapshot_date'] if prior_rows else None
    print(f'latest close {latest}, prior {prior}')

    # Closes for the latest (and prior) date.
    snaps = paginate('daily_stock_snapshots', 'ticker, snapshot_date, close, volume',
                     ['snapshot_date', 'ticker'],
                     filters=[lambda q: q.in_('snapshot_date',
                                              [d for d in (latest, prior) if d])])
    now_close, prev_close, vol = {}, {}, {}
    for r in snaps:
        if r['snapshot_date'] == latest:
            now_close[r['ticker']] = r.get('close')
            vol[r['ticker']] = r.get('volume')
        elif r['snapshot_date'] == prior:
            prev_close[r['ticker']] = r.get('close')

    # Existing live_prices freshness (skip rows already current).
    live = {r['ticker']: r.get('updated_at')
            for r in paginate('live_prices', 'ticker, updated_at', ['ticker'])}

    def is_stale(tk):
        ts = live.get(tk)
        if ts is None:
            return True                     # missing → seed it
        return str(ts)[:10] < latest        # updated before the latest close date

    now_iso = datetime.now(timezone.utc).isoformat()
    rows, skipped = [], 0
    for tk, c in now_close.items():
        if c is None:
            continue
        if not is_stale(tk):
            skipped += 1                    # streamed/fresh — leave the worker's row alone
            continue
        p = prev_close.get(tk)
        dc = (c - p) if (p is not None) else None
        dcp = ((c / p - 1) * 100) if p else None
        rows.append({
            'ticker': tk, 'price': c, 'regular_close': c, 'prev_close': p,
            'day_change': round(dc, 4) if dc is not None else None,
            'day_change_pct': round(dcp, 4) if dcp is not None else None,
            'volume': vol.get(tk), 'market_state': 'CLOSED',
            'pre_market_price': None, 'pre_market_change_pct': None,
            'post_market_price': None, 'post_market_change_pct': None,
            'updated_at': now_iso,
        })

    for i in range(0, len(rows), 500):
        sb.table('live_prices').upsert(rows[i:i + 500], on_conflict='ticker').execute()

    print(f'Seeded {len(rows)} stale/missing tickers; left {skipped} fresh rows untouched.')


if __name__ == '__main__':
    main()
