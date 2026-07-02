#!/usr/bin/env python3
"""Seed the GLP-1 / Obesity theme -> themes / theme_nodes / theme_members / theme_edges.

Third theme on the sector-research platform. Same shape as seed_theme_ai.py /
seed_theme_cyber.py: edit the maps below and re-run (idempotent). Validates tickers
vs us_stock_sectors and warns on any missing.

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

SLUG = 'glp1'
THEME = {'slug': SLUG, 'name': 'GLP-1 & Obesity',
         'description': 'The weight-loss drug boom, end to end — from the biotech pipeline and '
                        'the big drug makers, through the manufacturers and injection devices, to '
                        'the telehealth platforms that deliver it and the "Ozempic effect" rippling '
                        'across the economy.',
         'display_order': 3}

NODES = [
    (1, 'discovery', 'Drug Discovery & Pipeline',
     'Biotechs racing to build the next generation of obesity drugs — pills instead of '
     'injections, and even stronger results.'),
    (2, 'makers', 'The Drug Makers',
     'The giants behind today\'s blockbuster weight-loss and diabetes drugs, plus the big '
     'pharma challengers trying to catch up.'),
    (3, 'cdmo', 'Contract Manufacturing',
     'The specialists that actually make the drug at scale — the biggest bottleneck, since '
     'demand has far outstripped supply.'),
    (3, 'devices', 'Injection Devices & Delivery',
     'The pens, auto-injectors, needles and vials that get the drug into patients.'),
    (4, 'diagnostics', 'Diagnostics & Monitoring',
     'Continuous glucose monitors and tests used to manage patients on these drugs.'),
    (4, 'telehealth', 'Telehealth & Access',
     'The online platforms and pharmacies that prescribe, compound and deliver the drugs '
     'to millions of new patients.'),
    (5, 'ripple', 'The "Ozempic Effect"',
     'Second-order impact — as people eat less, it ripples into snacks, drinks, dialysis '
     'and other corners of the economy.'),
]

MEMBERS = {
    'discovery': [
        ('VKTX', 'Oral & injectable obesity pipeline (Viking)', False),
        ('ALT',  'Next-gen obesity (Altimmune)', False),
        ('GPCR', 'Oral GLP-1 (Structure Therapeutics)', False),
        ('TERN', 'Oral obesity pipeline (Terns)', False),
    ],
    'makers': [
        ('LLY',  'Zepbound / Mounjaro — the leader', False),
        ('NVO',  'Ozempic / Wegovy — the pioneer (ADR)', False),
        ('AMGN', 'MariTide monthly shot', False),
        ('PFE',  'Oral GLP-1 in development', False),
    ],
    'cdmo': [
        ('TMO',  'Bioprocessing & contract manufacturing (Thermo Fisher)', False),
        ('DHR',  'Bioprocessing tools (Danaher)', False),
        ('CRL',  'Drug development services (Charles River)', False),
        ('LONN', 'Lonza — the top peptide CDMO (foreign)', True),
    ],
    'devices': [
        ('WST',  'Vials, closures & drug-delivery components (West Pharma)', False),
        ('BDX',  'Needles & syringes (Becton Dickinson)', False),
        ('EMBC', 'Injection & delivery (Embecta)', False),
        ('STVN', 'Glass vials & syringes (Stevanato)', False),
    ],
    'diagnostics': [
        ('DXCM', 'Continuous glucose monitors (Dexcom)', False),
        ('ABT',  'Libre CGM & diagnostics (Abbott)', False),
        ('PODD', 'Insulin delivery (Insulet)', False),
        ('TNDM', 'Insulin pumps (Tandem)', False),
    ],
    'telehealth': [
        ('HIMS', 'Online weight-loss & compounding (Hims & Hers)', False),
        ('LFMD', 'Telehealth weight management (LifeMD)', False),
        ('GDRX', 'Drug price & access (GoodRx)', False),
        ('TDOC', 'Telehealth (Teladoc)', False),
    ],
    'ripple': [
        ('HSY',  'Snacks — demand headwind (Hershey)', False),
        ('MDLZ', 'Snacks — demand headwind (Mondelez)', False),
        ('PEP',  'Snacks & drinks — demand headwind (PepsiCo)', False),
        ('DVA',  'Dialysis — long-term kidney benefit is a headwind (DaVita)', False),
    ],
}

ICON = {'discovery': '🧪', 'makers': '💊', 'cdmo': '🏭', 'devices': '💉',
        'diagnostics': '🩸', 'telehealth': '📱', 'ripple': '🌊'}
SHORT = {'discovery': 'Pipeline', 'makers': 'Drug Makers', 'cdmo': 'Manufacturing',
         'devices': 'Devices', 'diagnostics': 'Diagnostics', 'telehealth': 'Telehealth',
         'ripple': 'Ozempic Effect'}
FLOW = {'discovery': (1, 3), 'makers': (2, 3), 'cdmo': (3, 1), 'devices': (3, 5),
        'diagnostics': (4, 1), 'telehealth': (4, 5), 'ripple': (5, 3)}
EDGES = [('discovery', 'makers'), ('makers', 'cdmo'), ('makers', 'devices'),
         ('makers', 'diagnostics'), ('cdmo', 'telehealth'), ('devices', 'telehealth'),
         ('telehealth', 'ripple'), ('diagnostics', 'ripple')]
STAGES = {'1': 'Discovery', '2': 'The Drug', '3': 'Make & Deliver', '4': 'Patient Access',
          '5': 'Ripple Effects'}


def main():
    print('=' * 60)
    print('SEED THEME: GLP-1 / Obesity ',
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
