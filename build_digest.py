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
    r"\bdown\s+\d+%\s+from\b|\bstill\s+own\b|\bshould\s+you\b|"
    r"\bi'?d\s+buy\b|no\s+matter\s+what|buy\s+and\s+hold|"
    r"\bthis\s+is\s+the\s+\d+\b|\bhold\s+forever\b|\bdividend\s+stock[s]?\s+to\b|"
    r"best-kept|little-known|\bnot\s+\w+\.\s*not\s+\w+\.|i'?d\s+consider|"
    r"\bsmall\s+position\b|\bsecret\b|\bhidden\s+gem\b|"
    r"is\s+the\s+[\w\s]{2,30}\s+a\s+buy|\bsurprising\b|\bdividend\s+king\b|"
    r"\bstock\s+a\s+buy\b|\bbuy\s+now\b|\bthreatening\b|"
    r"too\s+late\s+to\s+buy|here'?s\s+the\s+answer|\bgf\s+value\b|"
    r"\bprice\s+prediction\b|\bwhat\s+to\s+know\b|\bexplained\b)", re.I)

MACRO_LABEL = {'US10Y': '10-year yield', 'FED': 'the Fed', 'BRENT': 'oil',
               'GOLD': 'gold', 'CPI': 'inflation', 'JOBS': 'jobs',
               'DRAM': 'memory / HBM'}

STOPWORDS = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to',
             'for', 'of', 'as', 'is', 'it', 'its', 'with', 'from', 'by',
             'after', 'amid', 'over', 'this', 'that', 'has', 'have', 'says',
             'said', 'new', 'more', 'than', 'what', 'how', 'why', 'be'}


def write_portal(session, prev, tape, shown, movers, stories, arts, dropped,
                 names, ahead_days, ahead):
    """portal/data/digest.json — what the Sector Compass page reads.

    Only wire headlines and links go out. A headline plus attribution plus a
    link to the publisher is what every aggregator does; reproducing a
    commentator's own post would be republishing their work, so when X sources
    are added they raise the source COUNT and are named, but nothing of theirs
    is quoted here.
    """
    import json

    def co(tk):
        return (names.get(tk) or '').split(',')[0][:28]

    def pack(s, with_alt=True):
        d = {'subject': s['subject'], 'company': co(s['subject']),
             'move': s['move'], 'sources': s['n'],
             'names': sorted(x.split(':', 1)[1] for x in s['sources']),
             'headline': s['headline'][:200], 'url': s['urls'][0]}
        if with_alt and s['alt']:
            d['alt'] = s['alt'][0][:140]
        return d

    # Biggest movers, already computed and already excluding anything shown
    # above. The headline may be None — a double-digit move nobody wrote about
    # is the case this section exists to surface.
    minor = [{'subject': m['subject'], 'company': co(m['subject']),
              'move': m['move'], 'mom': m['mom'], 'sources': m['sources'],
              'headline': (m['headline'] or '')[:180] or None,
              'url': m['url']}
             for m in movers]

    n_x = sum(1 for a in arts.values() if a['type'] == 'x')
    out = {
        'asOf': session, 'prev': prev,
        'builtAt': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'tape': tape,
        'top': [pack(s) for s in shown],
        'minor': minor,
        # The week, not tomorrow. One session hid Alphabet and Microsoft on the
        # Wednesday and Amazon on the Thursday behind a Monday of mid-caps.
        'ahead': [{'date': d,
                   'names': [{'symbol': c['symbol'], 'mom': c['mom'],
                              'cap': c['cap'], 'when': c['when_of_day'],
                              'company': (c.get('company_name') or '')[:30],
                              'eps': c.get('eps_estimate')}
                             for c in ahead[d][:14]],
                   'more': max(0, len(ahead[d]) - 14)}
                  for d in ahead_days],
        'counts': {'stories': len(stories),
                   'sources': len({a['source'] for a in arts.values()}),
                   'wire': len(arts) - n_x, 'x': n_x, 'filtered': dropped},
    }
    d = os.path.join(BASE, 'portal', 'data')
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, 'digest.json')
    with open(p, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, separators=(',', ':'), ensure_ascii=False)

    # The page loads one bundle, so digest.json has to join it or the tab is
    # empty on a CDN that has cached the old data.js.
    js = os.path.join(d, 'data.js')
    if os.path.exists(js):
        raw = open(js, encoding='utf-8').read()
        if raw.startswith('window.PORTAL_DATA=') and raw.rstrip().endswith(';'):
            bundle = json.loads(raw[len('window.PORTAL_DATA='):].rstrip().rstrip(';'))
            bundle['digest'] = out
            with open(js, 'w', encoding='utf-8') as fh:
                fh.write('window.PORTAL_DATA=')
                json.dump(bundle, fh, separators=(',', ':'), ensure_ascii=False)
                fh.write(';')
            print(f'  portal/data/digest.json + data.js updated', file=sys.stderr)
        else:
            print('  WARNING: data.js not in the expected shape; digest not '
                  'merged into the bundle', file=sys.stderr)


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

    snap = {s['ticker']: s for s in page(
        'daily_stock_snapshots', 'ticker,close,momentum_score',
        lambda x: x.eq('snapshot_date', session))}
    closes = {t: s.get('close') for t, s in snap.items()}
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


    # Who reports, over the week rather than only tomorrow.
    #
    # Showing one session was wrong twice over. It hid the week's actual events
    # — Alphabet and Microsoft on the Wednesday, Amazon on the Thursday — behind
    # a Monday of mid-caps. And ranking within a day by momentum would have
    # buried them anyway: a mega-cap print moves the index whatever its
    # momentum score happens to be, so size is the right order for "what is
    # coming", and momentum is shown alongside rather than used to sort.
    caps = {}
    for f in page('stock_fundamentals', 'ticker,market_cap_usd'):
        try:
            caps[f['ticker']] = float(f['market_cap_usd'])
        except (TypeError, ValueError):
            pass
    cal = page('earnings_calendar',
               'symbol,report_date,when_of_day,company_name,eps_estimate',
               lambda x: x.gt('report_date', session).order('report_date'))
    upcoming = []
    for c in cal:
        upcoming.append({**c,
                         'mom': (snap.get(c['symbol']) or {}).get('momentum_score'),
                         'cap': caps.get(c['symbol'])})
    upcoming.sort(key=lambda c: (c['report_date'], -(c['cap'] or 0)))
    # The next five sessions, biggest first within each.
    ahead_days = sorted({c['report_date'] for c in upcoming})[:5]
    ahead = {d: [c for c in upcoming if c['report_date'] == d] for d in ahead_days}
    next_day = ahead_days[0] if ahead_days else None
    next_up = ahead.get(next_day, [])

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

    # One line per subject. The clustering above keeps genuinely separate
    # events apart, which is right for counting but wrong for reading: AT&T
    # falling on the SpaceX cell-site story came through as "Why AT&T Stock
    # Plunged on Friday" and "AT&T Slides on SpaceX Cell-Site Talks", and
    # printing both as items 3 and 7 reads like the digest is padding. Merged,
    # it is one company having one day, carried by five outlets rather than
    # three and two.
    merged = {}
    for s in stories:
        m = merged.get(s['subject'])
        if not m:
            merged[s['subject']] = s
            continue
        m['sources'] |= s['sources']
        m['urls'] += s['urls']
        # Keep the other angles as context, longest first.
        m['alt'] = sorted(set(m['alt'] + [s['headline']] + s['alt'])
                          - {m['headline']}, key=len, reverse=True)
    stories = list(merged.values())

    # Show a headline that is actually ABOUT the subject. Merging takes the
    # longest, which is usually right and occasionally badly wrong: a stray
    # match put a Spotify headline under Globe Life. Prefer one naming the
    # ticker or the company, and fall back to length only when none does.
    for s in stories:
        pat = patterns.get(s['subject'], (None, None))[0]
        cands = [s['headline']] + s['alt']
        named = [h for h in cands
                 if s['subject'] in h or (pat and pat.search(h))]
        if named and named[0] != s['headline']:
            best = max(named, key=len)
            s['alt'] = [h for h in cands if h != best]
            s['headline'] = best
    for s in stories:
        s['n'] = len(s['sources'])
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

    # Biggest movers, from our own closes rather than from whether a wire bot
    # wrote something.
    #
    # The section this replaces listed single-source stories, which sounded
    # reasonable and sourced the day backwards: it showed companies a bot had
    # written about instead of companies that had moved. On 2026-10-23 it ran
    # GNTX at -5% because GuruFocus auto-generated "Shares Fall 5.0% -- What GF
    # Score Tells Investors", and said nothing at all about UWMC, which moved
    # +26.1%, or LKQ at -22.5%, because no feed we read mentioned either.
    #
    # The move is the fact and always exists. A headline is colour, attached
    # when we happen to have one. "No coverage" against a 26% move is itself
    # worth reading.
    funds_syms = {e['symbol'] for e in page('etfs', 'symbol')}
    by_subject = {}
    for s in stories:
        prev_s = by_subject.get(s['subject'])
        if not prev_s or s['n'] > prev_s['n']:
            by_subject[s['subject']] = s
    shown_subjects = {s['subject'] for s in shown}
    movers = []
    for tk in snap:
        if tk in funds_syms or tk in shown_subjects:
            continue
        m = move(tk)
        if m is None or abs(m) < 0.03:
            continue
        st = by_subject.get(tk)
        movers.append({
            'subject': tk, 'move': m,
            'mom': (snap.get(tk) or {}).get('momentum_score'),
            'sources': st['n'] if st else 0,
            'headline': st['headline'] if st else None,
            'url': st['urls'][0] if st else None,
        })
    movers.sort(key=lambda x: -abs(x['move']))
    movers = movers[:14]
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

    if movers:
        out.append('*Biggest movers:*')
        for m in movers[:10]:
            tail = (f" — {m['headline'][:72]}" if m['headline']
                    else '  _no coverage_')
            out.append(f"  {m['subject']:6} {pc(m['move']):>7}{tail}")
        out.append('')

    if ahead_days:
        out.append('*Reporting ahead* — largest first:')
        for d in ahead_days:
            day = ahead[d]
            # Not `names`: that is the ticker -> company map in this scope, and
            # shadowing it broke the portal writer further down.
            line = []
            for c in day[:6]:
                cap = c['cap']
                size = (f'{cap/1e12:.1f}tn' if cap and cap >= 1e12 else
                        f'{cap/1e9:.0f}bn' if cap else '')
                line.append(f"{c['symbol']}{f' {size}' if size else ''}")
            out.append(f"  {d}  {', '.join(line)}"
                       + (f"  _+{len(day) - 6}_" if len(day) > 6 else ''))
        out.append('')

    n_x = sum(1 for a in arts.values() if a['type'] == 'x')
    out.append(f"_{len(stories)} stories from {len({a['source'] for a in arts.values()})} "
               f"sources ({len(arts) - n_x} wire, {n_x} X); {dropped} evergreen "
               f"pieces filtered. Moves are {prev} to {session} closes._")

    note = '\n'.join(out)
    print(note)
    print(f'\n[{len(note.split())} words]', file=sys.stderr)


    # Always write the portal payload. This is the version that matters: the
    # nightly chain commits portal/data and Netlify redeploys, so the digest
    # arrives on the site by itself. Nothing here has to be run by hand.
    write_portal(session, prev, tape, shown, movers, stories, arts, dropped,
                 names, ahead_days, ahead)

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
