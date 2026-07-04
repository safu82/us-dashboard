#!/usr/bin/env python3
"""Seed the Copper & Critical Minerals theme -> themes / theme_nodes / theme_members / theme_edges.

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

SLUG = 'minerals'
THEME = {'slug': SLUG, 'name': 'Copper & Critical Minerals',
         'description': 'The picks-and-shovels of everything electric — copper for the grid, EVs '
                        'and AI datacenters, plus the rare earths and magnets modern tech cannot '
                        'work without. From the miners digging it up and the diversified giants, to '
                        'the royalty financiers that take a cut, to the demand pulling it all.',
         'display_order': 10}

NODES = [
    (1, 'copper', 'Copper Miners',
     '"Dr. Copper" — the metal behind every wire, motor and datacenter. Pure-play miners are '
     'the most direct bet on the electrification super-cycle.'),
    (1, 'diversified', 'Diversified Mining Giants',
     'The global majors that mine copper, iron ore and more at massive scale — steadier, '
     'dividend-paying ways to own the commodity boom.'),
    (1, 'rareearth', 'Rare Earths & Magnets',
     'The niche metals that go into EV motors, wind turbines and defense systems — where the '
     'West is scrambling to break China\'s grip.'),
    (2, 'royalty', 'Royalty & Streaming',
     'The financiers of mining — they fund mines in exchange for a cut of production, earning '
     'miner-like upside with far less operating risk.'),
    (3, 'demand', 'The Metal Hunger',
     'Why it all matters — the grid build-out, EVs and AI datacenters devouring copper and '
     'critical minerals. Illustrative, not miners.'),
]

MEMBERS = {
    'copper': [
        ('FCX',  'Freeport-McMoRan — the copper bellwether', False),
        ('SCCO', 'Southern Copper — low-cost, high-margin', False),
        ('TECK', 'Teck Resources — copper pivot', False),
        ('ERO',  'Ero Copper — growth-stage miner', False),
        ('IE',   'Ivanhoe Electric — US copper + tech', False),
    ],
    'diversified': [
        ('RIO',  'Rio Tinto — global mining major', False),
        ('BHP',  'BHP — the world\'s biggest miner', False),
        ('VALE', 'Vale — iron ore + base metals', False),
    ],
    'rareearth': [
        ('MP',   'MP Materials — US rare-earth leader', False),
        ('UUUU', 'Energy Fuels — rare earths + uranium', False),
        ('TMC',  'TMC — deep-sea nickel & cobalt', False),
        ('USAR', 'USA Rare Earth — magnets & minerals', False),
    ],
    'royalty': [
        ('WPM', 'Wheaton Precious Metals — streaming', False),
        ('FNV', 'Franco-Nevada — royalty pioneer', False),
        ('RGLD', 'Royal Gold — royalty & streaming', False),
    ],
    'demand': [
        ('GEV',  'Grid build-out devours copper (GE Vernova)', True),
        ('TSLA', 'EVs use ~4x the copper of a car (Tesla)', True),
        ('NVDA', 'AI datacenters need vast power & copper (Nvidia)', True),
    ],
}

ICON = {'copper': '⛏️', 'diversified': '⛰️', 'rareearth': '🧲', 'royalty': '💰', 'demand': '🔌'}
SHORT = {'copper': 'Copper', 'diversified': 'Diversified', 'rareearth': 'Rare Earths',
         'royalty': 'Royalty', 'demand': 'Demand'}
FLOW = {'copper': (1, 1), 'diversified': (1, 3), 'rareearth': (1, 5), 'royalty': (2, 3),
        'demand': (3, 3)}
EDGES = [('copper', 'royalty'), ('diversified', 'royalty'), ('rareearth', 'royalty'),
         ('copper', 'demand'), ('royalty', 'demand')]
STAGES = {'1': 'The Miners', '2': 'Royalty', '3': 'The Metal Hunger'}


def main():
    print('=' * 60)
    print('SEED THEME: Copper & Critical Minerals ',
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
