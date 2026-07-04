#!/usr/bin/env python3
"""Seed the Nuclear & Power theme -> themes / theme_nodes / theme_members / theme_edges.

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

SLUG = 'nuclear'
THEME = {'slug': SLUG, 'name': 'Nuclear & Power',
         'description': 'The electricity boom behind the AI datacenter build-out — from the '
                        'uranium miners and fuel enrichers, through the reactor and small-modular '
                        'reactor (SMR) builders and the power producers running the fleet, to the '
                        'grid and electrical equipment straining to keep up.',
         'display_order': 5}

NODES = [
    (1, 'fuel', 'Uranium & Fuel',
     'The nuclear fuel supply chain — the miners digging up uranium and the enrichers that '
     'turn it into reactor-grade fuel.'),
    (2, 'reactors', 'Reactors & SMRs',
     'The companies building the reactors — from small modular reactors (SMRs) to next-gen '
     'designs racing to power the AI boom.'),
    (3, 'producers', 'Power Producers',
     'The utilities and independent power producers that run the nuclear fleet and sell the '
     'electricity — the most direct AI-datacenter beneficiaries.'),
    (3, 'grid', 'Grid & Electrical Equipment',
     'The picks-and-shovels of the power build-out — turbines, transformers, switchgear and '
     'the datacenter power gear straining to keep up.'),
    (4, 'demand', 'The Power Hunger',
     'Why it is all happening — the hyperscalers signing nuclear power deals to feed AI '
     'datacenters. Illustrative, not power stocks.'),
]

MEMBERS = {
    'fuel': [
        ('CCJ',  'Cameco — the top Western uranium miner', False),
        ('UEC',  'Uranium Energy — US-focused miner', False),
        ('UUUU', 'Energy Fuels — uranium + rare earths', False),
        ('DNN',  'Denison Mines — Canadian uranium', False),
        ('LEU',  'Centrus Energy — fuel enrichment', False),
    ],
    'reactors': [
        ('SMR',  'NuScale Power — leading SMR design', False),
        ('OKLO', 'Oklo — micro-reactor, Sam Altman-backed', False),
        ('BWXT', 'BWX Technologies — reactor components & naval', False),
    ],
    'producers': [
        ('CEG', 'Constellation — largest US nuclear fleet', False),
        ('VST', 'Vistra — nuclear + gas IPP', False),
        ('TLN', 'Talen Energy — Amazon datacenter deal', False),
        ('NRG', 'NRG Energy — power producer', False),
        ('SO',  'Southern Co — new Vogtle reactors', False),
    ],
    'grid': [
        ('GEV',  'GE Vernova — turbines + grid equipment', False),
        ('ETN',  'Eaton — electrical power management', False),
        ('PWR',  'Quanta Services — grid construction', False),
        ('VRT',  'Vertiv — datacenter power & cooling', False),
        ('POWL', 'Powell Industries — switchgear', False),
    ],
    'demand': [
        ('MSFT',  'Three Mile Island restart deal (Microsoft)', True),
        ('AMZN',  'Talen nuclear-datacenter deal (Amazon)', True),
        ('GOOGL', 'SMR power agreements (Alphabet)', True),
    ],
}

ICON = {'fuel': '⛏️', 'reactors': '☢️', 'producers': '⚡', 'grid': '🔌', 'demand': '🏢'}
SHORT = {'fuel': 'Uranium', 'reactors': 'Reactors', 'producers': 'Producers',
         'grid': 'Grid & Equip', 'demand': 'AI Demand'}
FLOW = {'fuel': (1, 3), 'reactors': (2, 3), 'producers': (3, 1), 'grid': (3, 5),
        'demand': (4, 3)}
EDGES = [('fuel', 'reactors'), ('reactors', 'producers'), ('grid', 'producers'),
         ('producers', 'demand'), ('grid', 'demand')]
STAGES = {'1': 'Fuel', '2': 'Reactors', '3': 'Power & Grid', '4': 'AI Demand'}


def main():
    print('=' * 60)
    print('SEED THEME: Nuclear & Power ',
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
