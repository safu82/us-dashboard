#!/usr/bin/env python3
"""Seed the EVs & Batteries theme -> themes / theme_nodes / theme_members / theme_edges.

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

SLUG = 'ev'
THEME = {'slug': SLUG, 'name': 'EVs & Batteries',
         'description': 'The electric-vehicle value chain, end to end — from the lithium and rare '
                        'earths dug out of the ground, through the battery makers and next-gen cell '
                        'chemistries, to the carmakers, the charging networks and the power '
                        'semiconductors that manage every electron in an EV.',
         'display_order': 8}

NODES = [
    (1, 'materials', 'Battery Materials',
     'The raw inputs — lithium, rare earths and the miners & refiners that feed the battery '
     'supply chain.'),
    (2, 'batteries', 'Battery Makers & Tech',
     'The cells themselves — and the next-gen chemistries (solid-state, silicon) racing to '
     'make EVs cheaper and go farther.'),
    (2, 'semis', 'EV Chips & Power Electronics',
     'The unsung winners — the power semiconductors and chips that manage every electron in '
     'an EV, the biggest jump in chip content autos have ever seen.'),
    (3, 'automakers', 'EV Makers',
     'The carmakers — from EV-only pioneers to the legacy giants betting billions to catch up.'),
    (4, 'charging', 'Charging & Infrastructure',
     'The plumbing — the charging networks and hardware that have to be built out before mass '
     'EV adoption can happen.'),
]

MEMBERS = {
    'materials': [
        ('ALB', 'Albemarle — the top lithium producer', False),
        ('SQM', 'SQM — low-cost lithium (Chile, ADR)', False),
        ('LAC', 'Lithium Americas — US Thacker Pass', False),
        ('MP',  'MP Materials — US rare earths', False),
    ],
    'batteries': [
        ('QS',   'QuantumScape — solid-state cells', False),
        ('ENVX', 'Enovix — silicon-anode batteries', False),
        ('AMPX', 'Amprius — silicon high-density cells', False),
        ('SLDP', 'Solid Power — solid-state electrolyte', False),
    ],
    'semis': [
        ('ON',   'ON Semiconductor — EV power chips', False),
        ('WOLF', 'Wolfspeed — silicon-carbide leader', False),
        ('NXPI', 'NXP — automotive processors', False),
        ('ALGM', 'Allegro Microsystems — motor & sensing', False),
    ],
    'automakers': [
        ('TSLA', 'Tesla — the EV pioneer', False),
        ('RIVN', 'Rivian — EV trucks & vans', False),
        ('LCID', 'Lucid — luxury EV sedans', False),
        ('GM',   'General Motors — legacy EV pivot', False),
        ('F',    'Ford — legacy EV pivot', False),
    ],
    'charging': [
        ('CHPT', 'ChargePoint — largest US network', False),
        ('EVGO', 'EVgo — fast-charging network', False),
        ('BLNK', 'Blink Charging — chargers & network', False),
    ],
}

ICON = {'materials': '⛏️', 'batteries': '🔋', 'semis': '🎛️', 'automakers': '🚗', 'charging': '⚡'}
SHORT = {'materials': 'Materials', 'batteries': 'Batteries', 'semis': 'Chips',
         'automakers': 'EV Makers', 'charging': 'Charging'}
FLOW = {'materials': (1, 3), 'batteries': (2, 1), 'semis': (2, 5), 'automakers': (3, 3),
        'charging': (4, 3)}
EDGES = [('materials', 'batteries'), ('batteries', 'automakers'), ('semis', 'automakers'),
         ('automakers', 'charging')]
STAGES = {'1': 'Materials', '2': 'Cells & Chips', '3': 'The Car', '4': 'Charging'}


def main():
    print('=' * 60)
    print('SEED THEME: EVs & Batteries ',
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
