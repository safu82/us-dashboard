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


CSS = """
:root{--bg:#0b0e14;--bg2:#141922;--bg3:#1b212c;--border:#2a323f;--text:#e8eef6;
--text2:#8e9bad;--green:#3fb950;--red:#f85149;--blue:#58a6ff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:15px/1.6 -apple-system,
BlinkMacSystemFont,'Segoe UI',Inter,sans-serif;padding-bottom:50px}
a{color:inherit;text-decoration:none}
.wrap{max-width:780px;margin:0 auto;padding:0 20px}
header{border-bottom:1px solid var(--border);
background:linear-gradient(180deg,#121823,var(--bg));padding:26px 0 20px;margin-bottom:24px}
h1{font-size:23px;font-weight:800;letter-spacing:-.5px}
.tape{color:var(--text2);font-size:13.5px;margin-top:9px;line-height:1.8}
.tape b{color:var(--text);font-weight:700}
.item{border-top:1px solid var(--border);padding:16px 0}
.item:first-of-type{border-top:none}
.hd{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:5px}
.n{color:var(--text2);font-size:13px;min-width:18px}
.sym{font-weight:800;font-size:16px}
.sym:hover{color:var(--blue)}
.co{color:var(--text2);font-size:12.5px}
.mv{font-weight:800;font-size:15px;margin-left:auto}
.src{font-size:11px;color:var(--text2);border:1px solid var(--border);
border-radius:20px;padding:2px 9px}
.hl{font-size:14.5px;line-height:1.55}
.hl a:hover{color:var(--blue)}
.alt{color:var(--text2);font-size:12.5px;margin-top:5px}
.sec{font-size:12px;letter-spacing:.5px;text-transform:uppercase;
color:var(--text2);margin:26px 0 4px;font-weight:700}
.minor{display:grid;grid-template-columns:72px 62px 1fr;gap:10px;
padding:7px 0;border-top:1px solid var(--border);font-size:13.5px;align-items:baseline}
.minor .mv2{text-align:right;font-weight:700;font-size:13px}
footer{color:var(--text2);font-size:11.5px;line-height:1.7;margin-top:28px;
border-top:1px solid var(--border);padding-top:16px}
@media(max-width:560px){.minor{grid-template-columns:62px 56px 1fr;gap:8px}
.hl{font-size:14px}}
"""


def write_html(session, prev, tape, shown, rest, stories, arts, dropped,
               label, pc):
    """Same digest, somewhere you can just look at it.

    Data is baked in: the private dashboard is a manual upload rather than a
    git deploy, so a page that fetched would need a Supabase key in its source.
    """
    import html as _h
    import webbrowser

    def esc(s):
        return _h.escape(str(s or ''))

    def col(v):
        return 'var(--text2)' if v is None else (
            'var(--green)' if v > 0 else 'var(--red)' if v < 0 else 'var(--text2)')

    def tv(t):
        return f'https://www.tradingview.com/chart/?symbol={esc(t)}'

    items = []
    for i, s in enumerate(shown, 1):
        sub, mv = s['subject'], s['move']
        lab = label(sub)
        tk = lab.split(' (')[0]
        co = lab.split(' (')[1].rstrip(')') if ' (' in lab else ''
        items.append(
            f'<div class="item"><div class="hd"><span class="n">{i}</span>'
            f'<a class="sym" href="{tv(tk)}" target="_blank" rel="noopener">{esc(tk)}</a>'
            f'<span class="co">{esc(co)}</span>'
            f'<span class="src">{s["n"]} sources</span>'
            f'<span class="mv" style="color:{col(mv)}">{pc(mv) or ""}</span></div>'
            f'<div class="hl"><a href="{esc(s["urls"][0])}" target="_blank" '
            f'rel="noopener">{esc(s["headline"])}</a></div>'
            + (f'<div class="alt">also: {esc(s["alt"][0][:120])}</div>' if s['alt'] else '')
            + '</div>')

    seen = {s['subject'] for s in shown}
    minor = []
    for s in sorted(rest, key=lambda x: -abs(x['move'] or 0)):
        if s['subject'] in seen:
            continue
        seen.add(s['subject'])
        lab = label(s['subject'])
        tk = lab.split(' (')[0]
        minor.append(
            f'<div class="minor"><a class="sym" style="font-size:13.5px" '
            f'href="{tv(tk)}" target="_blank" rel="noopener">{esc(tk)}</a>'
            f'<span class="mv2" style="color:{col(s["move"])}">{pc(s["move"]) or "–"}</span>'
            f'<a href="{esc(s["urls"][0])}" target="_blank" rel="noopener">'
            f'{esc(s["headline"][:120])}</a></div>')
        if len(minor) >= 12:
            break

    n_x = sum(1 for a in arts.values() if a['type'] == 'x')
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Market digest &mdash; {esc(session)}</title><style>{CSS}</style></head><body>
<header><div class="wrap"><h1>Market digest</h1>
<div class="tape">Session <b>{esc(session)}</b><br>{esc(' · '.join(tape))}</div>
</div></header><div class="wrap">
<div class="sec">Carried by more than one source</div>
{''.join(items) or '<div class="item" style="color:var(--text2)">Nothing today was carried by more than one source.</div>'}
<div class="sec">Also reported</div>
{''.join(minor)}
<footer>{len(stories)} stories from {len({a['source'] for a in arts.values()})} sources
({len(arts) - n_x} wire, {n_x} X). {dropped} evergreen pieces filtered out.
Moves are {esc(prev)} to {esc(session)} closes, from our own data.
Items are ranked by how many <b>distinct sources</b> carried them, never by article
count &mdash; one wire scoop becomes several stories within the hour.
Headlines link to their publisher; nothing is reproduced beyond the title.</footer>
</div></body></html>"""

    out = os.path.join(BASE, 'digest.html')
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write(body)
    print(f'wrote {out} ({os.path.getsize(out)/1024:.1f} KB)', file=sys.stderr)
    if '--open' in sys.argv:
        webbrowser.open('file:///' + out.replace('\\', '/'))


def write_portal(session, prev, tape, shown, rest, stories, arts, dropped,
                 names, macro_notes, reporting, closes):
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

    seen = {s['subject'] for s in shown}
    minor = []
    for s in sorted(rest, key=lambda x: -abs(x['move'] or 0)):
        if s['subject'] in seen:
            continue
        seen.add(s['subject'])
        minor.append(pack(s, with_alt=False))
        if len(minor) >= 14:
            break

    n_x = sum(1 for a in arts.values() if a['type'] == 'x')
    out = {
        'asOf': session, 'prev': prev,
        'builtAt': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'tape': tape,
        'top': [pack(s) for s in shown],
        'minor': minor,
        'macro': [{'text': __import__('re').sub(r'\\s+', ' ', n['text'])[:260],
                   'handle': n['handle'], 'url': n['url']}
                  for n in macro_notes[:6]],
        'reporting': [t for t in reporting if t in closes][:30],
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

    # Notes from the followed accounts. These are not stories about a company
    # and cannot be ranked by source count — a claim that buybacks hit a record
    # $1.3tn names nobody — so they sit in their own sections rather than being
    # forced through the counting and discarded.
    notes = page('x_notes', 'handle,url,posted_at,kind,text,tickers,item_no',
                 lambda x: x.eq('snapshot_date', session))
    macro_notes = sorted([n for n in notes if n['kind'] == 'macro'],
                         key=lambda n: n.get('posted_at') or '', reverse=True)
    # The calendar is the only forward-looking thing here, so it is read from a
    # window around the session rather than only the session itself: a Monday
    # post lists the whole week.
    cal = page('x_notes', 'handle,url,posted_at,text,tickers',
               lambda x: x.eq('kind', 'calendar').gte('snapshot_date', session))
    reporting = sorted({t for c in cal for t in (c.get('tickers') or [])})

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

    if macro_notes:
        out.append('*Macro:*')
        for n in macro_notes[:5]:
            txt = re.sub(r'\s+', ' ', n['text']).replace('BREAKING: ', '')
            out.append(f"  {txt[:150]}  _@{n['handle']}_")
        out.append('')

    if reporting:
        known = [t for t in reporting if t in closes]
        out.append(f"*Reporting ahead:* {', '.join(known[:18])}"
                   + (f" _+{len(known) - 18} more_" if len(known) > 18 else ''))
        out.append('')

    n_x = sum(1 for a in arts.values() if a['type'] == 'x')
    out.append(f"_{len(stories)} stories from {len({a['source'] for a in arts.values()})} "
               f"sources ({len(arts) - n_x} wire, {n_x} X); {dropped} evergreen "
               f"pieces filtered. Moves are {prev} to {session} closes._")

    note = '\n'.join(out)
    print(note)
    print(f'\n[{len(note.split())} words]', file=sys.stderr)

    if '--html' in sys.argv or '--open' in sys.argv:
        write_html(session, prev, tape, shown, rest, stories, arts, dropped,
                   label, pc)

    # Always write the portal payload. This is the version that matters: the
    # nightly chain commits portal/data and Netlify redeploys, so the digest
    # arrives on the site by itself. Nothing here has to be run by hand.
    write_portal(session, prev, tape, shown, rest, stories, arts, dropped,
                 names, macro_notes, reporting, closes)

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
