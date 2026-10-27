"""portal/ static pages — the part of Overnight Arc a search engine can read.

The dashboard is one URL that renders four boards from a JSON bundle in the
browser. That is the right shape for someone reading it and the wrong shape for
being found. There is exactly one page to index, it is rewritten every night,
and nothing accumulates — you cannot link anyone to last Tuesday, because last
Tuesday no longer exists.

This writes the same data out as ordinary pages that sit on disk: one per
theme, one per fund, one per country, and one per session that is never
rewritten again. They are plain HTML with the content already in it, so they
work with scripting off and read correctly to a crawler.

Two deliberate omissions.

No page per stock. There are 1,008 of them and a page carrying one momentum
score is thin content, which at that volume is a liability rather than an
asset — it is the pattern search engines demote sites for. The companies appear
on the theme and fund pages instead, where they sit in context.

No backfilled digests. Only the current session exists in the data; there is no
history to reconstruct. The archive therefore starts the day this first runs
and grows by one page a night. That is slower than it sounds: a year of
weeknights is about 250 pages, each one dated, specific and permanent.

Nothing here deletes. A dated digest directory, once written, is left alone on
every later run.
"""
import html
import io
import json
import os
import re
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'portal', 'data')
OUT = os.path.join(BASE, 'portal')
SITE = 'https://overnightarc.com'

written = []


# ---------------------------------------------------------------- formatting

def esc(s):
    return html.escape(str(s if s is not None else ''), quote=True)


def pct(x, dp=1, sign=True):
    """A fraction as a percentage. None reads as an em dash, not as zero."""
    if x is None:
        return '—'
    v = x * 100
    s = f'{v:+.{dp}f}%' if sign else f'{v:.{dp}f}%'
    return s


def num(x):
    return '—' if x is None else f'{x:g}'


def cap(x):
    """Market cap in the units people say out loud."""
    if not x:
        return '—'
    if x >= 1e12:
        return f'${x / 1e12:.2f}T'
    if x >= 1e9:
        return f'${x / 1e9:.1f}B'
    return f'${x / 1e6:.0f}M'


def tone(x):
    """Class name for a signed number, so colour follows the data."""
    if x is None:
        return ''
    return ' up' if x > 0 else (' dn' if x < 0 else '')


def slugify(s):
    return re.sub(r'[^a-z0-9]+', '-', str(s).lower()).strip('-')


# ---------------------------------------------------------------- page shell

CSS = """\
:root{--bg:#0b0e14;--bg2:#141922;--bg3:#1b212c;--border:#2a323f;--text:#e8eef6;
--text2:#8e9bad;--green:#3fb950;--red:#f85149;--blue:#58a6ff;--accent:#2563eb;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);line-height:1.6;
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
-webkit-font-smoothing:antialiased;}
.wrap{max-width:860px;margin:0 auto;padding-inline:18px;padding-block:0 64px;}
a{color:var(--blue);text-decoration:none;}
a:hover{text-decoration:underline;}
a:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
header{border-bottom:1px solid var(--border);background:var(--bg2);}
header .wrap{padding-block:14px;}
.brand{display:flex;align-items:center;gap:9px;font-weight:800;font-size:18px;
letter-spacing:-.4px;color:var(--text);}
.crumb{font-size:12.5px;color:var(--text2);margin-top:6px;}
.crumb a{color:var(--text2);}
h1{font-size:clamp(23px,4.4vw,31px);line-height:1.2;letter-spacing:-.4px;
margin:30px 0 10px;text-wrap:balance;}
h2{font-size:18px;letter-spacing:-.2px;margin:30px 0 10px;}
h3{font-size:15px;margin:0 0 4px;}
p{margin:10px 0;max-width:68ch;}
.lede{color:var(--text2);font-size:16px;}
.meta{color:var(--text2);font-size:12.5px;margin-top:4px;}
/* Always exactly eight stats, so four columns fill two rows with no dead cell.
   auto-fit chose five at this width and left a gap that read as a broken tile. */
.grid{display:grid;grid-template-columns:repeat(4,1fr);
gap:1px;background:var(--border);border:1px solid var(--border);
border-radius:9px;overflow:hidden;margin:18px 0;}
@media (max-width:620px){.grid{grid-template-columns:repeat(2,1fr);}}
.cell{background:var(--bg2);padding:11px 13px;}
.cell .k{font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--text2);}
.cell .v{font-size:18px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums;}
.up{color:var(--green);}
.dn{color:var(--red);}
.scroll{overflow-x:auto;border:1px solid var(--border);border-radius:9px;margin:14px 0;}
table{border-collapse:collapse;width:100%;font-size:13.5px;}
th{text-align:left;font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;
color:var(--text2);font-weight:600;padding:9px 12px;background:var(--bg3);
border-bottom:1px solid var(--border);white-space:nowrap;}
td{padding:9px 12px;border-bottom:1px solid var(--border);background:var(--bg2);
vertical-align:top;font-variant-numeric:tabular-nums;}
tr:last-child td{border-bottom:none;}
td.t{font-weight:700;white-space:nowrap;}
td.w{min-width:190px;font-variant-numeric:normal;}
.link{border:1px solid var(--border);border-radius:9px;background:var(--bg2);
padding:15px 17px;margin:14px 0;}
.link .pos{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--text2);}
.link .what{color:var(--text2);font-size:13.5px;margin:6px 0 2px;max-width:68ch;}
ul.plain{list-style:none;display:flex;flex-wrap:wrap;gap:7px;margin:12px 0;}
ul.plain li a{display:inline-block;border:1px solid var(--border);background:var(--bg2);
border-radius:7px;padding:5px 10px;font-size:13px;}
.story{border-bottom:1px solid var(--border);padding:13px 0;}
.story:last-child{border-bottom:none;}
.story .sub{font-weight:700;font-size:13px;}
.story .src{font-size:11px;color:var(--text2);border:1px solid var(--border);
border-radius:5px;padding:1px 6px;margin-left:7px;}
.story .hl{display:block;margin-top:3px;}
.tape{background:var(--bg3);border:1px solid var(--border);border-radius:9px;
padding:11px 14px;font-size:13px;color:var(--text2);margin:16px 0;
font-variant-numeric:tabular-nums;}
footer{border-top:1px solid var(--border);margin-top:44px;padding-top:20px;
font-size:12.5px;color:var(--text2);}
footer p{max-width:70ch;}
@media (max-width:520px){td.w{min-width:0;}}
"""


def shell(path, title, desc, crumbs, body):
    """One page. `path` is the published directory, '' for the site root."""
    canon = f'{SITE}/{path}/' if path else f'{SITE}/'
    trail = ' › '.join(
        f'<a href="{esc(h)}">{esc(t)}</a>' if h else esc(t) for t, h in crumbs)
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{esc(canon)}">
<link rel="stylesheet" href="{'../' * (path.count('/') + 1) if path else ''}a.css">
</head>
<body>
<header><div class="wrap">
  <div class="brand"><span>&#127762;</span><span>Overnight Arc</span></div>
  <div class="crumb">{trail}</div>
</div></header>
<div class="wrap">
{body}
<footer>
<p><b>How to read the numbers.</b> Momentum is a 0&#8211;100 score: each name ranked
against its own peer group on relative-strength level and trend, 9- and 20-day EMA
slopes, and price against its 50-day average. 50 is the median, not a neutral
return &#8212; in a falling market a high score means falling less than the rest.
Funds are scored against other funds and stocks against other stocks, so the two
are not comparable with each other. RS rank runs the other way: #1 is the
strongest stock we cover.</p>
<p>Rebuilt each weeknight after the US close. Delayed, not live. Value chains are
our own mapping and will not match any index or fund's holdings.
<b>This is educational research, not investment advice, and not personalised to
anyone.</b> Do your own work.</p>
<p><b>Elsewhere:</b> <a href="{SITE}/">the four live boards</a> &#183;
<a href="{SITE}/digest/">every session</a> &#183;
<a href="{SITE}/themes/">themes</a> &#183;
<a href="{SITE}/funds/">funds</a> &#183;
<a href="{SITE}/countries/">countries</a></p>
<p><b>Overnight Arc</b> is published by <b>Insight Arc Solutions</b>, Dubai, United
Arab Emirates. Corrections: <a href="mailto:contact@overnightarc.com">contact@overnightarc.com</a>.</p>
</footer>
</div>
</body>
</html>
"""
    d = os.path.join(OUT, *path.split('/')) if path else OUT
    os.makedirs(d, exist_ok=True)
    with io.open(os.path.join(d, 'index.html'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(doc)
    written.append(path)
    return path


def cells(pairs):
    out = ['<div class="grid">']
    for k, v, cls in pairs:
        out.append(f'<div class="cell"><div class="k">{esc(k)}</div>'
                   f'<div class="v{cls}">{v}</div></div>')
    out.append('</div>')
    return '\n'.join(out)


# ---------------------------------------------------------------- the boards

def fund_page(f, kind, asof, ret_dates, bench, stocks):
    """One fund or one country ETF."""
    sym, name = f['symbol'], f['name']
    root = 'funds' if kind == 'fund' else 'countries'
    label = 'fund' if kind == 'fund' else 'country'
    d20 = (ret_dates or {}).get('d20', '')
    d60 = (ret_dates or {}).get('d60', '')

    lede = (f"{name} ({sym}) ranks {f['rank']} on the "
            f"{'sector and theme' if kind == 'fund' else 'country'} board with a "
            f"momentum score of {f['mom']} out of 100, as of {asof}.")

    body = [f'<h1>{esc(sym)} &#183; {esc(name)}</h1>',
            f'<p class="lede">{esc(f.get("group") or "")}'
            f'{" &#183; " + esc(f["sector"]) if f.get("sector") else ""}</p>',
            f'<p class="meta">Closes to {esc(asof)}. Rank {f["rank"]} of its board.</p>',
            cells([
                ('Momentum', num(f['mom']), ''),
                ('Return 20d', pct(f.get('ret20')), tone(f.get('ret20'))),
                ('Return 60d', pct(f.get('ret60')), tone(f.get('ret60'))),
                ('vs SPY 20d', pct(f.get('vsSpy20')), tone(f.get('vsSpy20'))),
                ('vs QQQ 20d', pct(f.get('vsQqq20')), tone(f.get('vsQqq20'))),
                ('From high', pct(f.get('fromHigh')), tone(f.get('fromHigh'))),
                ('Durability', pct(f.get('durability'), 0, False), ''),
                ('8/21 trend', 'yes' if f.get('trend821') else 'no', ''),
            ])]

    spy, qqq = (bench or {}).get('SPY'), (bench or {}).get('QQQ')
    if spy and qqq:
        body.append(
            f'<p>Over the same 20 sessions &#8212; today&#8217;s close against '
            f'{esc(d20)} &#8212; SPY returned {pct(spy.get("ret20"))} and QQQ '
            f'{pct(qqq.get("ret20"))}. The 60-session figure compares with '
            f'{esc(d60)}. Each is a close against a single earlier day, so a window '
            f'starting on a spike or a trough can read very differently from the '
            f'trend; <i>from high</i> is the check when a green return sits beside a '
            f'deep drawdown.</p>')

    h = f.get('holdings') or {}
    if h:
        rows = []
        for tk, w in sorted(h.items(), key=lambda kv: kv[1], reverse=True)[:25]:
            s = stocks.get(tk) or {}
            rows.append(
                f'<tr><td class="t">{esc(tk)}</td>'
                f'<td class="w">{esc(s.get("company") or "")}</td>'
                f'<td>{pct(w, 2, False)}</td>'
                f'<td>{num(s.get("mom"))}</td>'
                f'<td>{num(s.get("rsRank"))}</td>'
                f'<td class="{tone(s.get("ret20")).strip()}">{pct(s.get("ret20"))}</td></tr>')
        complete = f.get('holdingsComplete')
        body.append(f'<h2>What is inside it</h2>')
        body.append(
            f'<p>{"The published holdings" if complete else "The published holdings we have"}, '
            f'largest weight first{"" if complete else " (partial)"}. '
            f'{len(h)} positions in all; the {min(25, len(h))} biggest are shown.</p>')
        body.append('<div class="scroll"><table><thead><tr>'
                    '<th>Ticker</th><th>Company</th><th>Weight</th><th>Mom</th>'
                    '<th>RS rank</th><th>20d</th></tr></thead><tbody>'
                    + ''.join(rows) + '</tbody></table></div>')

    body.append(f'<p><a href="{SITE}/{root}/">All {label} pages</a> '
                f'&#183; <a href="{SITE}/">The live board</a></p>')

    return shell(f'{root}/{sym}',
                 f'{sym} — {name} | Overnight Arc',
                 lede,
                 [('Overnight Arc', f'{SITE}/'),
                  ('Funds' if kind == 'fund' else 'Countries', f'{SITE}/{root}/'),
                  (sym, None)],
                 '\n'.join(body))


def theme_page(ch, asof):
    slug, name = ch['slug'], ch['name']
    body = [f'<h1>{esc(name)}</h1>',
            f'<p class="lede">{esc(ch.get("description") or "")}</p>',
            f'<p class="meta">{esc(ch.get("category") or "")} &#183; '
            f'{len(ch["links"])} stages &#183; closes to {esc(asof)}</p>',
            '<p>A value chain is the order in which value is actually added, from '
            'raw inputs through to whoever sells the finished thing. Reading a theme '
            'this way shows where in it the money is moving, which is rarely evenly '
            'spread. Each stage carries its own momentum score and the companies we '
            'map to it. The mapping is ours and deliberately hand-made; it will not '
            'match any index.</p>']

    for L in ch['links']:
        rows = []
        for m in L.get('members') or []:
            rows.append(
                f'<tr><td class="t">{esc(m["ticker"])}</td>'
                f'<td class="w">{esc(m.get("company") or "")}</td>'
                f'<td class="w">{esc(m.get("why") or "")}</td>'
                f'<td>{num(m.get("mom"))}</td>'
                f'<td>{num(m.get("rsRank"))}</td></tr>')
        thin = ' &#183; too few scored names to read the stage' if L.get('thin') else ''
        body.append(
            f'<div class="link"><div class="pos">Stage {L["position"]} of '
            f'{len(ch["links"])}</div><h3>{esc(L["name"])}</h3>'
            f'<div class="what">{esc(L.get("what") or "")}</div>'
            f'<div class="meta">Stage momentum {num(L.get("mom"))} &#183; '
            f'{L.get("scored", 0)} names scored{thin}</div>'
            + (f'<div class="scroll"><table><thead><tr><th>Ticker</th><th>Company</th>'
               f'<th>Why it is here</th><th>Mom</th><th>RS rank</th></tr></thead>'
               f'<tbody>{"".join(rows)}</tbody></table></div>' if rows else '')
            + '</div>')

    body.append(f'<p><a href="{SITE}/themes/">All eighteen themes</a> '
                f'&#183; <a href="{SITE}/">The live board</a></p>')

    return shell(f'themes/{slug}',
                 f'{name} value chain — every stage and the companies in it | Overnight Arc',
                 (ch.get('description') or name)[:300],
                 [('Overnight Arc', f'{SITE}/'), ('Themes', f'{SITE}/themes/'), (name, None)],
                 '\n'.join(body))


def digest_page(d):
    asof = d['asOf']
    tape = ' &#183; '.join(esc(x) for x in (d.get('tape') or []))
    body = [f'<h1>US market, {esc(asof)}</h1>',
            f'<p class="lede">What happened on the {esc(asof)} session, ordered by how '
            f'many separate outlets carried each item. The more newsrooms ran a story '
            f'independently, the more it mattered.</p>']
    if tape:
        body.append(f'<div class="tape">{tape}</div>')

    top = d.get('top') or []
    if top:
        body.append('<h2>Carried by more than one source</h2>')
        for i, s in enumerate(top, 1):
            mv = (f'<span class="{tone(s.get("move")).strip()}">{pct(s.get("move"))}</span>'
                  if s.get('move') is not None else '')
            hl = esc(s.get('headline') or '')
            link = (f'<a href="{esc(s["url"])}" rel="nofollow noopener">{hl}</a>'
                    if s.get('url') else hl)
            alt = f'<div class="meta">also: {esc(s["alt"])}</div>' if s.get('alt') else ''
            body.append(
                f'<div class="story"><span class="sub">{i}. {esc(s["subject"])}</span>'
                f'{" " + esc(s["company"]) if s.get("company") else ""}'
                f'<span class="src">{s.get("sources", 0)} sources</span> {mv}'
                f'<span class="hl">{link}</span>{alt}</div>')

    minor = d.get('minor') or []
    if minor:
        body.append('<h2>Biggest movers</h2>')
        body.append('<p>Taken from our own closes rather than from coverage, so the move '
                    'is the fact and a headline is colour attached where one exists. '
                    '&#8220;No coverage&#8221; against a double-digit move is itself '
                    'worth reading.</p>')
        rows = []
        for m in minor:
            hl = esc(m.get('headline') or 'no coverage')
            link = (f'<a href="{esc(m["url"])}" rel="nofollow noopener">{hl}</a>'
                    if m.get('url') else f'<span class="meta">{hl}</span>')
            rows.append(f'<tr><td class="t">{esc(m["subject"])}</td>'
                        f'<td class="w">{esc(m.get("company") or "")}</td>'
                        f'<td class="{tone(m.get("move")).strip()}">{pct(m.get("move"))}</td>'
                        f'<td>{num(m.get("mom"))}</td><td class="w">{link}</td></tr>')
        body.append('<div class="scroll"><table><thead><tr><th>Ticker</th><th>Company</th>'
                    '<th>Move</th><th>Mom</th><th>Coverage</th></tr></thead><tbody>'
                    + ''.join(rows) + '</tbody></table></div>')

    ahead = d.get('ahead') or []
    if ahead:
        body.append('<h2>Reporting ahead</h2>')
        rows = []
        for day in ahead:
            for n in day.get('names') or []:
                rows.append(
                    f'<tr><td>{esc(day["date"])}</td><td class="t">{esc(n["symbol"])}</td>'
                    f'<td class="w">{esc(n.get("company") or "")}</td>'
                    f'<td>{esc(n.get("when") or "")}</td><td>{cap(n.get("cap"))}</td>'
                    f'<td>{num(n.get("eps"))}</td></tr>')
        body.append('<div class="scroll"><table><thead><tr><th>Date</th><th>Ticker</th>'
                    '<th>Company</th><th>When</th><th>Market cap</th><th>EPS est</th>'
                    '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')

    c = d.get('counts') or {}
    if c:
        body.append(
            f'<p class="meta">Built from {c.get("wire", 0)} wire items across '
            f'{c.get("sources", 0)} sources, clustered into {c.get("stories", 0)} '
            f'stories; {c.get("filtered", 0)} evergreen listicles discarded.</p>')

    body.append(f'<p><a href="{SITE}/digest/">Every session</a> '
                f'&#183; <a href="{SITE}/">The live board</a></p>')

    return shell(f'digest/{asof}',
                 f'US market recap, {asof} — what moved and who reported it | Overnight Arc',
                 f'What happened on the US market on {asof}: the stories carried by the '
                 f'most independent outlets, the biggest movers from our own closes, and '
                 f'who reports next.',
                 [('Overnight Arc', f'{SITE}/'), ('Digest', f'{SITE}/digest/'), (asof, None)],
                 '\n'.join(body))


# ---------------------------------------------------------------- the indexes

def board_index(path, title, h1, lede, rows, asof):
    body = [f'<h1>{esc(h1)}</h1>', f'<p class="lede">{esc(lede)}</p>',
            f'<p class="meta">Closes to {esc(asof)}.</p>',
            '<div class="scroll"><table><thead><tr><th>#</th><th>Ticker</th>'
            '<th>Name</th><th>Group</th><th>Mom</th><th>20d</th><th>60d</th>'
            '</tr></thead><tbody>']
    for r in rows:
        body.append(
            f'<tr><td>{r["rank"]}</td>'
            f'<td class="t"><a href="{SITE}/{path}/{esc(r["symbol"])}/">{esc(r["symbol"])}</a></td>'
            f'<td class="w">{esc(r["name"])}</td><td class="w">{esc(r.get("group") or "")}</td>'
            f'<td>{num(r["mom"])}</td>'
            f'<td class="{tone(r.get("ret20")).strip()}">{pct(r.get("ret20"))}</td>'
            f'<td class="{tone(r.get("ret60")).strip()}">{pct(r.get("ret60"))}</td></tr>')
    body.append('</tbody></table></div>')
    return shell(path, title, lede,
                 [('Overnight Arc', f'{SITE}/'), (h1, None)], '\n'.join(body))


def themes_index(chains, asof):
    body = [f'<h1>Eighteen themes, mapped stage by stage</h1>',
            '<p class="lede">Each theme broken into the order value is actually added, '
            'with the companies at every stage. Our own mapping, made by hand, so it '
            'matches no index and no fund&#8217;s holdings.</p>',
            f'<p class="meta">Closes to {esc(asof)}.</p>',
            '<div class="scroll"><table><thead><tr><th>Theme</th><th>Category</th>'
            '<th>Stages</th><th>Companies</th></tr></thead><tbody>']
    for slug in sorted(chains, key=lambda s: chains[s]['name']):
        ch = chains[slug]
        n = sum(len(L.get('members') or []) for L in ch['links'])
        body.append(
            f'<tr><td class="w"><a href="{SITE}/themes/{esc(slug)}/">{esc(ch["name"])}</a></td>'
            f'<td class="w">{esc(ch.get("category") or "")}</td>'
            f'<td>{len(ch["links"])}</td><td>{n}</td></tr>')
    body.append('</tbody></table></div>')
    return shell('themes', 'Investment themes and their value chains | Overnight Arc',
                 'Eighteen investment themes broken into their value chains — every '
                 'stage from raw inputs to finished product, and the US-listed companies '
                 'at each one.',
                 [('Overnight Arc', f'{SITE}/'), ('Themes', None)], '\n'.join(body))


def digest_index():
    """Lists whatever dated digests exist on disk, newest first."""
    root = os.path.join(OUT, 'digest')
    dates = sorted(
        (d for d in (os.listdir(root) if os.path.isdir(root) else [])
         if re.fullmatch(r'\d{4}-\d{2}-\d{2}', d)
         and os.path.isfile(os.path.join(root, d, 'index.html'))),
        reverse=True)
    body = ['<h1>Every session, kept</h1>',
            '<p class="lede">One page per US trading session: the stories the most '
            'independent outlets carried, the biggest movers taken from our own closes, '
            'and who was due to report next. Each is written the night it happens and '
            'never rewritten.</p>',
            f'<p class="meta">{len(dates)} '
            f'{"session" if len(dates) == 1 else "sessions"} so far.</p>',
            '<ul class="plain">']
    for d in dates:
        body.append(f'<li><a href="{SITE}/digest/{d}/">{d}</a></li>')
    body.append('</ul>')
    return shell('digest', 'US market recaps, session by session | Overnight Arc',
                 'An archive of US market sessions — what moved each night and how many '
                 'independent outlets carried it.',
                 [('Overnight Arc', f'{SITE}/'), ('Digest', None)], '\n'.join(body))


# ---------------------------------------------------------------- sitemap

def sitemap():
    """Every index.html under portal/, from disk — so the archive accumulates."""
    urls = []
    for dirpath, dirnames, filenames in os.walk(OUT):
        dirnames[:] = [d for d in dirnames if d != 'data']
        if 'index.html' not in filenames:
            continue
        rel = os.path.relpath(dirpath, OUT).replace(os.sep, '/')
        loc = f'{SITE}/' if rel == '.' else f'{SITE}/{rel}/'
        mt = datetime.fromtimestamp(
            os.path.getmtime(os.path.join(dirpath, 'index.html')), timezone.utc)
        urls.append((loc, mt.strftime('%Y-%m-%d')))
    urls.sort()
    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lm in urls:
        xml.append(f'  <url><loc>{esc(loc)}</loc><lastmod>{lm}</lastmod></url>')
    xml.append('</urlset>')
    with io.open(os.path.join(OUT, 'sitemap.xml'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(xml) + '\n')
    with io.open(os.path.join(OUT, 'robots.txt'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(f'User-agent: *\nAllow: /\n\nSitemap: {SITE}/sitemap.xml\n')
    return len(urls)


# ---------------------------------------------------------------- main

def load(name):
    p = os.path.join(DATA, name)
    if not os.path.isfile(p):
        return None
    with io.open(p, encoding='utf-8') as fh:
        return json.load(fh)


def main():
    print('=' * 64)
    print('BUILD ARCHIVE  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 64)

    etfs = load('etfs.json') or {}
    countries = load('countries.json') or {}
    chains = load('chains.json') or {}
    stocks = (load('stocks.json') or {}).get('stocks') or {}
    digest = load('digest.json')

    with io.open(os.path.join(OUT, 'a.css'), 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(CSS)

    funds = etfs.get('funds') or []
    if funds:
        for f in funds:
            fund_page(f, 'fund', etfs.get('asOf', ''), etfs.get('retDates'),
                      etfs.get('benchmarks'), stocks)
        board_index('funds', 'Sector and thematic ETFs, ranked | Overnight Arc',
                    'Funds',
                    'Every US sector and thematic ETF we cover, scored against the '
                    'others rather than against the market, so a diversified fund is '
                    'measured against its peers.',
                    sorted(funds, key=lambda x: x['rank']), etfs.get('asOf', ''))
        print(f'  funds            {len(funds):>4}')

    cs = countries.get('countries') or []
    if cs:
        for f in cs:
            fund_page(f, 'country', countries.get('asOf', ''), countries.get('retDates'),
                      countries.get('benchmarks'), stocks)
        board_index('countries', 'Country ETFs, ranked | Overnight Arc',
                    'Countries',
                    'Single-country ETFs scored against each other, which is how you '
                    'see money rotating between markets rather than between sectors.',
                    sorted(cs, key=lambda x: x['rank']), countries.get('asOf', ''))
        print(f'  countries        {len(cs):>4}')

    chs = chains.get('chains') or {}
    if chs:
        for slug in chs:
            theme_page(chs[slug], chains.get('asOf', ''))
        themes_index(chs, chains.get('asOf', ''))
        print(f'  themes           {len(chs):>4}')

    if digest and digest.get('asOf'):
        digest_page(digest)
        print(f'  digest           {digest["asOf"]}')
    digest_index()

    n = sitemap()
    print(f'  sitemap.xml      {n:>4} urls')
    print(f'  pages written    {len(written):>4} this run')


if __name__ == '__main__':
    main()
