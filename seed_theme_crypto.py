#!/usr/bin/env python3
"""Seed the Crypto & Blockchain theme -> themes / theme_nodes / theme_members / theme_edges.

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

SLUG = 'crypto'
THEME = {'slug': SLUG, 'name': 'Crypto & Blockchain',
         'description': 'The public-equity ways to own the crypto economy — from the power-hungry '
                        'compute that secures the chain and the bitcoin miners, through the exchanges '
                        'and brokers, to the corporate treasuries and the stablecoin issuers turning '
                        'blockchain into a payments rail. High-beta, early, and narrow.',
         'display_order': 18}

NODES = [
    (1, 'infra', 'Crypto Compute & Power',
     'The data-center and power backbone — miners increasingly pivoting their cheap power and '
     'racks to AI/HPC as well as crypto.'),
    (2, 'miners', 'Bitcoin Miners',
     'The companies running the machines that mint new bitcoin — a leveraged, high-beta bet on '
     'the coin price and mining economics.'),
    (3, 'exchanges', 'Exchanges & Brokers',
     'Where crypto is traded and held — the exchanges, brokers and trading firms that earn '
     'fees on the flow.'),
    (4, 'treasuries', 'Corporate BTC Treasuries',
     'Companies that hold bitcoin on the balance sheet as a treasury asset — a leveraged, '
     'stock-market proxy for the coin.'),
    (4, 'stablecoins', 'Stablecoins & Payments',
     'The bridge to real-world money — issuers of dollar-pegged stablecoins turning blockchain '
     'into a payments and settlement rail.'),
]

MEMBERS = {
    'infra': [
        ('IREN', 'IREN — bitcoin mining + AI/HPC', False),
        ('APLD', 'Applied Digital — HPC & datacenters', False),
        ('CORZ', 'Core Scientific — mining + HPC hosting', False),
    ],
    'miners': [
        ('MARA', 'MARA Holdings — the largest US miner', False),
        ('RIOT', 'Riot Platforms — bitcoin mining', False),
        ('CLSK', 'CleanSpark — bitcoin mining', False),
        ('CIFR', 'Cipher Mining — mining + HPC', False),
        ('WULF', 'TeraWulf — mining + HPC', False),
    ],
    'exchanges': [
        ('COIN', 'Coinbase — the largest US exchange', False),
        ('HOOD', 'Robinhood — crypto + brokerage', False),
        ('GLXY', 'Galaxy Digital — crypto financial services', False),
    ],
    'treasuries': [
        ('MSTR', 'Strategy (MicroStrategy) — the BTC-treasury proxy', False),
    ],
    'stablecoins': [
        ('CRCL', 'Circle — USDC stablecoin issuer', False),
        ('PYPL', 'PayPal — PYUSD stablecoin', True),
    ],
}

ICON = {'infra': '🖥️', 'miners': '⛏️', 'exchanges': '🔄', 'treasuries': '🏦', 'stablecoins': '🪙'}
SHORT = {'infra': 'Compute', 'miners': 'Miners', 'exchanges': 'Exchanges',
         'treasuries': 'Treasuries', 'stablecoins': 'Stablecoins'}
FLOW = {'infra': (1, 3), 'miners': (2, 3), 'exchanges': (3, 3), 'treasuries': (4, 1),
        'stablecoins': (4, 5)}
EDGES = [('infra', 'miners'), ('miners', 'exchanges'), ('exchanges', 'treasuries'),
         ('exchanges', 'stablecoins')]
STAGES = {'1': 'Compute', '2': 'Mining', '3': 'Exchanges', '4': 'Financialize'}


def main():
    print('=' * 60)
    print('SEED THEME: Crypto & Blockchain ',
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
