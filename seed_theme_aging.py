#!/usr/bin/env python3
"""Seed the Aging & Longevity theme -> themes / theme_nodes / theme_members / theme_edges.

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

SLUG = 'aging'
THEME = {'slug': SLUG, 'name': 'Aging & Longevity',
         'description': 'Investing in the demographic megatrend of an aging population — from the '
                        'treatments and devices that keep people healthy longer, through the vision, '
                        'hearing and monitoring tech and the senior-living operators, to the insurers '
                        'and wealth managers funding a longer retirement. Spans health care, real '
                        'estate and financials.',
         'display_order': 15}

NODES = [
    (1, 'treatments', 'Age-Related Treatments',
     'The drugs targeting the diseases of ageing — Alzheimer\'s, cancer, heart and metabolic '
     'disease — where an older population means growing demand.'),
    (1, 'devices', 'Medical Devices',
     'The hardware that repairs an ageing body — replacement joints, heart valves, pacemakers '
     'and surgical robots.'),
    (1, 'caretech', 'Vision, Hearing & Monitoring',
     'The everyday maintenance of ageing — eye care, contact lenses and the respiratory & '
     'monitoring devices used for chronic conditions.'),
    (2, 'seniorliving', 'Senior Living & Care',
     'Where older people live and are cared for — the senior-housing REITs and the skilled-'
     'nursing and assisted-living operators.'),
    (3, 'wealth', 'Retirement & Insurance',
     'The money side of a long life — the life insurers, annuity providers and wealth managers '
     'that fund decades of retirement.'),
]

MEMBERS = {
    'treatments': [
        ('LLY',  'Eli Lilly — Alzheimer\'s + metabolic', False),
        ('ABBV', 'AbbVie — immunology & oncology', False),
        ('AMGN', 'Amgen — chronic-disease biologics', False),
        ('BIIB', 'Biogen — Alzheimer\'s & neuro', False),
    ],
    'devices': [
        ('SYK',  'Stryker — orthopaedics & joints', False),
        ('MDT',  'Medtronic — cardiac & devices', False),
        ('BSX',  'Boston Scientific — cardiovascular', False),
        ('EW',   'Edwards Lifesciences — heart valves', False),
        ('ISRG', 'Intuitive Surgical — surgical robots', False),
    ],
    'caretech': [
        ('ALC',  'Alcon — eye care & surgical', False),
        ('COO',  'Cooper Companies — contact lenses', False),
        ('RMD',  'ResMed — sleep apnea & respiratory', False),
    ],
    'seniorliving': [
        ('WELL', 'Welltower — senior-housing REIT', False),
        ('VTR',  'Ventas — healthcare REIT', False),
        ('OHI',  'Omega Healthcare — skilled-nursing REIT', False),
        ('ENSG', 'Ensign Group — skilled nursing', False),
        ('BKD',  'Brookdale — assisted living', False),
    ],
    'wealth': [
        ('PRU',  'Prudential Financial — life & annuities', False),
        ('MET',  'MetLife — life insurance', False),
        ('AMP',  'Ameriprise — wealth management', False),
        ('RJF',  'Raymond James — wealth management', False),
    ],
}

ICON = {'treatments': '💊', 'devices': '🩺', 'caretech': '👓', 'seniorliving': '🏡', 'wealth': '💰'}
SHORT = {'treatments': 'Treatments', 'devices': 'Devices', 'caretech': 'Vision/Hearing',
         'seniorliving': 'Senior Living', 'wealth': 'Retirement $'}
FLOW = {'treatments': (1, 1), 'devices': (1, 3), 'caretech': (1, 5), 'seniorliving': (2, 3),
        'wealth': (3, 3)}
EDGES = [('treatments', 'seniorliving'), ('devices', 'seniorliving'),
         ('caretech', 'seniorliving'), ('seniorliving', 'wealth')]
STAGES = {'1': 'Stay Healthy', '2': 'Care & Housing', '3': 'Fund It'}


def main():
    print('=' * 60)
    print('SEED THEME: Aging & Longevity ',
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
