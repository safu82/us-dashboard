#!/usr/bin/env python3
"""Seed the Clean Energy theme -> themes / theme_nodes / theme_members / theme_edges.

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

SLUG = 'cleanenergy'
THEME = {'slug': SLUG, 'name': 'Clean Energy: Solar, Wind & Storage',
         'description': 'The other side of the energy transition — from the solar-panel makers and '
                        'the inverters & trackers that wire them up, through the rooftop installers '
                        'and the battery & fuel-cell storage that fixes the intermittency problem, '
                        'to the renewable utilities feeding it all onto the grid.',
         'display_order': 9}

NODES = [
    (1, 'panels', 'Solar Manufacturers',
     'The companies making the panels & modules themselves — a brutal, low-margin business '
     'dominated by scale and squeezed by cheap imports.'),
    (2, 'hardware', 'Inverters, Trackers & Hardware',
     'The higher-value electronics around the panel — inverters, trackers and the electrical '
     'balance-of-system that turns sunlight into usable power.'),
    (3, 'install', 'Rooftop Solar & Adoption',
     'The installers and financiers that put solar on homes and businesses — where consumer '
     'adoption actually happens.'),
    (3, 'storage', 'Storage & Fuel Cells',
     'The fix for renewables\' biggest flaw — batteries and fuel cells that store power for '
     'when the sun sets and the wind drops.'),
    (4, 'utilities', 'Renewable Utilities & IPPs',
     'The utilities and independent power producers that own the wind & solar farms and sell '
     'the clean electricity onto the grid.'),
]

MEMBERS = {
    'panels': [
        ('FSLR', 'First Solar — the US thin-film leader', False),
        ('CSIQ', 'Canadian Solar — global module maker', False),
        ('MAXN', 'Maxeon — premium panels (turnaround)', False),
    ],
    'hardware': [
        ('ENPH', 'Enphase — microinverters', False),
        ('SEDG', 'SolarEdge — string inverters (turnaround)', False),
        ('NXT',  'Nextracker — solar trackers', False),
        ('ARRY', 'Array Technologies — solar trackers', False),
        ('SHLS', 'Shoals — electrical balance-of-system', False),
    ],
    'install': [
        ('RUN',  'Sunrun — largest US rooftop installer', False),
        ('TSLA', 'Tesla Energy — solar roof + Powerwall', True),
    ],
    'storage': [
        ('FLNC', 'Fluence — grid-scale battery storage', False),
        ('STEM', 'Stem — AI-optimised storage (turnaround)', False),
        ('PLUG', 'Plug Power — hydrogen fuel cells', False),
        ('BE',   'Bloom Energy — solid-oxide fuel cells', False),
    ],
    'utilities': [
        ('NEE',  'NextEra — the renewables utility giant', False),
        ('BEP',  'Brookfield Renewable — global clean IPP', False),
        ('AES',  'AES — utility going green', False),
        ('ORA',  'Ormat — geothermal & storage', False),
        ('CWEN', 'Clearway Energy — wind & solar yieldco', False),
    ],
}

ICON = {'panels': '☀️', 'hardware': '⚙️', 'install': '🏠', 'storage': '🔋', 'utilities': '⚡'}
SHORT = {'panels': 'Solar Makers', 'hardware': 'Inverters', 'install': 'Installers',
         'storage': 'Storage', 'utilities': 'Utilities'}
FLOW = {'panels': (1, 3), 'hardware': (2, 3), 'install': (3, 1), 'storage': (3, 5),
        'utilities': (4, 3)}
EDGES = [('panels', 'hardware'), ('hardware', 'install'), ('hardware', 'storage'),
         ('install', 'utilities'), ('storage', 'utilities')]
STAGES = {'1': 'Solar Cells', '2': 'Hardware', '3': 'Install & Store', '4': 'Grid & Utilities'}


def main():
    print('=' * 60)
    print('SEED THEME: Clean Energy ',
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
