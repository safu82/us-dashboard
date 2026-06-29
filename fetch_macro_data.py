#!/usr/bin/env python3
"""Macro + rates snapshot from FRED (keyless) -> macro_indicators.

Pulls the policy/rates data the newsletter was missing — Treasury yields and the
2s10s curve, plus the inflation, jobs and Fed-funds prints — from FRED's public
CSV endpoint (no API key), computes current levels + week-over-week change +
year-over-year inflation, and writes a single latest row to macro_indicators.

Series:
  DGS10 / DGS2   10y / 2y Treasury yields (%, daily)   -> levels, 2s10s, WoW bp
  CPIAUCSL       CPI all-urban (index, monthly)        -> headline CPI YoY %
  CPILFESL       core CPI (index, monthly)             -> core CPI YoY %
  UNRATE         unemployment rate (%, monthly)        -> latest
  PAYEMS         nonfarm payrolls (000s, monthly)      -> last monthly change (k)
  DFEDTARU       fed funds target upper (%, daily)     -> latest

FRED is egress-blocked in some sandboxes; this is built to run in CI / locally.
Run weekly before build_data_pack.py. Env: SUPABASE_URL, SUPABASE_SERVICE_KEY.
"""

import csv
import io
import os
import sys
import time
from datetime import datetime, timezone

import requests
from supabase import create_client
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

sb = create_client(URL, KEY)

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def fred(series_id, attempts=4):
    """Return sorted [(date_str, value_float)] for a FRED series (skips '.')."""
    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}'
    last = None
    for a in range(1, attempts + 1):
        try:
            r = requests.get(url, headers={'User-Agent': UA}, timeout=60)
            r.raise_for_status()
            break
        except Exception as e:
            last = e
            print(f'  {series_id} attempt {a} failed: {str(e)[:50]}; retrying ...')
            time.sleep(5 * a)
    else:
        raise RuntimeError(f'FRED unreachable for {series_id}: {last}')
    out = []
    for row in list(csv.reader(io.StringIO(r.text)))[1:]:
        if len(row) < 2:
            continue
        v = (row[1] or '').strip()
        if v in ('', '.'):
            continue
        try:
            out.append((row[0].strip(), float(v)))
        except ValueError:
            continue
    out.sort(key=lambda x: x[0])
    return out


def on_or_before(series, target):
    chosen = None
    for d, v in series:
        if d <= target:
            chosen = (d, v)
        else:
            break
    return chosen


def days_before(series, ref_date, days):
    """Value on-or-before (ref_date - days)."""
    from datetime import date, timedelta
    tgt = (date.fromisoformat(ref_date) - timedelta(days=days)).isoformat()
    hit = on_or_before(series, tgt)
    return hit[1] if hit else None


def yoy(series):
    """Latest value vs ~12 months earlier, as a percent change."""
    if not series:
        return None, None
    d_latest, v_latest = series[-1]
    prior = days_before(series, d_latest, 365)
    return (((v_latest / prior) - 1) * 100 if prior else None), d_latest


def main():
    print('=' * 60)
    print('FETCH MACRO DATA  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 60)

    dgs10 = fred('DGS10')
    dgs2 = fred('DGS2')
    cpi = fred('CPIAUCSL')
    core = fred('CPILFESL')
    unrate = fred('UNRATE')
    payems = fred('PAYEMS')
    fedfunds = fred('DFEDTARU')

    if not dgs10:
        sys.exit('No DGS10 data — aborting')

    snap = dgs10[-1][0]                       # latest 10y obs date = snapshot date
    ten = dgs10[-1][1]
    two = dgs2[-1][1] if dgs2 else None
    ten_wk = days_before(dgs10, snap, 7)
    two_wk = days_before(dgs2, dgs2[-1][0], 7) if dgs2 else None

    cpi_yoy, _ = yoy(cpi)
    core_yoy, _ = yoy(core)
    nonfarm_chg = (payems[-1][1] - payems[-2][1]) if len(payems) >= 2 else None

    row = {
        'snapshot_date': snap,
        'ust_10y': round(ten, 2) if ten is not None else None,
        'ust_2y': round(two, 2) if two is not None else None,
        'spread_10y_2y': round((ten - two) * 100, 0) if (ten is not None and two is not None) else None,
        'ust_10y_chg_bp': round((ten - ten_wk) * 100, 0) if ten_wk is not None else None,
        'ust_2y_chg_bp': round((two - two_wk) * 100, 0) if (two is not None and two_wk is not None) else None,
        'cpi_yoy': round(cpi_yoy, 1) if cpi_yoy is not None else None,
        'core_cpi_yoy': round(core_yoy, 1) if core_yoy is not None else None,
        'unemployment': round(unrate[-1][1], 1) if unrate else None,
        'nonfarm_chg_k': round(nonfarm_chg, 0) if nonfarm_chg is not None else None,
        'fed_funds': round(fedfunds[-1][1], 2) if fedfunds else None,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    }
    sb.table('macro_indicators').upsert(row, on_conflict='snapshot_date').execute()

    print(f"snapshot {snap}")
    print(f"  10y {row['ust_10y']}% ({row['ust_10y_chg_bp']:+}bp wk), "
          f"2y {row['ust_2y']}%, 2s10s {row['spread_10y_2y']}bp")
    print(f"  CPI YoY {row['cpi_yoy']}%, core {row['core_cpi_yoy']}%, "
          f"unemployment {row['unemployment']}%, last NFP {row['nonfarm_chg_k']}k, "
          f"fed funds {row['fed_funds']}%")
    print('Done.')


if __name__ == '__main__':
    main()
