#!/usr/bin/env python3
"""Seed the AI value-chain theme map -> themes / theme_nodes / theme_members.

This is the editorial layer of the sector-research platform: a curated map of the
AI value chain (upstream inputs -> chips -> components -> systems -> infra -> cloud
-> software), with the member companies in each node drawn from our own universe.
Edit the NODES / MEMBERS below and re-run to update; upserts are idempotent.

Validates each non-context ticker against us_stock_sectors and warns about any that
aren't in the scan universe (likely a typo, or a name to add / mark as context).
Context members (private or foreign — e.g. OpenAI, SK Hynix) are kept for a complete
picture even though we hold no market data for them.

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

THEME = {'slug': 'ai', 'name': 'Artificial Intelligence',
         'description': 'The end-to-end AI value chain — from the materials and tools '
                        'that make chips, through compute, memory, networking, power '
                        'and cooling, up to the clouds and software that sell AI.',
         'display_order': 1}

# (layer, node_key, name, beginner blurb). Layer = rough upstream->downstream order.
NODES = [
    (1, 'materials',    'Materials & Inputs',
     'The raw stuff of chips: ultra-pure silicon wafers, specialty gases, chemicals '
     'and substrates. Boring but essential — nothing gets made without it.'),
    (2, 'eda_ip',       'Chip Design Tools & IP',
     'The software used to design chips (EDA) and the reusable circuit blueprints (IP). '
     'You cannot design a modern chip without these tools.'),
    (2, 'equipment',    'Semiconductor Equipment',
     'The machines that actually etch and build chips inside a factory. A handful of '
     'firms make the tools every chipmaker on earth depends on.'),
    (3, 'compute',      'AI Compute Silicon (GPUs/Accelerators)',
     'The chips that do the AI maths — GPUs and custom accelerators. This is the part '
     'most people mean when they say "AI stocks."'),
    (3, 'foundry',      'Foundries (Manufacturing)',
     'The factories that physically manufacture the chips others design. Extremely hard '
     'and expensive to build; very few companies can do it at the cutting edge.'),
    (4, 'memory',       'Memory & Storage (incl. HBM)',
     'Chips that hold data for the AI to work on. High-Bandwidth Memory (HBM) sits right '
     'next to the GPU and has become a key bottleneck.'),
    (5, 'packaging',    'Advanced Packaging & Test',
     'Stitching multiple chips into one high-performance package, then testing them. '
     'As raw chip shrinks slow, packaging is where a lot of the gains now come from.'),
    (6, 'networking',   'Networking & Interconnect',
     'The switches and high-speed links that connect thousands of GPUs into one giant '
     'computer. A data center is only as fast as its slowest connection.'),
    (6, 'optical',      'Optical & Photonics',
     'Moving data as light — transceivers, lasers and optical fiber. Essential for the '
     'huge bandwidth between AI servers.'),
    (6, 'power_delivery', 'Power Delivery & Electrical',
     'Getting clean power to the chips: power semiconductors, voltage modules and the '
     'electrical gear feeding the rack.'),
    (7, 'systems',      'Servers, Systems & ODMs',
     'The companies that assemble chips, memory and cooling into finished AI servers '
     'and racks.'),
    (7, 'cooling',      'Thermal & Cooling',
     'AI chips run hot. Liquid cooling and thermal systems keep them from melting — one '
     'of the fastest-growing corners of the chain.'),
    (8, 'dc_reit',      'Data-Center Real Estate',
     'The physical buildings — landlords who own and lease the data centers the clouds '
     'fill with AI hardware.'),
    (8, 'power_gen',    'Power Generation & Grid',
     'AI is enormously power-hungry. Generators (including nuclear), grid builders and '
     'electrical equipment makers supply the electricity.'),
    (9, 'hyperscalers', 'Hyperscalers / Cloud (the Buyers)',
     'The giants that buy most of the AI hardware and rent AI out as a service. Their '
     'spending ("capex") drives the whole chain.'),
    (10, 'software_apps', 'AI Software, Platforms & Apps',
     'The models, data platforms and applications that turn AI into products people pay '
     'for — the top of the chain.'),
]

# node_key -> [(ticker, role note, is_context)]
MEMBERS = {
    'materials': [
        ('ENTG', 'Filtration & materials for chip fabs', False),
        ('MKSI', 'Process control & materials', False),
        ('APD',  'Industrial gases for fabs', False),
        ('LIN',  'Industrial gases', False),
        ('DD',   'Advanced materials / electronics', False),
    ],
    'eda_ip': [
        ('SNPS', 'EDA software — designs the chips', False),
        ('CDNS', 'EDA software', False),
        ('ARM',  'Chip architecture & IP licensing', False),
    ],
    'equipment': [
        ('ASML', 'Lithography — the machine that prints chips (near-monopoly)', False),
        ('AMAT', 'Broad chip-making equipment', False),
        ('LRCX', 'Etch & deposition equipment', False),
        ('KLAC', 'Inspection & metrology', False),
        ('TER',  'Chip testing equipment', False),
        ('ONTO', 'Metrology & inspection', False),
        ('ACLS', 'Ion implantation equipment', False),
    ],
    'compute': [
        ('NVDA', 'The GPU standard for AI training', False),
        ('AMD',  'GPUs & CPUs', False),
        ('AVGO', 'Custom AI accelerators (ASICs) + networking silicon', False),
        ('MRVL', 'Custom AI silicon & interconnect', False),
        ('INTC', 'CPUs & accelerators', False),
    ],
    'foundry': [
        ('TSM',  'Makes most of the world\'s advanced AI chips', False),
        ('GFS',  'Specialty foundry', False),
        ('UMC',  'United Micro — foundry (foreign ADR)', True),
        ('INTC', 'Intel Foundry — building 3rd-party manufacturing', False),
        ('SSNLF', 'Samsung Foundry (foreign)', True),
    ],
    'memory': [
        ('MU',   'HBM & DRAM — the key memory bottleneck', False),
        ('STX',  'Hard-drive storage for data centers', False),
        ('WDC',  'Storage', False),
        ('SNDK', 'Flash storage', False),
        ('HXSCL', 'SK Hynix — HBM leader (foreign)', True),
    ],
    'packaging': [
        ('AMKR', 'Outsourced chip packaging & test', False),
        ('ASX',  'ASE — packaging & test (foreign ADR)', True),
    ],
    'networking': [
        ('ANET', 'AI data-center switches', False),
        ('ALAB', 'Connectivity chips for AI racks', False),
        ('CRDO', 'High-speed connectivity', False),
        ('CSCO', 'Networking', False),
        ('AVGO', 'Merchant switching silicon', False),
    ],
    'optical': [
        ('COHR', 'Optical transceivers & lasers', False),
        ('LITE', 'Lasers & optical components', False),
        ('FN',   'Optical module manufacturing', False),
        ('GLW',  'Optical fiber & glass', False),
    ],
    'power_delivery': [
        ('MPWR', 'Power-management chips for AI boards', False),
        ('VICR', 'High-density power modules', False),
        ('POWI', 'Power semiconductors', False),
        ('ETN',  'Electrical power management for data centers', False),
        ('NVT',  'Electrical enclosures & connections', False),
    ],
    'systems': [
        ('SMCI', 'AI server systems', False),
        ('DELL', 'AI servers', False),
        ('HPE',  'AI servers & HPC', False),
        ('CLS',  'ODM / contract manufacturing for AI hardware', False),
    ],
    'cooling': [
        ('VRT',  'Liquid cooling & data-center thermal (a key winner)', False),
        ('NVT',  'Cooling & enclosures', False),
        ('MOD',  'Thermal management', False),
    ],
    'dc_reit': [
        ('DLR',  'Data-center REIT (landlord)', False),
        ('EQIX', 'Data-center & interconnection REIT', False),
        ('IRM',  'Data-center & storage REIT', False),
    ],
    'power_gen': [
        ('CEG',  'Nuclear power — signing deals to power data centers', False),
        ('VST',  'Independent power producer', False),
        ('NRG',  'Power generation', False),
        ('TLN',  'Talen — nuclear/independent power', False),
        ('GEV',  'GE Vernova — turbines & grid equipment', False),
        ('PWR',  'Builds the electrical grid & data-center power', False),
        ('CCJ',  'Uranium — nuclear fuel', False),
    ],
    'hyperscalers': [
        ('MSFT', 'Azure + OpenAI — huge AI capex', False),
        ('AMZN', 'AWS + custom silicon (Trainium)', False),
        ('GOOGL', 'Google Cloud + TPUs', False),
        ('META', 'Massive GPU buyer for its own AI', False),
        ('ORCL', 'Cloud infrastructure for AI', False),
    ],
    'software_apps': [
        ('PLTR', 'AI software platforms for enterprise/gov', False),
        ('SNOW', 'Data platform feeding AI', False),
        ('NOW',  'Enterprise workflow AI', False),
        ('CRM',  'Enterprise apps + AI', False),
        ('OPENAI', 'OpenAI — ChatGPT (private)', True),
        ('ANTHROP', 'Anthropic — Claude (private)', True),
    ],
}


ICON = {'materials':'🧱','eda_ip':'✏️','equipment':'🛠️','compute':'🧠','foundry':'🏭','memory':'💾','packaging':'📦','networking':'🔀','optical':'🌈','power_delivery':'🔌','systems':'🖥️','cooling':'❄️','dc_reit':'🏢','power_gen':'⚡','hyperscalers':'☁️','software_apps':'🤖'}
SHORT = {'materials':'Materials','eda_ip':'Design & IP','equipment':'Chip Equipment','compute':'AI Chips','foundry':'Foundries','memory':'Memory & HBM','packaging':'Packaging','networking':'Networking','optical':'Optical','power_delivery':'Power Delivery','systems':'AI Servers','cooling':'Cooling','dc_reit':'Data Centers','power_gen':'Power Gen','hyperscalers':'Cloud','software_apps':'AI Software'}
# node_key -> (flow_col, flow_lane): position in the left→right flow diagram
FLOW = {'materials':(1,0),'eda_ip':(1,3),'equipment':(1,6),'compute':(2,4),'foundry':(2,1),'memory':(3,1),'packaging':(3,4),'networking':(4,0),'optical':(4,1.7),'power_delivery':(4,4.3),'cooling':(4,6),'systems':(5,3),'dc_reit':(6,1.5),'power_gen':(6,4.5),'hyperscalers':(7,3),'software_apps':(8,3)}
EDGES = [('materials','foundry'),('equipment','foundry'),('eda_ip','compute'),('compute','packaging'),('foundry','packaging'),('memory','systems'),('packaging','systems'),('networking','systems'),('optical','systems'),('power_delivery','systems'),('cooling','systems'),('systems','dc_reit'),('dc_reit','hyperscalers'),('power_gen','hyperscalers'),('hyperscalers','software_apps')]
STAGES = {'1':'Inputs & Tools','2':'Make the Chip','3':'Assemble','4':'Components','5':'The Server','6':'House & Power','7':'The Cloud','8':'The Apps'}


def main():
    print('=' * 60)
    print('SEED THEME: AI value chain ',
          datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 60)

    # Universe for validation.
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
    node_rows = [{'theme_slug': 'ai', 'node_key': k, 'name': n, 'layer': l, 'blurb': b,
                  'icon': ICON.get(k), 'short_label': SHORT.get(k),
                  'flow_col': FLOW.get(k, (None, None))[0],
                  'flow_lane': FLOW.get(k, (None, None))[1]}
                 for (l, k, n, b) in NODES]
    sb.table('theme_nodes').upsert(node_rows, on_conflict='theme_slug,node_key').execute()
    sb.table('theme_edges').delete().eq('theme_slug', 'ai').execute()
    sb.table('theme_edges').upsert(
        [{'theme_slug': 'ai', 'src': a, 'dst': b} for (a, b) in EDGES],
        on_conflict='theme_slug,src,dst').execute()

    member_rows, missing, n_members, n_context = [], [], 0, 0
    for (_, node_key, _, _) in NODES:
        for (ticker, note, is_context) in MEMBERS.get(node_key, []):
            member_rows.append({'theme_slug': 'ai', 'node_key': node_key,
                                'ticker': ticker, 'note': note, 'is_context': is_context})
            n_members += 1
            if is_context:
                n_context += 1
            elif ticker not in universe:
                missing.append(f'{ticker} ({node_key})')

    # Replace the AI theme's members cleanly, then insert.
    sb.table('theme_members').delete().eq('theme_slug', 'ai').execute()
    for i in range(0, len(member_rows), 200):
        sb.table('theme_members').upsert(member_rows[i:i + 200],
                                         on_conflict='theme_slug,node_key,ticker').execute()

    print(f'Theme "ai": {len(NODES)} nodes, {n_members} member rows '
          f'({n_context} context/private).')
    in_uni = n_members - n_context - len(missing)
    print(f'  in-universe (full data): {in_uni}   context: {n_context}   '
          f'not-found: {len(missing)}')
    if missing:
        print('  NOT in us_stock_sectors (fix ticker, add to universe, or mark context):')
        for m in missing:
            print(f'    - {m}')
    print('\nPer-node member counts:')
    for (_, k, name, _) in NODES:
        print(f'  {k:14s} {len(MEMBERS.get(k, [])):2d}  {name}')


if __name__ == '__main__':
    main()
