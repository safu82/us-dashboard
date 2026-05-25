#!/usr/bin/env python3
"""Phase 3e — portfolio-only alerts scanner (structural subset of India's
integrated_alert_scanner.py — Trendlyne/Screener-driven types deferred).

Three alert types on holdings only:
  ema_crossover  — EMA20×EMA50 bullish or bearish cross today
  vol_breakout   — today vol_ratio ≥ 2 AND |day change| ≥ 3%
  blue_zone      — close within -10% of 52WH

Writes to `alerts`. 5-day rolling retention (older rows pruned).
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

RETENTION_DAYS = 5


def _f(x):
    if x is None:
        return None
    try:
        v = float(x)
        return v if not np.isnan(v) else None
    except (TypeError, ValueError):
        return None


def pull_holdings():
    res = sb.table('holdings').select('ticker').execute()
    return sorted({r['ticker'] for r in (res.data or []) if r.get('ticker')})


def pull_snapshots_for(tickers, lookback_days=10):
    """Pull last `lookback_days` snapshots for the given tickers."""
    rows = []
    for t in tickers:
        res = (sb.table('daily_stock_snapshots')
                 .select('ticker, snapshot_date, open, high, low, close, ema_20, ema_50, '
                         'high_52w, vol_ratio')
                 .eq('ticker', t)
                 .order('snapshot_date', desc=True)
                 .limit(lookback_days).execute())
        rows.extend(res.data or [])
    return rows


def pull_sectors(tickers):
    res = sb.table('us_stock_sectors').select('ticker, sector').in_('ticker', tickers).execute()
    return {r['ticker']: r['sector'] for r in (res.data or []) if r.get('sector')}


def main():
    print('=' * 70)
    print('PORTFOLIO ALERTS SCANNER  ',
          datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 70)

    tickers = pull_holdings()
    print(f'Holdings universe: {len(tickers)} tickers')
    if not tickers:
        sys.exit('No holdings')

    raw = pull_snapshots_for(tickers)
    df = pd.DataFrame(raw)
    if df.empty:
        sys.exit('No snapshot rows')
    df['snapshot_date'] = pd.to_datetime(df['snapshot_date']).dt.strftime('%Y-%m-%d')
    today_str = df['snapshot_date'].max()
    print(f'  {len(df)} rows pulled, latest = {today_str}')

    sec_map = pull_sectors(tickers)

    alerts, counters = [], {}

    def emit(ticker, alert_type, direction, close, meta):
        counters[alert_type] = counters.get(alert_type, 0) + 1
        alerts.append({
            'ticker': ticker, 'alert_type': alert_type, 'alert_date': today_str,
            'direction': direction, 'close': close, 'sector': sec_map.get(ticker),
            'metadata': meta,
        })

    for ticker, g in df.groupby('ticker'):
        g = g.sort_values('snapshot_date').reset_index(drop=True)
        if g['snapshot_date'].iloc[-1] != today_str:
            continue
        latest = g.iloc[-1]
        close = _f(latest['close']); high52 = _f(latest['high_52w'])
        ema20 = _f(latest['ema_20']); ema50 = _f(latest['ema_50'])
        vol_r = _f(latest['vol_ratio']) or 0

        # 1) EMA crossover — today vs yesterday
        if len(g) >= 2:
            prev = g.iloc[-2]
            p20, p50 = _f(prev['ema_20']), _f(prev['ema_50'])
            if None not in (ema20, ema50, p20, p50):
                if p20 <= p50 and ema20 > ema50:
                    emit(ticker, 'ema_crossover', 'bull', close,
                         {'ema_20': ema20, 'ema_50': ema50})
                elif p20 >= p50 and ema20 < ema50:
                    emit(ticker, 'ema_crossover', 'bear', close,
                         {'ema_20': ema20, 'ema_50': ema50})

        # 2) Vol breakout — vol_ratio ≥ 2 AND |today % change| ≥ 3%
        if len(g) >= 2 and vol_r >= 2.0:
            prev_close = _f(g.iloc[-2]['close'])
            if close and prev_close:
                day_pct = (close - prev_close) / prev_close * 100
                if abs(day_pct) >= 3.0:
                    emit(ticker, 'vol_breakout', 'up' if day_pct > 0 else 'down', close,
                         {'vol_ratio': vol_r, 'day_change_pct': round(day_pct, 2)})

        # 3) Blue zone — close within -10% of 52WH
        if close and high52 and close >= high52 * 0.90:
            emit(ticker, 'blue_zone', None, close,
                 {'pct_from_52wh': round((close / high52 - 1) * 100, 2)})

    print(f'\nDetected {len(alerts)} portfolio alerts:')
    for k, v in sorted(counters.items(), key=lambda x: -x[1]):
        print(f'  {k:<14} {v}')

    if alerts:
        for i in range(0, len(alerts), 500):
            chunk = alerts[i:i + 500]
            # `direction` is part of the unique key but can be NULL (for blue_zone);
            # Supabase upsert handles NULL in unique keys correctly only with explicit conflict target.
            sb.table('alerts').upsert(
                chunk, on_conflict='ticker,alert_type,alert_date,direction').execute()
        print(f'\nUpserted {len(alerts)} rows into alerts')

    # 5-day rolling retention
    cutoff = (datetime.strptime(today_str, '%Y-%m-%d').date()
              - timedelta(days=RETENTION_DAYS)).isoformat()
    try:
        sb.table('alerts').delete().lt('alert_date', cutoff).execute()
        print(f'Pruned alerts older than {cutoff}')
    except Exception as e:
        print(f'WARN prune: {e}')

    print('Done.')


if __name__ == '__main__':
    main()
