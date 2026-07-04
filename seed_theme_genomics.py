#!/usr/bin/env python3
"""Seed the Genomics & Precision Medicine theme -> themes / theme_nodes / theme_members / theme_edges.

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

SLUG = 'genomics'
THEME = {'slug': SLUG, 'name': 'Genomics & Precision Medicine',
         'description': 'Medicine rebuilt around DNA — from the machines that read the genome and '
                        'the tools & reagents behind them, through the gene-editing pioneers '
                        'rewriting it and the liquid-biopsy tests that catch disease early, to the '
                        'AI platforms designing tomorrow\'s drugs.',
         'display_order': 11}

NODES = [
    (1, 'sequencing', 'Sequencing Platforms',
     'The machines that read DNA — the foundation of the whole field. As the cost of '
     'sequencing a genome collapses, everything downstream gets cheaper.'),
    (2, 'tools', 'Tools & Reagents',
     'The "picks-and-shovels" of biotech — the instruments, chemicals and lab kits every '
     'genomics company buys, whoever wins the science.'),
    (3, 'editing', 'Gene Editing & Cell Therapy',
     'The pioneers actually rewriting DNA — CRISPR and cell therapies chasing one-time cures '
     'for genetic disease. High-risk, high-reward.'),
    (3, 'diagnostics', 'Diagnostics & Liquid Biopsy',
     'The tests — spotting cancer and disease early from a blood draw. The nearest-term, '
     'most commercial slice of genomics.'),
    (4, 'discovery', 'AI Drug Discovery',
     'The new layer — companies using AI and computation to design drugs faster and cheaper '
     'than the lab-bench trial-and-error of the past.'),
]

MEMBERS = {
    'sequencing': [
        ('ILMN', 'Illumina — the sequencing incumbent', False),
        ('PACB', 'Pacific Biosciences — long-read sequencing', False),
        ('TXG',  '10x Genomics — single-cell & spatial', False),
    ],
    'tools': [
        ('TMO',  'Thermo Fisher — lab tools giant', False),
        ('A',    'Agilent — instruments & reagents', False),
        ('RVTY', 'Revvity — life-science tools', False),
        ('QGEN', 'Qiagen — sample prep & assays', False),
    ],
    'editing': [
        ('CRSP', 'CRISPR Therapeutics — approved gene edit', False),
        ('NTLA', 'Intellia — in-vivo CRISPR', False),
        ('BEAM', 'Beam Therapeutics — base editing', False),
        ('EDIT', 'Editas Medicine — CRISPR pioneer', False),
    ],
    'diagnostics': [
        ('GH',   'Guardant Health — liquid biopsy', False),
        ('NTRA', 'Natera — genetic testing', False),
        ('TEM',  'Tempus AI — data-driven diagnostics', False),
    ],
    'discovery': [
        ('RXRX', 'Recursion — AI drug discovery', False),
        ('SDGR', 'Schrodinger — physics-based drug design', False),
        ('ABSI', 'Absci — generative-AI antibodies', False),
    ],
}

ICON = {'sequencing': '🧬', 'tools': '🔬', 'editing': '✂️', 'diagnostics': '🩺', 'discovery': '🤖'}
SHORT = {'sequencing': 'Sequencing', 'tools': 'Tools', 'editing': 'Gene Editing',
         'diagnostics': 'Diagnostics', 'discovery': 'AI Discovery'}
FLOW = {'sequencing': (1, 3), 'tools': (2, 3), 'editing': (3, 1), 'diagnostics': (3, 5),
        'discovery': (4, 3)}
EDGES = [('sequencing', 'tools'), ('tools', 'editing'), ('tools', 'diagnostics'),
         ('editing', 'discovery'), ('diagnostics', 'discovery')]
STAGES = {'1': 'Read the Genome', '2': 'Tools', '3': 'Edit & Diagnose', '4': 'AI Discovery'}


def main():
    print('=' * 60)
    print('SEED THEME: Genomics & Precision Medicine ',
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
