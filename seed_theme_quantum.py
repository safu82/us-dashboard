#!/usr/bin/env python3
"""Seed the Quantum Computing theme -> themes / theme_nodes / theme_members / theme_edges.

Fourth theme on the sector-research platform. Same shape as seed_theme_glp1.py /
seed_theme_cyber.py / seed_theme_ai.py: edit the maps below and re-run (idempotent).
Validates tickers vs us_stock_sectors and warns on any missing.

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

SLUG = 'quantum'
THEME = {'slug': SLUG, 'name': 'Quantum Computing',
         'description': 'The race to build computers that harness quantum physics — from the '
                        'picks-and-shovels hardware, through the pure-play startups and big-tech '
                        'giants building the machines, to the "quantum-safe" security layer rising '
                        'to defend against them and the industries set to benefit first.',
         'display_order': 4}

NODES = [
    (1, 'enabling', 'Enabling Hardware',
     'The picks-and-shovels every quantum computer needs — lasers, photonics, vacuum systems '
     'and the cryogenic test gear that keeps qubits near absolute zero.'),
    (2, 'pureplay', 'Pure-Play Quantum',
     'Small, high-risk companies built entirely around quantum — each betting on a different '
     'way to build a qubit, with little revenue but huge potential.'),
    (2, 'bigtech', 'Big Tech & Cloud',
     'The deep-pocketed giants building their own quantum chips and renting quantum access '
     'through their clouds — the ones most likely to cross the finish line.'),
    (3, 'security', 'Quantum-Safe Security',
     'The flip side: quantum computers could one day crack today\'s encryption, so a whole '
     '"post-quantum" security and networking layer is rising to defend against them.'),
    (4, 'apps', 'Who Benefits First',
     'The industries expected to gain earliest once the machines work — drug discovery, '
     'materials, finance and logistics. Illustrative, not pure quantum plays.'),
]

MEMBERS = {
    'enabling': [
        ('COHR', 'Lasers & photonics for quantum systems (Coherent)', False),
        ('LITE', 'Photonic components & lasers (Lumentum)', False),
        ('MKSI', 'Vacuum, lasers & control subsystems (MKS Instruments)', False),
        ('FORM', 'Cryogenic wafer probe & test systems (FormFactor)', False),
    ],
    'pureplay': [
        ('IONQ', 'Trapped-ion quantum computers — the largest pure-play (IonQ)', False),
        ('RGTI', 'Superconducting quantum chips (Rigetti)', False),
        ('QBTS', 'Quantum annealing systems (D-Wave)', False),
        ('QUBT', 'Photonic & thin-film quantum (Quantum Computing Inc)', False),
    ],
    'bigtech': [
        ('IBM',   'Quantum roadmap leader + IBM Quantum cloud', False),
        ('GOOGL', 'Google Quantum AI — the Willow chip', False),
        ('MSFT',  'Azure Quantum + topological qubit bet', False),
        ('HON',   'Majority owner of Quantinuum — trapped ion (Honeywell)', False),
        ('NVDA',  'CUDA-Q + GPU quantum simulation — the hybrid bridge', False),
    ],
    'security': [
        ('LAES', 'Post-quantum security chips (SEALSQ)', False),
        ('ARQQ', 'Quantum-safe symmetric-key encryption (Arqit)', False),
        ('CSCO', 'Quantum networking chip & entanglement (Cisco)', True),
    ],
    'apps': [
        ('JPM', 'Finance — portfolio & risk optimization (JPMorgan)', True),
        ('MRK', 'Pharma — molecular simulation for drug discovery (Merck)', True),
        ('LMT', 'Defense & aerospace — materials + logistics research (Lockheed)', True),
    ],
}

ICON = {'enabling': '🔬', 'pureplay': '⚛️', 'bigtech': '☁️',
        'security': '🔐', 'apps': '🎯'}
SHORT = {'enabling': 'Components', 'pureplay': 'Pure-Plays', 'bigtech': 'Big Tech',
         'security': 'Quantum-Safe', 'apps': 'Applications'}
FLOW = {'enabling': (1, 3), 'pureplay': (2, 1), 'bigtech': (2, 5),
        'security': (3, 3), 'apps': (4, 3)}
EDGES = [('enabling', 'pureplay'), ('enabling', 'bigtech'),
         ('pureplay', 'security'), ('bigtech', 'security'),
         ('pureplay', 'apps'), ('bigtech', 'apps'), ('security', 'apps')]
STAGES = {'1': 'Components', '2': 'The Machines', '3': 'Quantum-Safe', '4': 'Who Benefits'}


def main():
    print('=' * 60)
    print('SEED THEME: Quantum Computing ',
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
