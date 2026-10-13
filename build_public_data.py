#!/usr/bin/env python3
"""Freeze the sector-research data into static JSON for the public portal.

The public site gets no database access at all. It reads these files from a CDN,
which means there is no key in its page source, nothing for a visitor to point
at the portfolio project, and no load on Supabase from public traffic. The data
moves once a day, so a live connection would buy nothing.

What is deliberately NOT published:
  analyst_ratings   TipRanks, licensed to us and not ours to redistribute
  stock_fundamentals  raw P/S, margins and growth come from yfinance; only the
                      percentile we compute from them goes out, never the inputs
  holdings, transactions, cash_flows, portfolio_snapshots, paper_*  — personal
  conviction_scores, entry_signals  — ours, but they drive the paper algo

What IS published is our own: the hand-mapped value chains, and the momentum,
rank and durability scores computed nightly from them.

Writes portal/data/*.json. Run after compute_etf_momentum in the nightly chain.
Env: SUPABASE_URL, SUPABASE_SERVICE_KEY.
"""

import json
import os
import statistics
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))
OUT = os.path.join(BASE, 'portal', 'data')

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')
sb = create_client(URL, KEY)

MIN_COVERAGE_MEMBERS = 3      # below this a link's median is close to one stock
EMERGING_MIN_N, EMERGING_MIN_MOM, EMERGING_MIN_GAP = 5, 70, 0.5


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


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs)) if xs else None


BUNDLE = {}      # everything written, also emitted as one JS file (see write_bundle)


def write(name, obj):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    with open(p, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, separators=(',', ':'), ensure_ascii=False)
    BUNDLE[name.replace('.json', '')] = obj
    print(f'  {name:<16} {os.path.getsize(p)/1024:>7.1f} KB')


def write_bundle():
    """Same payloads as one JS file assigning window.PORTAL_DATA.

    The page loads this with a <script> tag rather than fetching the JSON,
    because fetch() is blocked under file:// — opening index.html by
    double-clicking it would otherwise fail with no obvious cause. A script tag
    works from the filesystem and from a CDN alike. The .json files stay: they
    cost nothing and are a usable raw-data endpoint on their own."""
    p = os.path.join(OUT, 'data.js')
    with open(p, 'w', encoding='utf-8') as fh:
        fh.write('window.PORTAL_DATA=')
        json.dump(BUNDLE, fh, separators=(',', ':'), ensure_ascii=False)
        fh.write(';')
    print(f'  {"data.js":<16} {os.path.getsize(p)/1024:>7.1f} KB  (bundle, works from file://)')


def main():
    print('=' * 64)
    print('BUILD PUBLIC DATA  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 64)

    stock_asof = (sb.table('daily_stock_snapshots').select('snapshot_date')
                  .not_.is_('momentum_score', 'null')
                  .order('snapshot_date', desc=True).limit(1).execute().data)
    etf_asof = (sb.table('etf_snapshots').select('snapshot_date')
                .order('snapshot_date', desc=True).limit(1).execute().data)
    if not stock_asof or not etf_asof:
        sys.exit('no scored data to publish')
    stock_asof, etf_asof = stock_asof[0]['snapshot_date'], etf_asof[0]['snapshot_date']
    print(f'stocks as of {stock_asof} | ETFs as of {etf_asof}\n')

    snaps = {r['ticker']: r for r in page(
        'daily_stock_snapshots', 'ticker,momentum_score,rs_rank',
        lambda q: q.eq('snapshot_date', stock_asof).not_.is_('momentum_score', 'null'))}
    sectors = {r['ticker']: r for r in page(
        'us_stock_sectors', 'ticker,company_name,sector,industry')}

    # ── ETF board ─────────────────────────────────────────────────────────────
    meta = {r['symbol']: r for r in page(
        'etfs', 'symbol,name,kind,grp,sector,theme_slug,benchmark_only,display_order')}
    esnap = {r['symbol']: r for r in page(
        'etf_snapshots',
        'symbol,momentum_score,etf_rank,pct_from_high,above_8_21,'
        'ret_20,vs_spy_20,vs_qqq_20,ret_60,vs_spy_60,vs_qqq_60,top_decile_60d',
        lambda q: q.eq('snapshot_date', etf_asof))}

    # Holdings, so the drill-down can mark which names a fund actually holds.
    # `complete` travels with them: only a full issuer file licenses a statement
    # that a fund does NOT hold something.
    held, complete_for = {}, set()
    for h in page('etf_holdings', 'etf,ticker,weight,complete'):
        held.setdefault(h['etf'], {})[h['ticker']] = h.get('weight')
        if h.get('complete'):
            complete_for.add(h['etf'])

    funds, benchmarks = [], {}
    for sym, m in sorted(meta.items(), key=lambda kv: kv[1].get('display_order') or 999):
        s = esnap.get(sym)
        if not s:
            continue
        row = {'symbol': sym, 'name': m.get('name'), 'group': m.get('grp'),
               'kind': m.get('kind'), 'theme': m.get('theme_slug'),
               'sector': m.get('sector'), 'mom': s.get('momentum_score'),
               'rank': s.get('etf_rank'), 'fromHigh': s.get('pct_from_high'),
               'trend821': s.get('above_8_21'),
               'vsSpy20': s.get('vs_spy_20'), 'vsQqq20': s.get('vs_qqq_20'),
               'vsSpy60': s.get('vs_spy_60'), 'vsQqq60': s.get('vs_qqq_60'),
               'durability': s.get('top_decile_60d'),
               'holdings': held.get(sym, {}),
               'holdingsComplete': sym in complete_for}
        if m.get('benchmark_only'):
            benchmarks[sym] = {'symbol': sym, 'ret20': s.get('ret_20'),
                               'ret60': s.get('ret_60')}
        elif row['mom'] is not None:
            funds.append(row)
    write('etfs.json', {'asOf': etf_asof, 'funds': funds, 'benchmarks': benchmarks})

    # ── Value chains ──────────────────────────────────────────────────────────
    themes = page('themes', 'slug,name,description,category,category_order,display_order')
    nodes = page('theme_nodes', 'theme_slug,node_key,name,short_label,layer,blurb')
    members = page('theme_members', 'theme_slug,node_key,ticker,note,is_context')

    by_node = {}
    for m in members:
        by_node.setdefault((m['theme_slug'], m['node_key']), []).append(m)

    chains, theme_rows = {}, []
    for t in sorted(themes, key=lambda x: (x.get('category_order') or 99,
                                           x.get('display_order') or 99)):
        links, all_scores = [], []
        for n in sorted([x for x in nodes if x['theme_slug'] == t['slug']],
                        key=lambda x: (x.get('layer') or 99, x['node_key'])):
            rows, scores = [], []
            for mm in sorted(by_node.get((t['slug'], n['node_key']), []),
                             key=lambda r: r['ticker']):
                tk, sn = mm['ticker'], snaps.get(mm['ticker'], {})
                ctx = bool(mm.get('is_context'))
                rows.append({'ticker': tk,
                             'company': (sectors.get(tk) or {}).get('company_name'),
                             'why': mm.get('note'),
                             'mom': None if ctx else sn.get('momentum_score'),
                             'rsRank': None if ctx else sn.get('rs_rank'),
                             'context': ctx})
                if not ctx and sn.get('momentum_score') is not None:
                    scores.append(sn['momentum_score'])
            all_scores += scores
            links.append({'key': n['node_key'], 'name': n['name'],
                          'short': n.get('short_label'), 'position': n.get('layer'),
                          'what': n.get('blurb'), 'mom': med(scores),
                          'scored': len(scores),
                          'thin': len(scores) < MIN_COVERAGE_MEMBERS,
                          'members': rows})
        chains[t['slug']] = {'slug': t['slug'], 'name': t['name'],
                             'description': t.get('description'),
                             'category': t.get('category'), 'links': links}
        theme_rows.append({'slug': t['slug'], 'name': t['name'],
                           'description': t.get('description'),
                           'category': t.get('category'),
                           'mom': med(all_scores), 'links': len(links),
                           'companies': sum(l['scored'] for l in links)})
    write('themes.json', {'asOf': stock_asof, 'themes': theme_rows})
    write('chains.json', {'asOf': stock_asof, 'chains': chains})

    # ── Leaders ───────────────────────────────────────────────────────────────
    # Momentum only. The blended board on the private site adds a value leg built
    # from yfinance fundamentals; the inputs are not ours to republish and the
    # percentile alone would need them explained, so the public cut stays on the
    # score we compute ourselves.
    mapped = {}
    for m in members:
        if not m.get('is_context'):
            mapped.setdefault(m['ticker'], set()).add(m['theme_slug'])
    # Ranked over the WHOLE scored universe, not just the names we have mapped.
    # Mapping covers about a fifth of it, so a leaderboard drawn from members
    # only was missing most of the actual leaders: 27 of the 40 strongest stocks
    # in the market had no theme, Everpure (P) among them at momentum 96 and
    # rank 2. Theme tags are shown where we have them and left blank where we
    # do not, which also makes the gap visible rather than silently excluding it.
    leaders = []
    for tk, sn in snaps.items():
        if sn.get('momentum_score') is None:
            continue
        sec = sectors.get(tk) or {}
        leaders.append({'ticker': tk, 'company': sec.get('company_name'),
                        'industry': sec.get('industry'),
                        'mom': sn['momentum_score'], 'rsRank': sn.get('rs_rank'),
                        'themes': sorted(mapped.get(tk, []))})
    leaders.sort(key=lambda r: (-r['mom'], r['rsRank'] or 9999))
    write('leaders.json', {
        'asOf': stock_asof,
        'universe': len(leaders),
        'top': leaders[:120],          # enough to filter down to the mapped subset
        'hubs': sorted([l for l in leaders if len(l['themes']) >= 2],
                       key=lambda r: (-len(r['themes']), -r['mom']))[:15]})

    # ── Emerging groups ───────────────────────────────────────────────────────
    buckets = {}
    for tk, sn in snaps.items():
        ind = (sectors.get(tk) or {}).get('industry')
        if not ind:
            continue
        b = buckets.setdefault(ind, {'mom': [], 'unmapped': []})
        b['mom'].append(sn['momentum_score'])
        if tk not in mapped:
            b['unmapped'].append({'ticker': tk, 'mom': sn['momentum_score']})
    emerging = []
    for ind, b in buckets.items():
        n = len(b['mom'])
        gap = len(b['unmapped']) / n if n else 0
        m = med(b['mom'])
        if n >= EMERGING_MIN_N and m and m >= EMERGING_MIN_MOM and gap >= EMERGING_MIN_GAP:
            emerging.append({'industry': ind, 'n': n, 'mom': m, 'gap': round(gap, 2),
                             'leaders': sorted(b['unmapped'], key=lambda r: -r['mom'])[:6]})
    emerging.sort(key=lambda r: -r['mom'])
    write('emerging.json', {'asOf': stock_asof, 'groups': emerging[:8]})

    # One lookup for every ticker the portal needs to render: theme members plus
    # everything the funds hold. A fund holds names we map to no theme at all
    # (AVGO is in CIBR but not in our cyber chain) and the drill-down has to be
    # able to score those, so the chains alone are not enough.
    needed = set(mapped) | {t for h in held.values() for t in h}
    stocks = {}
    for tk in sorted(needed):
        sn, sec = snaps.get(tk), sectors.get(tk) or {}
        stocks[tk] = {'company': sec.get('company_name'),
                      'mom': (sn or {}).get('momentum_score'),
                      'rsRank': (sn or {}).get('rs_rank'),
                      'themes': sorted(mapped.get(tk, []))}
    write('stocks.json', {'asOf': stock_asof, 'stocks': stocks})

    write('meta.json', {
        'stockAsOf': stock_asof, 'etfAsOf': etf_asof,
        'builtAt': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'counts': {'themes': len(theme_rows), 'funds': len(funds),
                   'companies': len(mapped), 'universe': len(snaps)},
    })
    write_bundle()
    print(f'\nDone. {len(theme_rows)} themes, {len(funds)} funds, {len(mapped)} mapped names.')


if __name__ == '__main__':
    main()
