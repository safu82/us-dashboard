#!/usr/bin/env python3
"""Seed the ETF universe for the sector/theme momentum board.

Idempotent: upserts the reference list into `etfs` (keyed on symbol). The daily
indicator + momentum snapshot is computed separately by compute_etf_momentum.py,
which ranks these ETFs in their OWN cross-section (not against the 1,900 stocks).

11 GICS sector SPDRs + one thematic ETF per theme (theme_slug matches themes.slug
in the sector-research platform, so research.html can line an ETF up with its theme).

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (.env auto-loaded).
Usage: python seed_etfs.py
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

# (symbol, name, kind, sector, theme_slug, blurb)
# Sector = the 11 SPDR Select Sector funds (the standard "sector ETF" set).
SECTOR_ETFS = [
    ('XLK',  'Technology Select Sector SPDR',            'Information Technology'),
    ('XLF',  'Financial Select Sector SPDR',             'Financials'),
    ('XLV',  'Health Care Select Sector SPDR',           'Health Care'),
    ('XLE',  'Energy Select Sector SPDR',                'Energy'),
    ('XLI',  'Industrial Select Sector SPDR',            'Industrials'),
    ('XLY',  'Consumer Discretionary Select Sector SPDR','Consumer Discretionary'),
    ('XLP',  'Consumer Staples Select Sector SPDR',      'Consumer Staples'),
    ('XLU',  'Utilities Select Sector SPDR',             'Utilities'),
    ('XLB',  'Materials Select Sector SPDR',             'Materials'),
    ('XLRE', 'Real Estate Select Sector SPDR',           'Real Estate'),
    ('XLC',  'Communication Services Select Sector SPDR','Communication Services'),
]

# (symbol, name, theme_slug) — one buyable ETF per sector-research theme.
# theme_slug must match themes.slug. aging has no clean pure-play ETF -> omitted.
THEME_ETFS = [
    ('AIQ',  'Global X Artificial Intelligence & Technology ETF', 'ai'),
    ('CIBR', 'First Trust Nasdaq Cybersecurity ETF',              'cyber'),
    ('SLIM', 'Roundhill GLP-1 & Weight Loss ETF',                 'glp1'),
    ('QTUM', 'Defiance Quantum ETF',                              'quantum'),
    ('URA',  'Global X Uranium ETF',                              'nuclear'),
    ('BOTZ', 'Global X Robotics & Artificial Intelligence ETF',   'robotics'),
    ('ARKX', 'ARK Space Exploration & Innovation ETF',            'space'),
    ('DRIV', 'Global X Autonomous & Electric Vehicles ETF',       'ev'),
    ('ICLN', 'iShares Global Clean Energy ETF',                   'cleanenergy'),
    ('COPX', 'Global X Copper Miners ETF',                        'minerals'),
    ('ARKG', 'ARK Genomic Revolution ETF',                        'genomics'),
    ('PHO',  'Invesco Water Resources ETF',                       'water'),
    ('XHB',  'SPDR S&P Homebuilders ETF',                         'housing'),
    ('AIRR', 'First Trust RBA American Industrial Renaissance ETF','reshoring'),
    ('FINX', 'Global X FinTech ETF',                              'fintech'),
    ('MOO',  'VanEck Agribusiness ETF',                           'agriculture'),
    ('BLOK', 'Amplify Transformational Data Sharing ETF',         'crypto'),
]


def build_rows():
    rows, order = [], 0
    for sym, name, sector in SECTOR_ETFS:
        order += 1
        rows.append({'symbol': sym, 'name': name, 'kind': 'sector',
                     'sector': sector, 'theme_slug': None,
                     'blurb': f'{sector} sector fund', 'display_order': order})
    for sym, name, slug in THEME_ETFS:
        order += 1
        rows.append({'symbol': sym, 'name': name, 'kind': 'theme',
                     'sector': None, 'theme_slug': slug,
                     'blurb': f'Thematic ETF for the {slug} value chain', 'display_order': order})
    return rows


def main():
    print('=' * 60)
    print('SEED ETFS  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 60)
    rows = build_rows()
    sb.table('etfs').upsert(rows, on_conflict='symbol').execute()
    n_sec = sum(1 for r in rows if r['kind'] == 'sector')
    n_thm = sum(1 for r in rows if r['kind'] == 'theme')
    print(f'Upserted {len(rows)} ETFs ({n_sec} sector, {n_thm} theme).')
    for r in rows:
        tag = r['sector'] if r['kind'] == 'sector' else f"->{r['theme_slug']}"
        print(f"  {r['symbol']:<5} {r['kind']:<6} {tag}")


if __name__ == '__main__':
    main()
