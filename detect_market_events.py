#!/usr/bin/env python3
"""Market-event detector — the 'smart' core of the news engine.

Most newsletters scan a news firehose and hope the important items surface. We
invert that: we already KNOW what moved (1,900-stock rankings, price/volume,
sector rotation, breadth, VIX, earnings reactions). So we detect the events that
actually moved the market from our OWN data, score them by magnitude x breadth,
and emit a ranked list with a suggested news query for each. The news layer then
fetches articles to EXPLAIN these events, instead of curating from scratch.

Output: market_events.json (for the news/synthesis layers) + a ranked print.
Read-only. No external dependency / API key.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (.env auto-loaded).
"""

import json
import os
import sys
from datetime import datetime, timezone, date, timedelta

from supabase import create_client
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

sb = create_client(URL, KEY)

try:                                  # Windows consoles default to cp1252; our
    sys.stdout.reconfigure(encoding='utf-8')   # headlines use → / arrows.
except Exception:
    pass

NON_STOCKS = {'^GSPC', '^VIX', 'QQQ'}
SP = '^GSPC'
WEEK_TD = 5


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
    """Last trading day of the most recent EARLIER calendar (ISO) week — the
    correct week-over-week baseline for a weekly report. Handles holidays and
    non-Friday runs (unlike a fixed 'N trading days back')."""
    latest_wk = date.fromisoformat(dates[-1]).isocalendar()[:2]
    prior = [d for d in dates if date.fromisoformat(d).isocalendar()[:2] < latest_wk]
    return prior[-1] if prior else dates[0]


def main():
    mh = sorted(paginate('market_health', '*', ['snapshot_date']),
                key=lambda r: r['snapshot_date'])
    if len(mh) < 2:
        sys.exit('need >=2 market_health rows')
    dates = [r['snapshot_date'] for r in mh]
    mh_by_date = {r['snapshot_date']: r for r in mh}
    latest = dates[-1]
    week_ago = previous_week_close(dates)        # last trading day of the prior week
    now_h, prev_h = mh_by_date[latest], mh_by_date[week_ago]

    meta = {r['ticker']: r for r in paginate('us_stock_sectors',
            'ticker, company_name, sector, industry', ['ticker'])}

    # Two-date snapshot slice (keep ^GSPC for the index move).
    snaps = paginate('daily_stock_snapshots',
                     'ticker, snapshot_date, rs_rank, close, vol_ratio',
                     ['snapshot_date', 'ticker'],
                     filters=[lambda q: q.in_('snapshot_date', [latest, week_ago])])
    now, prev = {}, {}
    for r in snaps:
        (now if r['snapshot_date'] == latest else prev)[r['ticker']] = r

    # Sector ranks, two dates.
    srows = paginate('sector_rankings',
                     'peer_group, snapshot_date, composite_rank, breadth, rrg_quadrant',
                     ['snapshot_date', 'peer_group'],
                     filters=[lambda q: q.in_('snapshot_date', [latest, week_ago])])
    s_now, s_prev = {}, {}
    for r in srows:
        (s_now if r['snapshot_date'] == latest else s_prev)[r['peer_group']] = r

    events = []

    def add(kind, scope, score, headline, news_query, extra=None):
        events.append({'kind': kind, 'scope': scope, 'impact_score': round(score, 1),
                       'headline': headline, 'news_query': news_query, **(extra or {})})

    # ── Macro / regime (market-wide => highest base scores) ─────────────────
    sp_now, sp_prev = now.get(SP, {}), prev.get(SP, {})
    if num(sp_now.get('close')) and num(sp_prev.get('close')):
        wk = (num(sp_now['close']) / num(sp_prev['close']) - 1) * 100
        add('index', 'market', 88,
            f"S&P 500 {wk:+.1f}% on the week (close {sp_now['close']}, "
            f"{'above' if now_h.get('sp_above_200d') else 'below'} its 200-day)",
            "S&P 500 stock market this week", {'value': round(wk, 2)})

    nh, nl, pnh, pnl = (now_h.get('new_highs'), now_h.get('new_lows'),
                        prev_h.get('new_highs'), prev_h.get('new_lows'))
    if None not in (nh, nl, pnh, pnl) and (abs((nh - nl) - (pnh - pnl)) >= 10):
        add('breadth', 'market', 92,
            f"Breadth shift: new 52w highs vs lows {nh}/{nl} (was {pnh}/{pnl} a week ago)",
            "stock market breadth new highs rally", {'value': nh - nl})

    v_now, v_prev = num(now_h.get('vix_close')), num(prev_h.get('vix_close'))
    if v_now and v_prev and abs(v_now - v_prev) >= 2:
        add('volatility', 'market', 90,
            f"VIX {v_now:.1f} (from {v_prev:.1f}) — {'fear rising' if v_now > v_prev else 'calming'}",
            "VIX volatility stock market", {'value': round(v_now - v_prev, 2)})

    a200, p200 = num(now_h.get('pct_above_200d')), num(prev_h.get('pct_above_200d'))
    if a200 and p200 and abs(a200 - p200) >= 4:
        add('breadth', 'market', 80,
            f"% of stocks above their 200-day: {a200:.0f}% (from {p200:.0f}%)",
            "stock market breadth participation", {'value': round(a200 - p200, 1)})

    # ── Sector rotation (sector-wide => high) ───────────────────────────────
    sect_moves = []
    for pg, r in s_now.items():
        pr = s_prev.get(pg)
        if not pr or r.get('composite_rank') is None or pr.get('composite_rank') is None:
            continue
        delta = pr['composite_rank'] - r['composite_rank']   # + = climbed
        sect_moves.append((pg, pr['composite_rank'], r['composite_rank'], delta,
                           r.get('rrg_quadrant')))
    for pg, pr_, nr_, delta, quad in sorted(sect_moves, key=lambda x: abs(x[3]), reverse=True)[:5]:
        if abs(delta) < 2:
            continue
        verb = 'surged up' if delta > 0 else 'fell down'
        add('sector', 'sector', 60 + min(abs(delta) * 2, 38),
            f"Sector rotation: {pg} {verb} the board ({pr_}→{nr_}, RRG {quad})",
            f"{pg} sector stocks", {'peer_group': pg, 'value': delta})

    # ── Themes: clusters among the top weekly climbers ──────────────────────
    climbers = []
    for tk, r in now.items():
        if tk in NON_STOCKS:
            continue
        nr, prr = r.get('rs_rank'), (prev.get(tk) or {}).get('rs_rank')
        if nr is None or prr is None:
            continue
        if (prr - nr) > 0 and nr <= 400:
            climbers.append((tk, prr - nr, (meta.get(tk) or {}).get('sector')))
    climbers.sort(key=lambda x: x[1], reverse=True)
    sec_counts = {}
    for _, _, sec in climbers[:25]:
        if sec:
            sec_counts[sec] = sec_counts.get(sec, 0) + 1
    for sec, n in sorted(sec_counts.items(), key=lambda x: x[1], reverse=True):
        if n >= 4:
            add('theme', 'sector', 75 + min(n, 10),
                f"Theme: {n} of the top 25 weekly climbers are {sec}",
                f"{sec} sector rally stocks", {'sector': sec, 'value': n})

    # ── Notable single stocks (rank + price moves) ──────────────────────────
    for tk, delta, sec in climbers[:8]:
        add('stock', 'stock', 30 + min(delta / 30, 25),
            f"{meta.get(tk, {}).get('company_name', tk)} ({tk}) jumped {delta} rank spots ({sec})",
            f"{meta.get(tk, {}).get('company_name', tk)} {tk} stock", {'ticker': tk, 'value': delta})

    price_moves = []
    for tk, r in now.items():
        if tk in NON_STOCKS:
            continue
        c, p = num(r.get('close')), num((prev.get(tk) or {}).get('close'))
        if c and p:
            price_moves.append((tk, (c / p - 1) * 100))
    for tk, mv in sorted(price_moves, key=lambda x: abs(x[1]), reverse=True)[:5]:
        add('stock', 'stock', 35 + min(abs(mv), 25),
            f"{meta.get(tk, {}).get('company_name', tk)} ({tk}) {mv:+.0f}% on the week",
            f"{meta.get(tk, {}).get('company_name', tk)} {tk} stock why", {'ticker': tk, 'value': round(mv, 1)})

    # ── Earnings — gated on yfinance earnings dates (stock_fundamentals), the
    # reliable source (TipRanks dates were stale/sparse). 'earnings' = a real
    # report this week with its week-over-week move; 'earnings_upcoming' = reports
    # due next week for the Key-Events calendar. Empty when none — by design.
    ef = {r['ticker']: r for r in paginate(
        'stock_fundamentals', 'ticker, last_earnings_date, next_earnings_date', ['ticker'])}
    upto = (date.fromisoformat(latest) + timedelta(days=8)).isoformat()
    for tk, r in ef.items():
        if tk in NON_STOCKS:
            continue
        nm = meta.get(tk, {}).get('company_name', tk)
        led = r.get('last_earnings_date')
        if led and week_ago < led <= latest:
            c, p = num((now.get(tk) or {}).get('close')), num((prev.get(tk) or {}).get('close'))
            if c and p:
                wk = (c / p - 1) * 100
                add('earnings', 'stock', 58,
                    f"{nm} ({tk}) reported earnings this week, {wk:+.0f}% week-over-week",
                    f"{nm} {tk} earnings", {'ticker': tk, 'value': round(wk, 1)})
        ned = r.get('next_earnings_date')
        if ned and latest < ned <= upto:
            add('earnings_upcoming', 'stock', 45,
                f"{nm} ({tk}) reports earnings next week ({ned})",
                f"{nm} {tk} earnings preview", {'ticker': tk, 'date': ned})

    events.sort(key=lambda e: e['impact_score'], reverse=True)

    out = {'generated': datetime.now(timezone.utc).isoformat(),
           'latest_date': latest, 'week_ago': week_ago, 'events': events}
    out_path = os.path.join(BASE, 'market_events.json')
    open(out_path, 'w', encoding='utf-8').write(json.dumps(out, indent=2))

    print(f"Detected {len(events)} market events ({latest} vs {week_ago}). "
          f"Top by impact x breadth:\n")
    for e in events[:18]:
        print(f"  [{e['impact_score']:>4}] {e['scope']:<7} {e['kind']:<10} | {e['headline']}")
    print(f"\n[written to {out_path}]")


if __name__ == '__main__':
    main()
