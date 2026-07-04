#!/usr/bin/env python3
"""Seed the Water theme -> themes / theme_nodes / theme_members / theme_edges.

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

SLUG = 'water'
THEME = {'slug': SLUG, 'name': 'Water',
         'description': 'The most essential resource, as an investment theme — the equipment makers '
                        'that pump, treat and pipe it, the regulated utilities that deliver it to '
                        'your tap, and the irrigation systems that grow the world\'s food. A classic '
                        'defensive way to own scarcity.',
         'display_order': 12}

NODES = [
    (1, 'treatment', 'Treatment & Filtration',
     'Making water clean and safe — the chemicals, filtration and quality-testing that treat '
     'drinking water and wastewater.'),
    (1, 'flow', 'Pumps, Valves & Metering',
     'Moving and measuring water — the pumps, valves and smart meters that run inside every '
     'water system.'),
    (1, 'infra', 'Pipes & Infrastructure',
     'The physical network — the pipes, drainage and engineering behind fixing America\'s '
     'ageing, leaking water infrastructure.'),
    (2, 'utilities', 'Water Utilities',
     'The regulated companies that actually deliver water to homes and businesses — steady, '
     'defensive, dividend-paying.'),
    (3, 'irrigation', 'Irrigation & Ag Water',
     'The biggest user of all — the irrigation systems that water the world\'s crops, where '
     'scarcity bites hardest.'),
]

MEMBERS = {
    'treatment': [
        ('ECL',  'Ecolab — water treatment & hygiene', False),
        ('VLTO', 'Veralto — water quality & analytics', False),
        ('DCI',  'Donaldson — filtration systems', False),
    ],
    'flow': [
        ('XYL',  'Xylem — the pure-play water-tech leader', False),
        ('PNR',  'Pentair — pumps & filtration', False),
        ('FELE', 'Franklin Electric — water pumps', False),
        ('BMI',  'Badger Meter — smart water metering', False),
    ],
    'infra': [
        ('WMS',  'Advanced Drainage — stormwater pipe', False),
        ('MWA',  'Mueller Water Products — pipe & valves', False),
        ('TTEK', 'Tetra Tech — water engineering', False),
    ],
    'utilities': [
        ('AWK',  'American Water Works — the US giant', False),
        ('WTRG', 'Essential Utilities — water & gas', False),
        ('AWR',  'American States Water', False),
        ('CWT',  'California Water Service', False),
    ],
    'irrigation': [
        ('LNN', 'Lindsay — irrigation & infrastructure', False),
        ('VMI', 'Valmont — irrigation & structures', False),
    ],
}

ICON = {'treatment': '💧', 'flow': '🔧', 'infra': '🏗️', 'utilities': '🚰', 'irrigation': '🌾'}
SHORT = {'treatment': 'Treatment', 'flow': 'Pumps & Meters', 'infra': 'Pipes',
         'utilities': 'Utilities', 'irrigation': 'Irrigation'}
FLOW = {'treatment': (1, 1), 'flow': (1, 3), 'infra': (1, 5), 'utilities': (2, 3),
        'irrigation': (3, 3)}
EDGES = [('treatment', 'utilities'), ('flow', 'utilities'), ('infra', 'utilities'),
         ('utilities', 'irrigation')]
STAGES = {'1': 'Equipment', '2': 'Utilities', '3': 'End Use'}


def main():
    print('=' * 60)
    print('SEED THEME: Water ',
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
