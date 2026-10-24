#!/usr/bin/env python3
"""Who reports, and when, from Nasdaq's public earnings calendar.

The only forward-looking data in the system. Everything else on the boards
describes a session that has already closed; this says what is coming.

Source choice matters. The obvious route was a commentary account that posts a
weekly cashtag list, which would have needed a login, a stored session cookie
that expires, and automated collection against a platform's terms. Nasdaq
publishes the same information as plain JSON with no key, no login and no
scraping, and it carries two things the cashtag list does not: the consensus
EPS estimate, and whether the company reports before the open or after the
close. That second one decides which session the print lands on.

Filtered to our own universe, because a calendar of four hundred names nobody
tracks is noise. The point is the join: of the names reporting this week, which
are already scoring well, and which are the ones a surprise would move.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Usage: python fetch_earnings_calendar.py [--days N] [--dry-run]
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

import sb_retry  # noqa: F401

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')
sb = create_client(URL, KEY)

API = 'https://api.nasdaq.com/api/calendar/earnings?date={d}'
DAYS_AHEAD = 10          # two trading weeks of warning is enough to act on
PAUSE = 1.2              # between day requests; it is someone else's server


def fetch_day(d):
    req = urllib.request.Request(API.format(d=d.isoformat()), headers={
        'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
    try:
        raw = urllib.request.urlopen(req, timeout=25).read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        print(f'  {d}  FAILED {type(e).__name__} {getattr(e, "code", "")}')
        return []
    try:
        rows = (json.loads(raw).get('data') or {}).get('rows') or []
    except (ValueError, AttributeError):
        print(f'  {d}  unparseable response')
        return []
    return rows


def when_of(raw):
    """Nasdaq spells it 'time-pre-market' / 'time-after-hours' / not-supplied."""
    s = (raw or '').lower()
    if 'pre' in s:
        return 'pre'
    if 'after' in s:
        return 'after'
    return 'unknown'


def num(v):
    """'$3.43' and '($0.21)' -> 3.43 and -0.21. Parentheses mean a loss."""
    if v is None:
        return None
    s = str(v).strip().replace('$', '').replace(',', '')
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()')
    try:
        f = float(s)
    except ValueError:
        return None
    return -f if neg else f


def universe():
    out, frm = set(), 0
    while True:
        r = (sb.table('us_stock_sectors').select('ticker')
             .range(frm, frm + 999).execute().data or [])
        out |= {x['ticker'] for x in r if x.get('ticker')}
        if len(r) < 1000:
            break
        frm += 1000
    return out


def main():
    dry = '--dry-run' in sys.argv
    days = DAYS_AHEAD
    if '--days' in sys.argv:
        days = int(sys.argv[sys.argv.index('--days') + 1])

    print('=' * 66)
    print('EARNINGS CALENDAR  ',
          datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 66)

    known = universe()
    print(f'universe: {len(known)} tickers\n')

    rows, seen_total = {}, 0
    today = date.today()
    for i in range(days):
        d = today + timedelta(days=i)
        if d.weekday() > 4:            # nobody reports at the weekend
            continue
        got = fetch_day(d)
        seen_total += len(got)
        kept = 0
        for r in got:
            sym = (r.get('symbol') or '').strip().upper()
            if not sym or sym not in known:
                continue
            # Nasdaq lists a name on more than one date when it revises; the
            # primary key keeps the pair, and the later fetch wins.
            rows[(sym, d.isoformat())] = {
                'symbol': sym, 'report_date': d.isoformat(),
                'when_of_day': when_of(r.get('time')),
                'company_name': (r.get('name') or '')[:120] or None,
                'eps_estimate': num(r.get('epsForecast')),
            }
            kept += 1
        print(f'  {d}  {len(got):4} reporting, {kept:3} in our universe')
        time.sleep(PAUSE)

    out = list(rows.values())
    print(f'\n{len(out)} rows kept from {seen_total} listings')
    if not out:
        print('nothing to write')
        return 0

    by_day = {}
    for r in out:
        by_day.setdefault(r['report_date'], []).append(r['symbol'])
    print('\nnext sessions:')
    for d in sorted(by_day)[:5]:
        names = sorted(by_day[d])
        print(f'  {d}  {len(names):3}  {", ".join(names[:12])}'
              + (' ...' if len(names) > 12 else ''))

    if dry:
        print('\n--dry-run: nothing written')
        return 0
    for i in range(0, len(out), 500):
        sb.table('earnings_calendar').upsert(
            out[i:i + 500], on_conflict='symbol,report_date').execute()
    print(f'\nupserted {len(out)} rows')
    return 0


if __name__ == '__main__':
    sys.exit(main())
