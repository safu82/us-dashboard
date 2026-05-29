#!/usr/bin/env python3
"""One-off: backfill missed paper fills using settled D1 OHLC.

When the live updater wasn't streaming paper-trade tickers, paper_fill_pending.py
returned 'no_live_price' for every pending row and never converted them to open.
This script does what the fill job would have done — fill each pending paper_trade
at the next-trading-day open from daily_stock_snapshots + 15 bps slippage.

Idempotent — skips rows whose D1 isn't settled yet (today's data, weekends).

Usage: python backfill_paper_fills.py
"""

import os
import sys
import traceback
from datetime import date, datetime

from supabase import create_client

from paper_trader import (
    SUPABASE_URL, SUPABASE_KEY,
    load_pending_paper_trades,
    log, to_float,
)
from paper_fill_pending import fill_pending


def main():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    pending = load_pending_paper_trades(sb)
    log(f'Found {len(pending)} pending paper_trades to backfill')
    if not pending:
        return

    counts = {'filled': 0, 'd1_not_settled': 0, 'rejected_no_atr': 0,
              'rejected_qty_zero': 0, 'errors': 0}

    for tr in pending:
        ticker = tr['ticker']
        try:
            scan_date = date.fromisoformat(tr['entry_date'])

            # Find the first settled trading day AFTER the scan date — this is
            # the intended D1 fill date. Using daily_stock_snapshots.snapshot_date
            # automatically skips weekends + holidays (no snapshot = no trading).
            resp = (sb.table('daily_stock_snapshots')
                      .select('snapshot_date, open')
                      .eq('ticker', ticker)
                      .gt('snapshot_date', scan_date.isoformat())
                      .order('snapshot_date').limit(1).execute())
            row = (resp.data or [None])[0]
            if not row or row.get('open') is None:
                counts['d1_not_settled'] += 1
                log(f'  {ticker}: D1 not settled yet (scan {scan_date}), skipping')
                continue

            d1_date = date.fromisoformat(row['snapshot_date'])
            d1_open = to_float(row['open'])
            outcome = fill_pending(sb, tr, d1_open, d1_date)
            counts[outcome] = counts.get(outcome, 0) + 1
            log(f'  {ticker}: scan {scan_date} -> D1 {d1_date} open ${d1_open:.2f} -> {outcome}')
        except Exception as e:
            counts['errors'] += 1
            log(f'  {ticker}: ERROR {e}')
            traceback.print_exc()

    log(f'done: {counts}')

    # Run log entry so it shows up alongside the regular fill phase logs
    try:
        sb.table('paper_run_log').insert({
            'run_date': date.today().isoformat(),
            'phase': 'backfill',
            'mode': 'paper',
            'funnel': counts,
        }).execute()
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
