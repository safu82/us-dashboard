#!/usr/bin/env python3
"""Theme value-chain MCP server — Phase 0 (local, stdio).

Exposes the hand-curated theme value chains (themes / theme_nodes /
theme_members) with the daily momentum overlay, as four read-only tools an
LLM can call:

    list_themes        what chains exist
    get_value_chain    one chain, upstream -> downstream, members per link
    rank_links         which link in a chain is strongest right now
    compare_peers      the names inside one link, ranked

Deliberate constraints — these are product decisions, not oversights:

  * Output carries ONLY our own derived numbers: momentum_score (our 0-100
    cross-sectional composite) and its rank. No prices, no OHLC, no
    fundamentals. The chain structure and the per-name notes are our own
    editorial work, so nothing here is redistributed third-party data.
  * Read-only. No writes, no user state, nothing stored per caller.
  * Every payload carries as_of plus a stale flag, so the model can say
    "as of <date>" instead of implying a live quote.

Run locally:
    python plugin/theme_server.py            # stdio, for Claude / ChatGPT dev mode
    python plugin/theme_server.py --http     # streamable-http, for Phase 1

Env: SUPABASE_URL plus SUPABASE_READONLY_KEY (preferred) or SUPABASE_ANON_KEY,
falling back to SUPABASE_SERVICE_KEY for local use only. Never deploy this
with the service key — see the warning emitted at startup.
"""

import logging
import os
import re
import sys
import time
from datetime import date

from dotenv import load_dotenv
from supabase import create_client

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

# stdio transport speaks MCP framing on stdout, so a stray INFO line from the
# HTTP stack corrupts the stream and the client drops the connection. Silence
# the chatty libraries before the first request.
for _noisy in ('httpx', 'httpcore', 'hpack', 'h2', 'supabase', 'postgrest',
               'storage3', 'realtime', 'urllib3'):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = (os.environ.get('SUPABASE_READONLY_KEY')
       or os.environ.get('SUPABASE_ANON_KEY')
       or os.environ.get('SUPABASE_SERVICE_KEY'))
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and a Supabase key must be set')

# stdio mode speaks MCP on stdout, so every log line goes to stderr.
def log(msg):
    print(msg, file=sys.stderr, flush=True)


if (not os.environ.get('SUPABASE_READONLY_KEY')
        and not os.environ.get('SUPABASE_ANON_KEY')):
    log('WARNING: falling back to SUPABASE_SERVICE_KEY. Fine on your own '
        'machine; it must NOT be used once this is a public endpoint — the '
        'service key bypasses every row-level security rule.')

sb = create_client(URL, KEY)

STALE_AFTER_DAYS = 5        # flag data older than this in every payload
CACHE_TTL = 900             # 15 min; the underlying data moves once a day
_cache = {}


def cached(key, fn):
    """Tiny TTL cache. The whole dataset is ~400 rows, so this is about
    keeping tool calls snappy, not about load."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    return float(xs[mid]) if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


# ── data access ────────────────────────────────────────────────────────────

def latest_scored_date():
    """Newest snapshot date that actually carries momentum scores.

    Not simply max(snapshot_date): the benchmark row and a few early-finalizing
    tickers can create a newer, unscored date that would read as empty."""
    def _q():
        r = (sb.table('daily_stock_snapshots').select('snapshot_date')
             .not_.is_('momentum_score', 'null')
             .order('snapshot_date', desc=True).limit(1).execute().data)
        return r[0]['snapshot_date'] if r else None
    return cached('latest_date', _q)


def as_of_block():
    """The freshness stanza attached to every response."""
    d = latest_scored_date()
    if not d:
        return {'as_of': None, 'stale': True,
                'note': 'No scored session available.'}
    age = (date.today() - date.fromisoformat(d)).days
    block = {'as_of': d, 'trading_days_old': age, 'stale': age > STALE_AFTER_DAYS}
    if block['stale']:
        block['note'] = (f'Data is {age} days old; say so when answering rather '
                         f'than implying it is current.')
    return block


def all_themes():
    def _q():
        return (sb.table('themes')
                .select('slug,name,description,category,category_order,display_order')
                .order('category_order').order('display_order').execute().data or [])
    return cached('themes', _q)


def theme_nodes(slug):
    def _q():
        return (sb.table('theme_nodes')
                .select('node_key,name,short_label,layer,blurb')
                .eq('theme_slug', slug).order('layer').execute().data or [])
    return cached(f'nodes:{slug}', _q)


def theme_members(slug):
    def _q():
        return (sb.table('theme_members')
                .select('node_key,ticker,note,is_context')
                .eq('theme_slug', slug).execute().data or [])
    return cached(f'members:{slug}', _q)


def momentum_for(tickers):
    """ticker -> {momentum_score, rs_rank} for the latest scored session."""
    d = latest_scored_date()
    if not d or not tickers:
        return {}
    out = {}
    tickers = sorted(set(tickers))
    for i in range(0, len(tickers), 200):
        rows = (sb.table('daily_stock_snapshots')
                .select('ticker,momentum_score,rs_rank')
                .eq('snapshot_date', d)
                .in_('ticker', tickers[i:i + 200]).execute().data or [])
        for r in rows:
            out[r['ticker']] = r
    return out


def company_names(tickers):
    out = {}
    tickers = sorted(set(tickers))
    for i in range(0, len(tickers), 200):
        rows = (sb.table('us_stock_sectors').select('ticker,company_name')
                .in_('ticker', tickers[i:i + 200]).execute().data or [])
        for r in rows:
            out[r['ticker']] = r.get('company_name')
    return out


# How people actually phrase these in a chat box. The theme names are our
# internal labels ("GLP-1 & Obesity"); nobody types that. Matching on the
# description alone is far too loose — "obesity drugs" hit both glp1 and
# genomics because both descriptions mention "drug". These are the deciding
# signal, and they are the same phrases the public tool descriptions use.
ALIASES = {
    'ai':          ['ai', 'artificial intelligence', 'machine learning', 'llm',
                    'chips', 'chip', 'semiconductor', 'semiconductors', 'semis',
                    'gpu', 'gpus', 'data center', 'datacenter', 'compute',
                    'nvidia', 'hyperscaler', 'inference'],
    'cyber':       ['cyber', 'cybersecurity', 'security', 'infosec', 'ransomware',
                    'zero trust', 'firewall', 'hacking'],
    'quantum':     ['quantum', 'quantum computing', 'qubit', 'qubits'],
    'robotics':    ['robot', 'robots', 'robotics', 'automation', 'humanoid',
                    'factory automation', 'cobot'],
    'space':       ['space', 'defense', 'defence', 'satellite', 'satellites',
                    'aerospace', 'drone', 'drones', 'rocket'],
    'nuclear':     ['nuclear', 'uranium', 'smr', 'reactor', 'reactors',
                    'fission', 'atomic'],
    'ev':          ['ev', 'evs', 'electric vehicle', 'electric vehicles',
                    'battery', 'batteries', 'lithium', 'charging', 'ev charging'],
    'cleanenergy': ['solar', 'wind', 'renewable', 'renewables', 'clean energy',
                    'green energy', 'energy storage', 'photovoltaic'],
    'minerals':    ['copper', 'critical minerals', 'rare earth', 'rare earths',
                    'mining', 'miners', 'metals', 'commodities'],
    'water':       ['water', 'desalination', 'water utilities', 'irrigation'],
    'agriculture': ['agriculture', 'agricultural', 'farming', 'farm', 'food',
                    'fertilizer', 'fertiliser', 'crop', 'crops', 'agtech'],
    'glp1':        ['glp1', 'glp-1', 'obesity', 'obesity drug', 'obesity drugs',
                    'weight loss', 'weight-loss', 'ozempic', 'wegovy', 'zepbound',
                    'mounjaro', 'semaglutide', 'tirzepatide', 'diabetes',
                    'anti-obesity'],
    'genomics':    ['genomics', 'genomic', 'gene', 'genes', 'dna', 'sequencing',
                    'crispr', 'gene editing', 'precision medicine', 'biotech'],
    'aging':       ['aging', 'ageing', 'longevity', 'anti-aging', 'senescence',
                    'life extension'],
    'housing':     ['housing', 'homebuilder', 'homebuilders', 'home builder',
                    'house building', 'mortgage', 'residential'],
    'reshoring':   ['reshoring', 'onshoring', 'nearshoring', 'us manufacturing',
                    'american manufacturing', 'industrials', 'factory build'],
    'fintech':     ['fintech', 'payments', 'digital payments', 'card networks',
                    'neobank', 'banking tech'],
    'crypto':      ['crypto', 'cryptocurrency', 'bitcoin', 'ethereum',
                    'blockchain', 'digital assets', 'miners bitcoin', 'stablecoin'],
}


def _words(s):
    return set(re.findall(r'[a-z0-9\-]+', (s or '').lower()))


def resolve_theme(query):
    """Map loose user wording ('obesity drugs', 'who makes chips for AI') onto
    a theme slug.

    Returns (theme_row, candidates). A confident match sets theme_row; an
    ambiguous one returns candidates so the caller asks instead of silently
    answering about the wrong chain. Scoring is tiered deliberately: an alias
    phrase is strong evidence, a description word is weak, and a win needs a
    clear margin over the runner-up."""
    themes = all_themes()
    q = (query or '').strip().lower()
    if not q:
        return None, themes
    by_slug = {t['slug']: t for t in themes}

    for t in themes:                                    # 1. exact slug or name
        if q == t['slug'].lower() or q == t['name'].lower():
            return t, []

    qwords = _words(q)
    scores = {}
    for slug, aliases in ALIASES.items():
        if slug not in by_slug:
            continue
        best = 0
        for a in aliases:
            if ' ' in a:                                # 2. multi-word phrase
                if a in q:
                    best = max(best, 10 + len(a.split()))
            elif a in qwords:                           # 3. whole word only
                best = max(best, 10)
        if best:
            scores[slug] = best

    for t in themes:                                    # 4. our own label text
        hit = 0
        if t['slug'].lower() in qwords or t['name'].lower() in q:
            hit += 8
        hit += 3 * len(qwords & _words(t['name']))
        hit += 1 * len(qwords & _words(t.get('description')))
        if hit:
            scores[t['slug']] = scores.get(t['slug'], 0) + hit

    if not scores:
        return None, themes
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if len(ranked) == 1 or ranked[0][1] >= ranked[1][1] + 3:
        return by_slug[ranked[0][0]], []
    return None, [by_slug[s] for s, _ in ranked]


def no_match(query, candidates):
    return {
        'error': f'No single theme matches "{query}".',
        'candidates': [{'theme': c['slug'], 'name': c['name']} for c in candidates[:8]],
        'hint': 'Ask the user which one they meant, or call list_themes.',
    }


def build_links(slug):
    """Links for a theme, upstream->downstream, each with members and the
    median momentum of its in-universe names."""
    nodes = theme_nodes(slug)
    members = theme_members(slug)
    mom = momentum_for([m['ticker'] for m in members if not m.get('is_context')])
    names = company_names([m['ticker'] for m in members])

    by_node = {}
    for m in members:
        by_node.setdefault(m['node_key'], []).append(m)

    links = []
    for n in nodes:
        rows = []
        for m in sorted(by_node.get(n['node_key'], []), key=lambda r: r['ticker']):
            s = mom.get(m['ticker'], {})
            rows.append({
                'ticker': m['ticker'],
                'company': names.get(m['ticker']),
                'why_it_belongs': m.get('note'),
                'momentum': s.get('momentum_score'),
                'rs_rank': s.get('rs_rank'),
                # Foreign / private / ADR names we show for completeness but do
                # not rank — they sit outside the scored US universe.
                'context_only': bool(m.get('is_context')),
            })
        scores = [r['momentum'] for r in rows if not r['context_only']]
        med = median(scores)
        links.append({
            'link': n['node_key'],
            'name': n['name'],
            'short_label': n.get('short_label'),
            'position': n['layer'],
            'what_it_is': n.get('blurb'),
            'median_momentum': round(med) if med is not None else None,
            'scored_members': len(scores),
            'confidence': confidence(len(scores)),
            'members': rows,
        })
    return links


def confidence(n):
    """How much weight a link's median deserves.

    A median over one stock is that stock. Several links are genuinely thin
    (AI 'Advanced Packaging' is AMKR alone) and four are narrative placeholders
    with no members at all, so the payload has to say which is which — otherwise
    the model reports 'packaging ranks 6th' as though it measured something."""
    if n == 0:
        return 'empty'
    if n <= 2:
        return 'low'
    return 'ok'


LOW_N_CAVEAT = ('Links marked confidence "low" hold 1-2 companies, so their '
                'median is close to a single stock — treat the ordering as '
                'indicative and say so. Links marked "empty" are narrative '
                'stages with no listed pure-plays mapped yet; describe them if '
                'useful but never present them as a stock list.')


MOMENTUM_EXPLAINER = (
    'momentum is our own 0-100 score: each name is ranked against ~1,900 US '
    'stocks on relative-strength level and trend, 9- and 20-day EMA slopes, '
    'and price vs its 50-day EMA. 50 is the median name, not a neutral return '
    '— in a falling market a high score means falling less than peers. It is '
    'a ~1-month read, not a forecast.'
)

DISCLAIMER = ('Educational research data, not investment advice. '
              'Not personalised to anyone.')

mcp = MCPServer(
    name='theme-value-chains',
    version='0.1.0',
    instructions=(
        'Hand-curated US equity value chains: for a theme such as AI, GLP-1 '
        'obesity drugs, nuclear power or copper, these tools return the supply '
        'chain broken into links from upstream to downstream, which listed '
        'companies sit in each link and why, and how each link is performing '
        'right now on a momentum score.\n\n'
        'Use get_value_chain to explain a theme, rank_links to say which part '
        'of a chain is working, compare_peers to go deep on one link. Always '
        'report the as_of date. ' + MOMENTUM_EXPLAINER + '\n\n' + DISCLAIMER
    ),
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)


@mcp.tool(
    name='list_themes',
    title='List available value chains',
    description=(
        'List every investment theme with a mapped value chain — AI, robotics, '
        'quantum, cybersecurity, space and defense, nuclear power, EVs and '
        'batteries, clean energy, copper and critical minerals, water, '
        'agriculture, GLP-1 and obesity drugs, genomics, longevity, housing, '
        'reshoring, fintech and crypto. Call this first when the user asks '
        'what themes or sectors are covered, or when a theme name is ambiguous.'
    ),
    annotations=READ_ONLY,
)
def list_themes() -> dict:
    by_cat = {}
    for t in all_themes():
        by_cat.setdefault(t.get('category') or 'Other', []).append({
            'theme': t['slug'],
            'name': t['name'],
            'description': t.get('description'),
        })
    return {
        'categories': [{'category': c, 'themes': v} for c, v in by_cat.items()],
        'total_themes': sum(len(v) for v in by_cat.values()),
        **as_of_block(),
        'disclaimer': DISCLAIMER,
    }


@mcp.tool(
    name='get_value_chain',
    title='Get a theme value chain',
    description=(
        'Break an investment theme into its supply chain and show which listed '
        'US companies sit at each stage, with a one-line reason for each and a '
        'momentum score. Use for questions like "who makes the chips for AI", '
        '"what companies are in the AI supply chain", "obesity drug stocks", '
        '"who benefits from nuclear power", "copper and critical minerals '
        'companies", or "explain the EV battery supply chain". Links are '
        'returned upstream to downstream.'
    ),
    annotations=READ_ONLY,
)
def get_value_chain(theme: str) -> dict:
    t, cands = resolve_theme(theme)
    if not t:
        return no_match(theme, cands)
    links = build_links(t['slug'])
    out = {
        'theme': t['slug'],
        'name': t['name'],
        'description': t.get('description'),
        'reading_order': 'upstream to downstream — each link feeds the next',
        'links': links,
        'momentum_explainer': MOMENTUM_EXPLAINER,
        **as_of_block(),
        'disclaimer': DISCLAIMER,
    }
    if any(l['confidence'] != 'ok' for l in links):
        out['sample_size_caveat'] = LOW_N_CAVEAT
    return out


@mcp.tool(
    name='rank_links',
    title='Rank the links in a chain by momentum',
    description=(
        'Rank the stages of a theme value chain from strongest to weakest on '
        'current momentum, so you can say which part of a supply chain is '
        'working and which is lagging. Use for "which part of the AI trade is '
        'hottest", "where is the momentum in nuclear", "is the AI rally still '
        'in chips or has it moved to power". Returns each link with its median '
        'momentum and its leading name; no per-member detail.'
    ),
    annotations=READ_ONLY,
)
def rank_links(theme: str) -> dict:
    t, cands = resolve_theme(theme)
    if not t:
        return no_match(theme, cands)

    ranked, unpopulated = [], []
    for ln in build_links(t['slug']):
        if ln['median_momentum'] is None:
            unpopulated.append({'link': ln['link'], 'name': ln['name'],
                                'what_it_is': ln['what_it_is']})
            continue
        scored = [m for m in ln['members']
                  if not m['context_only'] and m['momentum'] is not None]
        lead = max(scored, key=lambda m: m['momentum']) if scored else None
        ranked.append({
            'link': ln['link'],
            'name': ln['name'],
            'position': ln['position'],
            'median_momentum': ln['median_momentum'],
            'members': ln['scored_members'],
            'confidence': ln['confidence'],
            'leader': {'ticker': lead['ticker'], 'company': lead['company'],
                       'momentum': lead['momentum']} if lead else None,
        })
    ranked.sort(key=lambda r: -r['median_momentum'])

    # Headline strongest/weakest only from links with a real sample — calling a
    # one-stock link "the hottest part of the chain" would be a data artefact.
    solid = [r for r in ranked if r['confidence'] == 'ok']
    all_scores = [r['median_momentum'] for r in solid] or \
                 [r['median_momentum'] for r in ranked]

    out = {
        'theme': t['slug'],
        'name': t['name'],
        'links_ranked': ranked,
        'strongest': solid[0]['name'] if solid else None,
        'weakest': solid[-1]['name'] if solid else None,
        'headline_basis': ('strongest/weakest consider only links with 3+ '
                           'companies; thinner links still appear in '
                           'links_ranked with confidence "low"'),
        'theme_median_momentum': round(median(all_scores)) if all_scores else None,
        'momentum_explainer': MOMENTUM_EXPLAINER,
        **as_of_block(),
        'disclaimer': DISCLAIMER,
    }
    if unpopulated:
        out['unpopulated_links'] = unpopulated
    if any(r['confidence'] != 'ok' for r in ranked) or unpopulated:
        out['sample_size_caveat'] = LOW_N_CAVEAT
    return out


@mcp.tool(
    name='compare_peers',
    title='Compare the companies inside one link',
    description=(
        'List the companies in a single stage of a theme value chain, ranked by '
        'momentum, so they can be compared like for like — memory makers against '
        'memory makers rather than against software. Use for "compare the AI '
        'memory stocks", "who are the semiconductor equipment names", "which '
        'obesity drug maker has the best momentum". Pass the theme plus the link '
        'key or name from get_value_chain or rank_links.'
    ),
    annotations=READ_ONLY,
)
def compare_peers(theme: str, link: str) -> dict:
    t, cands = resolve_theme(theme)
    if not t:
        return no_match(theme, cands)

    links = build_links(t['slug'])
    q = (link or '').strip().lower()
    match = next((l for l in links if l['link'].lower() == q), None)
    if not match:
        match = next((l for l in links
                      if q and (q in l['name'].lower()
                                or q in (l['short_label'] or '').lower())), None)
    if not match:
        return {
            'error': f'No link matching "{link}" in theme "{t["slug"]}".',
            'available_links': [{'link': l['link'], 'name': l['name']} for l in links],
        }

    ranked = sorted(
        [m for m in match['members'] if not m['context_only']],
        key=lambda m: (m['momentum'] is None, -(m['momentum'] or 0)))
    context = [m for m in match['members'] if m['context_only']]

    out = {
        'theme': t['slug'],
        'link': match['link'],
        'name': match['name'],
        'what_it_is': match['what_it_is'],
        'median_momentum': match['median_momentum'],
        'confidence': match['confidence'],
        'companies': ranked,
        'context_only': context,
        'context_note': ('Foreign, private or ADR names listed for completeness; '
                         'outside our scored US universe, so unranked.')
        if context else None,
        'momentum_explainer': MOMENTUM_EXPLAINER,
        **as_of_block(),
        'disclaimer': DISCLAIMER,
    }
    if match['confidence'] == 'empty':
        out['note'] = ('This stage has no listed pure-plays mapped. Explain what '
                       'it is from what_it_is; do not imply a stock list exists.')
    elif match['confidence'] == 'low':
        out['note'] = (f'Only {match["scored_members"]} company(ies) here — too '
                       f'few to compare meaningfully. Present them individually '
                       f'rather than as a ranking.')
    return out


if __name__ == '__main__':
    if '--http' in sys.argv:                     # Phase 1 shape, same tools
        log('Starting theme-value-chains on streamable-http ...')
        mcp.run(transport='streamable-http')
    else:
        log(f'theme-value-chains ready (stdio) — latest scored session '
            f'{latest_scored_date()}')
        mcp.run(transport='stdio')
