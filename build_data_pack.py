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
    checks = [
        ("S&P 500 vs its 200-day", "ABOVE" if health.get('sp_above_200d') else "below",
         f"{health.get('sp_close')} vs {health.get('sp_ema_200')}"),
        ("Stocks above 200-day", f"{health.get('pct_above_200d')}%", ""),
        ("Stocks above 50-day", f"{health.get('pct_above_50d')}%", ""),
        ("New 52w highs vs lows", f"{nh} vs {nl}", ""),
        ("Advancers vs decliners", f"{health.get('advancers')} vs {health.get('decliners')}", ""),
        ("VIX", f"{health.get('vix_close')}", "calm <20" if num(health.get('vix_close')) and num(health['vix_close']) < 20 else "stress"),
        ("Credit spreads", f"{health.get('credit_spread') if health.get('credit_spread') is not None else 'n/a (FRED pending)'}", ""),
    ]
    L.append("| Check | Reading | Note |\n|---|---|---|")
    for a, b, c in checks:
        L.append(f"| {a} | {b} | {c} |")
    # NH/NL trend — last trading day of each of the last 3 ISO weeks
    trend = [mh_by_date[d] for d in last_dates_per_week(dates, 3)]
    if len(trend) >= 2:
        tr = " → ".join(f"{t['snapshot_date']}: {t['new_highs']}↑/{t['new_lows']}↓" for t in trend)
        L.append(f"\n_New-high/low trend: {tr}_")

    # RS Movers — strongest
    L.append("\n## RS Movers — Top 20 Strongest")
    L.append("| # | Ticker | Company | Industry |\n|---|---|---|---|")
    for i, r in enumerate(strongest, 1):
        L.append(f"| {i} | {r['ticker']} | {name(r['ticker'])} | {ind(r['ticker'])} |")

    # RS Movers — climbers
    L.append("\n## RS Movers — Top 20 Weekly Climbers")
    L.append("| Ticker | Sector | Rank: week ago → now | Climb |\n|---|---|---|---|")
    for tk, pr, nr, delta in climbers:
        L.append(f"| {tk} | {(meta.get(tk) or {}).get('sector','—')} | {pr} → {nr} | +{delta} |")

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
