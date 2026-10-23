#!/usr/bin/env python3
"""The daily digest: what actually happened, ranked by how many sources said it.

Modelled on the numbered end-of-day recaps that do this well by hand. The whole
value is in the ordering — an item carried by four outlets mattered more than
one carried by a single feed, and that is a judgement the counting can make
without anyone writing an opinion.

Three things have to happen for the output to be readable:

  CLUSTER   The same event arrives several times. "Meta stock sinks nearly 7%
            as AI spend pushes free cash flow negative" ran verbatim on two
            CNBC feeds the same afternoon. Stories are grouped by their subject
            and then by title overlap, so that lands as one item carried twice
            rather than two items.

  FILTER    Syndicated evergreen opinion is not news. "Better Memory Stock:
            Micron vs. Sandisk", "3 Key Takeaways for Long-Term Investors" and
            "If I Could Only Buy 1 AI Stock" all appeared on the wire the same
            day as the actual earnings, and would otherwise outrank them.

  PRICE     A headline without the move is half the story. Every item carries
            what the stock actually did, from our own close-to-close data.

Usage: python build_digest.py [--date YYYY-MM-DD] [--telegram] [--max N]
"""

import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

import sb_retry  # noqa: F401
from feed_entities import build_patterns

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')
sb = create_client(URL, KEY)

# Headlines that are opinion, evergreen or listicle rather than an event. These
# are syndicated heavily and would otherwise dominate the ranking on volume.
EVERGREEN = re.compile(
    r"(\d+\s+(key\s+)?takeaways|should\s+(you|investors)|here'?s\s+why|"
    r"if\s+i\s+could|better\s+\w+\s+stock|could\s+be\s+the|is\s+\w+\s+next\?|"
    r"a\s+better\s+buy|why\s+i\s+still|best\s+\w+\s+(stock|buy)|prediction|"
    r"motley|earnings\s+call\s+transcript|stock-split\s+watch|"
    r"\bvs\.?\s|\bwhich\b.*\bbetter\b|long-term\s+investors|"
    r"\bmy\s+(top|favorite)\b|worth\s+buying|time\s+to\s+buy|"
    r"what\s+it\s+means\s+for|never\s+done\s+before|here'?s\s+what|"
    r"\bcould\s+(be|make|turn)\b|\bmillionaire\b|\bno-brainer\b|"
    r"\bdown\s+\d+%\s+from\b|\bstill\s+own\b|\bshould\s+you\b)", re.I)

MACRO_LABEL = {'US10Y': '10-year yield', 'FED': 'the Fed', 'BRENT': 'oil',
               'GOLD': 'gold', 'CPI': 'inflation', 'JOBS': 'jobs',
               'DRAM': 'memory / HBM'}

STOPWORDS = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to',
             'for', 'of', 'as', 'is', 'it', 'its', 'with', 'from', 'by',
             'after', 'amid', 'over', 'this', 'that', 'has', 'have', 'says',
             'said', 'new', 'more', 'than', 'what', 'how', 'why', 'be'}


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


def toks(title):
    return {w for w in re.findall(r'[a-z0-9]{3,}', (title or '').lower())
            if w not in STOPWORDS}


def similar(a, b):
    """Jaccard on title tokens. Two reports of one event share their nouns."""
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def main():
    want = sys.argv[sys.argv.index('--date') + 1] if '--date' in sys.argv else None
    top_n = int(sys.argv[sys.argv.index('--max') + 1]) if '--max' in sys.argv else 12

    q = (sb.table('daily_stock_snapshots').select('snapshot_date')
         .not_.is_('momentum_score', 'null'))
    if want:
        q = q.lte('snapshot_date', want)
    r = q.order('snapshot_date', desc=True).limit(1).execute().data
    if not r:
        sys.exit('no scored session')
    session = r[0]['snapshot_date']

    prev = (sb.table('daily_stock_snapshots').select('snapshot_date')
            .lt('snapshot_date', session).not_.is_('momentum_score', 'null')
            .order('snapshot_date', desc=True).limit(1).execute().data)
    prev = prev[0]['snapshot_date'] if prev else None

    closes = {s['ticker']: s.get('close') for s in page(
        'daily_stock_snapshots', 'ticker,close',
        lambda x: x.eq('snapshot_date', session))}
    pcloses = {s['ticker']: s.get('close') for s in page(
        'daily_stock_snapshots', 'ticker,close',
        lambda x: x.eq('snapshot_date', prev))} if prev else {}

    def move(tk):
        try:
            a, b = float(closes.get(tk)), float(pcloses.get(tk))
            return a / b - 1 if b else None
        except (TypeError, ValueError):
            return None

    rows = page('us_stock_sectors', 'ticker,company_name')
    names = {r['ticker']: r.get('company_name') for r in rows}
    _, patterns, _ = build_patterns(rows)

    mentions = page('feed_mentions',
                    'entity,entity_type,source,source_type,item_url,item_title',
                    lambda x: x.eq('snapshot_date', session))

    # Collapse to articles. One row per (source, url); the entities come along.
    arts = {}
    for m in mentions:
        k = (m['source'], m['item_url'])
        a = arts.setdefault(k, {'source': m['source'], 'url': m['item_url'],
                                'title': m['item_title'] or '',
                                'type': m['source_type'], 'ents': set()})
        a['ents'].add((m['entity'], m['entity_type']))

    kept, dropped = [], 0
    for a in arts.values():
        if EVERGREEN.search(a['title']):
            dropped += 1
            continue
        # The SUBJECT is whatever the title is about; entities found only in the
        # body are context. A story titled "Meta stock sinks" that mentions
        # Nvidia in passing is a Meta story.
        subj = [e for e, t in a['ents'] if t == 'ticker'
                and patterns.get(e) and (patterns[e][0].search(a['title'])
                                         or f'${e}' in a['title'])]
        if not subj:
            subj = [e for e, t in a['ents'] if t == 'ticker']
        if not subj:
            subj = [e for e, t in a['ents'] if t == 'macro']
        a['subject'] = sorted(subj)[0] if subj else None
        if a['subject']:
            kept.append(a)

    # Group by subject, then split a subject into separate events when the
    # titles have nothing in common — Meta's earnings and a Meta opinion piece
    # are both "META" but are not the same thing happening.
    stories = []
    for subj, group in defaultdict(list, {
            s: [a for a in kept if a['subject'] == s]
            for s in {a['subject'] for a in kept}}).items():
        clusters = []
        for a in sorted(group, key=lambda x: -len(x['title'])):
            for c in clusters:
                if similar(a['title'], c[0]['title']) >= 0.28:
                    c.append(a)
                    break
            else:
                clusters.append([a])
        for c in clusters:
            srcs = {a['source'] for a in c}
            stories.append({
                'subject': subj,
                'headline': max((a['title'] for a in c), key=len),
                'sources': srcs,
                'n': len(srcs),
                'urls': [a['url'] for a in c],
                'alt': [a['title'] for a in c
                        if a['title'] != max((x['title'] for x in c), key=len)],
            })

    for s in stories:
        s['move'] = move(s['subject'])
    stories.sort(key=lambda s: (-s['n'], -abs(s['move'] or 0)))

    # ── render ────────────────────────────────────────────────────────────────
    def pc(v, d=1):
        return '' if v is None else f'{v * 100:+.{d}f}%'

    def label(e):
        if e in MACRO_LABEL:
            return MACRO_LABEL[e]
        c = (names.get(e) or '').split(',')[0]
        return f'{e}' + (f' ({c[:24]})' if c else '')

    out = [f"*NASDAQ DIGEST · {session}*", '']
    # The tape line comes from etf_snapshots, not the stock table: SPY, the
    # semis and the sector funds live there, and only QQQ was ever pulled into
    # the stock universe as a benchmark.
    ecl = {e['symbol']: e.get('close') for e in page(
        'etf_snapshots', 'symbol,close', lambda x: x.eq('snapshot_date', session))}
    epv = {e['symbol']: e.get('close') for e in page(
        'etf_snapshots', 'symbol,close',
        lambda x: x.eq('snapshot_date', prev))} if prev else {}

    def emove(sym):
        try:
            a, b = float(ecl.get(sym)), float(epv.get(sym))
            return a / b - 1 if b else None
        except (TypeError, ValueError):
            return None

    tape = []
    for sym, nm in (('QQQ', 'Nasdaq 100'), ('SPY', 'S&P 500'),
                    ('IWM', 'Russell 2000'), ('SOXX', 'semis'),
                    ('XLE', 'energy'), ('GLD', 'gold')):
        m = emove(sym)
        if m is not None:
            tape.append(f'{nm} {pc(m, 2)}')
    if tape:
        out.append(' · '.join(tape))
        out.append('')

    shown = [s for s in stories if s['n'] >= 2][:top_n]
    rest = [s for s in stories if s['n'] < 2]
    if shown:
        for i, s in enumerate(shown, 1):
            mv = pc(s['move'])
            out.append(f"{i}. *{label(s['subject'])}*{'  ' + mv if mv else ''}"
                       f"  · {s['n']} sources")
            out.append(f"   {s['headline'][:150]}")
            if s['alt']:
                out.append(f"   _also:_ {s['alt'][0][:110]}")
            out.append('')
    else:
        out.append('_Nothing was carried by more than one source today._')
        out.append('')

    if rest:
        out.append('*Also reported:*')
        # One line per subject. Two different single-source stories about the
        # same company are still one company having a day, and listing it twice
        # reads like the digest is padding.
        seen_subj = {s['subject'] for s in shown}
        for s in sorted(rest, key=lambda x: -abs(x['move'] or 0)):
            if s['subject'] in seen_subj:
                continue
            seen_subj.add(s['subject'])
            mv = pc(s['move'])
            out.append(f"  {label(s['subject'])}{'  ' + mv if mv else ''} — "
                       f"{s['headline'][:88]}")
            if len(seen_subj) >= 10:
                break
        out.append('')

    n_x = sum(1 for a in arts.values() if a['type'] == 'x')
    out.append(f"_{len(stories)} stories from {len({a['source'] for a in arts.values()})} "
               f"sources ({len(arts) - n_x} wire, {n_x} X); {dropped} evergreen "
               f"pieces filtered. Moves are {prev} to {session} closes._")

    note = '\n'.join(out)
    print(note)
    print(f'\n[{len(note.split())} words]', file=sys.stderr)

    if '--telegram' in sys.argv:
        tok, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
        if not tok or not chat:
            print('telegram secrets not set', file=sys.stderr)
            return
        import urllib.parse
        import urllib.request
        body = urllib.parse.urlencode({'chat_id': chat, 'parse_mode': 'Markdown',
                                       'text': note[:4000]}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f'https://api.telegram.org/bot{tok}/sendMessage', data=body), timeout=20)
        print('sent to telegram', file=sys.stderr)


if __name__ == '__main__':
    main()
