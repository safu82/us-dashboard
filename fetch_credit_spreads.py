#!/usr/bin/env python3
"""Populate market_health.credit_spread from FRED (keyless).

Pulls the ICE BofA US High-Yield Option-Adjusted Spread (series BAMLH0A0HYM2) —
the market's best early-warning gauge — via FRED's public CSV endpoint (no API
key needed) and writes it onto each market_health row (carry-forward for
weekends/holidays). Completes the 5th line of the newsletter's "Where We Stand".

Run after compute_market_health.py. Env: SUPABASE_URL, SUPABASE_SERVICE_KEY.
"""

import csv
import io
import os
import sys
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

SERIES = 'BAMLH0A0HYM2'          # ICE BofA US High Yield OAS (%), daily
FRED_CSV = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={SERIES}'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def fetch_fred_series(attempts=4):
    """Return sorted [(date_str, value_float)] from the FRED CSV (skips '.')"""
    import time
    last = None
    for a in range(1, attempts + 1):
        try:
            r = requests.get(FRED_CSV, headers={'User-Agent': UA}, timeout=60)
            r.raise_for_status()
            break
        except Exception as e:
            last = e
            print(f'  FRED fetch attempt {a} failed: {str(e)[:60]}; retrying ...')
            time.sleep(5 * a)
    else:
        raise RuntimeError(f'FRED unreachable after {attempts} attempts: {last}')
    rows = list(csv.reader(io.StringIO(r.text)))
    if not rows or len(rows[0]) < 2:
        raise ValueError(f'unexpected FRED CSV: {r.text[:120]!r}')
    out = []
    for date_s, val_s in rows[1:]:
        val_s = (val_s or '').strip()
        if val_s in ('', '.'):
            continue
        try:
            out.append((date_s.strip(), float(val_s)))
        except ValueError:
            continue
    out.sort(key=lambda x: x[0])
    return out


def value_on_or_before(series, target):
    """Latest series value with date <= target (carry-forward)."""
    chosen = None
    for d, v in series:
        if d <= target:
            chosen = v
        else:
            break
    return chosen


def main():
    print('=' * 60)
    print('FETCH CREDIT SPREADS  ',
          datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 60)

    # Credit spreads are an OPTIONAL enrichment — the newsletter handles a missing
    # reading honestly ("credit spreads: n/a"). A FRED outage must NOT fail the whole
    # weekly run, so degrade gracefully (exit 0) instead of raising.
    try:
        series = fetch_fred_series()
    except Exception as e:
        print(f'WARN: FRED unreachable — skipping credit spreads, leaving them n/a. {str(e)[:80]}')
        return
    if not series:
        print('WARN: no FRED data parsed — skipping credit spreads (left n/a).')
        return
    print(f'FRED {SERIES}: {len(series)} obs, latest {series[-1][0]} = {series[-1][1]}%')

    dates = [r['snapshot_date'] for r in
             (sb.table('market_health').select('snapshot_date').execute().data or [])]
    if not dates:
        sys.exit('market_health is empty — run compute_market_health.py first')

    updated = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for d in dates:
        v = value_on_or_before(series, d)
        if v is None:
            continue
        sb.table('market_health').update({'credit_spread': round(v, 2)}).eq(
            'snapshot_date', d).execute()
        updated += 1

    latest = max(dates)
    print(f'Updated credit_spread on {updated}/{len(dates)} market_health rows '
          f'(latest {latest} = {value_on_or_before(series, latest)}%)')


if __name__ == '__main__':
    main()
