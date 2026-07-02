#!/usr/bin/env python3
"""Seed the Cybersecurity theme map -> themes / theme_nodes / theme_members / theme_edges.

Second theme on the sector-research platform, proving the AI template generalises.
Same shape as seed_theme_ai.py: edit the maps below and re-run (idempotent). Validates
tickers vs us_stock_sectors and warns on any missing.

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

SLUG = 'cyber'
THEME = {'slug': SLUG, 'name': 'Cybersecurity',
         'description': 'The layers of digital defense — from who gets in (identity) through '
                        'the devices, network, cloud, data and code, up to the security control '
                        'room and the people who run it.',
         'display_order': 2}

# (layer, node_key, name, beginner blurb)
NODES = [
    (1, 'identity',  'Identity & Access',
     'Controls who is allowed in — logins, single sign-on, passwords and privileged accounts. '
     'The front door of security.'),
    (2, 'endpoint',  'Endpoint Protection',
     'Guards the actual devices — laptops, servers, phones — where attacks land and data lives.'),
    (3, 'network',   'Network & Firewall',
     'The walls and gates around the network; inspects the traffic flowing in and out.'),
    (4, 'cloud_app', 'Cloud & App Security',
     'Secures the cloud apps and infrastructure companies now run everything on.'),
    (5, 'email_web', 'Email & Web Security',
     'Blocks phishing, malicious links and web threats — the most common way attackers get in.'),
    (6, 'data_sec',  'Data Security & Backup',
     'Protects and backs up the data itself, so a breach or ransomware attack cannot destroy it.'),
    (7, 'vuln',      'Vulnerability & Code Security',
     'Finds the holes before attackers do — scanning systems and securing software as it is built.'),
    (8, 'detection', 'Detection & Response (SOC)',
     'The security control room — collects signals from everything and hunts threats in real time.'),
    (9, 'services',  'Security Services & Consulting',
     'The people who design, run and respond — managed security providers, consultants and '
     'government contractors.'),
]

MEMBERS = {
    'identity': [
        ('OKTA', 'Single sign-on & identity', False),
        ('CYBR', 'Privileged access (CyberArk)', False),
        ('MSFT', 'Entra ID — the biggest identity platform', False),
    ],
    'endpoint': [
        ('CRWD', 'Endpoint leader (CrowdStrike)', False),
        ('S',    'AI-native endpoint (SentinelOne)', False),
        ('MSFT', 'Defender endpoint', False),
    ],
    'network': [
        ('PANW', 'Network security leader (Palo Alto)', False),
        ('FTNT', 'Firewalls (Fortinet)', False),
        ('CHKP', 'Firewalls (Check Point)', False),
        ('CSCO', 'Networking & security', False),
        ('JNPR', 'Networking', False),
    ],
    'cloud_app': [
        ('ZS',   'Zero-trust cloud access (Zscaler)', False),
        ('NET',  'Cloud & edge security (Cloudflare)', False),
        ('PANW', 'Prisma Cloud', False),
        ('DDOG', 'Cloud monitoring & security (Datadog)', False),
        ('WIZ',  'Cloud security (private)', True),
    ],
    'email_web': [
        ('NET',  'Web / DDoS protection (Cloudflare)', False),
        ('AKAM', 'Web & edge security (Akamai)', False),
        ('MSFT', 'Email security', False),
        ('PFPT', 'Proofpoint — email security (private)', True),
    ],
    'data_sec': [
        ('VRNS', 'Data security (Varonis)', False),
        ('RBRK', 'Backup & recovery (Rubrik)', False),
        ('CVLT', 'Backup & recovery (Commvault)', False),
    ],
    'vuln': [
        ('QLYS', 'Vulnerability management (Qualys)', False),
        ('TENB', 'Vulnerability management (Tenable)', False),
        ('RPD',  'Vulnerability & detection (Rapid7)', False),
        ('GTLB', 'Secure DevOps (GitLab)', False),
    ],
    'detection': [
        ('ESTC', 'Search & SIEM (Elastic)', False),
        ('DDOG', 'Observability & security (Datadog)', False),
        ('CSCO', 'Splunk SIEM', False),
        ('PANW', 'Cortex XDR', False),
    ],
    'services': [
        ('ACN',  'Security consulting (Accenture)', False),
        ('BAH',  'Cyber for government (Booz Allen)', False),
        ('LDOS', 'Defense & cyber (Leidos)', False),
        ('CACI', 'Government cyber (CACI)', False),
        ('SAIC', 'Government IT & cyber (SAIC)', False),
    ],
}

ICON = {'identity': '🔑', 'endpoint': '💻', 'network': '🧱', 'cloud_app': '☁️',
        'email_web': '✉️', 'data_sec': '🗄️', 'vuln': '🐛', 'detection': '🚨',
        'services': '🤝'}
SHORT = {'identity': 'Identity', 'endpoint': 'Endpoint', 'network': 'Network',
         'cloud_app': 'Cloud & App', 'email_web': 'Email & Web', 'data_sec': 'Data & Backup',
         'vuln': 'Vuln & Code', 'detection': 'Detection (SOC)', 'services': 'Services'}
# node_key -> (flow_col, flow_lane): controls (left) feed the SOC (mid) -> services (right)
FLOW = {'identity': (1, 1), 'endpoint': (1, 3), 'network': (1, 5),
        'cloud_app': (2, 0), 'email_web': (2, 2), 'data_sec': (2, 4), 'vuln': (2, 6),
        'detection': (3, 3), 'services': (4, 3)}
EDGES = [('identity', 'detection'), ('endpoint', 'detection'), ('network', 'detection'),
         ('cloud_app', 'detection'), ('email_web', 'detection'), ('data_sec', 'detection'),
         ('vuln', 'detection'), ('detection', 'services')]
STAGES = {'1': 'Access & Perimeter', '2': 'Cloud, Data & Code', '3': 'The SOC', '4': 'People'}


def main():
    print('=' * 60)
    print('SEED THEME: Cybersecurity ',
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
