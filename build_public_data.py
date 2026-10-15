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

# Patches postgrest to retry transient Supabase failures (gateway
# errors, timeouts) instead of letting one kill the nightly chain.
import sb_retry  # noqa: F401

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

# Trading sessions back from the as-of date for the rank-change columns. Read
# from the table rather than counted in calendar days: a holiday or a session
# Yahoo never finalized would otherwise turn "a week ago" into four days ago.
LOOKBACK = {'d1': 1, 'd5': 5}

# Trading sessions behind each return figure, matching compute_etf_momentum's
# ret_20 / ret_60. Sessions, not calendar days: 20 is roughly a month and 60 is
# roughly three, not two.
RET_WINDOWS = (20, 60)


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


def etf_sessions(n):
    """The n most recent snapshot_dates in etf_snapshots, newest first.

    Read off SPY, which is present on every session the board has (it is the
    benchmark every vs_spy figure is computed against, so if it were missing the
    board would already be broken). One row per session keeps this to a single
    small request even when asking for a quarter's worth. Falls back to scanning
    every symbol if SPY ever comes back short.
    """
    def distinct(rows):
        out = []
        for r in rows:
            if r['snapshot_date'] not in out:
                out.append(r['snapshot_date'])
        return out[:n]

    rows = (sb.table('etf_snapshots').select('snapshot_date').eq('symbol', 'SPY')
            .order('snapshot_date', desc=True).limit(n).execute().data or [])
    got = distinct(rows)
    if len(got) >= n:
        return got
    return distinct(sb.table('etf_snapshots').select('snapshot_date')
                    .order('snapshot_date', desc=True)
                    .limit(80 * n).execute().data or [])


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
        'daily_stock_snapshots', 'ticker,momentum_score,rs_rank,close,high_52w',
        lambda q: q.eq('snapshot_date', stock_asof).not_.is_('momentum_score', 'null'))}
    sectors = {r['ticker']: r for r in page(
        'us_stock_sectors', 'ticker,company_name,sector,industry')}

    # Valuation context. Momentum says what is working; it cannot say whether
    # the price already reflects it. A P/E against the company's OWN three-year
    # average answers that without comparing a software firm to a miner, and
    # it is a figure we derive rather than a vendor's data republished.
    funda = {r['ticker']: r for r in page(
        'stock_fundamentals', 'ticker,pe_ttm,pe_3yr_avg,market_cap_usd')}

    def valuation(tk):
        f = funda.get(tk) or {}
        pe, pe3 = f.get('pe_ttm'), f.get('pe_3yr_avg')
        try:
            pe, pe3 = float(pe), float(pe3)
        except (TypeError, ValueError):
            return None, None
        if pe <= 0 or pe3 <= 0:
            return None, None     # a loss-making year makes the ratio meaningless
        return round(pe, 1), round(pe / pe3 - 1, 3)

    def from_high(tk):
        s = snaps.get(tk) or {}
        try:
            c, h = float(s.get('close')), float(s.get('high_52w'))
            return round(c / h - 1, 4) if h > 0 else None
        except (TypeError, ValueError):
            return None

    def mkt_cap(tk):
        v = (funda.get(tk) or {}).get('market_cap_usd')
        try:
            return round(float(v) / 1e9, 2)      # $bn
        except (TypeError, ValueError):
            return None

    # ── ETF board ─────────────────────────────────────────────────────────────
    meta = {r['symbol']: r for r in page(
        'etfs', 'symbol,name,kind,grp,sector,theme_slug,benchmark_only,display_order')}
    esnap = {r['symbol']: r for r in page(
        'etf_snapshots',
        'symbol,momentum_score,etf_rank,pct_from_high,above_8_21,close,'
        'ret_20,vs_spy_20,vs_qqq_20,ret_60,vs_spy_60,vs_qqq_60,top_decile_60d',
        lambda q: q.eq('snapshot_date', etf_asof))}

    # Rank history. A score on its own does not say whether a fund is arriving
    # or leaving, and the board can be sorted four different ways, so the useful
    # question is the same in each case: where did this fund sit on THIS measure
    # a day ago, and a week ago?
    #
    # Only the raw metric values go out, not ranks. The page ranks the full fund
    # set itself for whichever column is active, which keeps one definition of
    # the ordering instead of two that can drift, and means adding a sort column
    # later needs no change here. The keys match the page's sort keys exactly.
    # Enough sessions to name the far end of the 60-session return window too.
    sessions = etf_sessions(max(max(LOOKBACK.values()), *RET_WINDOWS) + 1)
    hist_dates = {lbl: (sessions[back] if len(sessions) > back else None)
                  for lbl, back in LOOKBACK.items()}
    # The session each return window actually reaches back to. Published because
    # "60d" reads as two calendar months and is nothing of the sort: 60 trading
    # sessions is nearer three. Worse, a single-day anchor can land on a spike or
    # a trough and make a falling fund read green — EWY on 2026-10-13 showed
    # +2.3% over 60 sessions purely because the window started on 2026-07-20, a
    # low in the middle of a slide. Naming the date is what lets a reader see
    # that, and keeps the figure reproducible from any chart.
    #
    # The offsets are counted off SPY's calendar, while ret_20/ret_60 are
    # computed per symbol as N rows back in that symbol's own history. The two
    # agree only while every fund has a bar on every session SPY does — true for
    # all 83 today, and the normal case for US-listed funds. If SPY itself ever
    # lags the board, every offset here is wrong by at least one session, so
    # rather than publish a date that is quietly off by a day, publish none and
    # let the page fall back to saying nothing.
    if sessions and sessions[0] != etf_asof:
        print(f'  WARNING: SPY newest is {sessions[0]} but board is {etf_asof}; '
              'return-window dates withheld')
        ret_dates = {f'd{w}': None for w in RET_WINDOWS}
    else:
        ret_dates = {f'd{w}': (sessions[w] if len(sessions) > w else None)
                     for w in RET_WINDOWS}
    hist = {}
    for lbl, d in hist_dates.items():
        if not d:
            print(f'  no session {LOOKBACK[lbl]} back yet; {lbl} rank change omitted')
            continue
        hist[lbl] = {r['symbol']: {'mom': r.get('momentum_score'),
                                   'dur': r.get('top_decile_60d'),
                                   'spy': r.get('vs_spy_20'),
                                   'ret': r.get('ret_20'),
                                   'high': r.get('pct_from_high'),
                                   # Carried for the short-window move below,
                                   # not for ranking. Nothing sorts on close.
                                   'close': r.get('close')}
                     for r in page('etf_snapshots',
                                   'symbol,momentum_score,top_decile_60d,'
                                   'vs_spy_20,ret_20,pct_from_high,close',
                                   lambda q, d=d: q.eq('snapshot_date', d))}
    print('rank history: ' + ', '.join(f'{k}={v}' for k, v in hist_dates.items()))
    print('return windows: ' + ', '.join(f'{k}={v}' for k, v in ret_dates.items()))

    # Holdings, so the drill-down can mark which names a fund actually holds.
    # `complete` travels with them: only a full issuer file licenses a statement
    # that a fund does NOT hold something.
    held, complete_for = {}, set()
    for h in page('etf_holdings', 'etf,ticker,weight,complete'):
        held.setdefault(h['etf'], {})[h['ticker']] = h.get('weight')
        if h.get('complete'):
            complete_for.add(h['etf'])

    # Two boards, two pools. compute_etf_momentum ranks country funds in their
    # own cross-section, so a country's momentum describes where it sits among
    # countries and is not comparable with a sector fund's score. They are kept
    # in separate files for the same reason they are ranked separately: so that
    # nothing downstream can quietly concatenate them into one list.
    def move(sym, lbl, now_close):
        """Return over the SAME window the rank change uses.

        Derived from the two closes rather than stored, so the move and the
        rank change can never describe different spans: both read the session
        hist_dates named. A fund with no bar on that session gets nothing
        rather than a figure silently measured over a different window.
        """
        then = (hist.get(lbl, {}).get(sym) or {}).get('close')
        try:
            now_c, then_c = float(now_close), float(then)
        except (TypeError, ValueError):
            return None
        return round(now_c / then_c - 1, 6) if then_c > 0 else None

    funds, countries, benchmarks = [], [], {}
    for sym, m in sorted(meta.items(), key=lambda kv: kv[1].get('display_order') or 999):
        s = esnap.get(sym)
        if not s:
            continue
        is_country = m.get('kind') == 'country'
        row = {'symbol': sym, 'name': m.get('name'), 'group': m.get('grp'),
               'kind': m.get('kind'), 'theme': m.get('theme_slug'),
               'sector': m.get('sector'), 'mom': s.get('momentum_score'),
               'rank': s.get('etf_rank'), 'fromHigh': s.get('pct_from_high'),
               'trend821': s.get('above_8_21'),
               # Absolute return. Every other score on the board is relative to
               # the pool, so without this a falling market still reads green:
               # the leaders would simply be whatever fell least.
               'ret20': s.get('ret_20'), 'ret60': s.get('ret_60'),
               # The same two windows the rank change uses, so a move up the
               # board can be read against what the price actually did over
               # exactly that span. Five sessions is the week; one is the day.
               'mv1': move(sym, 'd1', s.get('close')),
               'mv5': move(sym, 'd5', s.get('close')),
               'vsSpy20': s.get('vs_spy_20'), 'vsQqq20': s.get('vs_qqq_20'),
               'vsSpy60': s.get('vs_spy_60'), 'vsQqq60': s.get('vs_qqq_60'),
               'durability': s.get('top_decile_60d'),
               'hist': {lbl: h[sym] for lbl, h in hist.items() if sym in h}}
        if not is_country:
            # Country funds hold non-US companies, which our stock universe does
            # not score. An empty holdings map would read as "holds nothing"
            # rather than "we cannot see inside this one", so it is left off.
            row['holdings'] = held.get(sym, {})
            row['holdingsComplete'] = sym in complete_for
        if m.get('benchmark_only'):
            benchmarks[sym] = {'symbol': sym, 'name': m.get('name'),
                               'ret20': s.get('ret_20'), 'ret60': s.get('ret_60')}
        elif row['mom'] is not None:
            (countries if is_country else funds).append(row)
    write('etfs.json', {'asOf': etf_asof, 'histDates': hist_dates,
                        'retDates': ret_dates,
                        'funds': funds, 'benchmarks': benchmarks})
    write('countries.json', {'asOf': etf_asof, 'histDates': hist_dates,
                             'retDates': ret_dates,
                             'countries': countries, 'benchmarks': benchmarks})

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
    # fetch_ohlc pulls QQQ into daily_stock_snapshots as a dashboard benchmark,
    # so it is scored against the 1,930 stocks and would turn up in a list of
    # "strongest individual names" as soon as its momentum cleared the cutoff.
    # It sits at 84 today against a cutoff of 88, which is luck rather than
    # design. Anything that is a fund on our own board is not an individual name.
    funds_by_symbol = set(meta)
    leaders = []
    for tk, sn in snaps.items():
        if sn.get('momentum_score') is None or tk in funds_by_symbol:
            continue
        sec = sectors.get(tk) or {}
        pe, peVsOwn = valuation(tk)
        leaders.append({'ticker': tk, 'company': sec.get('company_name'),
                        'industry': sec.get('industry'),
                        'mom': sn['momentum_score'], 'rsRank': sn.get('rs_rank'),
                        'pe': pe, 'peVsOwn': peVsOwn,
                        'fromHigh': from_high(tk), 'mktCap': mkt_cap(tk),
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
        pe, peVsOwn = valuation(tk)
        stocks[tk] = {'company': sec.get('company_name'),
                      'mom': (sn or {}).get('momentum_score'),
                      'rsRank': (sn or {}).get('rs_rank'),
                      'pe': pe, 'peVsOwn': peVsOwn,
                      'fromHigh': from_high(tk), 'mktCap': mkt_cap(tk),
                      'themes': sorted(mapped.get(tk, []))}
    write('stocks.json', {'asOf': stock_asof, 'stocks': stocks})

    write('meta.json', {
        'stockAsOf': stock_asof, 'etfAsOf': etf_asof,
        'builtAt': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'counts': {'themes': len(theme_rows), 'funds': len(funds),
                   'countries': len(countries),
                   'companies': len(mapped), 'universe': len(snaps)},
    })
    write_bundle()
    print(f'\nDone. {len(theme_rows)} themes, {len(funds)} funds, {len(mapped)} mapped names.')


if __name__ == '__main__':
    main()
