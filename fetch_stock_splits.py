#!/usr/bin/env python3
"""Split ex-dates and ratios for everything we have ever traded or hold.

Why this exists. `transactions` records what was actually traded on the day:
30 SNDK at $798 on 2026-11-17. `live_prices` records what it costs now: $81.46,
after an 8:1 split on 2026-11-20. Both are correct and they are not comparable,
so the Transactions tab read that sale as +$21,496 of MTM against a true
+$4,390 — a $17,107 overstatement on one row, and the same error on all nine
SNDK rows going back to April.

The fix is not to rewrite the transactions. They have to keep tying to the
contract note, and "160 shares at $114.29" is not what happened. The fix is to
know the splits, so a stored price can be lifted into today's terms wherever it
meets a live quote.

    ratio is new shares per old share.
    8 means an 8:1 split: a trade before ex_date holds 8x the shares at 1/8
    the price. A reverse split is a fraction — 0.1 for a 1:10.

Scope is deliberately the traded set, not the scan universe. ~1,900 yfinance
lookups a night to learn about splits in names we have never owned would be
most of the runtime for none of the benefit; the ~24 we have touched is a
couple of seconds. A name bought tomorrow is picked up tomorrow, before any
split it could have can matter.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Usage: python fetch_stock_splits.py [--report-only] [--since YYYY-MM-DD]
"""
import os
import sys
from datetime import datetime, timezone

import yfinance as yf
from dotenv import load_dotenv
from supabase import create_client

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))
URL, KEY = os.environ.get('SUPABASE_URL'), os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')
sb = create_client(URL, KEY)

# A ratio of exactly 1 is not a split, and yfinance occasionally carries one.
# Anything beyond this is far likelier to be bad vendor data than a real
# corporate action, and a wrong ratio here silently rewrites every historical
# comparison for that name — worth refusing rather than storing.
MIN_RATIO, MAX_RATIO = 0.001, 1000.0


def traded_tickers():
    """Everything ever transacted, plus everything currently held.

    Both, not either: a closed position still needs its history comparable
    (SNDK is closed and is exactly the case that prompted this), and a holding
    could in principle predate the transaction log.
    """
    seen, first = set(), {}
    frm = 0
    while True:
        rows = (sb.table('transactions').select('ticker,date')
                .range(frm, frm + 999).execute().data or [])
        for r in rows:
            t = r['ticker']
            seen.add(t)
            if t not in first or r['date'] < first[t]:
                first[t] = r['date']
        if len(rows) < 1000:
            break
        frm += 1000
    for r in sb.table('holdings').select('ticker').execute().data or []:
        seen.add(r['ticker'])
    return sorted(seen), first


def main():
    report_only = '--report-only' in sys.argv
    since = None
    if '--since' in sys.argv:
        since = sys.argv[sys.argv.index('--since') + 1]

    tickers, first_trade = traded_tickers()
    print('=' * 72)
    print('STOCK SPLITS  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 72)
    print(f'{len(tickers)} tickers ever traded or currently held\n')

    rows, skipped = [], []
    for tk in tickers:
        try:
            s = yf.Ticker(tk).splits
        except Exception as e:
            skipped.append((tk, f'{type(e).__name__}: {e}'))
            continue
        if s is None or not len(s):
            continue
        for dt, ratio in s.items():
            ratio = float(ratio)
            ex = dt.date().isoformat()
            if ratio == 1.0 or not (MIN_RATIO <= ratio <= MAX_RATIO):
                skipped.append((tk, f'implausible ratio {ratio} on {ex}'))
                continue
            if since and ex < since:
                continue
            rows.append({'ticker': tk, 'ex_date': ex, 'ratio': ratio,
                         'source': 'yfinance'})

    rows.sort(key=lambda r: (r['ex_date'], r['ticker']), reverse=True)
    print(f'{"ticker":<8}{"ex-date":<12}{"ratio":>8}   first traded   affects')
    print('-' * 62)
    for r in rows[:40]:
        ft = first_trade.get(r['ticker'], '')
        # A split before we ever owned it changes nothing we store.
        bites = 'yes' if ft and r['ex_date'] >= ft else 'no — predates us'
        print(f'{r["ticker"]:<8}{r["ex_date"]:<12}{r["ratio"]:>8.4g}   '
              f'{ft:<14} {bites}')
    if len(rows) > 40:
        print(f'  ... and {len(rows) - 40} older')

    live = [r for r in rows
            if first_trade.get(r['ticker']) and r['ex_date'] >= first_trade[r['ticker']]]
    print(f'\n{len(rows)} splits on record; {len(live)} fall after a trade we hold '
          f'a record of and therefore change a stored comparison.')
    if skipped:
        print(f'skipped {len(skipped)}:')
        for tk, why in skipped[:10]:
            print(f'  {tk}: {why}')

    if report_only:
        print('\n(report only — nothing written)')
        return
    if not rows:
        print('\nnothing to write.')
        return
    for i in range(0, len(rows), 500):
        sb.table('stock_splits').upsert(rows[i:i + 500],
                                        on_conflict='ticker,ex_date').execute()
    print(f'\nupserted {len(rows)} split rows.')


if __name__ == '__main__':
    main()
