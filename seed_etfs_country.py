#!/usr/bin/env python3
"""Seed the single-country ETF universe for the geographic rotation board.

Idempotent: upserts into `etfs` (keyed on symbol), same as seed_etfs.py. These
carry kind='country', which is what compute_etf_momentum.py reads to decide
which cross-section a fund is ranked in.

Why a separate ranked set rather than adding these to the existing board:
momentum here is a percentile against the pool, so ranking Brazil against a US
software fund would quietly change what both scores mean. "Which theme is
working" and "which country is working" are different questions and each needs
its own denominator. The code change that enforces this is the groupby in
compute_etf_momentum.py; this file only decides who is in the pool.

Why these are US-listed iShares lines and not local or UCITS listings: every
one trades in USD on the NYSE calendar, so it goes through the existing
pipeline untouched. A EUR-priced listing measured against ^GSPC would carry the
dollar inside its relative strength, and a European listing would be missing
bars on Thanksgiving and carrying them on Easter Monday.

The three regional funds are benchmark_only, exactly as SPY and QQQ are on the
US board: they are context for reading the single countries, not competitors
for rank. A diversified basket of 24 countries would otherwise sit mid-pack
forever by construction.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (.env auto-loaded).
Usage: python seed_etfs_country.py
"""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

sb = create_client(URL, KEY)

# Grouping follows MSCI's own developed/emerging split rather than geography
# alone, because that is the line money actually rotates across. Korea and
# Taiwan sit in emerging Asia on that basis despite their economies.
#
# (symbol, country, region)
COUNTRIES = [
    ('EWC',  'Canada',        'Americas'),
    ('EWZ',  'Brazil',        'Americas'),
    ('EWW',  'Mexico',        'Americas'),
    ('ECH',  'Chile',         'Americas'),

    ('EWU',  'United Kingdom', 'Developed Europe'),
    ('EWG',  'Germany',        'Developed Europe'),
    ('EWQ',  'France',         'Developed Europe'),
    ('EWL',  'Switzerland',    'Developed Europe'),
    ('EWN',  'Netherlands',    'Developed Europe'),
    ('EWP',  'Spain',          'Developed Europe'),
    ('EWI',  'Italy',          'Developed Europe'),
    ('EWD',  'Sweden',         'Developed Europe'),

    ('EPOL', 'Poland',        'Emerging Europe / MEA'),
    ('TUR',  'Turkey',        'Emerging Europe / MEA'),
    ('KSA',  'Saudi Arabia',  'Emerging Europe / MEA'),
    ('EZA',  'South Africa',  'Emerging Europe / MEA'),

    ('EWJ',  'Japan',         'Developed Asia-Pacific'),
    ('EWA',  'Australia',     'Developed Asia-Pacific'),
    ('EWS',  'Singapore',     'Developed Asia-Pacific'),
    ('EWH',  'Hong Kong',     'Developed Asia-Pacific'),

    ('INDA', 'India',         'Emerging Asia'),
    ('MCHI', 'China',         'Emerging Asia'),
    ('EWY',  'South Korea',   'Emerging Asia'),
    ('EWT',  'Taiwan',        'Emerging Asia'),
]

# (symbol, label) — ranked against nothing, shown as context.
REGIONS = [
    ('ACWI', 'All-world'),
    ('EFA',  'Developed ex-US'),
    ('EEM',  'Emerging markets'),
]

# Country ETFs start well clear of the US board's ordering so the two never
# interleave if anything ever sorts on display_order across both.
ORDER_BASE = 100


def build_rows():
    rows = []
    for i, (sym, country, region) in enumerate(COUNTRIES, start=1):
        rows.append({'symbol': sym, 'name': f'iShares MSCI {country} ETF',
                     'kind': 'country', 'sector': None, 'theme_slug': None,
                     'grp': region, 'benchmark_only': False,
                     'blurb': f'Single-country fund: {country}',
                     'display_order': ORDER_BASE + i})
    for j, (sym, label) in enumerate(REGIONS, start=1):
        rows.append({'symbol': sym, 'name': f'iShares MSCI {label} ETF',
                     'kind': 'country', 'sector': None, 'theme_slug': None,
                     'grp': 'Benchmark', 'benchmark_only': True,
                     'blurb': f'Regional benchmark: {label}',
                     'display_order': ORDER_BASE + len(COUNTRIES) + j})
    return rows


def main():
    print('=' * 60)
    print('SEED COUNTRY ETFS  ',
          datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 60)

    rows = build_rows()

    # A symbol already on the US board would be silently re-pointed at the
    # country board by this upsert, changing the pool it is scored against.
    # Nothing overlaps today; this is here so that stays true.
    existing = {r['symbol']: r.get('kind') for r in
                (sb.table('etfs').select('symbol,kind').execute().data or [])}
    clash = [r['symbol'] for r in rows
             if existing.get(r['symbol']) not in (None, 'country')]
    if clash:
        sys.exit(f'ERROR: {", ".join(clash)} already seeded on another board; '
                 'remove from COUNTRIES or rename before re-running')

    sb.table('etfs').upsert(rows, on_conflict='symbol').execute()

    n_bench = sum(1 for r in rows if r['benchmark_only'])
    print(f'Upserted {len(rows)} country ETFs '
          f'({len(rows) - n_bench} ranked, {n_bench} benchmark-only).\n')
    for region in dict.fromkeys(r['grp'] for r in rows):
        syms = [r['symbol'] for r in rows if r['grp'] == region]
        print(f'  {region:<24} {len(syms):>2}  {", ".join(syms)}')
    print('\nNext: python compute_etf_momentum.py   (downloads their history '
          'and ranks them in their own cross-section)')


if __name__ == '__main__':
    main()
