#!/usr/bin/env python3
"""Seed the Space & Defense theme -> themes / theme_nodes / theme_members / theme_edges.

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

SLUG = 'space'
THEME = {'slug': SLUG, 'name': 'Space & Defense',
         'description': 'The new space race meets modern defense — from the rockets getting to '
                        'orbit cheaper than ever and the satellites beaming data back, through the '
                        'defense primes and the new wave of cheap drones and battlefield AI, down '
                        'to the specialist suppliers inside almost every mission.',
         'display_order': 7}

NODES = [
    (1, 'launch', 'Launch & Access to Space',
     'Getting to orbit — the rockets and landers that carry satellites and cargo into space, '
     'now cheaper than ever.'),
    (1, 'suppliers', 'Suppliers & Electronics',
     'The specialist parts makers inside almost every aircraft, missile and satellite — '
     'high-margin, mission-critical components.'),
    (2, 'satellites', 'Satellites & Earth Data',
     'The satellites in orbit and the data they beam back — imaging, connectivity and '
     'Earth-observation.'),
    (3, 'primes', 'Defense Primes',
     'The big defense contractors — missiles, fighter jets, warships and the space & '
     'missile-defense programs behind them.'),
    (3, 'drones', 'Drones & Defense Tech',
     'The new wave of defense — cheap drones, loitering munitions and the AI software that '
     'runs modern battlefields.'),
]

MEMBERS = {
    'launch': [
        ('RKLB', 'Rocket Lab — small-launch leader', False),
        ('LUNR', 'Intuitive Machines — lunar landers', False),
        ('RDW',  'Redwire — in-space infrastructure', False),
    ],
    'suppliers': [
        ('HEI',  'Heico — aftermarket aerospace parts', False),
        ('TDG',  'TransDigm — proprietary components', False),
        ('MRCY', 'Mercury Systems — secure processing', False),
    ],
    'satellites': [
        ('ASTS', 'AST SpaceMobile — satellite-to-phone', False),
        ('PL',   'Planet Labs — daily Earth imaging', False),
        ('BKSY', 'BlackSky — real-time imaging', False),
        ('SPIR', 'Spire Global — weather & tracking', False),
        ('IRDM', 'Iridium — satellite communications', False),
    ],
    'primes': [
        ('LMT', 'Lockheed Martin — jets & missiles', False),
        ('RTX', 'RTX (Raytheon) — missiles & defense', False),
        ('NOC', 'Northrop Grumman — space & missiles', False),
        ('GD',  'General Dynamics — warships & combat', False),
        ('BA',  'Boeing — aerospace & defense', False),
    ],
    'drones': [
        ('KTOS', 'Kratos — drones & hypersonics', False),
        ('AVAV', 'AeroVironment — tactical drones', False),
        ('PLTR', 'Palantir — battlefield AI software', False),
        ('RCAT', 'Red Cat — military drones', False),
    ],
}

ICON = {'launch': '🚀', 'suppliers': '🔧', 'satellites': '🛰️', 'primes': '🛡️', 'drones': '✈️'}
SHORT = {'launch': 'Launch', 'suppliers': 'Suppliers', 'satellites': 'Satellites',
         'primes': 'Defense Primes', 'drones': 'Drones'}
FLOW = {'launch': (1, 1), 'suppliers': (1, 5), 'satellites': (2, 3), 'primes': (3, 1),
        'drones': (3, 5)}
EDGES = [('launch', 'satellites'), ('suppliers', 'primes'), ('suppliers', 'drones'),
         ('satellites', 'primes'), ('satellites', 'drones')]
STAGES = {'1': 'Build & Launch', '2': 'In Orbit', '3': 'Defense'}


def main():
    print('=' * 60)
    print('SEED THEME: Space & Defense ',
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
