#!/usr/bin/env python3
"""Macro + rates snapshot -> macro_indicators.  FRED primary, Alpha Vantage fallback.

Pulls the policy/rates data the newsletter was missing — Treasury yields and the
2s10s curve, plus inflation, jobs and Fed-funds prints — computes current levels +
week-over-week change + year-over-year inflation, and writes a single latest row to
macro_indicators.

Source order:
  1. FRED public CSV (keyless, unlimited) — preferred.
  2. Alpha Vantage economic indicators — fallback when FRED is unreachable (some
     sandboxes block FRED egress). Needs ALPHAVANTAGE_API_KEY.

FRED series / AV functions:
  10y/2y yields   DGS10 / DGS2          TREASURY_YIELD(maturity=10year/2year)
  headline CPI    CPIAUCSL              CPI(interval=monthly)
  core CPI        CPILFESL              (AV has no core CPI -> null on fallback)
  unemployment    UNRATE                UNEMPLOYMENT
  nonfarm chg     PAYEMS                NONFARM_PAYROLL
  fed funds       DFEDTARU (target)     FEDERAL_FUNDS_RATE (effective)

Run weekly before build_data_pack.py. Env: SUPABASE_URL, SUPABASE_SERVICE_KEY,
and ALPHAVANTAGE_API_KEY (only used if FRED fails).
"""

import csv
import io
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

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

# A single month's nonfarm-payrolls change beyond this (in thousands) is, outside a
# COVID-scale dislocation, almost always a data glitch — most often Alpha Vantage's
# NONFARM_PAYROLL month-diff landing on a partial/misaligned month (it once returned
# +741k). We'd rather publish "n/a" than a hot number a macro-literate reader will
# instantly distrust. Normal monthly NFP runs ~100–300k; even blowout prints ~500k.
NFP_PLAUSIBLE_ABS_K = 600

AV_KEY = os.environ.get('ALPHAVANTAGE_API_KEY')
AV_URL = 'https://www.alphavantage.co/query'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


# ── shared series helpers (work on ascending [(date, value)] lists) ──────────
def on_or_before(series, target):
    chosen = None
    for d, v in series:
        if d <= target:
            chosen = (d, v)
        else:
            break
    return chosen


def days_before(series, ref_date, days):
    tgt = (date.fromisoformat(ref_date) - timedelta(days=days)).isoformat()
    hit = on_or_before(series, tgt)
    return hit[1] if hit else None


def yoy(series):
    if not series:
        return None
    d_latest, v_latest = series[-1]
    prior = days_before(series, d_latest, 365)
    return ((v_latest / prior) - 1) * 100 if prior else None


# ── FRED (primary) ───────────────────────────────────────────────────────────
def fred(series_id, attempts=4):
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
            time.sleep(4 * a)
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


def gather_fred():
    return dict(ten=fred('DGS10'), two=fred('DGS2'), cpi=fred('CPIAUCSL'),
                core=fred('CPILFESL'), unrate=fred('UNRATE'),
                payems=fred('PAYEMS'), fed=fred('DFEDTARU'))


# ── Alpha Vantage (fallback) ─────────────────────────────────────────────────
def av_series(function, pause=13, **extra):
    """One AV economic-indicator call -> ascending [(date, value)]."""
    params = {'function': function, 'apikey': AV_KEY, **extra}
    r = requests.get(AV_URL, params=params, headers={'User-Agent': UA}, timeout=30)
    r.raise_for_status()
    j = r.json()
    if 'data' not in j:                       # Note/Information => rate limit or bad key
        raise RuntimeError(f"AV {function}: {str(j)[:120]}")
    out = []
    for d in j['data']:
        v = (d.get('value') or '').strip()
        if v in ('', '.'):
            continue
        try:
            out.append((d['date'], float(v)))
        except (ValueError, KeyError):
            continue
    out.sort(key=lambda x: x[0])
    time.sleep(pause)                         # free tier: <=5 calls/min
    return out


def gather_av():
    if not AV_KEY:
        raise RuntimeError('ALPHAVANTAGE_API_KEY not set — cannot fall back')
    return dict(
        ten=av_series('TREASURY_YIELD', interval='daily', maturity='10year'),
        two=av_series('TREASURY_YIELD', interval='daily', maturity='2year'),
        cpi=av_series('CPI', interval='monthly'),
        core=[],                              # AV has no core CPI series
        unrate=av_series('UNEMPLOYMENT'),
        # AV's NONFARM_PAYROLL is the NON-seasonally-adjusted level: its month-over-month
        # diff is dominated by seasonal swings (~-2.6M every January, big spring ramps) and
        # is NOT the headline seasonally-adjusted jobs number markets react to. Deriving NFP
        # from it produces plausible-looking but wrong figures (e.g. +432k, +962k). Only FRED
        # PAYEMS (seasonally adjusted) is trustworthy here — on the AV fallback, leave payrolls
        # empty so the change renders n/a rather than a seasonal artifact.
        payems=[],
        fed=av_series('FEDERAL_FUNDS_RATE', interval='monthly'))


# ── compute the row from whichever source supplied the series ────────────────
def compute_row(ten, two, cpi, core, unrate, payems, fed):
    if not ten:
        raise ValueError('no 10-year yield data')
    snap = ten[-1][0]
    t10 = ten[-1][1]
    t2 = two[-1][1] if two else None
    t10_wk = days_before(ten, snap, 7)
    t2_wk = days_before(two, two[-1][0], 7) if two else None
    cpi_yoy = yoy(cpi)
    core_yoy = yoy(core)
    nf = (payems[-1][1] - payems[-2][1]) if len(payems) >= 2 else None
    if nf is not None and abs(nf) > NFP_PLAUSIBLE_ABS_K:
        print(f"  WARNING: nonfarm-payrolls month change {nf:+.0f}k exceeds the plausible "
              f"band (+/-{NFP_PLAUSIBLE_ABS_K}k) — treating as a data glitch, storing n/a.")
        nf = None
    return {
        'snapshot_date': snap,
        'ust_10y': round(t10, 2),
        'ust_2y': round(t2, 2) if t2 is not None else None,
        'spread_10y_2y': round((t10 - t2) * 100, 0) if t2 is not None else None,
        'ust_10y_chg_bp': round((t10 - t10_wk) * 100, 0) if t10_wk is not None else None,
        'ust_2y_chg_bp': round((t2 - t2_wk) * 100, 0) if (t2 is not None and t2_wk is not None) else None,
        'cpi_yoy': round(cpi_yoy, 1) if cpi_yoy is not None else None,
        'core_cpi_yoy': round(core_yoy, 1) if core_yoy is not None else None,
        'unemployment': round(unrate[-1][1], 1) if unrate else None,
        'nonfarm_chg_k': round(nf, 0) if nf is not None else None,
        'fed_funds': round(fed[-1][1], 2) if fed else None,
        'updated_at': datetime.now(timezone.utc).isoformat(),
    }


def main():
    print('=' * 60)
    print('FETCH MACRO DATA  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 60)

    source = 'FRED'
    try:
        series = gather_fred()
    except Exception as e:
        print(f'FRED unavailable ({str(e)[:70]}); falling back to Alpha Vantage ...')
        series = gather_av()
        source = 'AlphaVantage'

    row = compute_row(**series)
    sb.table('macro_indicators').upsert(row, on_conflict='snapshot_date').execute()

    sp = row['spread_10y_2y']
    print(f"[{source}] snapshot {row['snapshot_date']}")
    print(f"  10y {row['ust_10y']}% ({row['ust_10y_chg_bp']:+g}bp wk), 2y {row['ust_2y']}%, "
          f"2s10s {sp:+g}bp" if sp is not None else f"  10y {row['ust_10y']}%")
    print(f"  CPI YoY {row['cpi_yoy']}%, core {row['core_cpi_yoy']}%, "
          f"unemployment {row['unemployment']}%, last NFP {row['nonfarm_chg_k']}k, "
          f"fed funds {row['fed_funds']}%")
    print('Done.')


if __name__ == '__main__':
    main()
