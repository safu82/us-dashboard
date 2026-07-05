#!/usr/bin/env python3
"""Seed the Food & Agriculture theme -> themes / theme_nodes / theme_members / theme_edges.

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

SLUG = 'agriculture'
THEME = {'slug': SLUG, 'name': 'Food & Agriculture',
         'description': 'The farm-to-table supply chain and the food-security story behind it — from '
                        'the seeds, fertilizer and machinery that grow the crop, through animal health '
                        'and the processors & ingredient makers, to the distributors that get food to '
                        'the table. Spans materials, industrials, health care and staples.',
         'display_order': 17}

NODES = [
    (1, 'inputs', 'Seeds & Fertilizer',
     'What goes into the ground — the seeds, crop chemicals and fertilizers that set the '
     'yield. A commodity business, and a geopolitical one.'),
    (1, 'equipment', 'Farm Equipment',
     'The machines that work the land — tractors, combines and precision-ag tech driving '
     'higher output per acre.'),
    (1, 'animalhealth', 'Animal Health',
     'Keeping livestock and pets healthy — vaccines, diagnostics and medicines, a steadier '
     'high-margin corner of ag.'),
    (2, 'processing', 'Processing & Ingredients',
     'Turning raw crops into food — the grain traders, crushers and ingredient makers in the '
     'middle of the chain.'),
    (3, 'distribution', 'Food Distribution',
     'Getting it to the table — the broadline distributors that supply restaurants, '
     'cafeterias and grocers.'),
]

MEMBERS = {
    'inputs': [
        ('CTVA', 'Corteva — seeds & crop protection', False),
        ('NTR',  'Nutrien — fertilizer & retail', False),
        ('MOS',  'Mosaic — phosphate & potash', False),
        ('CF',   'CF Industries — nitrogen fertilizer', False),
        ('FMC',  'FMC — crop chemicals', False),
    ],
    'equipment': [
        ('DE',   'Deere — the farm-machinery leader', False),
        ('AGCO', 'AGCO — global farm equipment', False),
        ('CNH',  'CNH Industrial — ag & construction', False),
        ('LNN',  'Lindsay — irrigation systems', False),
    ],
    'animalhealth': [
        ('ZTS',  'Zoetis — animal health leader', False),
        ('IDXX', 'IDEXX — veterinary diagnostics', False),
        ('ELAN', 'Elanco — animal health', False),
    ],
    'processing': [
        ('ADM',  'Archer-Daniels-Midland — grain trading', False),
        ('BG',   'Bunge — oilseed processing', False),
        ('INGR', 'Ingredion — food ingredients', False),
        ('DAR',  'Darling — rendering & renewable feed', False),
    ],
    'distribution': [
        ('SYY',  'Sysco — the broadline leader', False),
        ('USFD', 'US Foods — food distribution', False),
        ('PFGC', 'Performance Food Group — distribution', False),
    ],
}

ICON = {'inputs': '🌱', 'equipment': '🚜', 'animalhealth': '🐄', 'processing': '🏭',
        'distribution': '🍽️'}
SHORT = {'inputs': 'Seeds/Fert', 'equipment': 'Equipment', 'animalhealth': 'Animal Health',
         'processing': 'Processing', 'distribution': 'Distribution'}
FLOW = {'inputs': (1, 1), 'equipment': (1, 3), 'animalhealth': (1, 5), 'processing': (2, 3),
        'distribution': (3, 3)}
EDGES = [('inputs', 'processing'), ('equipment', 'processing'), ('animalhealth', 'processing'),
         ('processing', 'distribution')]
STAGES = {'1': 'The Farm', '2': 'Processing', '3': 'To Table'}


def main():
    print('=' * 60)
    print('SEED THEME: Food & Agriculture ',
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
