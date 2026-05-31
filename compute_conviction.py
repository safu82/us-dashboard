#!/usr/bin/env python3
"""Compute Conviction Score for the whole scan universe and upsert to
conviction_scores. Runs nightly after the analyst_ratings + tipranks_data
refresh.

100-point composite (mirrors the proposal locked in 2026-05-31):
  Technical (30)   : RS Rank 12, EMA Stack 10, 52W high 4, Vol Ratio 4
  Peer Group (10)  : Peer composite rank 6, Stock-in-peer 4
  Fundamentals (15): Rev YoY 5, EPS YoY 5, ROE 5
  Analyst broad (15): yfinance consensus 5, yfinance upside 10
  Top analyst (15) : TipRanks best_consensus 7, TipRanks best_upside 8
  Smart+Sentiment (15): Smart Score 10, Insider 3, Hedge Fund 2

Grade: A >= 80, B >= 65, C >= 45, D < 45.

Single source of truth — both the dashboard and paper_trader.py read
conviction_scores; the formula lives here alone.
"""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY') or os.environ.get('SUPABASE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

sb = create_client(URL, KEY)


def _f(v):
    if v is None: return None
    try:
        x = float(v)
        if x != x: return None  # NaN
        return x
    except (TypeError, ValueError):
        return None


def paginate(table, columns, filters=None, page=1000):
    out, frm = [], 0
    while True:
        q = sb.table(table).select(columns)
        if filters:
            for f in filters:
                q = f(q)
        res = q.range(frm, frm + page - 1).execute()
        if not res.data:
            break
        out.extend(res.data)
        if len(res.data) < page:
            break
        frm += page
    return out


def compute_score(snap, fund, sec_rank, ar, live_price=None):
    """Return (score, grade, breakdown).

    snap: row from daily_stock_snapshots (latest for the ticker), or None
    fund: row from stock_fundamentals, or None
    sec_rank: row from sector_rankings keyed by peer_group, or None
    ar: row from analyst_ratings (joined yfinance + TipRanks), or None
    live_price: optional live tick override for upside computations
    """
    breakdown = []

    def add(label, pts, max_pts, detail=None):
        breakdown.append({'label': label, 'pts': round(pts, 1), 'max': max_pts, 'detail': detail})

    cmp_ = live_price or (snap and _f(snap.get('close')))

    # ── 1. RS Rank (12) ────────────────────────────────────────────────────
    rs_rank = snap and snap.get('rs_rank')
    if rs_rank is not None:
        rs = int(rs_rank)
        pts = 12 if rs <= 50 else 9 if rs <= 125 else 5 if rs <= 250 else 1
        add('RS Rank', pts, 12, f'#{rs}')
    else:
        add('RS Rank', 0, 12, '—')

    # ── 2. EMA Stack (10) ──────────────────────────────────────────────────
    e20, e50, e200 = _f(snap and snap.get('ema_20')), _f(snap and snap.get('ema_50')), _f(snap and snap.get('ema_200'))
    if cmp_ and e20 and e50 and e200:
        full_bull = cmp_ > e20 > e50 > e200
        above_200 = cmp_ > e200
        pts = 10 if full_bull else 5 if above_200 else 0
        detail = 'Fully bullish' if full_bull else 'Above 200' if above_200 else 'Below 200'
        add('EMA Stack', pts, 10, detail)
    else:
        add('EMA Stack', 0, 10, '—')

    # ── 3. Distance from 52W high (4) ──────────────────────────────────────
    h52 = _f(snap and snap.get('high_52w'))
    if cmp_ and h52:
        dist = (cmp_ - h52) / h52 * 100
        pts = 4 if dist >= -3 else 3 if dist >= -10 else 1 if dist >= -20 else 0
        add('52W High', pts, 4, f'{dist:+.1f}%')
    else:
        add('52W High', 0, 4, '—')

    # ── 4. Volume Ratio (4) ────────────────────────────────────────────────
    vr = _f(snap and snap.get('vol_ratio'))
    if vr is not None:
        pts = 4 if vr >= 2 else 3 if vr >= 1.5 else 2 if vr >= 1 else 0
        add('Vol Ratio', pts, 4, f'{vr:.2f}x')
    else:
        add('Vol Ratio', 0, 4, '—')

    # ── 5. Peer composite rank (6) ─────────────────────────────────────────
    if sec_rank:
        rank = sec_rank.get('composite_rank')
        # Lower rank = stronger peer group
        if rank is not None:
            r = int(rank)
            pts = 6 if r <= 5 else 5 if r <= 12 else 3 if r <= 25 else 1
            quad = sec_rank.get('rrg_quadrant') or ''
            add('Peer Group Rank', pts, 6, f'#{r} ({quad})')
        else:
            add('Peer Group Rank', 0, 6, '—')
    else:
        add('Peer Group Rank', 0, 6, '—')

    # ── 6. Stock vs peer percentile (4) ────────────────────────────────────
    pp = snap and snap.get('peer_percentile')
    if pp is not None:
        p = int(pp)
        # Higher percentile = stronger relative to peers
        pts = 4 if p >= 90 else 3 if p >= 75 else 2 if p >= 50 else 0
        add('Stock vs Peers', pts, 4, f'{p}th pct')
    else:
        add('Stock vs Peers', 0, 4, '—')

    # ── 7. Revenue Growth YoY (5) ──────────────────────────────────────────
    rg = _f(fund and fund.get('revenue_growth_yoy'))
    if rg is not None:
        pts = 5 if rg >= 20 else 3 if rg >= 10 else 1 if rg >= 0 else 0
        add('Rev Growth', pts, 5, f'{rg:+.1f}%')
    else:
        add('Rev Growth', 0, 5, '—')

    # ── 8. EPS Growth YoY (5) ──────────────────────────────────────────────
    eg = _f(fund and fund.get('earnings_growth_yoy'))
    if eg is not None:
        pts = 5 if eg >= 20 else 3 if eg >= 10 else 1 if eg >= 0 else 0
        add('EPS Growth', pts, 5, f'{eg:+.1f}%')
    else:
        add('EPS Growth', 0, 5, '—')

    # ── 9. ROE (5) ─────────────────────────────────────────────────────────
    roe = _f(fund and fund.get('roe'))
    if roe is not None:
        # yfinance returns ROE as percent (e.g. 114 for NVDA)
        pts = 5 if roe >= 20 else 3 if roe >= 10 else 0
        add('ROE', pts, 5, f'{roe:.1f}%')
    else:
        add('ROE', 0, 5, '—')

    # ── 10. yfinance broad consensus (5) ───────────────────────────────────
    yc = (ar and ar.get('consensus') or '').lower()
    yc_pts_map = {'strong_buy': 5, 'buy': 4, 'hold': 2, 'underperform': 1, 'sell': 0, 'strong_sell': 0}
    yc_pts = yc_pts_map.get(yc, 0)
    add('yfinance Consensus', yc_pts, 5, yc.replace('_', ' ').upper() if yc else '—')

    # ── 11. yfinance broad upside (10) ─────────────────────────────────────
    tgt = _f(ar and ar.get('target_mean'))
    if tgt and cmp_:
        upside = (tgt / cmp_ - 1) * 100
        pts = 10 if upside >= 30 else 8 if upside >= 15 else 5 if upside >= 5 else 2 if upside >= 0 else 0
        add('yf Upside %', pts, 10, f'{upside:+.1f}%')
    else:
        add('yf Upside %', 0, 10, '—')

    # ── 12. TipRanks top-analyst consensus (7) ─────────────────────────────
    bc_raw = (ar and ar.get('best_consensus') or '')
    bc = bc_raw.lower().replace(' ', '')
    bc_pts_map = {'strongbuy': 7, 'buy': 5, 'hold': 3, 'sell': 0, 'strongsell': 0}
    bc_pts = bc_pts_map.get(bc, 0)
    add('Top Analyst Consensus', bc_pts, 7, bc_raw if bc_raw else '—')

    # ── 13. TipRanks top-analyst upside (8) ────────────────────────────────
    bu = _f(ar and ar.get('best_target_upside_pct'))
    if bu is not None:
        pts = 8 if bu >= 30 else 6 if bu >= 15 else 4 if bu >= 5 else 2 if bu >= 0 else 0
        add('Top Analyst Upside', pts, 8, f'{bu:+.1f}%')
    else:
        add('Top Analyst Upside', 0, 8, '—')

    # ── 14. Smart Score (10) ───────────────────────────────────────────────
    ss = _f(ar and ar.get('smart_score'))
    if ss is not None:
        # TipRanks Smart Score is 1-10. Linear mapping: 10 -> 10pts, 1 -> 1pt.
        pts = max(0, min(10, ss))
        add('Smart Score', pts, 10, f'{int(ss)}/10')
    else:
        add('Smart Score', 0, 10, '—')

    # ── 15. Insider Sentiment (3) ──────────────────────────────────────────
    ins = (ar and ar.get('insider_rating') or '').lower()
    ins_pts = 3 if ins == 'buy' else 2 if ins == 'hold' else 0
    add('Insider', ins_pts, 3, ins.upper() if ins else '—')

    # ── 16. Hedge Fund Trend (2) ───────────────────────────────────────────
    hf = (ar and ar.get('hedge_fund_rating') or '').lower()
    hf_pts = 2 if hf == 'buy' else 1 if hf == 'hold' else 0
    add('Hedge Fund', hf_pts, 2, hf.upper() if hf else '—')

    score = round(sum(b['pts'] for b in breakdown), 1)
    grade = 'A' if score >= 80 else 'B' if score >= 65 else 'C' if score >= 45 else 'D'
    return score, grade, breakdown


def main():
    print('=' * 60)
    print('COMPUTE CONVICTION  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 60)

    # Latest snapshot per ticker — pull latest date first, then all rows for it
    latest = sb.table('daily_stock_snapshots').select('snapshot_date') \
        .order('snapshot_date', desc=True).limit(1).execute()
    if not latest.data:
        sys.exit('No snapshots found')
    snap_date = latest.data[0]['snapshot_date']
    print(f'Using snapshot_date: {snap_date}')

    snaps = paginate('daily_stock_snapshots',
                     'ticker, close, ema_20, ema_50, ema_200, rs_rank, high_52w, vol_ratio, peer_group, peer_percentile',
                     filters=[lambda q: q.eq('snapshot_date', snap_date)])
    snap_by_ticker = {r['ticker']: r for r in snaps}

    funds = paginate('stock_fundamentals',
                     'ticker, revenue_growth_yoy, earnings_growth_yoy, roe')
    fund_by_ticker = {r['ticker']: r for r in funds}

    sec_rows = paginate('sector_rankings',
                        'peer_group, composite_rank, rrg_quadrant, snapshot_date',
                        filters=[lambda q: q.eq('snapshot_date', snap_date)])
    sec_by_pg = {r['peer_group']: r for r in sec_rows}

    ars = paginate('analyst_ratings',
                   'ticker, consensus, target_mean, best_consensus, best_target_upside_pct, smart_score, insider_rating, hedge_fund_rating')
    ar_by_ticker = {r['ticker']: r for r in ars}

    print(f'  snapshots: {len(snap_by_ticker)}  fundamentals: {len(fund_by_ticker)}'
          f'  peer-groups: {len(sec_by_pg)}  analyst rows: {len(ar_by_ticker)}')

    rows = []
    grade_counts = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
    for tk, snap in snap_by_ticker.items():
        fund = fund_by_ticker.get(tk)
        sec  = sec_by_pg.get(snap.get('peer_group'))
        ar   = ar_by_ticker.get(tk)
        score, grade, breakdown = compute_score(snap, fund, sec, ar)
        grade_counts[grade] += 1
        rows.append({
            'ticker': tk,
            'score': score,
            'grade': grade,
            'breakdown': breakdown,
            'snapshot_date': snap_date,
            'last_updated': datetime.now(timezone.utc).isoformat(),
        })

    # Bulk upsert
    inserted = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        sb.table('conviction_scores').upsert(chunk, on_conflict='ticker').execute()
        inserted += len(chunk)
    print(f'Upserted {inserted} conviction_scores rows')
    print(f'Grades: A {grade_counts["A"]} · B {grade_counts["B"]} · C {grade_counts["C"]} · D {grade_counts["D"]}')


if __name__ == '__main__':
    main()
