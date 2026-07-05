#!/usr/bin/env python3
"""Seed the Fintech & Digital Payments theme -> themes / theme_nodes / theme_members / theme_edges.

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

SLUG = 'fintech'
THEME = {'slug': SLUG, 'name': 'Fintech & Digital Payments',
         'description': 'The digitisation of money — from the card networks that move it, through the '
                        'processors that plug merchants in and the neobanks & apps that hold it, to the '
                        'digital lenders and the infrastructure behind the scenes. A convergence of '
                        'finance and software.',
         'display_order': 16}

NODES = [
    (1, 'networks', 'Card Networks & Rails',
     'The toll-roads of money — the networks that carry a payment between bank and merchant and '
     'take a tiny cut of nearly every swipe.'),
    (2, 'processors', 'Payment Processors',
     'The plumbing that connects merchants to the networks — handling the transaction, the '
     'terminal and the settlement.'),
    (3, 'neobanks', 'Neobanks & Apps',
     'The consumer front-end — app-first banks, brokerages and wallets taking share from the '
     'old branch-based incumbents.'),
    (3, 'lending', 'Digital Lending & BNPL',
     'Credit, reinvented — buy-now-pay-later and data-driven lenders underwriting loans in '
     'real time online.'),
    (4, 'infra', 'Fintech Infrastructure',
     'The picks-and-shovels — the credit-scoring, core-banking and cross-border rails every '
     'other fintech is built on.'),
]

MEMBERS = {
    'networks': [
        ('V',   'Visa — the largest card network', False),
        ('MA',  'Mastercard — global card network', False),
        ('AXP', 'American Express — network + lender', False),
    ],
    'processors': [
        ('FIS',  'FIS — banking & payment processing', False),
        ('GPN',  'Global Payments — merchant acquiring', False),
        ('FOUR', 'Shift4 — integrated payments', False),
        ('TOST', 'Toast — restaurant payments & software', False),
    ],
    'neobanks': [
        ('SOFI', 'SoFi — app-first bank', False),
        ('NU',   'Nu Holdings — LatAm neobank', False),
        ('HOOD', 'Robinhood — commission-free brokerage', False),
        ('PYPL', 'PayPal — digital wallet', False),
    ],
    'lending': [
        ('AFRM', 'Affirm — buy-now-pay-later leader', False),
        ('UPST', 'Upstart — AI lending marketplace', False),
        ('SEZL', 'Sezzle — buy-now-pay-later', False),
    ],
    'infra': [
        ('FICO', 'Fair Isaac — the FICO credit score', False),
        ('JKHY', 'Jack Henry — core banking tech', False),
        ('FLYW', 'Flywire — cross-border payments', False),
        ('PAYO', 'Payoneer — global SMB payments', False),
    ],
}

ICON = {'networks': '💳', 'processors': '🔌', 'neobanks': '📱', 'lending': '📊', 'infra': '🏗️'}
SHORT = {'networks': 'Networks', 'processors': 'Processors', 'neobanks': 'Neobanks',
         'lending': 'Lending/BNPL', 'infra': 'Infrastructure'}
FLOW = {'networks': (1, 3), 'processors': (2, 3), 'neobanks': (3, 1), 'lending': (3, 5),
        'infra': (4, 3)}
EDGES = [('networks', 'processors'), ('processors', 'neobanks'), ('processors', 'lending'),
         ('neobanks', 'infra'), ('lending', 'infra')]
STAGES = {'1': 'Rails', '2': 'Processing', '3': 'Consumer', '4': 'Infrastructure'}


def main():
    print('=' * 60)
    print('SEED THEME: Fintech & Digital Payments ',
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
