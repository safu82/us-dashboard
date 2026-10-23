#!/usr/bin/env python3
"""The morning note: one short read, not a report.

Deliberately constrained to roughly 250 words. The dashboard already holds the
detail and will still be there at 9am; this is the thing you read once with a
coffee, and anything that does not change what you look at first does not earn
a line.

The one idea it exists to deliver is the divergence between what the press is
loud about and what the scanner scores well. Either alone is already on a
screen somewhere:

    loud + strong   confirmation, the move is understood
    loud + weak     distribution, the story outlived the trend
    strong + quiet  nobody has written it up yet

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Usage: python build_morning_note.py [--telegram] [--date YYYY-MM-DD]
"""

import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

# Patches postgrest to retry transient Supabase failures (gateway
# errors, timeouts) instead of letting one kill the nightly chain.
import sb_retry  # noqa: F401

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')
sb = create_client(URL, KEY)

LOUD = 2          # distinct sources before a name counts as "being talked about"
STRONG, WEAK = 80, 50
MACRO_LABEL = {'US10Y': '10-year', 'FED': 'the Fed', 'BRENT': 'oil',
               'GOLD': 'gold', 'CPI': 'inflation', 'JOBS': 'jobs',
               'DRAM': 'memory'}


def page(table, cols, f=None, size=1000):
    out, frm = [], 0
    while True:
        q = sb.table(table).select(cols)
        if f:
            q = f(q)
        rows = q.range(frm, frm + size - 1).execute().data or []
        out.extend(rows)
        if len(rows) < size:
            return out
        frm += size


def pct(v, d=1):
    return '–' if v is None else f'{v * 100:+.{d}f}%'


def main():
    want = None
    if '--date' in sys.argv:
        want = sys.argv[sys.argv.index('--date') + 1]

    q = (sb.table('daily_stock_snapshots').select('snapshot_date')
         .not_.is_('momentum_score', 'null'))
    if want:
        q = q.lte('snapshot_date', want)
    r = q.order('snapshot_date', desc=True).limit(1).execute().data
    if not r:
        sys.exit('no scored session')
    session = r[0]['snapshot_date']

    snaps = {s['ticker']: s for s in page(
        'daily_stock_snapshots', 'ticker,momentum_score,rs_rank',
        lambda x: x.eq('snapshot_date', session).not_.is_('momentum_score', 'null'))}
    names = {s['ticker']: s.get('company_name') for s in page(
        'us_stock_sectors', 'ticker,company_name')}

    # Mentions for the session, counted by DISTINCT SOURCE. One wire story
    # syndicated six ways is one story, and counting rows would call it six.
    srcs = defaultdict(set)
    for m in page('feed_mentions', 'entity,entity_type,source,item_title',
                  lambda x: x.eq('snapshot_date', session)):
        srcs[(m['entity'], m['entity_type'])].add(m['source'])

    tick = {e: s for (e, t), s in srcs.items() if t == 'ticker'}
    macro = sorted(((e, len(s)) for (e, t), s in srcs.items() if t == 'macro'),
                   key=lambda kv: -kv[1])

    # The board, for one line of context.
    funds = page('etf_snapshots', 'symbol,momentum_score,ret_20',
                 lambda x: x.eq('snapshot_date', session))
    fmap = {f['symbol']: f for f in funds}

    def fret(sym):
        f = fmap.get(sym) or {}
        return f.get('ret_20')

    loud = sorted(((t, len(s)) for t, s in tick.items() if len(s) >= LOUD),
                  key=lambda kv: -kv[1])
    loud_strong = [(t, n) for t, n in loud
                   if (snaps.get(t) or {}).get('momentum_score', 0) >= STRONG]
    loud_weak = [(t, n) for t, n in loud
                 if (snaps.get(t) or {}).get('momentum_score', 100) < WEAK]
    quiet = sorted((t for t, s in snaps.items()
                    if s.get('momentum_score', 0) >= 95 and t not in tick),
                   key=lambda t: -snaps[t]['momentum_score'])[:4]

    def nm(t):
        c = (names.get(t) or '').split(',')[0]
        return f'{t} ({c[:22]})' if c else t

    L = []
    L.append(f"*Morning note — session {session}*")
    L.append('')
    spy, qqq = fret('SPY'), fret('QQQ')
    if spy is not None:
        L.append(f"SPY {pct(spy)} and QQQ {pct(qqq)} over the last 20 sessions.")
    if macro:
        L.append('Press focus: '
                 + ', '.join(MACRO_LABEL.get(e, e) for e, _ in macro[:3]) + '.')
    L.append('')

    if loud_strong:
        L.append('*Loud and strong* — story and tape agree:')
        for t, n in loud_strong[:3]:
            L.append(f"  {nm(t)} · {n} sources · momentum {snaps[t]['momentum_score']}")
        L.append('')
    if loud_weak:
        L.append('*Loud but weak* — the story has outlived the trend:')
        for t, n in loud_weak[:3]:
            L.append(f"  {nm(t)} · {n} sources · momentum {snaps[t]['momentum_score']}")
        L.append('')
    if quiet:
        L.append('*Strong and unwritten* — top scores nobody covered:')
        for t in quiet:
            L.append(f"  {nm(t)} · momentum {snaps[t]['momentum_score']}"
                     f" · rank #{snaps[t].get('rs_rank')}")
        L.append('')

    covered = len(tick)
    L.append(f"_{covered} names in the press today; {len(loud)} in two or more "
             f"outlets. Scores are from the {session} close._")

    note = '\n'.join(L)
    words = len(note.split())
    print(note)
    print(f'\n[{words} words]', file=sys.stderr)

    if '--telegram' in sys.argv:
        tok, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
        if not tok or not chat:
            print('telegram secrets not set; not sending', file=sys.stderr)
            return
        import urllib.parse
        import urllib.request
        body = urllib.parse.urlencode({'chat_id': chat, 'parse_mode': 'Markdown',
                                       'text': note}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f'https://api.telegram.org/bot{tok}/sendMessage',
                                   data=body), timeout=20)
        print('sent to telegram', file=sys.stderr)


if __name__ == '__main__':
    main()
