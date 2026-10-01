#!/usr/bin/env python3
"""Universe attrition report — which scan tickers have stopped reporting.

The coverage guard in fetch_ohlc catches a whole-market stall. It cannot catch
a slow leak: individual tickers that quietly stop returning price history and
drop out of every scan while the run stays green. 32 names had leaked out
before anyone looked, including EQR and AVB — $25bn S&P 500 REITs sitting in
the housing theme.

Two causes, and they want different responses:

  dead     Yahoo 404s the symbol entirely. Delisted, acquired or taken
           private. The universe row is stale and should be pruned.
  stalled  Yahoo still knows the company (.info resolves, market cap, sector)
           but serves a truncated history — typically one lone bar from the
           day it stopped. Seen clustering around pending corporate actions.
           Nothing to fetch; decide whether to keep carrying it.

This only reports. Pruning the universe is a judgement call, so it prints what
it found and leaves the decision alone.

Usage:
  python check_universe_health.py            # report
  python check_universe_health.py --probe    # also ask Yahoo dead vs stalled
                                             # (one .info call per missing name)
Env: SUPABASE_URL, SUPABASE_SERVICE_KEY.
"""

import os
import sys
from datetime import date, timedelta

from dotenv import load_dotenv
from supabase import create_client

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

sb = create_client(URL, KEY)

STALE_DAYS = 5          # absent this many calendar days -> listed individually
NEW_LEAK_ALERT = 10     # newly-missing in one session -> worth shouting about


def paginate(table, cols, filters=None, page=1000):
    """Keyset-free pagination via range(). Offset past ~100k rows times out on
    this project, but these tables are small enough for range() to be safe."""
    out, frm = [], 0
    while True:
        q = sb.table(table).select(cols)
        for f in (filters or []):
            q = f(q)
        rows = q.range(frm, frm + page - 1).execute().data or []
        out.extend(rows)
        if len(rows) < page:
            return out
        frm += page


def main():
    print('=' * 70)
    print('UNIVERSE HEALTH')
    print('=' * 70)

    scored = (sb.table('daily_stock_snapshots').select('snapshot_date')
              .not_.is_('momentum_score', 'null')
              .order('snapshot_date', desc=True).limit(1).execute().data)
    if not scored:
        sys.exit('No scored snapshot to measure against.')
    latest = scored[0]['snapshot_date']

    universe = {r['ticker']: r for r in
                paginate('us_stock_sectors', 'ticker,company_name,sector')}
    present = {r['ticker'] for r in
               paginate('daily_stock_snapshots', 'ticker',
                        [lambda q: q.eq('snapshot_date', latest)])}
    missing = sorted(set(universe) - present)

    print(f'Latest scored session : {latest}')
    print(f'Universe              : {len(universe):,}')
    print(f'Reported              : {len(universe) - len(missing):,}')
    print(f'Missing               : {len(missing)} '
          f'({len(missing) / max(len(universe), 1):.1%})')

    if not missing:
        print('\nNo attrition. Nothing to do.')
        return

    # When did each missing ticker last report? Oldest leaks first.
    last_seen = {}
    for t in missing:
        r = (sb.table('daily_stock_snapshots').select('snapshot_date')
             .eq('ticker', t).order('snapshot_date', desc=True)
             .limit(1).execute().data)
        last_seen[t] = r[0]['snapshot_date'] if r else None

    # A name that leaked out weeks ago is known; one that vanished since the
    # previous session is news. Compare against the session before this one.
    prev = (sb.table('daily_stock_snapshots').select('snapshot_date')
            .not_.is_('momentum_score', 'null').lt('snapshot_date', latest)
            .order('snapshot_date', desc=True).limit(1).execute().data)
    new_leaks = []
    if prev:
        prev_date = prev[0]['snapshot_date']
        prev_present = {r['ticker'] for r in
                        paginate('daily_stock_snapshots', 'ticker',
                                 [lambda q: q.eq('snapshot_date', prev_date)])}
        new_leaks = sorted(set(missing) & prev_present)

    in_themes = {r['ticker'] for r in
                 paginate('theme_members', 'ticker')} & set(missing)

    never = [t for t in missing if last_seen[t] is None]
    today = date.today()
    stale = sorted((t for t in missing if last_seen[t]),
                   key=lambda t: last_seen[t])

    if new_leaks:
        print(f'\n--- DROPPED OUT SINCE {prev_date} ({len(new_leaks)}) ---')
        for t in new_leaks:
            print(f'  {t:<8} {(universe[t].get("company_name") or "")[:42]}')

    if never:
        print(f'\n--- NEVER FETCHED ({len(never)}) — in the universe table but '
              f'no price history has ever arrived ---')
        for t in never:
            print(f'  {t:<8} {(universe[t].get("company_name") or "")[:42]}')

    old = [t for t in stale
           if (today - date.fromisoformat(last_seen[t])).days > STALE_DAYS]
    if old:
        print(f'\n--- STALE > {STALE_DAYS}d ({len(old)}) ---')
        for t in old:
            days = (today - date.fromisoformat(last_seen[t])).days
            flag = '  [in a theme]' if t in in_themes else ''
            print(f'  {t:<8} last {last_seen[t]}  {days:>3}d  '
                  f'{(universe[t].get("company_name") or "")[:34]:<34}{flag}')

    if in_themes:
        print(f'\n{len(in_themes)} missing ticker(s) are mapped into a theme '
              f'value chain and will render without momentum: '
              f'{", ".join(sorted(in_themes))}')

    if '--probe' in sys.argv:
        probe(missing, universe)

    print()
    if len(new_leaks) >= NEW_LEAK_ALERT:
        sys.exit(f'ERROR: {len(new_leaks)} tickers dropped out in one session — '
                 f'that is a fetch problem, not attrition.')
    print(f'Report only — {len(missing)} missing, {len(new_leaks)} of them new. '
          f'Pruning the universe is a manual call.')


def probe(missing, universe):
    """Ask Yahoo which missing names are dead vs merely stalled. Rate-limited
    and slow, so it stays behind a flag rather than running every night."""
    try:
        import yfinance as yf
    except ImportError:
        print('\n(--probe needs yfinance)')
        return
    print(f'\n--- PROBING {len(missing)} TICKERS AT SOURCE ---')
    dead, stalled = [], []
    for t in missing:
        try:
            info = yf.Ticker(t).info
            (stalled if info.get('quoteType') else dead).append(t)
        except Exception:
            dead.append(t)
    print(f'\n  DEAD ({len(dead)}) — 404 at source, safe to prune:')
    print('    ' + ', '.join(dead) if dead else '    none')
    print(f'\n  STALLED ({len(stalled)}) — still listed, history truncated:')
    for t in stalled:
        print(f'    {t:<8} {(universe[t].get("company_name") or "")[:44]}')


if __name__ == '__main__':
    main()
