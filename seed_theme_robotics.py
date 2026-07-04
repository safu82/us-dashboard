#!/usr/bin/env python3
"""Seed the Robotics & Automation theme -> themes / theme_nodes / theme_members / theme_edges.

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

SLUG = 'robotics'
THEME = {'slug': SLUG, 'name': 'Robotics & Automation',
         'description': 'The march of the machines — from the sensors, vision and motion parts '
                        'that make up a robot, through the robot and cobot makers and the factory '
                        'automation giants, to the AI brains turning pre-programmed arms into '
                        'machines that perceive and adapt.',
         'display_order': 6}

NODES = [
    (1, 'enabling', 'Sensors, Vision & Motion',
     'The robot\'s body parts — machine-vision cameras, precision sensors, motors and '
     'actuators that let a robot see and move.'),
    (2, 'makers', 'Robot & Cobot Makers',
     'The companies that actually build the robots — from factory arms and collaborative '
     'cobots to surgical and delivery robots.'),
    (2, 'brains', 'AI Brains & Chips',
     'The AI models and chips that give robots intelligence — the leap from pre-programmed '
     'arms to machines that perceive, learn and adapt.'),
    (3, 'automation', 'Factory Automation',
     'The control systems and industrial giants that automate whole factories and connect '
     'the machines together.'),
    (4, 'adopters', 'Who Deploys It',
     'Where robots go to work first — e-commerce warehouses and autonomous farm & '
     'construction equipment. Illustrative, not robot pure-plays.'),
]

MEMBERS = {
    'enabling': [
        ('CGNX', 'Cognex — machine-vision cameras', False),
        ('AME',  'Ametek — precision motion & sensors', False),
        ('TDY',  'Teledyne — imaging & sensors', False),
        ('PH',   'Parker Hannifin — motion & actuation', False),
    ],
    'makers': [
        ('TER',  'Teradyne — Universal Robots cobots', False),
        ('ISRG', 'Intuitive Surgical — surgical robots', False),
        ('SYM',  'Symbotic — AI warehouse automation systems', False),
        ('ZBRA', 'Zebra — warehouse robotics & scanning', False),
        ('SERV', 'Serve Robotics — sidewalk delivery robots', False),
    ],
    'brains': [
        ('NVDA', 'Nvidia — Isaac robotics platform + GPUs', False),
        ('TSLA', 'Tesla — Optimus humanoid + FSD', False),
        ('QCOM', 'Qualcomm — edge-AI robotics chips', False),
        ('MBLY', 'Mobileye — autonomy & perception', False),
    ],
    'automation': [
        ('ROK', 'Rockwell Automation — factory control', False),
        ('EMR', 'Emerson Electric — process automation', False),
        ('HON', 'Honeywell — industrial automation', False),
        ('ETN', 'Eaton — power & controls', False),
    ],
    'adopters': [
        ('AMZN', 'Fulfillment-center robots (Amazon)', True),
        ('DE',   'Autonomous farm & construction machines (Deere)', True),
    ],
}

ICON = {'enabling': '👁️', 'makers': '🤖', 'brains': '🧠', 'automation': '🏭', 'adopters': '📦'}
SHORT = {'enabling': 'Sensors', 'makers': 'Robot Makers', 'brains': 'AI Brains',
         'automation': 'Automation', 'adopters': 'Adopters'}
FLOW = {'enabling': (1, 3), 'makers': (2, 1), 'brains': (2, 5), 'automation': (3, 3),
        'adopters': (4, 3)}
EDGES = [('enabling', 'makers'), ('brains', 'makers'), ('makers', 'automation'),
         ('brains', 'automation'), ('automation', 'adopters')]
STAGES = {'1': 'Components', '2': 'Robots & AI', '3': 'Automation', '4': 'Deployed'}


def main():
    print('=' * 60)
    print('SEED THEME: Robotics & Automation ',
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
