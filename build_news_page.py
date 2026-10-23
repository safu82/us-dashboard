#!/usr/bin/env python3
"""Render feed_mentions joined to the scores as a standalone page.

Data is baked into the file, so it opens by double-clicking and carries no
Supabase key. That matters twice over: the private dashboard is a manual upload
rather than a git deploy, and a page with a key in it is the thing we spent the
public portal's whole design avoiding.

The page shows one idea. Mentions alone are a popularity contest and momentum
alone is already on three other screens; it is where they DISAGREE that says
something:

    loud and strong        story and tape agree, the move is understood
    loud but weak          the story outlived the trend
    strong and unwritten   nobody has covered it yet

Counting is by distinct source throughout. One wire scoop becomes six stories
within the hour, so counting articles would measure syndication and call it
consensus.

Usage: python build_news_page.py [--open] [--date YYYY-MM-DD]
"""

import html
import os
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

import sb_retry  # noqa: F401

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))
OUT = os.path.join(BASE, 'news.html')

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')
sb = create_client(URL, KEY)

LOUD, STRONG, WEAK = 2, 80, 50
MACRO_LABEL = {'US10Y': '10-year yield', 'FED': 'the Fed', 'BRENT': 'oil',
               'GOLD': 'gold', 'CPI': 'inflation', 'JOBS': 'jobs',
               'DRAM': 'memory / HBM'}

CSS = """
:root{--bg:#0b0e14;--bg2:#141922;--bg3:#1b212c;--border:#2a323f;--text:#e8eef6;
--text2:#8e9bad;--green:#3fb950;--red:#f85149;--orange:#f0883e;--blue:#58a6ff;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,
BlinkMacSystemFont,'Segoe UI',Inter,sans-serif;padding:0 0 50px}
a{color:inherit;text-decoration:none}
.wrap{max-width:1100px;margin:0 auto;padding:0 20px}
header{border-bottom:1px solid var(--border);
background:linear-gradient(180deg,#121823,var(--bg));padding:22px 0 18px;margin-bottom:22px}
h1{font-size:21px;font-weight:800;letter-spacing:-.4px}
.sub{color:var(--text2);font-size:13px;margin-top:6px;max-width:760px}
.panel{background:var(--bg2);border:1px solid var(--border);border-radius:12px;
padding:16px 18px;margin-bottom:14px}
.ptitle{font-size:15px;font-weight:800;margin-bottom:2px}
.psub{font-size:12px;color:var(--text2);margin-bottom:12px;line-height:1.5}
.row{display:grid;grid-template-columns:86px 58px 64px 1fr;gap:10px;
padding:8px 0;border-top:1px solid var(--border);align-items:baseline}
.row:first-of-type{border-top:none}
.sym{font-weight:800;font-size:14px}
.sym:hover{color:var(--blue)}
.nm{color:var(--text2);font-size:11.5px}
.mom{font-weight:800;text-align:right}
.src{font-size:11.5px;color:var(--text2);text-align:right}
.heads{font-size:12px;color:var(--text2);line-height:1.6}
.heads a{border-bottom:1px dotted var(--border)}
.heads a:hover{color:var(--blue)}
.pill{display:inline-block;font-size:9.5px;background:#1d2531;color:var(--text2);
border-radius:5px;padding:1px 6px;margin-left:5px;vertical-align:1px}
.empty{color:var(--text2);font-size:12.5px;font-style:italic}
footer{color:var(--text2);font-size:11.5px;line-height:1.7;margin-top:20px;
border-top:1px solid var(--border);padding-top:16px}
@media(max-width:620px){.row{grid-template-columns:76px 46px 54px;}
.heads{grid-column:1/-1;padding-left:2px}}
"""


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


def esc(s):
    return html.escape(str(s or ''))


def main():
    want = sys.argv[sys.argv.index('--date') + 1] if '--date' in sys.argv else None

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

    mentions = page('feed_mentions',
                    'entity,entity_type,source,source_type,item_url,item_title,published_at',
                    lambda x: x.eq('snapshot_date', session))
    srcs, heads = defaultdict(set), defaultdict(list)
    for m in mentions:
        srcs[(m['entity'], m['entity_type'])].add(m['source'])
        heads[m['entity']].append(m)

    tick = {e: s for (e, t), s in srcs.items() if t == 'ticker'}
    macro = sorted(((e, len(s)) for (e, t), s in srcs.items() if t == 'macro'),
                   key=lambda kv: -kv[1])

    def mom(t):
        return (snaps.get(t) or {}).get('momentum_score')

    loud = sorted(((t, len(s)) for t, s in tick.items() if len(s) >= LOUD),
                  key=lambda kv: (-kv[1], -(mom(kv[0]) or 0)))
    loud_strong = [(t, n) for t, n in loud if (mom(t) or 0) >= STRONG]
    loud_weak = [(t, n) for t, n in loud if mom(t) is not None and mom(t) < WEAK]
    quiet = sorted((t for t, s in snaps.items()
                    if (s.get('momentum_score') or 0) >= 95 and t not in tick),
                   key=lambda t: -(mom(t) or 0))[:8]
    covered = sorted(((t, len(s)) for t, s in tick.items()),
                     key=lambda kv: (-kv[1], -(mom(kv[0]) or 0)))

    def links(t, limit=2):
        out, seen = [], set()
        for m in sorted(heads.get(t, []),
                        key=lambda x: x.get('published_at') or '', reverse=True):
            key = (m['item_title'] or '')[:60]
            if key in seen:
                continue
            seen.add(key)
            tag = m['source'].split(':', 1)[1]
            out.append(f'<a href="{esc(m["item_url"])}" target="_blank" '
                       f'rel="noopener">{esc((m["item_title"] or "")[:92])}</a>'
                       f'<span class="pill">{esc(tag)}</span>')
            if len(out) >= limit:
                break
        return ' · '.join(out) or '<span class="empty">no headline stored</span>'

    def rows(items, show_links=True):
        if not items:
            return '<div class="empty">Nothing in this bucket today.</div>'
        out = []
        for it in items:
            t, n = it if isinstance(it, tuple) else (it, 0)
            s = snaps.get(t) or {}
            m = s.get('momentum_score')
            col = ('var(--green)' if (m or 0) >= 70 else
                   'var(--red)' if (m or 0) < 45 else 'var(--text2)')
            nm = (names.get(t) or '').split(',')[0][:26]
            out.append(
                f'<div class="row"><div><a class="sym" target="_blank" rel="noopener" '
                f'href="https://www.tradingview.com/chart/?symbol={esc(t)}">{esc(t)}</a>'
                f'<div class="nm">{esc(nm)}</div></div>'
                f'<div class="mom" style="color:{col}">{"–" if m is None else m}</div>'
                f'<div class="src">{n} src</div>'
                f'<div class="heads">{links(t) if show_links else ""}</div></div>')
        return ''.join(out)

    built = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    n_x = sum(1 for m in mentions if m['source_type'] == 'x')
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>News vs tape — {esc(session)}</title><style>{CSS}</style></head><body>
<header><div class="wrap"><h1>&#128240; News vs tape</h1>
<div class="sub">What the market press covered on the <b>{esc(session)}</b> session,
set against what the scanner scored. Mentions are counted by <b>distinct source</b>,
never by article: one wire scoop becomes several stories within the hour, and
counting those would measure syndication rather than agreement.</div></div></header>
<div class="wrap">

<div class="panel"><div class="ptitle">&#128293; Loud and strong</div>
<div class="psub">Covered by {LOUD}+ sources and scoring {STRONG}+. Story and tape agree —
the move is understood, and you are not early.</div>{rows(loud_strong)}</div>

<div class="panel"><div class="ptitle">&#9888;&#65039; Loud but weak</div>
<div class="psub">Covered by {LOUD}+ sources while scoring under {WEAK}. The story has
outlived the trend: the press arrives, the move has gone.</div>{rows(loud_weak)}</div>

<div class="panel"><div class="ptitle">&#128161; Strong and unwritten</div>
<div class="psub">Scoring 95+ with no coverage at all today. Either genuinely early,
or there is nothing to write about — the chart is the only evidence either way.</div>
{rows(quiet, show_links=False)}</div>

<div class="panel"><div class="ptitle">Everything covered today</div>
<div class="psub">{len(covered)} names and {len(macro)} macro subjects appeared across
{len({m['source'] for m in mentions})} sources.
{'Macro focus: ' + ', '.join(MACRO_LABEL.get(e, e) for e, _ in macro[:4]) + '.' if macro else ''}</div>
{rows(covered)}</div>

<footer>Built {esc(built)} from {len(mentions)} stored mentions
({len(mentions) - n_x} RSS, {n_x} X). Scores are the {esc(session)} close.
Momentum is a percentile against every US-listed stock we cover, so it says where a
name stands among its peers and not whether the price rose — read it next to the
Return column on the main board.
Headlines link out to their publisher; nothing of theirs is reproduced here beyond
the title.</footer>
</div></body></html>"""

    with open(OUT, 'w', encoding='utf-8') as fh:
        fh.write(body)
    print(f'wrote {OUT}  ({os.path.getsize(OUT)/1024:.1f} KB)')
    print(f'  session {session}: {len(covered)} names covered, '
          f'{len(loud_strong)} loud+strong, {len(loud_weak)} loud+weak, '
          f'{len(quiet)} strong+unwritten')
    if '--open' in sys.argv:
        webbrowser.open('file://' + OUT.replace('\\', '/'))


if __name__ == '__main__':
    main()
