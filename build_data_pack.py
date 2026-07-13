#!/usr/bin/env python3
"""Weekly newsletter DATA PACK generator.

Emits the deterministic, quantitative half of the weekly market newsletter as a
markdown artifact (newsletter_data_pack.md), pulling entirely from Supabase:

  - Where We Stand   : the 5-point regime check (from market_health)
  - RS Movers        : Top 20 strongest (rs_rank) + Top 20 biggest weekly climbers
  - Watchlist        : names passing 4 filters — strong sector, rank climbing,
                       genuine uptrend, NOT stretched above the 20-day

This is the "data pack" the editorial draft is built on top of. It is read-only.

rs_rank: 1 = strongest. A climber's rank number FALLS (e.g. 967 -> 153), so
climb = rank_week_ago - rank_now > 0.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (.env auto-loaded).
"""

import os
import sys
from datetime import datetime, timezone, date

from supabase import create_client
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

sb = create_client(URL, KEY)

NON_STOCKS = {'^GSPC', '^VIX', 'QQQ'}
TOP_N = 20                  # RS Movers list length
MEGACAP_N = 12             # "two clocks" price-vs-rank table: N largest by market cap
DIVERGE_N = 12             # price-up / rank-not-confirming divergences to surface
DIVERGE_MIN_PX = 5.0       # a divergence needs at least this weekly price gain (%)
DIVERGE_WEAK_RANK = 300    # ...while still ranked worse than this, or with rank flat/falling
WATCHLIST_N = 15
SECTOR_TOP_RANK = 12       # peer_group must rank in top-N by composite
SECTOR_MIN_BREADTH = 50    # ...and clear this breadth %
MAX_STRETCH_PCT = 10.0     # watchlist: at most this far above the 20-day
WEEK_TD = 5                # trading days that count as "a week ago"


def paginate(table, columns, order_cols, filters=None, page=1000):
    out, frm = [], 0
    while True:
        q = sb.table(table).select(columns)
        for c in order_cols:
            q = q.order(c)
        for f in (filters or []):
            q = f(q)
        res = q.range(frm, frm + page - 1).execute()
        if not res.data:
            break
        out.extend(res.data)
        if len(res.data) < page:
            break
        frm += page
    return out


def num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _median(xs):
    xs = sorted(v for v in xs if v is not None)
    if not xs:
        return None
    n, m = len(xs), len(xs) // 2
    return xs[m] if n % 2 else (xs[m - 1] + xs[m]) / 2


def previous_week_close(dates):
    """Last trading day of the most recent EARLIER ISO week — the week-over-week
    baseline for a weekly report (holiday- and weekday-robust)."""
    latest_wk = date.fromisoformat(dates[-1]).isocalendar()[:2]
    prior = [d for d in dates if date.fromisoformat(d).isocalendar()[:2] < latest_wk]
    return prior[-1] if prior else dates[0]


def last_dates_per_week(dates, n=3):
    """The last trading day of each of the most recent n ISO weeks (ascending)."""
    by_week = {}
    for d in dates:
        by_week[date.fromisoformat(d).isocalendar()[:2]] = d   # dates ascending → keeps last
    return [by_week[w] for w in sorted(by_week)[-n:]]


TIER_LABELS = {'T2_STRONG_REG': 'T2 strong-regime', 'T3_MULTI_REG': 'T3 multi-regime',
               'T4_RS_ACCEL': 'T4 RS-acceleration'}

# Static, accurate description of the paper strategy (mirrors paper_trader.py config).
# Single source of truth; the synthesis reproduces it verbatim as the Algo footnote.
METHODOLOGY_FOOTNOTE = (
    "The Algo is a long-only, rules-based paper strategy on a $50,000 notional sleeve, "
    "trading the same ~1,900-stock universe. A name is bought only if it clears every gate: "
    "its sector ranks near the top of the board with broad participation, earnings are growing "
    "year-over-year in both revenue and profit, it isn't already held, and it's past a 21-day "
    "cooldown. Qualifying setups are sorted into four conviction tiers by how many independent "
    "signals fire — from T1 (multiple 'strong' chart patterns, risked at 1.5% of the sleeve) "
    "down to T4 (relative-strength acceleration, 0.5%). Size is set by risk, not dollars: each "
    "trade is sized so a stop 2×ATR below entry costs only that tier's risk budget, capped at "
    "$2,000–$6,000. Exits are mechanical — scale out a third at +2R and a third at +4R "
    "(1R = the initial stop distance), trail the remainder by 2×ATR, book some after a +25% "
    "spike, and time out positions left flat after 25 trading days. New entries pause if the book "
    "is down 2% on the day or 8% from its peak. Signals are generated after the close and filled "
    "the next session at the live price plus 0.15% slippage. These are educational paper results "
    "— not advice, and not a live account.")


def _pct(a, b):
    a, b = num(a), num(b)
    return (a / b - 1) * 100 if (a is not None and b) else None


def algo_section(latest, week_ago, now, meta):
    """Markdown block: the paper strategy's equity, track record, the trades it
    closed this week, and its current open book. Returns [] if no paper data."""
    def eq_row(desc=True, on_or_before=None):
        q = sb.table('paper_equity').select(
            'snapshot_date,total_value,drawdown_pct,open_positions,bench_sp500,bench_qqq')
        if on_or_before:
            q = q.lte('snapshot_date', on_or_before)
        d = q.order('snapshot_date', desc=desc).limit(1).execute().data
        return d[0] if d else None

    cur = eq_row(desc=True)
    if not cur:
        return []
    first = eq_row(desc=False)
    wk = eq_row(on_or_before=week_ago)

    trades = paginate('paper_trades',
                      'ticker, status, entry_price, entry_date, exit_date, strategy_tier, '
                      'conviction_grade, total_pnl_pct, r_multiple, exit_reason, holding_days',
                      ['ticker'])
    closed = [t for t in trades if t.get('status') == 'closed']
    open_ = [t for t in trades if t.get('status') == 'open']
    closed_wk = [t for t in closed if t.get('exit_date') and week_ago < t['exit_date'] <= latest]

    wins = [t for t in closed if (num(t.get('total_pnl_pct')) or 0) > 0]
    pnls = [num(t['total_pnl_pct']) for t in closed if num(t.get('total_pnl_pct')) is not None]
    rs = [num(t['r_multiple']) for t in closed if num(t.get('r_multiple')) is not None]
    avg_pnl = sum(pnls) / len(pnls) if pnls else None
    avg_r = sum(rs) / len(rs) if rs else None

    L = ["\n## Algo (Paper Portfolio) — systematic strategy track record",
         "_A rules-based paper strategy traded on the same universe. Educational track "
         "record, not advice or a live account._\n"]

    ret_in = _pct(cur['total_value'], (first or {}).get('total_value'))
    ret_wk = _pct(cur['total_value'], (wk or {}).get('total_value'))
    sp_in = _pct(cur.get('bench_sp500'), (first or {}).get('bench_sp500'))
    qqq_in = _pct(cur.get('bench_qqq'), (first or {}).get('bench_qqq'))

    head = f"**Equity:** ${num(cur['total_value']):,.0f} as of {cur['snapshot_date']}"
    if ret_in is not None and first:
        head += f" — {ret_in:+.1f}% since inception ({first['snapshot_date']})"
        if sp_in is not None:
            head += f", vs S&P {sp_in:+.1f}% / QQQ {qqq_in:+.1f}% over the same span"
    L.append(head + ".")
    extra = []
    if ret_wk is not None:
        extra.append(f"week-over-week {ret_wk:+.1f}%")
    if cur.get('drawdown_pct') is not None:
        extra.append(f"drawdown {num(cur['drawdown_pct']):.1f}%")
    extra.append(f"{len(open_)} open / {len(closed)} closed")
    L.append("**Status:** " + ", ".join(extra) + ".")
    if closed:
        wr = 100 * len(wins) / len(closed)
        tr = f"**Track record (all closed):** {len(closed)} trades, {len(wins)} winners ({wr:.0f}% win rate)"
        if avg_pnl is not None:
            tr += f", avg {avg_pnl:+.1f}%/trade"
        if avg_r is not None:
            tr += f", avg {avg_r:+.2f}R"
        L.append(tr + ".")

    if closed_wk:
        L.append("\n### Closed this week")
        L.append("| Ticker | P&L % | R | Exit reason | Days held |\n|---|---|---|---|---|")
        for t in sorted(closed_wk, key=lambda x: num(x.get('total_pnl_pct')) if num(x.get('total_pnl_pct')) is not None else -999, reverse=True):
            p, r = num(t.get('total_pnl_pct')), num(t.get('r_multiple'))
            L.append(f"| {t['ticker']} | {p:+.1f}% | {('%+.2fR' % r) if r is not None else '—'} "
                     f"| {t.get('exit_reason', '—')} | {t.get('holding_days') or '—'} |")

    if open_:
        L.append("\n### Open positions (unrealized vs latest close)")
        L.append("| Ticker | Sector | Tier | Conv | Entry date | Unreal % |\n|---|---|---|---|---|---|")
        rows = [( _pct((now.get(t['ticker']) or {}).get('close'), t.get('entry_price')), t)
                for t in open_]
        for un, t in sorted(rows, key=lambda x: (x[0] is None, -(x[0] or 0))):
            tk = t['ticker']
            tier = TIER_LABELS.get(t.get('strategy_tier'), t.get('strategy_tier') or '—')
            sec = (meta.get(tk) or {}).get('sector', '—')
            L.append(f"| {tk} | {sec} | {tier} | {t.get('conviction_grade') or '—'} "
                     f"| {t.get('entry_date')} | {('%+.1f%%' % un) if un is not None else '—'} |")

    L.append("\n**Methodology.** " + METHODOLOGY_FOOTNOTE)
    return L


def analyst_moves_section(week_ago, latest, meta):
    """This week's analyst upgrades / downgrades / initiations (from
    analyst_ratings.recent_actions via the analyst_moves_since() SQL function),
    deduped and ranked by coverage. Returns [] if none."""
    try:
        rows = sb.rpc('analyst_moves_since', {'cutoff': week_ago}).execute().data or []
    except Exception:
        return []
    rows = [r for r in rows if (r.get('act_date') or '') <= latest]
    if not rows:
        return []
    best = {}                                   # dedup (ticker, firm, action) → latest
    for r in rows:
        k = (r['ticker'], r.get('firm'), r['action'])
        if k not in best or (r.get('act_date') or '') > (best[k].get('act_date') or ''):
            best[k] = r
    rows = list(best.values())
    downs = sorted([r for r in rows if r['action'] == 'down'],
                   key=lambda r: r.get('num_analysts') or 0, reverse=True)
    ups = sorted([r for r in rows if r['action'] == 'up'],
                 key=lambda r: r.get('num_analysts') or 0, reverse=True)
    inits = sorted([r for r in rows if r['action'] == 'init'],
                   key=lambda r: r.get('num_analysts') or 0, reverse=True)

    def fmt(r, label):
        tk = r['ticker']
        up = num(r.get('upside'))
        cons = (r.get('consensus') or '—').replace('_', ' ')
        return (f"| {tk} | {(meta.get(tk) or {}).get('company_name', tk)} | {label} | "
                f"{r.get('firm') or '—'} | {r.get('from_grade') or '?'} → {r.get('to_grade') or '?'} "
                f"| {cons} | {('%+.0f%%' % up) if up is not None else '—'} |")

    L = ["\n## Analyst Moves (this week)",
         f"_Rating changes over the week: {len(downs)} downgrades, {len(ups)} upgrades, "
         f"{len(inits)} new initiations across the universe. Table shows the best-covered names._\n",
         "| Ticker | Company | Move | Firm | From → To | Consensus | Target upside |\n"
         "|---|---|---|---|---|---|---|"]
    for r in downs[:10]:
        L.append(fmt(r, 'DOWNGRADE'))
    for r in ups[:10]:
        L.append(fmt(r, 'UPGRADE'))
    if inits:
        names = ", ".join(f"{r['ticker']} ({r.get('to_grade')}, {r.get('firm')})" for r in inits[:6])
        L.append(f"\n_Notable new coverage: {names}._")
    return L


def econ_calendar(latest):
    """Rule-deterministic recurring US releases due in the next 7 days. Only the
    cadences that are genuinely rule-based (so we don't fabricate exact dates):
    nonfarm payrolls = first Friday; initial jobless claims = every Thursday."""
    from datetime import date, timedelta
    d0 = date.fromisoformat(latest)
    out, cur = [], d0 + timedelta(days=1)
    while cur <= d0 + timedelta(days=7):
        if cur.weekday() == 4 and cur.day <= 7:
            out.append((cur.isoformat(), "Nonfarm payrolls & unemployment (BLS jobs report)",
                        "the headline jobs number and wage growth — the Fed's other mandate"))
        if cur.weekday() == 3:
            out.append((cur.isoformat(), "Initial jobless claims (weekly)",
                        "early read on whether the labor market is softening"))
        cur += timedelta(days=1)
    return out


# Fixed "why we watch it" context per symbol — a descriptor of the linkage to US
# markets, NOT a market call. Gives the writer the mechanism; the direction/read
# comes from the week's actual % move.
GLOBAL_TELL = {
    '^TWII':    'TSMC & the AI / semiconductor supply chain — the lead tell for US chips',
    '^KS11':    'Samsung + SK Hynix — the global memory cycle (leads MU and the semis)',
    '^N225':    'global risk appetite + the yen carry trade (a sharp drop precedes US risk-off)',
    '^HSI':     'China demand — materials, industrials, copper, US multinationals\' China revenue',
    'DX-Y.NYB': 'the US dollar — a rising dollar is a headwind for US multinationals & commodities',
    'JPY=X':    'USD/JPY, the carry-trade gauge — fast yen strength = a carry unwind = global risk-off',
    'HG=F':     '"Dr. Copper", the global-growth barometer (ties to the copper theme when it runs)',
    'BZ=F':     'Brent crude — the direct oil / supply-shock gauge behind any Middle-East headline',
    'BTC-USD':  '24/7 risk-sentiment proxy — trades through the weekend when equities are shut',
    'GC=F':     'gold — the oldest safe haven; a rally signals fear, flat/soft through a shock signals calm',
    'HYG':      'US high-yield ("junk") bonds — the risk-appetite tell; a drop = credit stress spreading, steady/up = calm',
}
CATEGORY_LABEL = {'asia_equity': 'Asian equities (they trade before the US opens)',
                  'fx': 'Currencies', 'commodity': 'Commodities', 'crypto': 'Crypto',
                  'credit': 'Credit'}


def _fmt_level(v):
    """Price/level formatting by magnitude: big indices no decimals, FX/commodities 2dp."""
    if v is None:
        return '—'
    av = abs(v)
    if av >= 1000:
        return f'{v:,.0f}'
    if av >= 100:
        return f'{v:,.1f}'
    return f'{v:,.2f}'


def global_tape_section(latest):
    """The global cross-asset tape (from global_indices): Asian equity indices + FX +
    commodities + BTC. TWO windows per market so the path isn't hidden: (1) the move over
    the SAME week as the US numbers (comparable), and (2) the move SINCE the US last close
    — sessions the US hasn't priced yet (e.g. an Asian Friday on a US holiday), the forward
    tell for the next US session. Notable movers are flagged and get explanatory headlines
    in global_news.json. Context for the macro sections — NOT a US rank/rotation read.
    Returns [] if the table is empty."""
    snaps = (sb.table('global_indices').select('snapshot_date')
             .order('snapshot_date', desc=True).limit(1).execute().data)
    if not snaps:
        return []
    snap = snaps[0]['snapshot_date']
    rows = (sb.table('global_indices').select('*')
            .eq('snapshot_date', snap).order('display_order').execute().data or [])
    if not rows:
        return []

    any_gap = any((r.get('since_us_days') or 0) > 0 for r in rows)
    L = ["\n## Global Tape (overnight / cross-asset)",
         "_How the rest of the world traded into the US week. Asia closes before the US "
         "opens, so these lead; FX and commodities set the backdrop. Two columns: **Wk %** "
         "is the move over the SAME week as our US numbers (comparable); **Since US close** "
         "is the move in sessions that traded AFTER the US last print — un-priced into this "
         "issue, so it's the forward tell for the next US session (blank when there were "
         "none). Read the path, not just the endpoint, and weave the standouts into the "
         "macro narrative — do not dump the whole table._\n"]
    if any_gap:
        L.append(f"_(The US last traded {snap}; foreign markets that kept trading after it "
                 f"show a 'Since US close' move — treat that as the lead into the next US "
                 f"session, not stale history.)_\n")

    last_cat = None
    L.append("| Market | Last | Wk % (US-aligned) | Since US close | Why it matters for US stocks |"
             "\n|---|---|---|---|---|")
    for r in rows:
        cat = r.get('category')
        if cat != last_cat:
            L.append(f"| **{CATEGORY_LABEL.get(cat, cat)}** | | | | |")
            last_cat = cat
        wk = num(r.get('pct_chg_wk'))
        wk_txt = f'{wk:+.1f}%' if wk is not None else 'n/a'
        su, sud = num(r.get('since_us_pct')), (r.get('since_us_days') or 0)
        su_txt = f'{su:+.1f}% ({sud}d)' if (su is not None and sud > 0) else '—'
        flag = ' ⚠' if r.get('is_notable') else ''
        tell = GLOBAL_TELL.get(r['symbol'], '')
        L.append(f"| {r['name']}{flag} | {_fmt_level(num(r.get('last_close')))} | {wk_txt} "
                 f"| {su_txt} | {tell} |")

    movers = [r for r in rows if r.get('is_notable')]
    if movers:
        L.append("\n**Notable moves this week (⚠ — explanatory headlines are in the Global News "
                 "input; say WHAT moved, WHY, and what it means for the US group named):**")
        for r in movers:
            wk = num(r.get('pct_chg_wk'))
            su, sud = num(r.get('since_us_pct')), (r.get('since_us_days') or 0)
            parts = []
            if wk is not None:
                parts.append(f"{wk:+.1f}% on the US week")
            if su is not None and sud > 0:
                parts.append(f"{su:+.1f}% in {sud} session(s) since the US close")
            L.append(f"- **{r['name']}**: {', '.join(parts)}. {GLOBAL_TELL.get(r['symbol'], '')}")
    return L


def rates_macro_section(latest):
    """Treasury yields + 2s10s + inflation/jobs/Fed prints (from macro_indicators),
    plus next week's recurring economic calendar. Each part renders only if present."""
    L = []
    d = (sb.table('macro_indicators').select('*')
           .order('snapshot_date', desc=True).limit(1).execute().data)
    if d:
        m = d[0]

        def bp(x):
            return f"{int(x):+d}bp" if x is not None else "—"

        if m.get('ust_10y') is not None:
            sp = m.get('spread_10y_2y')
            curve = ("inverted" if sp < 0 else "positive/steepening") if sp is not None else ""
            L.append(f"- **Treasury yields:** 10-year {m['ust_10y']}% ({bp(m.get('ust_10y_chg_bp'))} "
                     f"on the week), 2-year {m.get('ust_2y')}% ({bp(m.get('ust_2y_chg_bp'))}); "
                     f"2s10s spread {int(sp) if sp is not None else '—'}bp ({curve}).")
        if m.get('cpi_yoy') is not None:
            core = m.get('core_cpi_yoy')
            L.append(f"- **Inflation:** CPI {m['cpi_yoy']}% YoY"
                     + (f", core {core}% YoY." if core is not None else "."))
        if m.get('unemployment') is not None:
            nf = num(m.get('nonfarm_chg_k'))
            # Defensive: suppress an implausible payrolls print (>|600k| monthly) even if a
            # stale bad row survives in the DB — mirrors the gate in fetch_macro_data.py so a
            # data glitch (e.g. an AV +741k) never reaches the reader as a "hot" number.
            if nf is not None and abs(nf) > 600:
                nf = None
            L.append(f"- **Jobs:** unemployment {m['unemployment']}%; last nonfarm payrolls "
                     f"{('%+d k' % int(nf)) if nf is not None else 'n/a'}.")
        if m.get('fed_funds') is not None:
            L.append(f"- **Fed funds (target upper):** {m['fed_funds']}%.")
        if L:
            L.append(f"\n_Rates/macro as of {m['snapshot_date']} (US Treasury / BLS)._")

    cal = econ_calendar(latest)
    if cal:
        L.append("\n**Scheduled next week (recurring releases):**")
        for dt, name, tell in cal:
            L.append(f"- {dt}: {name} — *{tell}*")

    return (["\n## Rates & Macro Data"] + L) if L else []


def theme_of_week_section(latest, week_ago, meta):
    """Rotating 'Theme of the Week' — the raw material for the section-5 teaching
    deep-dive, pulled from the sector platform (themes / theme_nodes / theme_members).
    One theme per week, chosen deterministically by ISO week number (cycles through
    all themes, never back-to-back), overridable via NEWSLETTER_THEME=<slug>.
    Emits the value chain (each link's companies + median 0-100 momentum_score, hottest
    link flagged), theme momentum vs a week ago, and one/two standout names. It is a
    curated EDUCATIONAL spotlight — the themes cover only part of the ~1,900 universe
    and this is NOT a sector-rotation read. Returns [] if the theme tables are empty."""
    themes = paginate('themes', 'slug, name, description, display_order', ['display_order'])
    if not themes:
        return []
    override = os.environ.get('NEWSLETTER_THEME', '').strip().lower()
    t = next((x for x in themes if x['slug'] == override), None) if override else None
    if t is None:
        wk = date.fromisoformat(latest).isocalendar()[1]        # ISO week number
        t = themes[wk % len(themes)]
    slug = t['slug']

    nodes = paginate('theme_nodes', 'node_key, name, short_label, layer', ['layer', 'node_key'],
                     filters=[lambda q: q.eq('theme_slug', slug)])
    members = paginate('theme_members', 'node_key, ticker, is_context', ['node_key'],
                       filters=[lambda q: q.eq('theme_slug', slug)])
    core = [m for m in members if not m.get('is_context')]
    tickers = sorted({m['ticker'] for m in core})
    if not tickers:
        return []

    srows = (sb.table('daily_stock_snapshots')
             .select('ticker, snapshot_date, rs_rank, momentum_score')
             .in_('ticker', tickers).in_('snapshot_date', [latest, week_ago]).execute().data or [])
    mom_now, mom_prev, rank = {}, {}, {}
    for r in srows:
        if r['snapshot_date'] == latest:
            mom_now[r['ticker']] = num(r.get('momentum_score'))
            rank[r['ticker']] = r.get('rs_rank')
        else:
            mom_prev[r['ticker']] = num(r.get('momentum_score'))

    frows = (sb.table('stock_fundamentals')
             .select('ticker, price_to_sales, revenue_growth_yoy')
             .in_('ticker', tickers).execute().data or [])
    fund = {r['ticker']: r for r in frows}
    gaps = {}
    for r in frows:                                   # growth-adjusted P/S; needs positive growth
        ps, g = num(r.get('price_to_sales')), num(r.get('revenue_growth_yoy'))
        if ps and ps > 0 and g is not None and g > 0:
            gaps[r['ticker']] = ps / (max(g, 3) / 100)

    theme_now = _median([mom_now.get(m['ticker']) for m in core])
    theme_prev = _median([mom_prev.get(m['ticker']) for m in core])
    node_tks = {nd['node_key']: [m['ticker'] for m in core if m['node_key'] == nd['node_key']]
                for nd in nodes}
    node_mom = {k: _median([mom_now.get(x) for x in tks]) for k, tks in node_tks.items()}
    hottest = max((k for k in node_mom if node_mom[k] is not None),
                  key=lambda k: node_mom[k], default=None)
    top_tk = max((x for x in tickers if mom_now.get(x) is not None),
                 key=lambda x: mom_now[x], default=None)
    val_tk = min(gaps, key=lambda x: gaps[x], default=None)

    def nm(tk):
        return (meta.get(tk) or {}).get('company_name', tk)

    trend = 'steady'
    if theme_now is not None and theme_prev is not None:
        d = theme_now - theme_prev
        trend = 'heating up' if d >= 3 else 'cooling' if d <= -3 else 'steady'

    L = [f"\n## Theme of the Week — {t['name']}",
         f"_{t.get('description', '')}_",
         "_Rotating educational spotlight for the section-5 teaching deep-dive. The platform maps "
         "value-chain themes covering only PART of the ~1,900 universe — this is this week's lesson, "
         "NOT a coverage claim and NOT a sector-rotation call (sector rotation is the broad-market "
         "read elsewhere; keep them distinct)._\n"]
    if theme_now is not None:
        prev_txt = f"{theme_prev:.0f}" if theme_prev is not None else "n/a"
        L.append(f"**Theme momentum (median member momentum score, 0-100):** {theme_now:.0f} now vs "
                 f"{prev_txt} a week ago — {trend}. (The momentum score blends RS-rank level & trend, "
                 f"EMA-9/20 slopes and price vs the 50-day; 50 = middle of the universe.)")
    L.append("\n**The value chain — where money is flowing (link → companies → node momentum); "
             "🔥 marks the hottest link:**")
    L.append("| Link | Companies | Node momentum |\n|---|---|---|")
    for nd in nodes:
        tks = node_tks.get(nd['node_key'], [])
        if not tks:
            continue
        shown = ", ".join(tks[:6]) + (" …" if len(tks) > 6 else "")
        mm = node_mom.get(nd['node_key'])
        flag = " 🔥" if nd['node_key'] == hottest else ""
        L.append(f"| {(nd.get('short_label') or nd['name'])}{flag} | {shown} | "
                 f"{('%.0f/100' % mm) if mm is not None else '—'} |")
    stand = []
    if top_tk is not None:
        ms = mom_now[top_tk]
        rs_txt = f", RS #{rank[top_tk]} of ~1,900" if rank.get(top_tk) else ""
        # Honest framing: only crown a "strongest" if it's actually strong. In a weak theme the
        # top member is a relative leader only — say so (a #1,700 name is not "strongest").
        if ms >= 55:
            stand.append(f"- Strongest momentum: {top_tk} ({nm(top_tk)}) — momentum {ms:.0f}/100{rs_txt}.")
        elif ms <= 45:
            stand.append(f"- Relative leader only — the whole chain is weak: {top_tk} ({nm(top_tk)}) — "
                         f"momentum {ms:.0f}/100{rs_txt}; no member is in a genuine uptrend.")
        else:
            stand.append(f"- Leads the theme (middling at best): {top_tk} ({nm(top_tk)}) — "
                         f"momentum {ms:.0f}/100{rs_txt}.")
    if val_tk is not None:
        ps, g = num(fund[val_tk].get('price_to_sales')), num(fund[val_tk].get('revenue_growth_yoy'))
        stand.append(f"- Cheapest on growth-adjusted sales (a screen, not advice): {val_tk} ({nm(val_tk)}) "
                     f"— P/S {ps:.1f}, revenue growth {g:.0f}% YoY.")
    if stand:
        L.append("\n**Standouts:**")
        L += stand
    L.append(f"\n_Theme rotates weekly by ISO week ({t['name']}); override with NEWSLETTER_THEME=<slug>._")
    return L


def main():
    # Dates from market_health (one row per date — cheap).
    mh = sorted(paginate('market_health', '*', ['snapshot_date']),
                key=lambda r: r['snapshot_date'])
    if not mh:
        sys.exit('market_health is empty — run compute_market_health.py first')
    dates = [r['snapshot_date'] for r in mh]
    mh_by_date = {r['snapshot_date']: r for r in mh}
    latest = dates[-1]
    week_ago = previous_week_close(dates)        # last trading day of the prior week
    health = mh[-1]

    # Reference data.
    meta = {r['ticker']: r for r in paginate('us_stock_sectors',
            'ticker, company_name, sector, industry', ['ticker'])}
    conv = {r['ticker']: r for r in paginate('conviction_scores',
            'ticker, grade, score', ['ticker'])}
    sect = {r['peer_group']: r for r in paginate('sector_rankings',
            'peer_group, composite_rank, breadth, rrg_quadrant', ['peer_group'],
            filters=[lambda q: q.eq('snapshot_date', latest)])}
    # Market cap for the "two clocks" mega-cap block: the RS board ranks by relative
    # strength and hides already-strong mega-caps, so a cap-weighted view is the
    # corrective that shows who actually drove the index.
    fund_cap = {r['ticker']: num(r.get('market_cap_usd'))
                for r in paginate('stock_fundamentals', 'ticker, market_cap_usd', ['ticker'])}
    # Latest cross-asset tape (dollar / gold / junk bonds) for the fear panel.
    gsnap = (sb.table('global_indices').select('snapshot_date')
             .order('snapshot_date', desc=True).limit(1).execute().data)
    gi = {}
    if gsnap:
        gi = {r['symbol']: r for r in (sb.table('global_indices').select('*')
              .eq('snapshot_date', gsnap[0]['snapshot_date']).execute().data or [])}

    # Two-date snapshot slice.
    snaps = paginate('daily_stock_snapshots',
                     'ticker, snapshot_date, rs_rank, close, ema_20, ema_50, '
                     'ema_200, peer_group, high_52w, vol_ratio',
                     ['snapshot_date', 'ticker'],
                     filters=[lambda q: q.in_('snapshot_date', [latest, week_ago])])
    now, prev = {}, {}
    for r in snaps:
        if r['ticker'] in NON_STOCKS:
            continue
        (now if r['snapshot_date'] == latest else prev)[r['ticker']] = r

    def rk(r):
        return r.get('rs_rank')

    def wkpx(tk):
        """Weekly price % for a ticker (last-week close → this-week close). The
        second 'clock' next to rank: price can rally while rank lags (money exiting
        into strength) — the two-clocks read the RS board alone can't show."""
        return _pct((now.get(tk) or {}).get('close'), (prev.get(tk) or {}).get('close'))

    ranked = [r for r in now.values() if rk(r) is not None]

    # ── RS Movers ──────────────────────────────────────────────────────────
    strongest = sorted(ranked, key=rk)[:TOP_N]

    climbers = []
    for tk, r in now.items():
        nr, pr = rk(r), (prev.get(tk) or {}).get('rs_rank')
        if nr is None or pr is None:
            continue
        delta = pr - nr
        if delta > 0 and nr <= 400:        # meaningful climbers only
            climbers.append((tk, pr, nr, delta))
    climbers.sort(key=lambda x: x[3], reverse=True)
    climbers = climbers[:TOP_N]

    # ── Two clocks: price vs rank ──────────────────────────────────────────
    # (a) Mega-caps by market cap — the index-driver view the RS board hides.
    megacaps = []
    for tk in sorted((t for t in now if fund_cap.get(t) and rk(now[t]) is not None),
                     key=lambda t: fund_cap[t], reverse=True)[:MEGACAP_N]:
        nr = rk(now[tk])
        pr = (prev.get(tk) or {}).get('rs_rank')
        megacaps.append({'tk': tk, 'wk': wkpx(tk), 'rank': nr,
                         'delta': (pr - nr) if pr is not None else None})
    # (b) Divergences — price rallied but rank did not confirm ("exiting into strength").
    diverge = []
    for tk, r in now.items():
        nr, wp = rk(r), wkpx(tk)
        pr = (prev.get(tk) or {}).get('rs_rank')
        if nr is None or pr is None or wp is None:
            continue
        rank_delta = pr - nr                       # >0 = rank improving
        if wp >= DIVERGE_MIN_PX and (rank_delta <= 0 or nr > DIVERGE_WEAK_RANK):
            diverge.append({'tk': tk, 'wk': wp, 'rank': nr, 'delta': rank_delta})
    diverge.sort(key=lambda d: d['wk'], reverse=True)
    diverge = diverge[:DIVERGE_N]

    # ── Watchlist (4 filters) ──────────────────────────────────────────────
    watch = []
    for tk, r in now.items():
        nr = rk(r)
        pr = (prev.get(tk) or {}).get('rs_rank')
        if nr is None or pr is None:
            continue
        pg = r.get('peer_group')
        s = sect.get(pg)
        c, e20, e50, e200 = (num(r['close']), num(r['ema_20']),
                             num(r['ema_50']), num(r['ema_200']))
        if not all((c, e20, e50, e200)):
            continue
        # 1. strong sector  2. rank climbing  3. genuine uptrend  4. not stretched
        strong_sector = (s and s.get('composite_rank') is not None
                         and s['composite_rank'] <= SECTOR_TOP_RANK
                         and num(s.get('breadth')) is not None
                         and num(s['breadth']) >= SECTOR_MIN_BREADTH)
        climbing = (pr - nr) > 0
        uptrend = c > e20 > e50 > e200
        stretch = (c / e20 - 1) * 100
        not_stretched = 0 <= stretch <= MAX_STRETCH_PCT
        if strong_sector and climbing and uptrend and not_stretched:
            watch.append({'tk': tk, 'rank': nr, 'delta': pr - nr,
                          'stretch': stretch, 'grade': (conv.get(tk) or {}).get('grade')})
    watch.sort(key=lambda w: w['rank'])
    watch = watch[:WATCHLIST_N]

    # ── Render markdown ────────────────────────────────────────────────────
    def name(tk):
        return (meta.get(tk) or {}).get('company_name', tk)

    def ind(tk):
        m = meta.get(tk) or {}
        return m.get('industry') or m.get('sector') or '—'

    L = []
    L.append(f"# Weekly Data Pack — {latest}")
    L.append(f"_Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · "
             f"universe {health.get('universe_count')} · week-over-week vs prior-week close {week_ago}_\n")

    # Where We Stand
    L.append("## Where We Stand")
    nh, nl = health.get('new_highs'), health.get('new_lows')
    # Record-high flag: compare this week's S&P close to the highest close on record
    # (in our market_health history). The absolute state of the tape — a record vs a
    # drawdown — is the first thing to say, not just where breadth sits.
    sp_hist = [(r['snapshot_date'], num(r.get('sp_close'))) for r in mh if num(r.get('sp_close')) is not None]
    sp_now = num(health.get('sp_close'))
    sp_max, sp_max_date = (max(sp_hist, key=lambda x: x[1])[1], max(sp_hist, key=lambda x: x[1])[0]) if sp_hist else (None, None)
    sp_record = sp_now is not None and sp_max is not None and sp_now >= sp_max - 1e-9
    from_high = _pct(sp_now, sp_max)
    sp_note = ("RECORD high (highest close on record)" if sp_record
               else (f"{from_high:.1f}% below the {sp_max_date} high" if from_high is not None else ""))

    def gmv(sym):
        r = gi.get(sym)
        return num(r.get('pct_chg_wk')) if r else None

    def _wk(x):
        return f"{x:+.1f}% wk" if x is not None else "n/a"

    gold, junk, dxy = gmv('GC=F'), gmv('HYG'), gmv('DX-Y.NYB')
    checks = [
        ("S&P 500 level", f"{health.get('sp_close')}", sp_note),
        ("S&P 500 vs its 200-day", "ABOVE" if health.get('sp_above_200d') else "below",
         f"{health.get('sp_close')} vs {health.get('sp_ema_200')}"),
        ("Stocks above 200-day", f"{health.get('pct_above_200d')}%", ""),
        ("Stocks above 50-day", f"{health.get('pct_above_50d')}%", ""),
        ("New 52w highs vs lows", f"{nh} vs {nl}", ""),
        ("Advancers vs decliners", f"{health.get('advancers')} vs {health.get('decliners')}", ""),
        ("VIX", f"{health.get('vix_close')}", "calm <20" if num(health.get('vix_close')) and num(health['vix_close']) < 20 else "stress"),
        ("Credit spreads", f"{health.get('credit_spread') if health.get('credit_spread') is not None else 'n/a (FRED pending)'}",
         "widening = stress, tight = calm"),
    ]
    # Cross-asset fear panel — did fear actually spread beyond the VIX? (only if global tape present)
    if gold is not None:
        checks.append(("Gold (safe haven)", _wk(gold), "rallying = fear bid; flat/soft through a shock = calm"))
    if junk is not None:
        checks.append(("High-yield / junk (HYG)", _wk(junk), "falling = credit stress spreading; steady/up = risk-on"))
    if dxy is not None:
        checks.append(("US dollar (DXY)", _wk(dxy), "spiking = flight to safety; soft = risk-on"))
    L.append("| Check | Reading | Note |\n|---|---|---|")
    for a, b, c in checks:
        L.append(f"| {a} | {b} | {c} |")
    L.append("\n_Fear panel: read VIX, credit spreads, gold, junk bonds and the dollar together — "
             "if the index holds a record while none of these blink, a scary headline was noise; "
             "if two or more turn, the tape is starting to hedge._")
    # NH/NL trend — last trading day of each of the last 3 ISO weeks
    trend = [mh_by_date[d] for d in last_dates_per_week(dates, 3)]
    if len(trend) >= 2:
        tr = " → ".join(f"{t['snapshot_date']}: {t['new_highs']}↑/{t['new_lows']}↓" for t in trend)
        L.append(f"\n_New-high/low trend: {tr}_")

    def wp_txt(tk):
        wp = wkpx(tk)
        return f"{wp:+.1f}%" if wp is not None else "—"

    # RS Movers — strongest (Wk % = the price clock next to the rank clock)
    L.append("\n## RS Movers — Top 20 Strongest")
    L.append("| # | Ticker | Company | Industry | Wk % |\n|---|---|---|---|---|")
    for i, r in enumerate(strongest, 1):
        L.append(f"| {i} | {r['ticker']} | {name(r['ticker'])} | {ind(r['ticker'])} | {wp_txt(r['ticker'])} |")

    # RS Movers — climbers
    L.append("\n## RS Movers — Top 20 Weekly Climbers")
    L.append("| Ticker | Sector | Rank: week ago → now | Climb | Wk % |\n|---|---|---|---|---|")
    for tk, pr, nr, delta in climbers:
        L.append(f"| {tk} | {(meta.get(tk) or {}).get('sector','—')} | {pr} → {nr} | +{delta} | {wp_txt(tk)} |")

    # Two clocks — price vs rank. Mega-caps drive the cap-weighted index but the RS
    # board hides them; the divergences flag names rallying on price while rank fades
    # (money exiting into strength). Lets the writer separate index return from board
    # leadership instead of calling a group "buried" when it just bounced.
    L.append("\n## Two Clocks — Price vs Rank")
    L.append("_The RS board is a RANK clock; price is a second clock. When a name (or a "
             "mega-cap that moves the index) rises on price while its rank stays weak or "
             "falls, money is using the strength to leave — it can hold support and still "
             "stop leading. Read both columns together; don't call a group 'down' on rank "
             "alone if its price bounced, and don't call the index weak because the board "
             "hides the mega-caps that carried it._\n")
    L.append("**Mega-caps (largest by market cap — the index drivers):**")
    L.append("| Ticker | Company | Wk % | Rank now | Rank Δwk |\n|---|---|---|---|---|")
    for m in megacaps:
        wk = f"{m['wk']:+.1f}%" if m['wk'] is not None else "—"
        dl = (f"+{m['delta']}" if m['delta'] is not None and m['delta'] > 0
              else (str(m['delta']) if m['delta'] is not None else "—"))
        L.append(f"| {m['tk']} | {name(m['tk'])} | {wk} | {m['rank']} | {dl} |")
    if diverge:
        L.append("\n**Divergences — price up, rank not confirming (money exiting into strength):**")
        L.append("| Ticker | Company | Wk % | Rank now | Rank Δwk |\n|---|---|---|---|---|")
        for d in diverge:
            dl = f"+{d['delta']}" if d['delta'] > 0 else str(d['delta'])
            L.append(f"| {d['tk']} | {name(d['tk'])} | {d['wk']:+.1f}% | {d['rank']} | {dl} |")

    # Watchlist
    L.append("\n## Watchlist — Strong sector · climbing · uptrend · not stretched")
    L.append(f"_Filters: peer-group composite_rank ≤ {SECTOR_TOP_RANK} & breadth ≥ "
             f"{SECTOR_MIN_BREADTH}%; rank improved over the week; close > 20 > 50 > 200 EMA; "
             f"≤ {MAX_STRETCH_PCT}% above the 20-day._\n")
    if watch:
        L.append("| Ticker | Company | Sector | Rank (Δwk) | Conviction | % above 20d |\n|---|---|---|---|---|---|")
        for w in watch:
            L.append(f"| {w['tk']} | {name(w['tk'])} | {(meta.get(w['tk']) or {}).get('sector','—')} "
                     f"| {w['rank']} (+{w['delta']}) | {w['grade'] or '—'} | +{w['stretch']:.1f}% |")
    else:
        L.append("_No names passed all four filters this week._")

    # Theme of the Week — rotating educational deep-dive from the sector platform
    L += theme_of_week_section(latest, week_ago, meta)

    # Global tape — overnight/cross-asset context (Asian indices, FX, commodities, BTC)
    L += global_tape_section(latest)

    # Rates & macro data (+ next-week economic calendar)
    L += rates_macro_section(latest)

    # Analyst moves (upgrades / downgrades / initiations this week)
    L += analyst_moves_section(week_ago, latest, meta)

    # Algo (paper portfolio) track record
    L += algo_section(latest, week_ago, now, meta)

    pack = "\n".join(L) + "\n"
    out_path = os.path.join(BASE, 'newsletter_data_pack.md')
    open(out_path, 'w', encoding='utf-8').write(pack)
    # Echo to stdout as UTF-8 bytes (Windows consoles default to cp1252 and choke
    # on the ↑/→ glyphs); the markdown file is the real artifact regardless.
    try:
        sys.stdout.buffer.write(pack.encode('utf-8'))
    except Exception:
        pass
    print(f"\n[data pack written to {out_path}]  strongest={len(strongest)} "
          f"climbers={len(climbers)} watchlist={len(watch)}")


if __name__ == '__main__':
    main()
