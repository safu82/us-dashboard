#!/usr/bin/env python3
"""Seed the Reshoring & US Manufacturing theme -> themes / theme_nodes / theme_members / theme_edges.

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

SLUG = 'reshoring'
THEME = {'slug': SLUG, 'name': 'Reshoring & US Manufacturing',
         'description': 'The multi-year push to rebuild US factory capacity — from the raw materials '
                        'and the firms that construct the plants, through the automation and electrical '
                        'gear that outfit them, to the rail and warehousing that moves the output. A '
                        'policy-driven story cutting across industrials, materials and real estate.',
         'display_order': 14}

NODES = [
    (1, 'materials', 'Materials & Inputs',
     'The raw stuff of a factory build-out — steel, cement and aggregates. Demand rises with '
     'every new plant, highway and grid upgrade.'),
    (2, 'construction', 'Factory & Infra Builders',
     'The engineering and construction firms that actually put up the fabs, plants and '
     'infrastructure — the direct beneficiaries of the capex wave.'),
    (3, 'automation', 'Factory Automation',
     'The control systems and machines that run a modern factory floor — the productivity '
     'layer that makes higher-cost US manufacturing viable.'),
    (3, 'electrical', 'Electrical & Power Equipment',
     'The switchgear, transformers and power gear every new factory and datacenter needs to '
     'plug into the grid — a supply bottleneck.'),
    (4, 'logistics', 'Logistics & Warehousing',
     'Moving the output — the railroads, truckers and industrial-property owners that carry '
     'and store reshored production.'),
]

MEMBERS = {
    'materials': [
        ('NUE',  'Nucor — the top US steelmaker', False),
        ('STLD', 'Steel Dynamics — low-cost steel', False),
        ('VMC',  'Vulcan Materials — aggregates', False),
        ('MLM',  'Martin Marietta — aggregates & cement', False),
    ],
    'construction': [
        ('PWR', 'Quanta Services — infrastructure build', False),
        ('ACM', 'AECOM — engineering & design', False),
        ('J',   'Jacobs Solutions — technical construction', False),
        ('FLR', 'Fluor — large plant construction', False),
        ('MTZ', 'MasTec — infrastructure construction', False),
    ],
    'automation': [
        ('ROK',  'Rockwell Automation — factory control', False),
        ('EMR',  'Emerson Electric — process automation', False),
        ('ZBRA', 'Zebra — warehouse & tracking automation', False),
        ('PH',   'Parker Hannifin — motion & controls', False),
    ],
    'electrical': [
        ('ETN',  'Eaton — electrical power management', False),
        ('HUBB', 'Hubbell — electrical components', False),
        ('GEV',  'GE Vernova — grid & power equipment', False),
        ('POWL', 'Powell Industries — switchgear', False),
        ('NVT',  'nVent Electric — electrical connection', False),
    ],
    'logistics': [
        ('UNP',  'Union Pacific — Western railroad', False),
        ('CSX',  'CSX — Eastern railroad', False),
        ('PLD',  'Prologis — industrial warehouses (REIT)', False),
        ('ODFL', 'Old Dominion — less-than-truckload freight', False),
    ],
}

ICON = {'materials': '🪨', 'construction': '🏗️', 'automation': '⚙️', 'electrical': '⚡',
        'logistics': '🚚'}
SHORT = {'materials': 'Materials', 'construction': 'Builders', 'automation': 'Automation',
         'electrical': 'Electrical', 'logistics': 'Logistics'}
FLOW = {'materials': (1, 3), 'construction': (2, 3), 'automation': (3, 1), 'electrical': (3, 5),
        'logistics': (4, 3)}
EDGES = [('materials', 'construction'), ('construction', 'automation'),
         ('construction', 'electrical'), ('automation', 'logistics'), ('electrical', 'logistics')]
STAGES = {'1': 'Materials', '2': 'Build', '3': 'Equip', '4': 'Move'}


def main():
    print('=' * 60)
    print('SEED THEME: Reshoring & US Manufacturing ',
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
