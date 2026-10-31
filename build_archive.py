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
.brand .logo{width:28px;height:28px;flex:0 0 auto;}
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
/* Reference definitions read better against a rule than inside a card: they
   are a list of terms, not a set of separate objects. */
.def{border-left:2px solid var(--border);padding:1px 0 1px 15px;margin:18px 0;}
.def h3{font-size:15px;margin-bottom:3px;}
.def p{margin:5px 0;color:var(--text2);font-size:14px;}
.def p b,.def p strong{color:var(--text);}
.lead-in{font-size:16px;color:var(--text2);max-width:66ch;margin:10px 0 4px;}
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


# Cloudflare Web Analytics. Free, needs no cookie banner because it sets no
# cookies and stores no per-visitor identifier, and it is the only script on
# these pages - the archive is otherwise pure HTML and CSS.
#
# Manual snippet rather than Cloudflare's automatic injection. The DNS is
# grey-cloud so Netlify terminates TLS, which means traffic never passes
# through Cloudflare's proxy - automatic injection would have silently
# collected nothing. The site is set to "JS Snippet installation" to match.
#
# The token is not a secret: it is in the page source of every site that uses
# it and only says which site a page view belongs to. Hardcoding it keeps the
# nightly build from depending on an env var that would be easy to forget;
# CF_ANALYTICS_TOKEN still overrides, and an empty value writes no tag at all.
CF_ANALYTICS_TOKEN = os.environ.get(
    'CF_ANALYTICS_TOKEN', 'd45aef33f45b49918e3ba6c4e5cbfe7c').strip()


def social(title, desc, canon):
    """Open Graph and Twitter card tags, plus a little JSON-LD.

    Without these a link to the site posted anywhere renders as a bare URL.
    Every link reply we sent on 2026-10-30 looked like that. og:image is the
    1200x630 card from brand/make_og_image.py.

    The JSON-LD says WebPage rather than Article: these pages are generated
    reference tables, not written pieces, and claiming otherwise is the kind
    of structured-data overreach search engines discount.
    """
    j = json.dumps({
        '@context': 'https://schema.org',
        '@type': 'WebPage',
        'name': title,
        'description': desc,
        'url': canon,
        'isPartOf': {'@type': 'WebSite', 'name': 'Overnight Arc',
                     'url': SITE + '/'},
        'publisher': {'@type': 'Organization',
                      'name': 'Insight Arc Solutions',
                      'url': SITE + '/'},
        'inLanguage': 'en',
    }, ensure_ascii=False)
    return (
        f'<meta property="og:type" content="website">\n'
        f'<meta property="og:site_name" content="Overnight Arc">\n'
        f'<meta property="og:title" content="{esc(title)}">\n'
        f'<meta property="og:description" content="{esc(desc)}">\n'
        f'<meta property="og:url" content="{esc(canon)}">\n'
        f'<meta property="og:image" content="{SITE}/og.png">\n'
        f'<meta property="og:image:width" content="1200">\n'
        f'<meta property="og:image:height" content="630">\n'
        f'<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:site" content="@overnightarc">\n'
        f'<script type="application/ld+json">{j}</script>\n')


def analytics():
    if not CF_ANALYTICS_TOKEN:
        return ''
    return ('<script defer src="https://static.cloudflareinsights.com/beacon.min.js"'
            ' data-cf-beacon=\'{"token": "%s"}\'></script>\n'
            % esc(CF_ANALYTICS_TOKEN))


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
<link rel="icon" href="{SITE}/arc.svg" type="image/svg+xml">
<link rel="stylesheet" href="{'../' * (path.count('/') + 1) if path else ''}a.css">
{social(title, desc, canon)}{analytics()}</head>
<body>
<header><div class="wrap">
  <div class="brand"><svg class="logo" viewBox="0 0 100 100" aria-hidden="true"><defs><linearGradient id="arcGrad" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#3a4aa8"/><stop offset=".55" stop-color="#8a6ebe"/><stop offset="1" stop-color="#f2a44c"/></linearGradient></defs><path d="M14 78 Q42 6 88 30" fill="none" stroke="url(#arcGrad)" stroke-width="10" stroke-linecap="round"/><circle cx="88" cy="30" r="7.5" fill="#ffe2ba"/><circle cx="88" cy="30" r="2.6" fill="#fff"/></svg><span>Overnight Arc</span></div>
  <div class="crumb">{trail}</div>
</div></header>
<div class="wrap">
{body}
<footer>
<p><b>A score is a ranking, not a return.</b> Momentum is a 0&#8211;100 position
against a peer group, so 50 is the median and a high score in a falling market
means fell least. <a href="{SITE}/method/">How every figure here is
calculated</a>.</p>
<p>Rebuilt each weeknight after the US close; delayed, not live.
<b>Educational research, not investment advice, and not personalised to
anyone.</b> Published by Insight Arc Solutions, Dubai.
<a href="{SITE}/about/">About Overnight Arc</a> &#183;
<a href="mailto:contact@overnightarc.com">contact@overnightarc.com</a></p>
<p><b>Elsewhere:</b> <a href="{SITE}/">the four live boards</a> &#183;
<a href="{SITE}/digest/">every session</a> &#183;
<a href="{SITE}/themes/">themes</a> &#183;
<a href="{SITE}/funds/">funds</a> &#183;
<a href="{SITE}/countries/">countries</a></p>
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

    # Number the stages as they are listed. The data's own "position" is a
    # tier, and parallel stages deliberately share one — GLP-1 has two tier-3
    # stages — so using it as the numerator against a count of links printed
    # "Stage 3 of 7" twice on 17 of the 18 chains. The header above counts
    # links as stages, and so does the chain post, so this follows them.
    for i, L in enumerate(ch['links'], 1):
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
            f'<div class="link"><div class="pos">Stage {i} of '
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


# ---------------------------------------------------------- about & method

def about_page(counts, asof):
    n = counts or {}
    body = [
        '<h1>About Overnight Arc</h1>',
        '<p class="lede">A record of what happened on the US market last session, '
        'and of where money is moving, rebuilt from scratch every weeknight by a '
        'program rather than written by a desk.</p>',

        '<h2>Who publishes it</h2>',
        '<p>Overnight Arc is published by <b>Insight Arc Solutions</b>, a company '
        'registered in Dubai, United Arab Emirates. It is built and maintained by '
        'one person. There is no newsroom, no analyst team, and no third-party '
        'research behind any figure on the site.</p>',
        '<p>In practice that means nothing here is chosen by hand. The same scoring '
        'runs against every name in the universe every night, and no story is '
        'promoted because somebody found it interesting. A research desk can '
        'exercise judgement where the numbers are misleading; this cannot. That is '
        'a real limitation in some places and the entire point in others.</p>',

        '<h2>What it does</h2>',
        f'<p>Every weeknight after the US close, a scan reads end-of-day prices for '
        f'<b>{n.get("universe", 0):,} US-listed stocks</b> and the funds that hold '
        f'them, scores each one against its own peer group, and writes the result '
        f'here. The same run reads the market wires, works out which company each '
        f'story is about, and ranks the day\'s news by how many <i>separate</i> '
        f'outlets carried each item.</p>',
        f'<p>The four boards show the latest session. Alongside them sit permanent '
        f'pages: one for each of <a href="{SITE}/funds/">{n.get("funds", 0)} funds</a>, '
        f'<a href="{SITE}/countries/">{n.get("countries", 0)} country ETFs</a> and '
        f'<a href="{SITE}/themes/">{n.get("themes", 0)} themes</a>, and '
        f'<a href="{SITE}/digest/">one for every session</a>, written the night it '
        f'happens and never rewritten.</p>',

        '<h2>What it never does</h2>',
        '<div class="def"><h3>No recommendations</h3>'
        '<p>Nothing here is a view on whether to buy or sell anything. A ranking is '
        'a statement about where something sits against its peers, which is not the '
        'same as a reason to own it.</p></div>',
        '<div class="def"><h3>Nothing is personalised</h3>'
        '<p>The site does not know who you are, what you hold, or what you are '
        'trying to achieve, and nothing on it is adjusted for any of that.</p></div>',
        '<div class="def"><h3>Nothing is sold, sponsored or paid for</h3>'
        '<p>No advertising, no sponsored placements, no affiliate links, no paid '
        'inclusion. Nobody can buy their way onto a board, and no ordering here was '
        'chosen by a person &#8212; every rank is computed.</p></div>',
        '<div class="def"><h3>No opinion in the ordering</h3>'
        '<p>Stories are ordered by how many independent outlets carried them. '
        'Companies are ordered by where they sit against their own peers. Earnings '
        'are ordered by size. None of those orderings is a judgement.</p></div>',

        '<h2>What it is not good at</h2>',
        '<p>Five things to know before relying on anything here:</p>',
        '<div class="def"><h3>It is delayed, never live</h3>'
        f'<p>Everything is end-of-day. The newest figures on the site are the close '
        f'of {esc(asof)}. Nothing updates during market hours.</p></div>',
        '<div class="def"><h3>A score cannot tell you what the market did</h3>'
        '<p>Momentum is cross-sectional: it ranks a name against others, so in a '
        'market where everything is falling the leader is simply whatever fell '
        'least. That is why the return column sits beside it and why the board '
        'states the direction of the benchmark in words before any score is '
        'read.</p></div>',
        '<div class="def"><h3>The value chains are ours, and are hand-made</h3>'
        f'<p>{n.get("companies", 0)} companies mapped across 107 stages in '
        f'{n.get("themes", 0)} themes, by hand. They match no index and no fund\'s '
        f'holdings, they reflect a judgement about what belongs where, and they will '
        f'sometimes be wrong or out of date.</p></div>',
        '<div class="def"><h3>News matching is automated and makes mistakes</h3>'
        '<p>Working out which company a headline is about is done by a program. It '
        'has reported a Canadian newspaper as an insurance company, an Australian '
        'mining junior as a US chemicals firm, and a bank quoted in somebody else\'s '
        'story as the subject of it. Each of those was found and fixed; others will '
        'be there now.</p></div>',
        '<div class="def"><h3>Listed in the US is not the same as American</h3>'
        '<p>Everything here is US-listed and priced in dollars. Taiwan '
        'Semiconductor, ASML and Novo Nordisk are all in the maps.</p></div>',

        '<h2>Corrections</h2>',
        '<p>Mistakes get fixed and the fix gets explained. If a figure looks wrong, '
        'or a company is in the wrong place, or a story is attributed to the wrong '
        'name, write to '
        '<a href="mailto:contact@overnightarc.com">contact@overnightarc.com</a> and '
        'it will be looked at. Specifics help more than impressions &#8212; the page '
        'and the number are enough.</p>',

        '<h2>Not advice</h2>',
        '<p>This is educational research. It is not investment advice, it is not a '
        'recommendation to buy or sell any security, and it is not personalised to '
        'anyone. Insight Arc Solutions is not a licensed investment adviser and '
        'nothing here should be treated as the opinion of one. Do your own work, and '
        'take advice from somebody qualified to give it.</p>',

        f'<p><a href="{SITE}/method/">How every figure here is calculated</a> '
        f'&#183; <a href="{SITE}/">the four live boards</a></p>',
    ]
    return shell('about', 'About Overnight Arc — who publishes it and how it is built',
                 'Overnight Arc is published by Insight Arc Solutions, Dubai. What '
                 'the site does, what it never does, and what it is not good at.',
                 [('Overnight Arc', f'{SITE}/'), ('About', None)], '\n'.join(body))


def method_page(counts, asof, ret_dates):
    n = counts or {}
    d20 = (ret_dates or {}).get('d20', '')
    d60 = (ret_dates or {}).get('d60', '')
    body = [
        '<h1>How every figure here is calculated</h1>',
        '<p class="lede">Nothing on this site is a judgement, so every number has a '
        'definition. These are the definitions.</p>',

        '<h2>The scores</h2>',
        '<div class="def"><h3>Momentum (0&#8211;100)</h3>'
        '<p>A <b>cross-sectional percentile</b>, not a return. Each name is ranked '
        'against its peer group on relative-strength level and trend, 9- and 20-day '
        'EMA slopes, and price against its 50-day average; the composite is then '
        'expressed as a position from 0 to 100.</p>'
        '<p><b>50 is the median, not a neutral return.</b> In a falling market a '
        'score of 90 means fell least, not rose. This is the single most '
        'misread number on the site, which is why the return column sits next to it '
        'everywhere.</p>'
        f'<p>Stocks are scored against the whole {n.get("universe", 0):,}-name '
        f'universe; funds are scored against the other funds on their board. The two '
        f'numbers are not comparable with each other.</p></div>',

        '<div class="def"><h3>RS rank</h3>'
        '<p>A position, not a score, and it runs the other way: <b>#1 is the '
        'strongest name we cover</b>, so lower is better. It ranks a stock\'s price '
        'against the S&amp;P 500 over the past few weeks.</p>'
        '<p>A name can hold high momentum and a middling RS rank at once. Momentum '
        'rewards a trend that is <i>improving</i>; RS rewards one that is already '
        '<i>ahead</i>.</p></div>',

        '<div class="def"><h3>Durability</h3>'
        '<p>The share of the last 60 trading days a fund spent in the top 10% of its '
        'board. The average fund scores about 10% by construction. It separates a '
        'fund that has led for months from one that led yesterday.</p></div>',

        '<div class="def"><h3>8/21</h3>'
        '<p>A yes/no: the price closed above its 8-day EMA, with the 8-day above the '
        '21-day. A short-term trend check, nothing more.</p></div>',

        '<div class="def"><h3>&#916; rank</h3>'
        '<p>How far a row has moved on the column you are currently sorting by, '
        'against the previous session and the one five sessions back. Change the '
        'sort and the column follows it &#8212; it is not a fixed measure.</p>'
        '<p>Ties take the minimum rank, so equal values share a position rather than '
        'being split arbitrarily. Without that, two funds on identical scores could '
        'show a rank change that no price caused.</p></div>',

        '<h2>The returns</h2>',
        '<div class="def"><h3>Windows are trading sessions, not calendar days</h3>'
        f'<p>The 20-session figure compares the latest close with <b>{esc(d20)}</b> '
        f'and the 60-session figure with <b>{esc(d60)}</b> &#8212; about a month and '
        f'nearer three months than two.</p>'
        '<p>Each is a close against a <b>single earlier day</b>, so a window that '
        'happens to start on a spike or a trough can read very differently from the '
        'trend. <i>From high</i> is the check to run when a green return sits beside '
        'a deep drawdown.</p></div>',

        '<div class="def"><h3>vs SPY and vs QQQ</h3>'
        '<p>The benchmark\'s return over the same window, subtracted. Worth reading '
        'together: beating the S&amp;P while lagging the Nasdaq says the move '
        'belongs to the broad market rather than to the fund.</p></div>',

        '<div class="def"><h3>The regime line</h3>'
        '<p>A cross-sectional score cannot tell you what the market did, only where '
        'something stands inside it. So the board states the benchmark\'s direction '
        'in words before any score is read, and counts the rows where a good rank '
        'and a falling price disagree. It switches at <b>&#177;2%</b> on the '
        'benchmark\'s 20-session return.</p></div>',

        '<h2>The news</h2>',
        '<div class="def"><h3>Ranked by separate sources, never by article count</h3>'
        '<p>Stories are ordered by how many <b>independent outlets</b> carried them. '
        'Counting articles instead would measure syndication &#8212; one wire story '
        'reprinted forty times would outrank a scoop that four newsrooms chased '
        'themselves.</p></div>',

        '<div class="def"><h3>What a story is about is decided by a program</h3>'
        '<p>Cashtags first, then full company names, then unambiguous first words &#8212; '
        'and the last of those only when market vocabulary sits nearby, because '
        'without that constraint a story about a politician matched a media company '
        'and a Nobel prize matched a staffing firm.</p>'
        '<p>Research houses have to be the <b>subject</b> of a story, not the source '
        'of a view in it. "Goldman says" is not a Goldman story, and counting it as '
        'one put a bank at the top of a board it had nothing to do with.</p></div>',

        '<div class="def"><h3>Movers come from our own closes</h3>'
        '<p>The biggest movers are taken from the prices, not from coverage. The move '
        'is the fact; a headline is colour attached where one exists. "No coverage" '
        'against a double-digit move is itself worth reading, and general market '
        'feeds routinely miss small and mid-caps entirely.</p></div>',

        '<div class="def"><h3>Sessions, not filing dates</h3>'
        '<p>US cash equities open at 13:30 UTC. Anything filed before that belongs to '
        'the session that just finished, and weekends roll back to the Friday. '
        'Without this, a recap written at 03:00 about yesterday\'s close would be '
        'filed under a session that has not happened yet.</p></div>',

        '<h2>The value chains</h2>',
        f'<p>{n.get("companies", 0)} companies mapped by hand across 107 stages in '
        f'{n.get("themes", 0)} themes. A chain is ordered the way value is actually '
        f'added, from raw inputs through to whoever sells the finished thing, because '
        f'money rarely moves into a theme evenly &#8212; it moves into a stage of '
        f'one.</p>'
        f'<p>This mapping is ours. It matches no index and no fund\'s published '
        f'holdings, and it reflects a judgement about what belongs where. Where a '
        f'stage carries too few scored names to read, the page says so rather than '
        f'printing a number.</p>',

        '<h2>Where the data comes from</h2>',
        f'<p>End-of-day prices and fundamentals from public market data; fund '
        f'holdings as published by the issuers; the earnings calendar from Nasdaq\'s '
        f'public feed; news from publisher RSS and an aggregator, with the publisher '
        f'recovered per story so that one request yields many separate sources '
        f'rather than one.</p>'
        f'<p>Everything is rebuilt each weeknight after the US close. The newest '
        f'figures on the site are the close of <b>{esc(asof)}</b>. Nothing updates '
        f'intraday.</p>',

        f'<p><a href="{SITE}/about/">About Overnight Arc</a> &#183; '
        f'<a href="{SITE}/">the four live boards</a></p>',
    ]
    return shell('method', 'How Overnight Arc calculates momentum, RS rank and durability',
                 'Definitions for every figure on the site: cross-sectional momentum, '
                 'RS rank, durability, return windows, and how the news is ranked by '
                 'independent sources.',
                 [('Overnight Arc', f'{SITE}/'), ('Method', None)], '\n'.join(body))


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

    # The two pages that say who stands behind the numbers and what each one
    # means. Every other page now links to them instead of repeating a footer.
    meta = load('meta.json') or {}
    asof = meta.get('stockAsOf') or etfs.get('asOf', '')
    about_page(meta.get('counts'), asof)
    method_page(meta.get('counts'), asof, etfs.get('retDates'))
    print('  about + method      2')

    n = sitemap()
    print(f'  sitemap.xml      {n:>4} urls')
    print(f'  pages written    {len(written):>4} this run')


if __name__ == '__main__':
    main()
