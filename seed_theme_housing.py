#!/usr/bin/env python3
"""Seed the Housing & Homebuilders theme -> themes / theme_nodes / theme_members / theme_edges.

Same shape as seed_theme_quantum.py / seed_theme_glp1.py: edit the maps below and
re-run (idempotent). Validates tickers vs us_stock_sectors and warns on any missing.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY.
"""

import os
import sys
from datetime import datetime, timezone

from supabase import create_client
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

sb = create_client(URL, KEY)

SLUG = 'housing'
THEME = {'slug': SLUG, 'name': 'Housing & Homebuilders',
         'description': 'The US housing machine, end to end — from the building products that go '
                        'into a home, through the homebuilders that put them up and the mortgage '
                        'lenders that finance the buyers, to the retailers where owners renovate '
                        'and the REITs that rent it all out. A classic rate-cut play.',
         'display_order': 13}

NODES = [
    (1, 'products', 'Building Products & Materials',
     'The stuff homes are made of — lumber, insulation, paint, decking and fixtures. Sells '
     'into both new builds and renovations.'),
    (2, 'builders', 'Homebuilders',
     'The companies that actually build and sell new homes — the most direct bet on US '
     'housing supply and demand.'),
    (3, 'finance', 'Mortgage & Title',
     'The money side — the lenders, mortgage insurers and title companies that finance the '
     'purchase. Extremely sensitive to interest rates.'),
    (4, 'retail', 'Home Improvement Retail',
     'Where homeowners spend once they own — the big-box and specialty retailers riding the '
     'repair & remodel cycle.'),
    (4, 'reit', 'Residential REITs & Rental',
     'The landlords — companies that own and rent out homes and apartments, an income play on '
     'the same housing demand.'),
]

MEMBERS = {
    'products': [
        ('BLDR', 'Builders FirstSource — building materials', False),
        ('MAS',  'Masco — paints & home fixtures', False),
        ('OC',   'Owens Corning — insulation & roofing', False),
        ('SHW',  'Sherwin-Williams — paint leader', False),
        ('TREX', 'Trex — composite decking', False),
    ],
    'builders': [
        ('DHI', 'D.R. Horton — the #1 US builder', False),
        ('LEN', 'Lennar — #2 national builder', False),
        ('PHM', 'PulteGroup — national builder', False),
        ('NVR', 'NVR — high-margin, land-light model', False),
        ('TOL', 'Toll Brothers — luxury homes', False),
    ],
    'finance': [
        ('RKT',  'Rocket Companies — mortgage lender', False),
        ('UWMC', 'UWM Holdings — wholesale mortgages', False),
        ('ESNT', 'Essent Group — mortgage insurance', False),
        ('FNF',  'Fidelity National Financial — title', False),
    ],
    'retail': [
        ('HD',  'Home Depot — home-improvement giant', False),
        ('LOW', 'Lowe\'s — #2 home-improvement retailer', False),
        ('FND', 'Floor & Decor — specialty flooring', False),
    ],
    'reit': [
        ('INVH', 'Invitation Homes — single-family rentals', False),
        ('AMH',  'American Homes 4 Rent — SFR landlord', False),
        ('EQR',  'Equity Residential — apartments', False),
        ('AVB',  'AvalonBay — apartment REIT', False),
    ],
}

ICON = {'products': '🧱', 'builders': '🏗️', 'finance': '🏦', 'retail': '🛒', 'reit': '🏘️'}
SHORT = {'products': 'Materials', 'builders': 'Homebuilders', 'finance': 'Mortgage',
         'retail': 'Retail', 'reit': 'REITs'}
FLOW = {'products': (1, 3), 'builders': (2, 3), 'finance': (3, 3), 'retail': (4, 1),
        'reit': (4, 5)}
EDGES = [('products', 'builders'), ('builders', 'finance'), ('finance', 'retail'),
         ('finance', 'reit')]
STAGES = {'1': 'Materials', '2': 'Builders', '3': 'Financing', '4': 'Own & Improve'}


def main():
    print('=' * 60)
    print('SEED THEME: Housing & Homebuilders ',
          datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 60)

    universe, frm = set(), 0
    while True:
        resp = sb.table('us_stock_sectors').select('ticker').range(frm, frm + 999).execute()
        for r in (resp.data or []):
            if r.get('ticker'):
                universe.add(r['ticker'])
        if not resp.data or len(resp.data) < 1000:
            break
        frm += 1000

    sb.table('themes').upsert({**THEME, 'flow_stages': STAGES}, on_conflict='slug').execute()
    node_rows = [{'theme_slug': SLUG, 'node_key': k, 'name': n, 'layer': l, 'blurb': b,
                  'icon': ICON.get(k), 'short_label': SHORT.get(k),
                  'flow_col': FLOW.get(k, (None, None))[0],
                  'flow_lane': FLOW.get(k, (None, None))[1]}
                 for (l, k, n, b) in NODES]
    sb.table('theme_nodes').upsert(node_rows, on_conflict='theme_slug,node_key').execute()
    sb.table('theme_edges').delete().eq('theme_slug', SLUG).execute()
    sb.table('theme_edges').upsert(
        [{'theme_slug': SLUG, 'src': a, 'dst': b} for (a, b) in EDGES],
        on_conflict='theme_slug,src,dst').execute()

    member_rows, missing, n_members, n_context = [], [], 0, 0
    for (_, node_key, _, _) in NODES:
        for (ticker, note, is_context) in MEMBERS.get(node_key, []):
            member_rows.append({'theme_slug': SLUG, 'node_key': node_key,
                                'ticker': ticker, 'note': note, 'is_context': is_context})
            n_members += 1
            if is_context:
                n_context += 1
            elif ticker not in universe:
                missing.append(f'{ticker} ({node_key})')

    sb.table('theme_members').delete().eq('theme_slug', SLUG).execute()
    for i in range(0, len(member_rows), 200):
        sb.table('theme_members').upsert(member_rows[i:i + 200],
                                         on_conflict='theme_slug,node_key,ticker').execute()

    print(f'Theme "{SLUG}": {len(NODES)} nodes, {n_members} member rows '
          f'({n_context} context/private).')
    print(f'  in-universe (full data): {n_members - n_context - len(missing)}   '
          f'context: {n_context}   not-found: {len(missing)}')
    if missing:
        print('  NOT in us_stock_sectors (add to universe or mark context):')
        for m in missing:
            print(f'    - {m}')


if __name__ == '__main__':
    main()
