#!/usr/bin/env python3
"""
US Paper Trading Executor (Strategy v3 — Conviction Stack)
==========================================================
US adaptation of India's paper_trader.py. Same architecture, USD units.

Entry buckets (priority A > B > C > D; signal_score breaks ties):
    T1_MULTI_STRONG  >=2 distinct entry_signals.pattern with signal_strength='Strong'
                     (patterns from {blue_zone, golden_cross, macd, bull_flag, vcp}).
                     Sizing: 1.5% risk, $6,000 cap.
    T2_STRONG_REG    >=2 distinct families, EXACTLY one strong family + >=1 regular.
                     Sizing: 1.0% risk, $5,000 cap.
    T3_MULTI_REG     >=2 distinct families, no strong.
                     Sizing: 0.66% risk, $3,000 cap.
    T4_RS_ACCEL      presignal rs_acceleration score >= 80 AND any other rule
                     (bz_buy/bz_strong/golden_cross/macd/pullback_bounce) >= 70.
                     Sizing: 0.5% risk, $2,000 cap.

Universal gates:
  - Sector gate: ticker's peer_group must be in the top SECTOR_GATE_TOP_N peer
    groups by composite_rank AND have n_tickers >= 5 AND breadth >= 50%.
  - Earnings filter: latest quarter YoY positive for BOTH revenue and net income.
    Needs >=5 quarters (yfinance cap). Relaxed vs India's 6-quarter "improving"
    check because yfinance.quarterly_income_stmt only returns ~5 quarters.
  - Not in holdings, not already in open/pending paper, 21 trading-day cooldown.
  - Position floor $800, sector concentration <= 25%, max 20 open,
    committed notional <= 95% of sleeve, <= MAX_NEW_PER_DAY new entries/day.
  - Kill switches: pause entries if daily P&L < -2% or DD < -8% from ATH.

Execution model (India parity):
  D0 22:35 UTC — this script writes candidates as PENDING (status='pending')
                 with D0 ATR captured.
  D1 market open onward — yahoo_live_updater.py (Railway worker, every 15s
                 during market hours) reads each pending row, fills at the
                 first live LTP + 15 bps slippage. Same worker also runs
                 intraday stop checks (exit at LTP if LTP <= current_stop) and
                 MFE/MAE updates. paper_fill_pending.py remains as a manual
                 rerun tool only.

Exits:
  - Initial stop: 2 x ATR_14 below entry.
  - T1: 33% at +3R + breakeven, 33% at +6R + 2xATR trail, rest trails.
  - T2/T3/T4: 33% at +2R + breakeven, 33% at +4R + 2xATR trail, rest trails.
  - Universal book-25%: book 25% if up >25% within 15 trading days.
  - Time stop: close if flat (-2%..+2%) after 25 trading days.
  - Early trail arm: at +10% MFE.

Inputs:  entry_signals, presignal_scores, daily_stock_snapshots, holdings,
         us_stock_sectors, sector_rankings, stock_fundamentals,
         paper_trades, paper_equity.
Outputs: paper_trades (insert/update), paper_equity (upsert),
         paper_run_log (insert).
"""

import json
import os
import sys
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta

from supabase import create_client

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except ImportError:
    pass

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY') or os.environ.get('SUPABASE_KEY')
if not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

SLEEVE = 50_000  # $50,000 paper sleeve

COOLDOWN_DAYS = 21

TIER_PARAMS = {
    'T1_MULTI_STRONG': {'risk_pct': 0.015,  'cap': 6_000, 'partial_R': [3, 6]},
    'T2_STRONG_REG':   {'risk_pct': 0.010,  'cap': 5_000, 'partial_R': [2, 4]},
    'T3_MULTI_REG':    {'risk_pct': 0.0066, 'cap': 3_000, 'partial_R': [2, 4]},
    'T4_RS_ACCEL':     {'risk_pct': 0.005,  'cap': 2_000, 'partial_R': [2, 4]},
}
POSITION_FLOOR = 800           # USD; below this the position is too small to bother
SLIPPAGE_BPS = 15
STOP_ATR_MULT = 2.0
TRAIL_ATR_MULT = 2.0
TIME_STOP_DAYS = 25
TIME_STOP_LOW = -2.0
TIME_STOP_HIGH = 2.0
UNIVERSAL_BOOK_DAYS = 15
UNIVERSAL_BOOK_PCT = 25.0
UNIVERSAL_BOOK_QTY_PCT = 25
PARTIAL_QTY_PCT = 33
EARLY_TRAIL_PCT = 10.0
MAX_OPEN_POSITIONS = 20
MAX_DEPLOYED_PCT = 0.95
MAX_SECTOR_CONC = 0.25
MAX_NEW_PER_DAY = 5            # US spec: 5/day (India was 3)
DAILY_KILL_PCT = -2.0
DD_KILL_PCT = -8.0

# Patterns that qualify a Strong signal for bucket A's "strong" set.
# Mirrors India's STRONG_ENTRY_TYPES — Narrow CPR and Darvas don't count
# as "strong" for the multi-strong bucket even if signal_strength='Strong'.
STRONG_BUCKET_A_PATTERNS = {
    'blue_zone', 'golden_cross', 'macd', 'bull_flag', 'vcp',
}

# entry_signals.pattern -> family (for B/C dedupe so a Strong+Buy pair from the
# same family doesn't count as multi-signal)
ENTRY_FAMILY_MAP = {
    'blue_zone':        'BLUE_ZONE',
    'golden_cross':     'GOLDEN_CROSS',
    'macd':             'MACD',
    'bull_flag':        'BULL_FLAG',
    'vcp':              'VCP',
    'narrow_cpr':       'CPR_BREAKAWAY',
    'darvas':           'DARVAS',
    'pullback_bounce':  'PULLBACK',
}

# Pre-signal Bucket D (T4) thresholds — relaxed vs India because the US
# presignal table doesn't carry rank_slope / rs_30d_high columns (those were
# already gated upstream in score_presignals.py — only meaningfully-rising
# names get a high rs_acceleration score in the first place).
RS_ACCEL_RULE = 'rs_acceleration'
RS_ACCEL_REQUIRED_SCORE = 80
RS_ACCEL_OTHER_RULE_MIN = 70
RS_ACCEL_OTHER_RULES = ('bz_buy', 'bz_strong', 'golden_cross', 'macd', 'pullback_bounce')

# Top-N peer groups by composite_rank that count as "tradeable".
SECTOR_GATE_TOP_N = 12  # US has 50 peer groups (39 industries + 11 sectors); top 12 ~ same fraction as India top 6 of 20
SECTOR_GATE_MIN_N = 5         # peer group must have >= 5 tickers
SECTOR_GATE_MIN_BREADTH = 50  # peer group breadth >= 50% (% above EMA50)

# 5 quarters (yfinance cap) is enough for the latest-YoY check.
EARNINGS_MIN_QUARTERS = 5

# Intraday distribution filter: reject candidates whose D0 close sits in the
# bottom N% of the D0 range. A weak close on the signal-day suggests the move
# already faded intraday and we'd be entering into distribution. See the
# MRVL 2026-05-27 case (signal day closed at 0.11 of range, then gapped down -9%).
MIN_INTRADAY_CLOSE_POSITION = 0.30

BENCHMARK_TICKER = '^GSPC'    # market regime + bench column on paper_equity
SECONDARY_BENCHMARK = 'QQQ'


# ----------------------------------------------------------------------
# UTILS
# ----------------------------------------------------------------------

def log(msg):
    print(f"[{datetime.utcnow().isoformat()}Z] {msg}", flush=True)


def paginate(query, batch_size=1000):
    out, off = [], 0
    while True:
        resp = query.range(off, off + batch_size - 1).execute()
        if not resp.data:
            break
        out.extend(resp.data)
        if len(resp.data) < batch_size:
            break
        off += batch_size
    return out


def to_float(v, default=None):
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def to_int(v, default=0):
    if v is None:
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def trading_days_between(start: date, end: date) -> int:
    if end <= start:
        return 0
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


# ----------------------------------------------------------------------
# DATA LOADERS
# ----------------------------------------------------------------------

_BENCHMARK_TICKERS_FOR_DATES = ('AAPL', 'MSFT', 'NVDA', 'AMZN')


def get_recent_trading_dates(sb, days_back=10):
    """Liquid single-ticker lookup avoids the 1000-row pagination cap that would
    silently truncate a multi-ticker date query."""
    for bench in _BENCHMARK_TICKERS_FOR_DATES:
        resp = (sb.table('daily_stock_snapshots')
                  .select('snapshot_date')
                  .eq('ticker', bench)
                  .order('snapshot_date', desc=True)
                  .limit(days_back).execute())
        rows = resp.data or []
        if len(rows) >= days_back:
            return [r['snapshot_date'] for r in rows]
    return []


def load_snapshots_for_date(sb, dt):
    rows = paginate(sb.table('daily_stock_snapshots').select('*').eq('snapshot_date', dt))
    return {r['ticker']: r for r in rows}


def load_presignals(sb, dt_from, dt_to):
    return paginate(
        sb.table('presignal_scores').select('*')
          .gte('snapshot_date', dt_from).lte('snapshot_date', dt_to)
    )


def load_entry_signals(sb, dt_from, dt_to):
    return paginate(
        sb.table('entry_signals').select('*')
          .gte('alert_date', dt_from).lte('alert_date', dt_to)
    )


def load_holdings(sb):
    resp = sb.table('holdings').select('ticker').execute()
    return {r['ticker'] for r in (resp.data or [])}


def load_open_paper_trades(sb):
    resp = (sb.table('paper_trades').select('*')
              .eq('status', 'open').eq('mode', 'paper').execute())
    return resp.data or []


def load_pending_paper_trades(sb):
    resp = (sb.table('paper_trades').select('*')
              .eq('status', 'pending').eq('mode', 'paper').execute())
    return resp.data or []


def load_recent_closed_tickers(sb, today):
    cutoff = (today - timedelta(days=COOLDOWN_DAYS + 7)).isoformat()
    resp = (sb.table('paper_trades').select('ticker, exit_date')
              .eq('status', 'closed').gte('exit_date', cutoff).execute())
    return {r['ticker']: r['exit_date'] for r in (resp.data or [])}


def load_sectors(sb):
    """ticker -> peer_group (using the same rollup the scanners do).
    Reads us_stock_sectors directly; the peer_group choice is encoded by
    the dashboard front-end + compute_cross_sectional. For the paper trader's
    sector-concentration check we use peer_group (industry-or-rolled-up-sector),
    which is what sector_rankings keys on."""
    rows = paginate(sb.table('us_stock_sectors').select('ticker, sector, industry'))
    # Build peer_group exactly as compute_cross_sectional does
    ind_counts = defaultdict(int)
    for r in rows:
        if r.get('industry'):
            ind_counts[r['industry']] += 1
    out = {}
    for r in rows:
        ind = r.get('industry')
        sec = r.get('sector')
        if ind and ind_counts[ind] >= 5:
            out[r['ticker']] = ind
        elif sec:
            out[r['ticker']] = sec
    return out


def load_recent_equity(sb, limit=60):
    resp = (sb.table('paper_equity').select('*')
              .order('snapshot_date', desc=True).limit(limit).execute())
    return resp.data or []


def load_cumulative_closed_pnl(sb):
    resp = sb.table('paper_trades').select('total_pnl').eq('status', 'closed').execute()
    return sum(to_float(r.get('total_pnl'), 0) for r in (resp.data or []))


def load_sector_rankings(sb, today_iso):
    """peer_group -> ranking dict for the latest sector_rankings snapshot.
    No RPC — read the table directly. The gate uses composite_rank +
    n_tickers + breadth (mirrors India's gate_eligible)."""
    try:
        # Latest available snapshot date in sector_rankings
        latest = (sb.table('sector_rankings').select('snapshot_date')
                    .lte('snapshot_date', today_iso)
                    .order('snapshot_date', desc=True).limit(1).execute())
        if not latest.data:
            return {}
        d = latest.data[0]['snapshot_date']
        rows = paginate(
            sb.table('sector_rankings').select('*').eq('snapshot_date', d)
        )
        out = {}
        for r in rows:
            pg = r.get('peer_group')
            if not pg:
                continue
            out[pg] = {
                'composite': to_float(r.get('composite')),
                'composite_rank': r.get('composite_rank'),
                'breadth': to_float(r.get('breadth')),
                'avg_alkalyme_rs': to_float(r.get('avg_alkalyme_rs')),
                'n_tickers': r.get('n_tickers'),
                'rrg_quadrant': r.get('rrg_quadrant'),
                'rs_slope': to_float(r.get('rs_slope')),
                'parent_sector': r.get('parent_sector'),
                'peer_group_type': r.get('peer_group_type'),
            }
        return out
    except Exception as e:
        log(f'load_sector_rankings failed: {e}')
        return {}


def load_fundamentals(sb, tickers):
    """ticker -> quarterly_financials list (newest first). Skips missing/short."""
    out = {}
    if not tickers:
        return out
    tickers = list(tickers)
    BATCH = 100
    for i in range(0, len(tickers), BATCH):
        chunk = tickers[i:i + BATCH]
        try:
            resp = (sb.table('stock_fundamentals')
                      .select('ticker, quarterly_financials')
                      .in_('ticker', chunk).execute())
        except Exception as e:
            log(f'load_fundamentals batch failed: {e}')
            continue
        for r in (resp.data or []):
            qf = r.get('quarterly_financials')
            if isinstance(qf, str):
                try:
                    qf = json.loads(qf)
                except json.JSONDecodeError:
                    qf = None
            if isinstance(qf, list):
                out[r['ticker']] = qf
    return out


# ----------------------------------------------------------------------
# BUCKET CLASSIFICATION
# ----------------------------------------------------------------------

def group_signals_by_ticker(presignals, entry_signals):
    by_ticker = defaultdict(lambda: {
        'presignal_ids': [], 'entry_signal_ids': [],
        'presignal_rows': [], 'entry_signal_rows': [],
    })
    for r in presignals:
        by_ticker[r['ticker']]['presignal_ids'].append(r['id'])
        by_ticker[r['ticker']]['presignal_rows'].append(r)
    for r in entry_signals:
        by_ticker[r['ticker']]['entry_signal_ids'].append(r['id'])
        by_ticker[r['ticker']]['entry_signal_rows'].append(r)
    return by_ticker


def classify_entry_bucket(entry_signal_rows):
    """Classify ticker into T1/T2/T3 from entry_signals.
    Returns (bucket, families, family_strength, signal_score) or (None, [], {}, 0)."""
    # Build "signal_type-like" tokens: (pattern, strength)
    types_seen = []  # list of (pattern, strength) tuples
    for r in entry_signal_rows:
        p = r.get('pattern')
        s = r.get('signal_strength')
        if p in ENTRY_FAMILY_MAP and s in ('Buy', 'Strong'):
            types_seen.append((p, s))
    if not types_seen:
        return None, [], {}, 0.0

    # Distinct (pattern, strength) tuples
    types_seen_set = set(types_seen)

    # Bucket A: >=2 distinct patterns that hit Strong AND are in the strong allowlist
    strong_patterns = {p for (p, s) in types_seen_set if s == 'Strong' and p in STRONG_BUCKET_A_PATTERNS}
    if len(strong_patterns) >= 2:
        all_patterns = {p for (p, _) in types_seen_set}
        families = sorted({ENTRY_FAMILY_MAP[p] for p in all_patterns})
        fam_strength = {ENTRY_FAMILY_MAP[p]: 'strong' for p in strong_patterns}
        for p in all_patterns - strong_patterns:
            fam_strength.setdefault(ENTRY_FAMILY_MAP[p], 'regular')
        score = len(strong_patterns) * 100.0
        return 'T1_MULTI_STRONG', families, fam_strength, score

    # B / C: dedupe to families. A pattern only counts as a "strong family" if
    # it's both Strong AND in STRONG_BUCKET_A_PATTERNS — mirrors India where
    # narrow_cpr is intentionally a lesser tier (one-day breakaway, not a
    # confluence pattern). Without this guard, narrow_cpr Strong was being
    # marked strong for family-dedupe and the resulting n_strong=2 fell through
    # to the dead T3 branch.
    fam_strength = {}
    for (p, s) in types_seen_set:
        fam = ENTRY_FAMILY_MAP[p]
        is_strong = (s == 'Strong' and p in STRONG_BUCKET_A_PATTERNS)
        prev = fam_strength.get(fam)
        if prev == 'strong':
            continue
        fam_strength[fam] = 'strong' if is_strong else (prev or 'regular')

    if len(fam_strength) < 2:
        return None, [], {}, 0.0

    n_strong = sum(1 for v in fam_strength.values() if v == 'strong')
    families = sorted(fam_strength.keys())
    score = len(fam_strength) * 50.0 + n_strong * 30.0
    if n_strong == 1:
        return 'T2_STRONG_REG', families, fam_strength, score
    # n_strong == 0  (>= 2 strong handled by bucket A above)
    return 'T3_MULTI_REG', families, fam_strength, score


def classify_presignal_bucket(presignal_rows):
    """Bucket D (T4_RS_ACCEL): rs_acceleration rule >= 80 AND any other allowed
    rule >= 70 for the SAME ticker on the same snapshot.
    Returns (bucket, families, family_strength, signal_score, qualifying_row_id)."""
    # Group rows by snapshot_date (typically all today)
    by_date = defaultdict(list)
    for r in presignal_rows:
        by_date[r.get('snapshot_date')].append(r)

    best = (None, None, -1.0)  # (rs_accel_row, other_max_score, score)
    for d, rows in by_date.items():
        rs_accel_row = None
        for r in rows:
            if r.get('rule') == RS_ACCEL_RULE and to_float(r.get('score'), 0) >= RS_ACCEL_REQUIRED_SCORE:
                rs_accel_row = r
                break
        if not rs_accel_row:
            continue
        other_scores = []
        for r in rows:
            if r.get('rule') in RS_ACCEL_OTHER_RULES and to_float(r.get('score'), 0) >= RS_ACCEL_OTHER_RULE_MIN:
                other_scores.append((r.get('rule'), to_float(r.get('score'), 0)))
        if not other_scores:
            continue
        max_other = max(s for _, s in other_scores)
        row_score = to_float(rs_accel_row.get('score'), 0) + max_other
        if row_score > best[2]:
            best = (rs_accel_row, max_other, row_score)

    if best[0] is None:
        return None, [], {}, 0.0, None
    return ('T4_RS_ACCEL', ['RS_ACCEL'], {'RS_ACCEL': 'strong'}, best[2], best[0])


# ----------------------------------------------------------------------
# UNIVERSAL FILTERS
# ----------------------------------------------------------------------

def peer_group_for(ticker, sector_map):
    return sector_map.get(ticker)


def passes_sector_filter(ticker, sector_map, sector_rankings):
    """Peer-group gate: peer_group must clear breadth + min ticker floor, AND
    rank in the top SECTOR_GATE_TOP_N peer groups by composite_rank."""
    pg = peer_group_for(ticker, sector_map)
    if not pg:
        return False, 'peer_group_unknown'
    row = sector_rankings.get(pg)
    if not row:
        return False, 'peer_group_data_missing'
    n = row.get('n_tickers') or 0
    if n < SECTOR_GATE_MIN_N:
        return False, f'peer_size_{n}'
    breadth = row.get('breadth') or 0
    if breadth < SECTOR_GATE_MIN_BREADTH:
        return False, f'breadth_{int(breadth)}'
    rank = row.get('composite_rank')
    if rank is None or rank > SECTOR_GATE_TOP_N:
        return False, f'peer_rank_{rank}'
    return True, None


def passes_earnings_filter(ticker, fundamentals_map):
    """Latest YoY positive for BOTH revenue and net income. Relaxed vs India:
    skips the "improving" (Q1/Q5) check because yfinance caps at ~5 quarters."""
    qf = fundamentals_map.get(ticker)
    if not qf:
        return False, 'fundamentals_missing'
    if len(qf) < EARNINGS_MIN_QUARTERS:
        return False, f'quarters_{len(qf)}'

    q0 = qf[0]
    q4 = qf[4] if len(qf) > 4 else None
    if not q4:
        return False, 'no_yoy_comparable'

    rev_now = to_float(q0.get('revenue'))
    rev_yr  = to_float(q4.get('revenue'))
    ni_now  = to_float(q0.get('net_income'))
    ni_yr   = to_float(q4.get('net_income'))

    if rev_now is None or rev_yr is None or rev_yr <= 0:
        return False, 'revenue_data_incomplete'
    if ni_now is None or ni_yr is None:
        return False, 'profit_data_incomplete'

    rev_yoy = (rev_now - rev_yr) / rev_yr
    if rev_yoy <= 0:
        return False, 'revenue_yoy_not_positive'
    # Net income: just require positive growth (sign change handled below).
    if ni_yr <= 0:
        # Year-ago loss; require current to be positive (turnaround) for a pass
        if ni_now <= 0:
            return False, 'profit_yoy_not_positive'
    else:
        ni_yoy = (ni_now - ni_yr) / ni_yr
        if ni_yoy <= 0:
            return False, 'profit_yoy_not_positive'
    return True, None


def passes_intraday_filter(ticker, today_snap):
    """Reject candidates whose D0 close sits in the bottom MIN_INTRADAY_CLOSE_POSITION
    of the D0 range — a weak close that suggests intraday distribution."""
    snap = today_snap.get(ticker)
    if not snap:
        return False, 'no_snapshot'
    high = to_float(snap.get('high'))
    low = to_float(snap.get('low'))
    close = to_float(snap.get('close'))
    if high is None or low is None or close is None:
        return False, 'price_data_missing'
    rng = high - low
    if rng <= 0:
        return True, None  # no intraday range to assess; let it through
    pos = (close - low) / rng
    if pos < MIN_INTRADAY_CLOSE_POSITION:
        return False, f'close_in_bottom_{int(pos * 100)}pct'
    return True, None


def size_position(tier, entry_price, atr):
    p = TIER_PARAMS[tier]
    risk_usd = SLEEVE * p['risk_pct']
    stop_dist = STOP_ATR_MULT * atr
    if stop_dist <= 0 or entry_price <= 0:
        return 0, 0, 0
    qty_by_risk = int(risk_usd // stop_dist)
    qty_by_cap = int(p['cap'] // entry_price)
    qty = max(0, min(qty_by_risk, qty_by_cap))
    notional = qty * entry_price
    if notional < POSITION_FLOOR:
        return 0, 0, 0
    return qty, notional, risk_usd


# ----------------------------------------------------------------------
# EXIT PROCESSING
# ----------------------------------------------------------------------

def _close_trade(sb, tr, exit_price, exit_date, exit_reason,
                 realised_pnl, realised_qty, partials, partials_taken,
                 current_stop, breakeven_armed, trail_armed,
                 max_unrealized_pct, min_unrealized_pct, trail_armed_reason):
    entry_price = to_float(tr['entry_price'])
    initial_qty = to_int(tr['initial_quantity'])
    initial_stop = to_float(tr['initial_stop'])
    entry_value = entry_price * initial_qty
    total_pnl_pct = realised_pnl / entry_value * 100 if entry_value else 0
    risk_per_share = entry_price - initial_stop
    r_mult = ((realised_pnl / initial_qty) / risk_per_share
              if (initial_qty and risk_per_share > 0) else None)
    holding_days = trading_days_between(date.fromisoformat(tr['entry_date']), exit_date)
    sb.table('paper_trades').update({
        'exit_date': exit_date.isoformat(),
        'exit_price': round(exit_price, 4),
        'exit_reason': exit_reason,
        'total_pnl': round(realised_pnl, 2),
        'total_pnl_pct': round(total_pnl_pct, 3),
        'r_multiple': round(r_mult, 3) if r_mult is not None else None,
        'holding_days': holding_days,
        'current_quantity': 0,
        'realised_pnl': round(realised_pnl, 2),
        'realised_qty': realised_qty,
        'partials': partials,
        'partials_taken': partials_taken,
        'current_stop': round(current_stop, 4),
        'breakeven_armed': breakeven_armed,
        'trail_armed': trail_armed,
        'trail_armed_reason': trail_armed_reason,
        'max_unrealized_pct': round(max_unrealized_pct, 3) if max_unrealized_pct is not None else None,
        'min_unrealized_pct': round(min_unrealized_pct, 3) if min_unrealized_pct is not None else None,
        'status': 'closed',
        'updated_at': datetime.utcnow().isoformat(),
    }).eq('id', tr['id']).execute()


def _update_open_trade(sb, trade_id, current_qty, current_stop, partials_taken,
                       partials, realised_pnl, realised_qty,
                       breakeven_armed, trail_armed,
                       max_unrealized_pct, min_unrealized_pct, trail_armed_reason):
    sb.table('paper_trades').update({
        'current_quantity': current_qty,
        'current_stop': round(current_stop, 4),
        'partials_taken': partials_taken,
        'partials': partials,
        'realised_pnl': round(realised_pnl, 2),
        'realised_qty': realised_qty,
        'breakeven_armed': breakeven_armed,
        'trail_armed': trail_armed,
        'trail_armed_reason': trail_armed_reason,
        'max_unrealized_pct': round(max_unrealized_pct, 3) if max_unrealized_pct is not None else None,
        'min_unrealized_pct': round(min_unrealized_pct, 3) if min_unrealized_pct is not None else None,
        'updated_at': datetime.utcnow().isoformat(),
    }).eq('id', trade_id).execute()


def process_exits(sb, open_trades, today_snap, today_date):
    actions = []
    realised_today = 0.0
    closed_today = 0

    for tr in open_trades:
        ticker = tr['ticker']
        snap = today_snap.get(ticker)
        if not snap:
            continue
        bar_high = to_float(snap.get('high'))
        bar_low = to_float(snap.get('low'))
        bar_close = to_float(snap.get('close'))
        bar_atr = to_float(snap.get('atr_14'), to_float(tr.get('entry_atr')))
        if bar_close is None or bar_high is None or bar_low is None:
            continue

        entry_price = to_float(tr['entry_price'])
        initial_stop = to_float(tr['initial_stop'])
        current_stop = to_float(tr['current_stop'])
        initial_qty = to_int(tr['initial_quantity'])
        current_qty = to_int(tr['current_quantity'])
        if current_qty <= 0:
            continue
        risk_per_share = entry_price - initial_stop
        if risk_per_share <= 0:
            continue
        tier = tr['strategy_tier']
        partial_levels = TIER_PARAMS[tier]['partial_R']
        partials_taken = to_int(tr['partials_taken'])
        partials = tr.get('partials') or []
        if isinstance(partials, str):
            try:
                partials = json.loads(partials)
            except json.JSONDecodeError:
                partials = []
        realised_pnl = to_float(tr['realised_pnl'], 0)
        realised_qty = to_int(tr['realised_qty'], 0)
        breakeven_armed = bool(tr.get('breakeven_armed'))
        trail_armed = bool(tr.get('trail_armed'))
        trail_armed_reason = tr.get('trail_armed_reason')
        partial_qty = max(1, int(initial_qty * PARTIAL_QTY_PCT / 100))

        bar_unrealized_high = (bar_high - entry_price) / entry_price * 100
        bar_unrealized_low  = (bar_low  - entry_price) / entry_price * 100
        prev_max = to_float(tr.get('max_unrealized_pct'))
        prev_min = to_float(tr.get('min_unrealized_pct'))
        max_unrealized_pct = bar_unrealized_high if prev_max is None else max(prev_max, bar_unrealized_high)
        min_unrealized_pct = bar_unrealized_low  if prev_min is None else min(prev_min, bar_unrealized_low)

        # 1. Stop hit
        if bar_low <= current_stop:
            exit_qty = current_qty
            pnl_chunk = (current_stop - entry_price) * exit_qty
            realised_pnl += pnl_chunk
            realised_today += pnl_chunk
            realised_qty += exit_qty
            current_qty = 0
            _close_trade(sb, tr, current_stop, today_date,
                         'trail_stop' if trail_armed else 'stop',
                         realised_pnl, realised_qty, partials, partials_taken,
                         current_stop, breakeven_armed, trail_armed,
                         max_unrealized_pct, min_unrealized_pct, trail_armed_reason)
            closed_today += 1
            actions.append({'id': tr['id'], 'ticker': ticker, 'action': 'stop'})
            continue

        # 2. R-multiple partials
        for idx, R in enumerate(partial_levels):
            if partials_taken > idx:
                continue
            target_price = entry_price + R * risk_per_share
            if bar_high < target_price:
                break
            book_qty = min(partial_qty, current_qty)
            if book_qty <= 0:
                break
            pnl_chunk = (target_price - entry_price) * book_qty
            realised_pnl += pnl_chunk
            realised_today += pnl_chunk
            realised_qty += book_qty
            current_qty -= book_qty
            partials_taken += 1
            partials.append({
                'date': today_date.isoformat(),
                'kind': f'R{R}',
                'price': round(target_price, 2),
                'qty': book_qty,
                'pnl': round(pnl_chunk, 2),
            })
            if partials_taken == 1 and not breakeven_armed:
                current_stop = max(current_stop, entry_price)
                breakeven_armed = True
            if partials_taken == 2 and not trail_armed:
                trail_armed = True
                trail_armed_reason = 'partial_2'
            if current_qty == 0:
                break

        if current_qty == 0:
            last_price = partials[-1]['price'] if partials else bar_close
            _close_trade(sb, tr, last_price, today_date, 'partials_full',
                         realised_pnl, realised_qty, partials, partials_taken,
                         current_stop, breakeven_armed, trail_armed,
                         max_unrealized_pct, min_unrealized_pct, trail_armed_reason)
            closed_today += 1
            actions.append({'id': tr['id'], 'ticker': ticker, 'action': 'partials_full'})
            continue

        # 2b. Early trail-arm via MFE threshold
        if not trail_armed and max_unrealized_pct >= EARLY_TRAIL_PCT:
            trail_armed = True
            trail_armed_reason = 'pct_threshold'
            if not breakeven_armed:
                breakeven_armed = True
            current_stop = max(current_stop, entry_price)

        # 3. Universal 25%/15-day partial
        entry_date = date.fromisoformat(tr['entry_date'])
        holding_days = trading_days_between(entry_date, today_date)
        if partials_taken == 0 and holding_days < UNIVERSAL_BOOK_DAYS:
            unr_pct = (bar_close - entry_price) / entry_price * 100
            if unr_pct >= UNIVERSAL_BOOK_PCT:
                book_qty = min(max(1, int(initial_qty * UNIVERSAL_BOOK_QTY_PCT / 100)),
                               current_qty)
                pnl_chunk = (bar_close - entry_price) * book_qty
                realised_pnl += pnl_chunk
                realised_today += pnl_chunk
                realised_qty += book_qty
                current_qty -= book_qty
                partials.append({
                    'date': today_date.isoformat(),
                    'kind': 'universal_25pct',
                    'price': round(bar_close, 2),
                    'qty': book_qty,
                    'pnl': round(pnl_chunk, 2),
                })
                if not breakeven_armed:
                    current_stop = max(current_stop, entry_price)
                    breakeven_armed = True

        # 4. Trailing stop update
        if trail_armed and bar_atr:
            new_stop = bar_close - TRAIL_ATR_MULT * bar_atr
            if new_stop > current_stop:
                current_stop = new_stop

        # 5. Time stop
        if holding_days >= TIME_STOP_DAYS:
            unr_pct = (bar_close - entry_price) / entry_price * 100
            if TIME_STOP_LOW <= unr_pct <= TIME_STOP_HIGH:
                exit_qty = current_qty
                pnl_chunk = (bar_close - entry_price) * exit_qty
                realised_pnl += pnl_chunk
                realised_today += pnl_chunk
                realised_qty += exit_qty
                current_qty = 0
                _close_trade(sb, tr, bar_close, today_date, 'time_stop',
                             realised_pnl, realised_qty, partials, partials_taken,
                             current_stop, breakeven_armed, trail_armed,
                             max_unrealized_pct, min_unrealized_pct, trail_armed_reason)
                closed_today += 1
                actions.append({'id': tr['id'], 'ticker': ticker, 'action': 'time_stop'})
                continue

        _update_open_trade(
            sb, tr['id'], current_qty, current_stop, partials_taken, partials,
            realised_pnl, realised_qty, breakeven_armed, trail_armed,
            max_unrealized_pct, min_unrealized_pct, trail_armed_reason,
        )

    return actions, closed_today, realised_today


# ----------------------------------------------------------------------
# ENTRY CONTEXT
# ----------------------------------------------------------------------

def compute_market_regime(sb, as_of_date):
    """risk_on / neutral / risk_off from ^GSPC close vs its own 50/200-day SMAs."""
    try:
        resp = (sb.table('daily_stock_snapshots')
                  .select('close')
                  .eq('ticker', BENCHMARK_TICKER)
                  .lte('snapshot_date', as_of_date)
                  .order('snapshot_date', desc=True)
                  .limit(200).execute())
        closes = [to_float(r.get('close')) for r in (resp.data or [])]
        closes = [c for c in closes if c is not None]
        if len(closes) < 50:
            return None
        close_now = closes[0]
        sma50 = sum(closes[:50]) / 50
        if len(closes) < 200:
            return 'risk_on' if close_now > sma50 else 'risk_off'
        sma200 = sum(closes) / len(closes)
        if close_now > sma50 and sma50 > sma200:
            return 'risk_on'
        if close_now < sma50 and sma50 < sma200:
            return 'risk_off'
        return 'neutral'
    except Exception as e:
        log(f'compute_market_regime failed: {e}')
        return None


def compute_entry_context(ticker, today_snap, prior_snap, snap_3d, market_regime):
    snap = today_snap.get(ticker) or {}
    close = to_float(snap.get('close'))
    ema20 = to_float(snap.get('ema_20'))
    ema50 = to_float(snap.get('ema_50'))
    atr = to_float(snap.get('atr_14'))
    high = to_float(snap.get('high'))
    low = to_float(snap.get('low'))

    ctx = {'entry_market_regime': market_regime}
    if close and ema20 and ema20 > 0:
        ctx['entry_dist_ema20_pct'] = round((close - ema20) / ema20 * 100, 3)
    if close and ema50 and ema50 > 0:
        ctx['entry_dist_ema50_pct'] = round((close - ema50) / ema50 * 100, 3)
    if close and atr and close > 0:
        ctx['entry_atr_pct'] = round(atr / close * 100, 3)
    prev_close = to_float((prior_snap.get(ticker) or {}).get('close'))
    if close and prev_close and prev_close > 0:
        ctx['entry_prior_1d_pct'] = round((close - prev_close) / prev_close * 100, 3)
    close_3d = to_float((snap_3d.get(ticker) or {}).get('close'))
    if close and close_3d and close_3d > 0:
        ctx['entry_prior_3d_pct'] = round((close - close_3d) / close_3d * 100, 3)
    if high is not None and low is not None and atr and atr > 0:
        ctx['entry_bar_range_atr'] = round((high - low) / atr, 3)
    return ctx


# ----------------------------------------------------------------------
# ENTRY PROCESSING
# ----------------------------------------------------------------------

def process_entries(sb, candidates, today_snap, today_date, holdings,
                    recent_closed, open_tickers, sector_map,
                    sector_exposure, max_new, funnel,
                    prior_snap, snap_3d, market_regime):
    tier_order = {'T1_MULTI_STRONG': 0, 'T2_STRONG_REG': 1,
                  'T3_MULTI_REG': 2,    'T4_RS_ACCEL': 3}
    candidates.sort(key=lambda c: (tier_order[c['tier']], -c['signal_score']))

    deployed = sum(sector_exposure.values())
    cash_cap = SLEEVE * MAX_DEPLOYED_PCT

    inserted = []
    for c in candidates:
        if len(inserted) >= max_new:
            break
        ticker = c['ticker']
        if ticker in holdings or ticker in open_tickers or ticker in recent_closed:
            continue
        snap = today_snap.get(ticker)
        if not snap:
            continue
        close_today = to_float(snap.get('close'))
        atr14 = to_float(snap.get('atr_14'))
        if not close_today or not atr14 or atr14 <= 0:
            continue

        est_entry_price = close_today * (1 + SLIPPAGE_BPS / 10_000)
        qty, notional, _ = size_position(c['tier'], est_entry_price, atr14)
        if qty == 0:
            funnel['qty_zero_rejected'] = funnel.get('qty_zero_rejected', 0) + 1
            continue
        if deployed + notional > cash_cap:
            funnel['cash_rejected'] = funnel.get('cash_rejected', 0) + 1
            continue

        peer_group = peer_group_for(ticker, sector_map) or 'Unknown'
        sector_notional_after = sector_exposure.get(peer_group, 0) + notional
        if peer_group != 'Unknown' and sector_notional_after > MAX_SECTOR_CONC * SLEEVE:
            funnel['sector_conc_rejected'] = funnel.get('sector_conc_rejected', 0) + 1
            continue

        est_initial_stop = est_entry_price - STOP_ATR_MULT * atr14
        est_initial_risk = qty * (est_entry_price - est_initial_stop)

        row = {
            'ticker': ticker,
            'sector': peer_group,
            'strategy_tier': c['tier'],
            'signal_families': c['families'],
            'strong_family_count': sum(
                1 for s in c['family_strength'].values() if s == 'strong'
            ),
            'signal_ids': c['signal_ids'],
            'signal_score': round(c['signal_score'], 2),
            'entry_date': today_date.isoformat(),
            'entry_price': round(est_entry_price, 4),
            'entry_atr': round(atr14, 4),
            'initial_quantity': qty,
            'current_quantity': qty,
            'entry_value': round(qty * est_entry_price, 2),
            'initial_risk': round(est_initial_risk, 2),
            'initial_stop': round(est_initial_stop, 4),
            'current_stop': round(est_initial_stop, 4),
            'status': 'pending',
            'mode': 'paper',
        }
        row.update(compute_entry_context(ticker, today_snap, prior_snap, snap_3d, market_regime))
        sb.table('paper_trades').insert(row).execute()
        inserted.append(row)
        deployed += notional
        sector_exposure[peer_group] = sector_exposure.get(peer_group, 0) + notional
        open_tickers.add(ticker)

    return inserted


# ----------------------------------------------------------------------
# MTM + KILL SWITCHES
# ----------------------------------------------------------------------

def positions_mtm(open_trades, today_snap):
    pv = 0.0
    for tr in open_trades:
        snap = today_snap.get(tr['ticker'])
        if not snap:
            continue
        close = to_float(snap.get('close'))
        if not close:
            continue
        pv += close * to_int(tr['current_quantity'])
    return pv


def open_capital_tied(open_trades):
    return sum(
        to_int(tr['current_quantity']) * to_float(tr['entry_price'])
        for tr in open_trades
    )


def kill_switch(equity_history, provisional_total):
    if not equity_history:
        return False, None
    ath = max([SLEEVE] + [to_float(e['total_value'], SLEEVE) for e in equity_history])
    dd_pct = (provisional_total - ath) / ath * 100 if ath else 0
    if dd_pct < DD_KILL_PCT:
        return True, f'drawdown_{dd_pct:.2f}%'
    last_value = to_float(equity_history[0].get('total_value'), SLEEVE)
    if last_value > 0:
        daily_pct = (provisional_total - last_value) / last_value * 100
        if daily_pct < DAILY_KILL_PCT:
            return True, f'daily_{daily_pct:.2f}%'
    return False, None


def fetch_benchmark_closes(sb, today_date):
    """Latest ^GSPC + QQQ close on or before today. Falls back to most recent
    prior close if today's row is missing — equity curve never gets a gap."""
    out = {BENCHMARK_TICKER: None, SECONDARY_BENCHMARK: None}
    for tk in out:
        try:
            r = (sb.table('daily_stock_snapshots').select('close').eq('ticker', tk)
                   .lte('snapshot_date', today_date.isoformat())
                   .order('snapshot_date', desc=True).limit(1).execute())
            if r.data and r.data[0].get('close') is not None:
                out[tk] = round(float(r.data[0]['close']), 2)
        except Exception as e:
            log(f'benchmark fetch failed for {tk}: {e}')
    return out[BENCHMARK_TICKER], out[SECONDARY_BENCHMARK]


def write_equity_row(sb, today_date, open_trades, today_snap, equity_history,
                     cum_realised_closed, opened, closed, realised_today):
    pv = positions_mtm(open_trades, today_snap)
    cash = SLEEVE - open_capital_tied(open_trades) + cum_realised_closed
    total_value = cash + pv
    ath = max([SLEEVE] + [to_float(e['total_value'], SLEEVE) for e in equity_history]
              + [total_value])
    dd_pct = (total_value - ath) / ath * 100 if ath else 0
    bench_sp500, bench_qqq = fetch_benchmark_closes(sb, today_date)

    sb.table('paper_equity').upsert({
        'snapshot_date': today_date.isoformat(),
        'cash': round(cash, 2),
        'positions_value': round(pv, 2),
        'total_value': round(total_value, 2),
        'drawdown_pct': round(dd_pct, 3),
        'open_positions': len(open_trades),
        'trades_opened_today': opened,
        'trades_closed_today': closed,
        'realised_today': round(realised_today, 2),
        'bench_sp500': bench_sp500,
        'bench_qqq': bench_qqq,
    }, on_conflict='snapshot_date').execute()
    return total_value, dd_pct


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    funnel = {
        'signal_rows': 0, 'unique_tickers': 0,
        'no_bucket': 0,
        'sector_rejected': 0, 'earnings_rejected': 0, 'intraday_rejected': 0,
        'in_holdings': 0, 'in_cooldown': 0, 'already_open': 0,
        'qualified': 0, 'inserted': 0,
        'qty_zero_rejected': 0, 'cash_rejected': 0, 'sector_conc_rejected': 0,
        'deployed_pct': 0, 'deployed_usd': 0,
        'bucket_counts': {},
        'sector_reasons': {}, 'earnings_reasons': {}, 'intraday_reasons': {},
    }
    errors = []

    try:
        recent_dates = get_recent_trading_dates(sb, days_back=5)
        if not recent_dates:
            log('No trading data; aborting.')
            return
        today_date = date.fromisoformat(recent_dates[0])
        signal_date_from = today_date.isoformat()
        signal_date_to = today_date.isoformat()
        log(f'today={today_date}  signal_window={signal_date_from}..{signal_date_to}')

        today_snap = load_snapshots_for_date(sb, today_date.isoformat())
        prior_snap = (load_snapshots_for_date(sb, recent_dates[1])
                      if len(recent_dates) > 1 else {})
        snap_3d = (load_snapshots_for_date(sb, recent_dates[3])
                   if len(recent_dates) > 3 else {})
        market_regime = compute_market_regime(sb, today_date.isoformat())
        holdings = load_holdings(sb)
        open_trades = load_open_paper_trades(sb)
        pending_trades = load_pending_paper_trades(sb)
        recent_closed = load_recent_closed_tickers(sb, today_date)
        sector_map = load_sectors(sb)
        sector_rankings = load_sector_rankings(sb, today_date.isoformat())
        equity_history = load_recent_equity(sb)
        tradeable_pgs = sorted(
            pg for pg, r in sector_rankings.items()
            if (r.get('composite_rank') and r['composite_rank'] <= SECTOR_GATE_TOP_N
                and (r.get('n_tickers') or 0) >= SECTOR_GATE_MIN_N
                and (r.get('breadth') or 0) >= SECTOR_GATE_MIN_BREADTH)
        )
        log(f'open={len(open_trades)} pending={len(pending_trades)} '
            f'holdings={len(holdings)} cooldown={len(recent_closed)} '
            f'peer_groups={len(sector_rankings)} regime={market_regime} '
            f'tradeable_pgs={tradeable_pgs}')

        exit_actions, closed_today, realised_today = process_exits(
            sb, open_trades, today_snap, today_date
        )
        log(f'exits processed: {len(exit_actions)} (closed={closed_today})')
        open_trades = load_open_paper_trades(sb)
        pending_trades = load_pending_paper_trades(sb)
        committed_trades = open_trades + pending_trades
        open_tickers = {t['ticker'] for t in committed_trades}

        sector_exposure = defaultdict(float)
        for tr in committed_trades:
            sector_exposure[tr.get('sector') or 'Unknown'] += to_float(tr['entry_value'], 0)

        cum_realised_closed = load_cumulative_closed_pnl(sb)
        pv_now = positions_mtm(open_trades, today_snap)
        provisional_total = (
            SLEEVE - open_capital_tied(open_trades) + cum_realised_closed + pv_now
        )
        kill, kill_reason = kill_switch(equity_history, provisional_total)

        inserted = []
        if kill:
            log(f'KILL SWITCH: {kill_reason} -- skipping new entries')
        elif len(committed_trades) >= MAX_OPEN_POSITIONS:
            log(f'At max open positions ({MAX_OPEN_POSITIONS}, incl. pending) -- skipping')
        else:
            slots = min(MAX_NEW_PER_DAY, MAX_OPEN_POSITIONS - len(committed_trades))
            presignals = load_presignals(sb, signal_date_from, signal_date_to)
            entry_signals_data = load_entry_signals(sb, signal_date_from, signal_date_to)
            funnel['signal_rows'] = len(presignals) + len(entry_signals_data)
            by_ticker = group_signals_by_ticker(presignals, entry_signals_data)
            funnel['unique_tickers'] = len(by_ticker)
            log(f'signals: presignal={len(presignals)} entry={len(entry_signals_data)} '
                f'tickers={len(by_ticker)}')

            raw_candidates = []
            for ticker, bundle in by_ticker.items():
                bucket, families, fam_strength, score = classify_entry_bucket(
                    bundle['entry_signal_rows']
                )
                presignal_row = None
                if not bucket:
                    bucket, families, fam_strength, score, presignal_row = (
                        classify_presignal_bucket(bundle['presignal_rows'])
                    )
                if not bucket:
                    funnel['no_bucket'] += 1
                    continue
                raw_candidates.append({
                    'ticker': ticker,
                    'tier': bucket,
                    'families': families,
                    'family_strength': fam_strength,
                    'signal_score': score,
                    'signal_ids': {
                        'presignal_ids': (
                            [presignal_row['id']] if presignal_row
                            else bundle['presignal_ids']
                        ),
                        'entry_signal_ids': bundle['entry_signal_ids'],
                    },
                })

            fundamentals = load_fundamentals(
                sb, [c['ticker'] for c in raw_candidates]
            )

            candidates = []
            for c in raw_candidates:
                ticker = c['ticker']
                if ticker in holdings:
                    funnel['in_holdings'] += 1; continue
                if ticker in recent_closed:
                    funnel['in_cooldown'] += 1; continue
                if ticker in open_tickers:
                    funnel['already_open'] += 1; continue
                sec_ok, sec_reason = passes_sector_filter(
                    ticker, sector_map, sector_rankings
                )
                if not sec_ok:
                    funnel['sector_rejected'] += 1
                    funnel['sector_reasons'][sec_reason] = (
                        funnel['sector_reasons'].get(sec_reason, 0) + 1
                    )
                    continue
                e_ok, e_reason = passes_earnings_filter(ticker, fundamentals)
                if not e_ok:
                    funnel['earnings_rejected'] += 1
                    funnel['earnings_reasons'][e_reason] = (
                        funnel['earnings_reasons'].get(e_reason, 0) + 1
                    )
                    continue
                i_ok, i_reason = passes_intraday_filter(ticker, today_snap)
                if not i_ok:
                    funnel['intraday_rejected'] += 1
                    funnel['intraday_reasons'][i_reason] = (
                        funnel['intraday_reasons'].get(i_reason, 0) + 1
                    )
                    continue
                funnel['bucket_counts'][c['tier']] = (
                    funnel['bucket_counts'].get(c['tier'], 0) + 1
                )
                candidates.append(c)

            funnel['qualified'] = len(candidates)
            inserted = process_entries(
                sb, candidates, today_snap, today_date, holdings, recent_closed,
                open_tickers, sector_map, sector_exposure, max_new=slots,
                funnel=funnel,
                prior_snap=prior_snap, snap_3d=snap_3d, market_regime=market_regime,
            )
            funnel['inserted'] = len(inserted)
            log(f'entries: qualified={len(candidates)} inserted={len(inserted)}')

        final_open = load_open_paper_trades(sb)
        final_pending = load_pending_paper_trades(sb)
        deployed_usd = sum(to_float(t.get('entry_value'), 0)
                           for t in final_open + final_pending)
        deployed_pct = deployed_usd / SLEEVE * 100 if SLEEVE else 0
        funnel['deployed_usd'] = round(deployed_usd, 2)
        funnel['deployed_pct'] = round(deployed_pct, 2)
        total_value, dd_pct = write_equity_row(
            sb, today_date, final_open, today_snap, equity_history,
            cum_realised_closed, opened=len(inserted),
            closed=closed_today, realised_today=realised_today,
        )
        log(f'equity: total={total_value:,.0f} dd={dd_pct:.2f}% '
            f'open={len(final_open)} pending={len(final_pending)} '
            f'deployed={deployed_pct:.1f}%')

        sb.table('paper_run_log').insert({
            'run_date': today_date.isoformat(),
            'phase': 'scan',
            'mode': 'paper',
            'signals_seen': funnel['signal_rows'],
            'signals_passed': funnel['qualified'],
            'signals_taken': funnel['inserted'],
            'exits_processed': len(exit_actions),
            'kill_switch_active': kill,
            'kill_switch_reason': kill_reason,
            'deployed_pct': round(deployed_pct, 2),
            'funnel': funnel,
            'errors': errors or None,
        }).execute()
        log('Done.')

    except Exception as e:
        log(f'FATAL: {e}')
        traceback.print_exc()
        errors.append({'fatal': str(e), 'trace': traceback.format_exc()[:2000]})
        try:
            sb.table('paper_run_log').insert({
                'run_date': date.today().isoformat(),
                'phase': 'scan',
                'mode': 'paper',
                'errors': errors,
            }).execute()
        except Exception:
            pass
        sys.exit(1)


if __name__ == '__main__':
    main()
