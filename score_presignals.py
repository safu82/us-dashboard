#!/usr/bin/env python3
"""Phase 3c — presignal_scores scanner (US port of India score_presignals.py).

6 early-warning rules per ticker, each scored 0-100. Only scores >= 70 written.
30-day rolling history (older rows pruned).

Rules (from India spec):
  bz_buy           — proximity to the Blue Zone Buy thresholds (RSI 65/55 + >EMA20)
  bz_strong        — proximity to the Blue Zone Strong thresholds (RSI 72/65 + >EMA50 + vol 1.5×)
  golden_cross     — EMA20 < EMA50, gap ≤ 5%, slopes converging, close > EMA200
  macd             — MACD < signal, histogram improving 3+ days, gap ≤ 0.5%
  pullback_bounce  — strong-trend stack, close within 5% above EMA20
  rs_acceleration  — rs_rank ≤ 200, rank_slope ≤ -3, close > EMA50, alkalyme_rs ≥ 55
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from supabase import create_client

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

sb = create_client(URL, KEY)

SCORE_THRESHOLD = 70
HISTORY_DAYS = 30


def pull_all(table, columns, order_cols=None, page=1000, key_col='id'):
    """Keyset-paginate on the primary key instead of OFFSET/.range().

    OFFSET pagination re-runs the full sort+scan for every page and discards
    the skipped rows (O(n^2)); once daily_stock_snapshots grew to ~122k rows
    that pushed the pull past the 8s statement_timeout (57014). Keyset
    (WHERE key_col > last ORDER BY key_col LIMIT n) reads only each page's own
    slice via the PK index — a single pass. order_cols is kept for call-site
    compatibility but ignored: DB order is irrelevant here (the caller re-sorts
    per-ticker in pandas); we order by the PK purely as a stable cursor.
    """
    sel_cols = [c.strip() for c in columns.split(',')]
    sel = columns if key_col in sel_cols else f'{columns}, {key_col}'
    rows, last = [], None
    while True:
        q = sb.table(table).select(sel).order(key_col).limit(page)
        if last is not None:
            q = q.gt(key_col, last)
        res = q.execute()
        if not res.data:
            break
        rows.extend(res.data)
        last = res.data[-1][key_col]
        if len(res.data) < page:
            break
    return rows


def _f(x):
    if x is None:
        return None
    try:
        v = float(x)
        return v if not np.isnan(v) else None
    except (TypeError, ValueError):
        return None


def _ramp(x, lo, hi):
    """Linear ramp: returns 0 at lo, 1 at hi. Clamped to [0, 1]."""
    if x is None:
        return 0.0
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    t = (x - lo) / (hi - lo)
    return max(0.0, min(1.0, t))


# ── Rule scorers (return (score, metadata) where score in [0, 100]) ────────
def score_bz_buy(latest):
    """Proximity to Buy-tier Blue Zone: -10% of 52WH + RSI 65/55 + >EMA20."""
    close = _f(latest['close']); high52 = _f(latest['high_52w'])
    if close is None or not high52:
        return 0.0, {}
    rsi_d = _f(latest['rsi_14']) or 0
    rsi_w = _f(latest['weekly_rsi_ema_9']) or 0
    ema20 = _f(latest['ema_20'])
    # Components
    near52 = _ramp(close / high52, 0.85, 0.90)        # 1.0 at -10% of 52WH
    rsi_d_c = _ramp(rsi_d, 60, 65)                    # 1.0 at RSI 65
    rsi_w_c = _ramp(rsi_w, 50, 55)
    ema20_c = 1.0 if (ema20 and close > ema20) else 0.0
    score = (near52 + rsi_d_c + rsi_w_c + ema20_c) / 4 * 100
    return round(score, 1), {
        'pct_from_52wh': round((close / high52 - 1) * 100, 2),
        'rsi_d': rsi_d, 'rsi_w': rsi_w, 'gt_ema20': bool(ema20_c),
    }


def score_bz_strong(latest):
    """Proximity to Strong-tier Blue Zone: -10% + RSI 72/65 + >EMA50 + vol 1.5×."""
    close = _f(latest['close']); high52 = _f(latest['high_52w'])
    if close is None or not high52:
        return 0.0, {}
    rsi_d = _f(latest['rsi_14']) or 0
    rsi_w = _f(latest['weekly_rsi_ema_9']) or 0
    ema50 = _f(latest['ema_50'])
    vol_r = _f(latest['vol_ratio']) or 0
    near52 = _ramp(close / high52, 0.85, 0.90)
    rsi_d_c = _ramp(rsi_d, 67, 72)
    rsi_w_c = _ramp(rsi_w, 60, 65)
    ema50_c = 1.0 if (ema50 and close > ema50) else 0.0
    vol_c   = _ramp(vol_r, 1.0, 1.5)
    score = (near52 + rsi_d_c + rsi_w_c + ema50_c + vol_c) / 5 * 100
    return round(score, 1), {
        'pct_from_52wh': round((close / high52 - 1) * 100, 2),
        'rsi_d': rsi_d, 'rsi_w': rsi_w, 'vol_ratio': vol_r,
        'gt_ema50': bool(ema50_c),
    }


def score_golden_cross(g_tail):
    """EMA20 < EMA50, gap ≤ 5%, EMA20 slope > EMA50 slope (converging), close > EMA200."""
    latest = g_tail.iloc[-1]
    ema20 = _f(latest['ema_20']); ema50 = _f(latest['ema_50']); ema200 = _f(latest['ema_200'])
    close = _f(latest['close'])
    if None in (ema20, ema50, ema200, close) or ema50 == 0:
        return 0.0, {}
    if ema20 >= ema50:                              # already crossed → not a pre-signal
        return 0.0, {}
    gap = (ema50 - ema20) / ema50                   # positive; we want small
    gap_c = _ramp(-gap, -0.05, -0.01)               # 1.0 at gap ≤ 1%, 0 at gap ≥ 5%
    # Slope convergence over last 5 bars
    if len(g_tail) < 6:
        slope_c = 0.0
    else:
        e20_old = _f(g_tail.iloc[-6]['ema_20']); e50_old = _f(g_tail.iloc[-6]['ema_50'])
        if None in (e20_old, e50_old) or e20_old == 0 or e50_old == 0:
            slope_c = 0.0
        else:
            s20 = (ema20 - e20_old) / e20_old
            s50 = (ema50 - e50_old) / e50_old
            slope_c = _ramp(s20 - s50, 0.0, 0.01)   # 1.0 if EMA20 outpacing EMA50 by ≥1% over 5d
    ema200_c = 1.0 if close > ema200 else 0.0
    score = (gap_c + slope_c + ema200_c) / 3 * 100
    return round(score, 1), {
        'gap_pct': round(gap * 100, 2),
        'slope_diff_pct': round((s20 - s50) * 100, 2) if (len(g_tail) >= 6 and 'e50_old' in dir() and e50_old) else None,
        'gt_ema200': bool(ema200_c),
    }


def score_macd(g_tail):
    """MACD < signal, histogram improving 3+ days, gap ≤ 0.5% of close."""
    if len(g_tail) < 5:
        return 0.0, {}
    latest = g_tail.iloc[-1]
    macd = _f(latest['macd_line']); sig = _f(latest['macd_signal']); close = _f(latest['close'])
    if None in (macd, sig, close) or close == 0:
        return 0.0, {}
    if macd >= sig:                                  # already crossed
        return 0.0, {}
    gap_pct = (sig - macd) / close                   # in price units / close
    gap_c = _ramp(-gap_pct, -0.005, -0.001)          # 1.0 at gap ≤ 0.1%, 0 at gap ≥ 0.5%
    # Histogram improving for 3+ days (each day > previous)
    hist = g_tail['macd_hist'].apply(_f).tail(4).tolist()
    improving_days = 0
    for i in range(1, len(hist)):
        if hist[i] is not None and hist[i - 1] is not None and hist[i] > hist[i - 1]:
            improving_days += 1
        else:
            break  # need consecutive
    hist_c = _ramp(improving_days, 1, 3)             # 1.0 at 3+ improving days
    score = (gap_c + hist_c) / 2 * 100
    return round(score, 1), {
        'gap_pct': round(gap_pct * 100, 3),
        'hist_improving_days': improving_days,
        'macd_line': macd, 'macd_signal': sig,
    }


def score_pullback_bounce(latest):
    """Strong stack (EMA20>EMA50×1.05>EMA200), close within 5% above EMA20."""
    ema20 = _f(latest['ema_20']); ema50 = _f(latest['ema_50']); ema200 = _f(latest['ema_200'])
    close = _f(latest['close'])
    if None in (ema20, ema50, ema200, close) or ema20 == 0:
        return 0.0, {}
    stack_c = 1.0 if (ema20 > ema50 * 1.05 and ema50 > ema200) else 0.0
    if not stack_c or close < ema20:
        return 0.0, {}
    dist = (close - ema20) / ema20                   # ≥ 0
    dist_c = _ramp(-dist, -0.05, 0.0)                # 1.0 at close = ema20, 0 at +5%
    score = (stack_c + dist_c) / 2 * 100
    return round(score, 1), {
        'pct_above_ema20': round(dist * 100, 2),
        'stack_ok': bool(stack_c),
    }


def score_rs_acceleration(latest):
    """rs_rank ≤ 200, rank_slope ≤ -3, close > EMA50, alkalyme_rs ≥ 55."""
    rs_rank = latest.get('rs_rank')
    rs_rank = int(rs_rank) if pd.notna(rs_rank) else None
    slope = _f(latest['rank_slope'])
    rs    = _f(latest['alkalyme_rs']) or 0
    close = _f(latest['close']); ema50 = _f(latest['ema_50'])
    if rs_rank is None or slope is None:
        return 0.0, {}
    rank_c  = _ramp(-rs_rank, -200, -100)            # 1.0 at rank ≤ 100, 0 at rank ≥ 200
    slope_c = _ramp(-slope, 3, 5)                    # 1.0 at slope ≤ -5, 0 at slope ≥ -3
    ema50_c = 1.0 if (ema50 and close and close > ema50) else 0.0
    rs_c    = _ramp(rs, 55, 65)                       # 1.0 at alkalyme_rs ≥ 65
    score = (rank_c + slope_c + ema50_c + rs_c) / 4 * 100
    return round(score, 1), {
        'rs_rank': rs_rank, 'rank_slope': slope, 'alkalyme_rs': rs,
        'gt_ema50': bool(ema50_c),
    }


# ── Driver ─────────────────────────────────────────────────────────────────
def main():
    print('=' * 70)
    print('PRESIGNAL SCORER  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 70)

    print('Pulling snapshots ...')
    rows = pull_all('daily_stock_snapshots',
                    'ticker, snapshot_date, close, ema_20, ema_50, ema_200, rsi_14, '
                    'weekly_rsi_ema_9, high_52w, vol_ratio, alkalyme_rs, rs_rank, rank_slope, '
                    'macd_line, macd_signal, macd_hist',
                    ['ticker', 'snapshot_date'])
    df = pd.DataFrame(rows)
    if df.empty:
        sys.exit('No data')
    df['snapshot_date'] = pd.to_datetime(df['snapshot_date']).dt.strftime('%Y-%m-%d')
    today_str = df['snapshot_date'].max()
    print(f'  {len(df):,} rows, {df["ticker"].nunique()} tickers, latest = {today_str}')

    print('Pulling sector map ...')
    sectors = pull_all('us_stock_sectors', 'ticker, sector', ['ticker'])
    sec_map = {r['ticker']: r['sector'] for r in sectors if r.get('sector')}

    scores, counters = [], {}
    for ticker, g in df.groupby('ticker'):
        g = g.sort_values('snapshot_date').reset_index(drop=True)
        if g['snapshot_date'].iloc[-1] != today_str:
            continue
        latest = g.iloc[-1]
        rs_rank = int(latest['rs_rank']) if pd.notna(latest['rs_rank']) else None

        def emit(rule, result):
            score, meta = result
            if score >= SCORE_THRESHOLD:
                counters[rule] = counters.get(rule, 0) + 1
                scores.append({
                    'ticker': ticker, 'rule': rule, 'snapshot_date': today_str,
                    'score': score, 'sector': sec_map.get(ticker),
                    'rs_rank': rs_rank, 'alkalyme_rs': _f(latest['alkalyme_rs']),
                    'close': _f(latest['close']), 'metadata': meta,
                })

        emit('bz_buy',           score_bz_buy(latest))
        emit('bz_strong',        score_bz_strong(latest))
        emit('golden_cross',     score_golden_cross(g.tail(7)))
        emit('macd',             score_macd(g.tail(5)))
        emit('pullback_bounce',  score_pullback_bounce(latest))
        emit('rs_acceleration',  score_rs_acceleration(latest))

    print(f'\nScored {len(scores)} entries (score >= {SCORE_THRESHOLD}):')
    for k, v in sorted(counters.items(), key=lambda x: -x[1]):
        print(f'  {k:<18} {v}')

    if scores:
        for i in range(0, len(scores), 500):
            sb.table('presignal_scores').upsert(
                scores[i:i + 500], on_conflict='ticker,rule,snapshot_date').execute()
        print(f'\nUpserted {len(scores)} rows into presignal_scores')

    # Prune rows older than 30 days
    cutoff = (datetime.strptime(today_str, '%Y-%m-%d').date()
              - timedelta(days=HISTORY_DAYS)).isoformat()
    try:
        sb.table('presignal_scores').delete().lt('snapshot_date', cutoff).execute()
        print(f'Pruned rows older than {cutoff}')
    except Exception as e:
        print(f'WARN prune: {e}')

    print('Done.')


if __name__ == '__main__':
    main()
