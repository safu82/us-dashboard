#!/usr/bin/env python3
"""Phase 3b — entry_signals scanner (US port of India PRODUCTION_entry_signals_scanner.py).

8 patterns scanned daily across the S&P 500 ∪ NASDAQ 100 ∪ holdings universe:
  Narrow CPR · Blue Zone · Golden Cross · MACD · Darvas Box · Pullback Bounce · Bull Flag · VCP

All except Narrow CPR gate on rs_rank <= 125.

Pulls 60d of daily_stock_snapshots (needed for Darvas 60d window), detects
patterns on the latest bar, writes to entry_signals (PK = ticker+pattern+date,
5-day expires_at). Idempotent — re-runs on same data overwrite the same rows.
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

RS_RANK_GATE = 125
EXPIRY_DAYS = 5


# ── Data pull (paginated) ──────────────────────────────────────────────────
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


# ── Pattern detectors ──────────────────────────────────────────────────────
# Each returns (strength, metadata_dict) or None.

def _f(x):
    """Safe numeric conversion; returns None for NaN/None/missing."""
    if x is None:
        return None
    try:
        v = float(x)
        return v if not np.isnan(v) else None
    except (TypeError, ValueError):
        return None


def detect_narrow_cpr(df_tail):
    """Narrow CPR breakout. CPR width < 0.3% of pivot, close within 0.5% of TC, vol > 1.2×."""
    if len(df_tail) < 2:
        return None
    prev = df_tail.iloc[-2]
    today = df_tail.iloc[-1]
    h, l, c = _f(prev['high']), _f(prev['low']), _f(prev['close'])
    if None in (h, l, c) or c == 0:
        return None
    pivot = (h + l + c) / 3
    bc    = (h + l) / 2
    tc    = 2 * pivot - bc
    cpr_width_pct = abs(tc - bc) / pivot if pivot else 0
    close_t = _f(today['close'])
    vol_r   = _f(today['vol_ratio']) or 0
    if close_t is None:
        return None
    near_tc = abs(close_t - tc) / tc < 0.005 if tc else False
    if cpr_width_pct < 0.003 and near_tc and vol_r > 1.2:
        return ('Strong', {'cpr_width_pct': round(cpr_width_pct * 100, 3),
                           'tc': round(tc, 2), 'vol_ratio': vol_r})
    return None


def detect_blue_zone(latest):
    """Within -10% of 52WH. Strong: RSI_D>=72 & RSI_W>=65 & >EMA50 & vol>1.5×. Buy: 65/55 & >EMA20."""
    close = _f(latest['close']); high52 = _f(latest['high_52w'])
    if close is None or not high52 or close < high52 * 0.90:
        return None
    rsi_d = _f(latest['rsi_14']) or 0
    rsi_w = _f(latest['weekly_rsi_ema_9']) or 0
    ema20 = _f(latest['ema_20']); ema50 = _f(latest['ema_50'])
    vol_r = _f(latest['vol_ratio']) or 0
    meta = {'pct_from_52wh': round((close / high52 - 1) * 100, 2),
            'rsi_d': rsi_d, 'rsi_w': rsi_w, 'vol_ratio': vol_r}
    if (rsi_d >= 72 and rsi_w >= 65 and ema50 and close > ema50 and vol_r > 1.5):
        return ('Strong', meta)
    if (rsi_d >= 65 and rsi_w >= 55 and ema20 and close > ema20):
        return ('Buy', meta)
    return None


def detect_golden_cross(latest, today_str):
    """gc_crossover_date in last 1-7 days. Tier by days since + filters."""
    gc = latest.get('gc_crossover_date')
    if not gc:
        return None
    try:
        days = (datetime.strptime(today_str, '%Y-%m-%d').date()
                - datetime.strptime(gc, '%Y-%m-%d').date()).days
    except Exception:
        return None
    if days < 1 or days > 7:
        return None
    close = _f(latest['close']); ema200 = _f(latest['ema_200'])
    vol_r = _f(latest['vol_ratio']) or 0
    rsi   = _f(latest['rsi_14']) or 0
    adx   = _f(latest['adx_14']) or 0
    meta = {'days_since_cross': days, 'vol_ratio': vol_r, 'rsi_14': rsi, 'adx_14': adx}
    if (1 <= days <= 3 and ema200 and close and close > ema200
            and vol_r > 2 and rsi > 55 and adx > 25):
        return ('Strong', meta)
    if (4 <= days <= 7 and vol_r > 1.5 and rsi > 45):
        return ('Buy', meta)
    return None


def detect_macd(df_tail):
    """MACD line crossed above signal recently. Tier by days + histogram + ADX."""
    if len(df_tail) < 6:
        return None
    macd  = df_tail['macd_line'].apply(_f).values
    sig   = df_tail['macd_signal'].apply(_f).values
    hist  = df_tail['macd_hist'].apply(_f).values
    # Find days since the most recent bullish cross (line crossed above signal).
    days_since = None
    for k in range(1, min(6, len(macd))):
        prev, cur = macd[-k - 1], macd[-k]
        psg, csg = sig[-k - 1], sig[-k]
        if None in (prev, cur, psg, csg):
            continue
        if prev <= psg and cur > csg:
            days_since = k - 1 if k > 1 else 0
            # Adjust: days since the cross bar (0 = crossed today, 1 = yesterday, ...)
            days_since = len(macd) - 1 - (len(macd) - 1 - k)  # = k positions from today
            days_since = k - 1 if k >= 1 else 0
            # Simplest: index from end. k=1 means cross between t-1 and t (today). days_since = 0.
            days_since = k - 1
            break
    if days_since is None or days_since > 4:
        return None
    today = df_tail.iloc[-1]
    macd_t, sig_t, hist_t = _f(today['macd_line']), _f(today['macd_signal']), _f(today['macd_hist'])
    adx_t = _f(today['adx_14']) or 0
    hist_yest = _f(df_tail.iloc[-2]['macd_hist']) if len(df_tail) >= 2 else None
    expanding = (hist_t is not None and hist_yest is not None and hist_t > hist_yest)
    meta = {'days_since_cross': days_since, 'macd_line': macd_t, 'macd_signal': sig_t,
            'macd_hist': hist_t, 'adx_14': adx_t}
    if (0 <= days_since <= 2 and expanding
            and macd_t and sig_t and macd_t > 0 and sig_t > 0 and adx_t > 25):
        return ('Strong', meta)
    if (3 <= days_since <= 4 and hist_t is not None and hist_t > 0):
        return ('Buy', meta)
    return None


def detect_darvas(df_tail, latest):
    """10-60d box, depth <15%, today's high > top×1.002, vol>1.5×, close within 1% of 52WH."""
    close = _f(latest['close']); high52 = _f(latest['high_52w'])
    if close is None or not high52 or close < high52 * 0.99:
        return None
    vol_r = _f(latest['vol_ratio']) or 0
    if vol_r <= 1.5:
        return None
    # Look for a tight box across some lookback in [10, 60]
    box = None
    for window in range(60, 9, -5):
        if len(df_tail) < window + 1:
            continue
        prior = df_tail.iloc[-window - 1:-1]  # exclude today
        hi = pd.to_numeric(prior['high'], errors='coerce').max()
        lo = pd.to_numeric(prior['low'], errors='coerce').min()
        if not hi or not lo or lo == 0:
            continue
        depth = (hi - lo) / lo
        today_high = _f(latest['high'])
        if depth < 0.15 and today_high and today_high > hi * 1.002:
            box = {'window': window, 'box_top': round(hi, 2),
                   'box_bottom': round(lo, 2), 'depth_pct': round(depth * 100, 2),
                   'vol_ratio': vol_r}
            break
    if box is None:
        return None
    return ('Strong', box)


def detect_pullback_bounce(latest):
    """EMA20>EMA50×1.05>EMA200; low<=EMA20; close in upper half of (low, high)."""
    ema20 = _f(latest['ema_20']); ema50 = _f(latest['ema_50']); ema200 = _f(latest['ema_200'])
    low   = _f(latest['low']); high = _f(latest['high']); close = _f(latest['close'])
    vol_r = _f(latest['vol_ratio']) or 0
    if None in (ema20, ema50, ema200, low, high, close):
        return None
    if not (ema20 > ema50 * 1.05 and ema50 > ema200):
        return None
    if low > ema20:                                # we want a tag of EMA20 from above
        return None
    if close < (low + high) / 2:                   # close in upper half of day
        return None
    meta = {'low': low, 'ema20': round(ema20, 2), 'close': close, 'vol_ratio': vol_r}
    if vol_r >= 1.5:
        return ('Strong', meta)
    if vol_r >= 1.2:
        return ('Buy', meta)
    return None


def detect_bull_flag(df_tail, latest, rs_rank):
    """Pole 5-15d ≥15% ≥2× vol; flag 3-15d retrace ≤50%; within 10% of 52WH."""
    close = _f(latest['close']); high52 = _f(latest['high_52w'])
    if close is None or not high52 or close < high52 * 0.90:
        return None
    if len(df_tail) < 8:
        return None
    closes = pd.to_numeric(df_tail['close'], errors='coerce').values
    vols   = pd.to_numeric(df_tail['volume'], errors='coerce').values
    n = len(closes)
    avg_vol = np.nanmean(vols[:-20]) if n >= 20 else np.nanmean(vols)
    if not avg_vol:
        return None
    best = None
    # Flag window: last 3-15 bars
    for flag_len in range(3, 16):
        if n < flag_len + 5:
            continue
        flag_window = closes[-flag_len:]
        flag_peak = np.nanmax(flag_window)
        flag_trough = np.nanmin(flag_window)
        # Pole window: 5-15 bars BEFORE the flag
        for pole_len in range(5, 16):
            if n < flag_len + pole_len + 1:
                continue
            pole_start = closes[-(flag_len + pole_len)]
            pole_end   = closes[-(flag_len + 1)]
            if not pole_start or not pole_end:
                continue
            pole_gain = (pole_end - pole_start) / pole_start
            if pole_gain < 0.15:
                continue
            pole_vols = vols[-(flag_len + pole_len):-flag_len]
            pole_vol_mean = np.nanmean(pole_vols) if len(pole_vols) else 0
            if not pole_vol_mean or pole_vol_mean < 2 * avg_vol:
                continue
            retrace = (pole_end - flag_trough) / (pole_end - pole_start) if (pole_end - pole_start) else 0
            if retrace > 0.50:
                continue
            cand = {'pole_len': pole_len, 'flag_len': flag_len,
                    'pole_gain_pct': round(pole_gain * 100, 2),
                    'retrace_pct': round(retrace * 100, 2),
                    'pole_vol_mult': round(pole_vol_mean / avg_vol, 2)}
            if best is None or cand['pole_gain_pct'] > best['pole_gain_pct']:
                best = cand
        if best:
            break
    if not best:
        return None
    if best['pole_gain_pct'] >= 20 and best['pole_vol_mult'] >= 3 and rs_rank is not None and rs_rank <= 75:
        return ('Strong', best)
    if rs_rank is not None and rs_rank <= 125:
        return ('Buy', best)
    return None


def detect_vcp(df_tail, rs_rank):
    """≥3 progressively tighter pullbacks, latest <15%, recent vol <60% of 50d, EMA50 rising 4w."""
    closes = pd.to_numeric(df_tail['close'], errors='coerce').values
    vols   = pd.to_numeric(df_tail['volume'], errors='coerce').values
    ema50  = pd.to_numeric(df_tail['ema_50'], errors='coerce').values
    n = len(closes)
    if n < 50:
        return None
    # EMA50 rising over last 4 weeks (~20 bars)
    if ema50[-20] is None or ema50[-1] is None or ema50[-1] <= ema50[-20]:
        return None
    # Walk recent peaks/troughs (60-bar window) to find progressively tighter pullbacks.
    window = closes[-60:] if n >= 60 else closes
    peaks_troughs = []
    for i in range(2, len(window) - 2):
        if window[i] > window[i - 1] and window[i] > window[i + 1] and window[i] > window[i - 2] and window[i] > window[i + 2]:
            peaks_troughs.append(('P', i, window[i]))
        elif window[i] < window[i - 1] and window[i] < window[i + 1] and window[i] < window[i - 2] and window[i] < window[i + 2]:
            peaks_troughs.append(('T', i, window[i]))
    # Pull pullback depths in order
    pullbacks = []
    last_peak = None
    for kind, idx, val in peaks_troughs:
        if kind == 'P':
            last_peak = val
        elif kind == 'T' and last_peak is not None and last_peak > 0:
            pullbacks.append((last_peak - val) / last_peak)
            last_peak = None
    if len(pullbacks) < 3:
        return None
    recent = pullbacks[-3:]                            # last 3 contractions
    tighter = all(recent[i] < recent[i - 1] for i in range(1, len(recent)))
    if not tighter:
        return None
    latest_depth = recent[-1]
    if latest_depth >= 0.15:
        return None
    # Vol dry-up: last 10 bars vol < 60% of 50d avg
    if len(vols) < 50:
        return None
    vol_50 = np.nanmean(vols[-50:])
    vol_10 = np.nanmean(vols[-10:])
    if not vol_50 or vol_10 >= 0.60 * vol_50:
        return None
    meta = {'n_contractions': len(pullbacks), 'latest_depth_pct': round(latest_depth * 100, 2),
            'vol_dryup_pct': round(vol_10 / vol_50 * 100, 1)}
    if (len(pullbacks) >= 4 and latest_depth < 0.10 and rs_rank is not None and rs_rank <= 75):
        return ('Strong', meta)
    if rs_rank is not None and rs_rank <= 125:
        return ('Buy', meta)
    return None


# ── Scanner driver ─────────────────────────────────────────────────────────
def main():
    print('=' * 70)
    print('ENTRY SIGNALS SCANNER  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 70)

    print('Pulling daily_stock_snapshots ...')
    rows = pull_all('daily_stock_snapshots',
                    'ticker, snapshot_date, open, high, low, close, volume, ema_20, ema_50, ema_200, '
                    'rsi_14, weekly_rsi_ema_9, high_52w, vol_ratio, alkalyme_rs, rs_rank, '
                    'macd_line, macd_signal, macd_hist, adx_14, bz_streak, gc_crossover_date, '
                    'sector_composite_pct',
                    ['ticker', 'snapshot_date'])
    df = pd.DataFrame(rows)
    if df.empty:
        sys.exit('No snapshot data')
    df['snapshot_date'] = pd.to_datetime(df['snapshot_date']).dt.strftime('%Y-%m-%d')
    today_str = df['snapshot_date'].max()
    print(f'  {len(df):,} rows, universe = {df["ticker"].nunique()} tickers, latest = {today_str}')

    print('Pulling sector map ...')
    sectors = pull_all('us_stock_sectors', 'ticker, sector', ['ticker'])
    sec_map = {r['ticker']: r['sector'] for r in sectors if r.get('sector')}

    expires_at = (datetime.strptime(today_str, '%Y-%m-%d').date()
                  + timedelta(days=EXPIRY_DAYS)).isoformat()

    signals, counters = [], {}
    for ticker, g in df.groupby('ticker'):
        g = g.sort_values('snapshot_date').reset_index(drop=True)
        if g['snapshot_date'].iloc[-1] != today_str:    # missing today's bar — skip
            continue
        latest = g.iloc[-1]
        rs_rank = int(latest['rs_rank']) if pd.notna(latest['rs_rank']) else None
        # rs_rank gate (all except Narrow CPR)
        gated_ok = rs_rank is not None and rs_rank <= RS_RANK_GATE

        def emit(pattern, result):
            if result is None:
                return
            strength, meta = result
            counters[pattern] = counters.get(pattern, 0) + 1
            signals.append({
                'ticker': ticker, 'pattern': pattern, 'signal_strength': strength,
                'alert_date': today_str, 'expires_at': expires_at,
                'sector': sec_map.get(ticker),
                'rs_rank': rs_rank,
                'alkalyme_rs': _f(latest['alkalyme_rs']),
                'sector_composite_pct': _f(latest['sector_composite_pct']),
                'close': _f(latest['close']),
                'metadata': meta,
            })

        # Narrow CPR — no rs_rank gate
        emit('narrow_cpr', detect_narrow_cpr(g.tail(3)))
        if not gated_ok:
            continue
        emit('blue_zone',       detect_blue_zone(latest))
        emit('golden_cross',    detect_golden_cross(latest, today_str))
        emit('macd',            detect_macd(g.tail(6)))
        emit('pullback_bounce', detect_pullback_bounce(latest))
        emit('darvas',          detect_darvas(g.tail(61), latest))
        emit('bull_flag',       detect_bull_flag(g.tail(32), latest, rs_rank))
        emit('vcp',             detect_vcp(g.tail(60), rs_rank))

    print(f'Detected {len(signals)} signals:')
    for k, v in sorted(counters.items(), key=lambda x: -x[1]):
        print(f'  {k:<18} {v}')

    if signals:
        for i in range(0, len(signals), 500):
            sb.table('entry_signals').upsert(
                signals[i:i + 500], on_conflict='ticker,pattern,alert_date').execute()
        print(f'Upserted {len(signals)} rows into entry_signals')
    else:
        print('No signals to write.')

    print('Done.')


if __name__ == '__main__':
    main()
