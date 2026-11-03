"""Chain-level analysis — the unit the single-company pipeline was missing.

A note on one company argues from one side. SanDisk's margins looked like a
cycle peak until you put Amazon's "AWS growth over 40% in 2027 and 2028" and
Alphabet's "CapEx to increase significantly in 2027" next to them, and those
sat in adjacent files the writer never saw. Supply-side reasoning with the
demand side missing is how a note reaches a confident wrong answer.

The chain is the fix, and it is also the thing this system has that others do
not: 18 hand-mapped value chains, 94 stages, each stage carrying a `position`
that orders it from raw inputs through to the buyers. That ordering is what
makes "where in the chain is the market paying?" a question with an answer.

Two layers, deliberately separated by cost:

  breadth   every member's standing, from our own database. Free, no API,
            no model. This is most of the value.
  depth     estimates and guidance, metered per ticker because Alpha
            Vantage allows 25 calls a day. Spend it on names that matter.

Stage scores are RECOMPUTED here from the current snapshot rather than read
from chains.json, which is rebuilt nightly and lags by a session. Mixing the
two put one ticker at two different momentum values in a single pack earlier
today.
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crosssection as X

CHAINS = X.CHAINS
MIN_SCORED = 3


def load_chains():
    with io.open(CHAINS, encoding='utf-8-sig') as fh:
        doc = json.load(fh)
    raw = doc['chains']
    return doc.get('asOf'), (list(raw.values()) if isinstance(raw, dict) else raw)


def list_chains():
    _, chains = load_chains()
    return [{'name': c['name'], 'slug': c.get('slug'),
             'category': c.get('category'),
             'stages': len(c.get('links') or [])} for c in chains]


def _members(link):
    return [m for m in (link.get('members') or []) if not m.get('context')]


def spread(rows):
    """The full distribution of a group's members, computed once.

    Left to choose, a writer cites the members that suit its argument. It
    recommended Materials & Inputs on APD, DD and LIN while omitting MKSI at
    momentum 7, and called SanDisk's de-rate group-wide while omitting NTAP
    at rank 906. Both were caught by the critic, which is the expensive way
    to find them.

    So the selection is removed. The weakest and strongest member, the
    median, and how many sit in the weak tail are computed here, and the
    prompt requires a stage to be cited by its spread rather than by
    whichever names were picked out of it.
    """
    vals = sorted((r['momentum'], r['ticker']) for r in rows
                  if r.get('momentum') is not None)
    if not vals:
        return None
    mid = vals[len(vals) // 2][0] if len(vals) % 2 else round(
        (vals[len(vals) // 2 - 1][0] + vals[len(vals) // 2][0]) / 2, 1)
    return {
        'members_scored': len(vals),
        'weakest': {'ticker': vals[0][1], 'momentum': vals[0][0]},
        'strongest': {'ticker': vals[-1][1], 'momentum': vals[-1][0]},
        'median_momentum': mid,
        'range': [vals[0][0], vals[-1][0]],
        'below_20': sum(1 for v, _ in vals if v < 20),
        'above_50': sum(1 for v, _ in vals if v > 50),
        'all_momentum': {t: v for v, t in vals},
    }


def _score(members, uni):
    """Mean momentum of the members we can score today.

    Labelled as our own calculation, not the portal's published stage score,
    because the formula behind that is not ours to assume.
    """
    vals = [uni[m['ticker']]['momentum_score'] for m in members
            if m['ticker'] in uni
            and uni[m['ticker']]['momentum_score'] is not None]
    if not vals:
        return None, 0
    return round(sum(vals) / len(vals), 1), len(vals)


def board(asof=None, back=21):
    """Every chain ranked, so one chain can be placed against the rest.

    "The AI chain fell" is a weaker statement than "the AI chain fell while
    fourteen of eighteen others did too" or "...while cybersecurity rose".
    Without the board a chain note cannot tell a rotation from a market.
    """
    asof = asof or X.latest_date()
    uni = X.universe(asof)
    prior = X.nearest_date(
        (__import__('datetime').date.fromisoformat(asof)
         - __import__('datetime').timedelta(days=int(back * 1.45))).isoformat())
    uni_then = X.universe(prior) if prior else None

    _, chains = load_chains()
    rows = []
    for c in chains:
        now_scores, then_scores = [], []
        for L in (c.get('links') or []):
            mem = _members(L)
            s, n = _score(mem, uni)
            if s is not None and n >= MIN_SCORED:
                now_scores.append(s)
                if uni_then:
                    t, tn = _score(mem, uni_then)
                    if t is not None and tn >= MIN_SCORED:
                        then_scores.append(t)
        if not now_scores:
            continue
        mean_now = round(sum(now_scores) / len(now_scores), 1)
        row = {'chain': c['name'], 'category': c.get('category'),
               'stages_scored': len(now_scores), 'mean_momentum': mean_now}
        if then_scores:
            mean_then = sum(then_scores) / len(then_scores)
            row['mean_then'] = round(mean_then, 1)
            row['change'] = round(mean_now - mean_then, 1)
        rows.append(row)
    rows.sort(key=lambda r: r['mean_momentum'])
    for i, r in enumerate(rows, 1):
        r['rank_weakest_first'] = i
    return {'asof': asof, 'compared_with': prior, 'chains': rows,
            'chains_ranked': len(rows),
            'chains_down': sum(1 for r in rows if (r.get('change') or 0) < 0)}


def profile(chain_name, asof=None):
    """Every stage of one chain, in supply-chain order, scored today.

    The shape of this list is the finding: whether the market is paying at
    the input end, the middle, or the buyer end.
    """
    asof = asof or X.latest_date()
    uni = X.universe(asof)
    chains_asof, chains = load_chains()
    hit = next((c for c in chains
                if c['name'].lower() == chain_name.lower()
                or (c.get('slug') or '').lower() == chain_name.lower()), None)
    if not hit:
        return {'error': f'no chain named {chain_name}',
                'available': [c['name'] for c in chains]}

    # Rank every stage in every chain so a stage can be placed against the
    # whole board, not just against its own neighbours.
    board = []
    for c in chains:
        for L in (c.get('links') or []):
            mem = _members(L)
            s, n = _score(mem, uni)
            if s is not None and n >= MIN_SCORED:
                board.append((s, c['name'], L['name']))
    board.sort()
    rank_of = {(c, s): i for i, (_, c, s) in enumerate(board, 1)}

    stages = []
    for L in sorted(hit.get('links') or [], key=lambda l: l.get('position') or 0):
        mem = _members(L)
        s, n = _score(mem, uni)
        rows = []
        for m in mem:
            r = uni.get(m['ticker'])
            rows.append({'ticker': m['ticker'],
                         'company': m.get('company'),
                         'momentum': r['momentum_score'] if r else None,
                         'rs_rank': r['rs_rank'] if r else None,
                         'in_universe': bool(r)})
        rows.sort(key=lambda r: (r['momentum'] is None, r['momentum']))
        stages.append({
            'position': L.get('position'), 'stage': L['name'],
            'what': L.get('what'),
            'momentum_mean_today': s, 'scored_members': n,
            'members_mapped': len(mem),
            'rank_weakest_first': rank_of.get((hit['name'], L['name'])),
            'of_stages_ranked': len(board),
            'too_thin_to_cite': n < MIN_SCORED,
            'spread': spread(rows),
            'members': rows,
        })
    scored = [s for s in stages if s['momentum_mean_today'] is not None
              and not s['too_thin_to_cite']]
    out = {
        'chain': hit['name'], 'asof': asof, 'chains_file_asof': chains_asof,
        'universe': len(uni),
        'stages': stages,
        'stages_scored': len(scored),
        'note': ('Stage scores are the mean momentum of members scored on '
                 f'{asof}, computed here, not the portal stage score.'),
    }
    if scored:
        out['strongest_stage'] = max(scored, key=lambda s: s['momentum_mean_today'])['stage']
        out['weakest_stage'] = min(scored, key=lambda s: s['momentum_mean_today'])['stage']
        out['chain_mean'] = round(
            sum(s['momentum_mean_today'] for s in scored) / len(scored), 1)
        # Is the market paying upstream or downstream? Correlate position
        # against score; a clear slope is the headline of a chain note.
        pos = [(s['position'] or 0, s['momentum_mean_today']) for s in scored]
        if len(pos) >= 4:
            n_ = len(pos)
            mx = sum(p for p, _ in pos) / n_
            my = sum(v for _, v in pos) / n_
            num = sum((p - mx) * (v - my) for p, v in pos)
            den = sum((p - mx) ** 2 for p, _ in pos)
            out['position_slope'] = round(num / den, 2) if den else None
            out['slope_reading'] = (
                'momentum rises toward the buyer end of the chain'
                if (out['position_slope'] or 0) > 1 else
                'momentum falls toward the buyer end of the chain'
                if (out['position_slope'] or 0) < -1 else
                'no clear tilt along the chain')
    return out


def movement(chain_name, back=21, asof=None):
    """How each stage has moved. A chain note is about change, not level."""
    asof = asof or X.latest_date()
    prior = X.nearest_date(
        (__import__('datetime').date.fromisoformat(asof)
         - __import__('datetime').timedelta(days=int(back * 1.45))).isoformat())
    now = profile(chain_name, asof)
    if now.get('error') or not prior:
        return now
    then = profile(chain_name, prior)
    was = {s['stage']: s['momentum_mean_today'] for s in then['stages']}
    for s in now['stages']:
        before = was.get(s['stage'])
        s['momentum_then'] = before
        s['momentum_change'] = (
            round(s['momentum_mean_today'] - before, 1)
            if before is not None and s['momentum_mean_today'] is not None
            else None)
    now['compared_with'] = prior
    moved = [s for s in now['stages'] if s.get('momentum_change') is not None
             and not s['too_thin_to_cite']]
    if moved:
        now['biggest_fall'] = min(moved, key=lambda s: s['momentum_change'])
        best = max(moved, key=lambda s: s['momentum_change'])
        # Do not call the least-bad stage a "rise". When every stage in a
        # chain is negative, that is the finding, and naming a loser a riser
        # hides it.
        now['best_stage'] = best
        now['any_stage_rose'] = best['momentum_change'] > 0
        now['stages_down'] = sum(1 for s in moved if s['momentum_change'] < 0)
        now['stages_moved'] = len(moved)
    return now


# ---------------------------------------------------------------------------
# depth layer — what the buyers have committed to
# ---------------------------------------------------------------------------

# Guidance that speaks to demand for the chain's output, as opposed to the
# guiding company's own P&L. Capex is the strongest: a hyperscaler's capex
# line is a supplier's revenue line.
DEMAND_WORDS = ('capital expenditure', 'capex', 'capital spend',
                'infrastructure', 'revenue', 'growth', 'net sales',
                'cloud', 'bookings', 'backlog')


def _release_pack(ticker, out_dir, allow_fetch=True):
    """Reuse an extracted press release if we already have one.

    Each fetch costs an EDGAR round trip and about 1.5 cents of model time.
    Across a chain that adds up, and the 8-K for a reported quarter does not
    change, so re-reading it is pure waste.
    """
    import glob as _g
    hits = sorted(_g.glob(os.path.join(out_dir, f'{ticker}_*_8K_release.json')))
    if hits:
        with io.open(hits[-1], encoding='utf-8') as fh:
            return json.load(fh), True
    if not allow_fetch:
        return None, False
    import pressrelease as PR
    try:
        return PR.read(ticker, out_dir), False
    except Exception as e:                                     # noqa: BLE001
        return {'ticker': ticker, 'error': str(e)}, False


def demand_board(chain_name, stages=None, max_new=6, asof=None):
    """Forward commitments from the stages that buy this chain's output.

    This is the join the single-company pipeline could not make. A note on a
    memory supplier argued mean reversion from its own margins while Amazon
    was guiding AWS growth above 40% through 2028 and Alphabet was guiding
    capex significantly higher in 2027 — both extracted the same week, both
    sitting in adjacent files nothing read together.

    Stages default to the furthest downstream in the chain, which is where
    the buyers sit by construction of the position field.
    """
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
    prof = profile(chain_name, asof)
    if prof.get('error'):
        return prof

    scored = [s for s in prof['stages'] if s.get('position')]
    if stages:
        want = {s.lower() for s in stages}
        chosen = [s for s in scored if s['stage'].lower() in want]
    else:
        top = max(s['position'] for s in scored)
        chosen = [s for s in scored if s['position'] >= top - 1]

    board, fetched = [], 0
    for st in chosen:
        for m in st['members']:
            t = m['ticker']
            allow = fetched < max_new
            pack, cached = _release_pack(t, out_dir, allow_fetch=allow)
            if pack is None:
                board.append({'ticker': t, 'stage': st['stage'],
                              'status': 'not fetched (budget reached)'})
                continue
            if not cached:
                fetched += 1
            if pack.get('error'):
                board.append({'ticker': t, 'stage': st['stage'],
                              'status': 'error: ' + str(pack['error'])[:90]})
                continue
            g = (pack.get('extraction') or {}).get('guidance') or []
            demand = [x for x in g
                      if any(w in (x.get('metric') or '').lower()
                             for w in DEMAND_WORDS)]
            board.append({
                'ticker': t, 'stage': st['stage'],
                'momentum': m.get('momentum'), 'rs_rank': m.get('rs_rank'),
                'filed': pack.get('filed'), 'source': pack.get('source_url'),
                'cached': cached,
                'demand_guidance': demand,
                'other_guidance_count': len(g) - len(demand),
                'absent_from_release': (pack.get('extraction') or {}).get(
                    'absent_from_this_document') or [],
            })

    supplier_stages = [s for s in scored
                       if s not in chosen and not s['too_thin_to_cite']]
    supplier_stages.sort(key=lambda s: s['momentum_mean_today'] or 999)
    return {
        'chain': prof['chain'], 'asof': prof['asof'],
        'buyer_stages': [s['stage'] for s in chosen],
        'buyers': board,
        'fetched_this_run': fetched,
        'suppliers_weakest_first': [
            {'stage': s['stage'], 'position': s['position'],
             'momentum': s['momentum_mean_today'],
             'rank': s['rank_weakest_first'],
             'members': [m['ticker'] for m in s['members'][:6]]}
            for s in supplier_stages],
    }


def render_demand(b):
    if b.get('error'):
        return b['error']
    L = [f"{b['chain']} — demand board, close {b['asof']}",
         f"  buyer stages: {', '.join(b['buyer_stages'])}"
         f"   ({b['fetched_this_run']} releases fetched this run)", '']
    any_g = False
    for r in b['buyers']:
        if r.get('status'):
            L.append(f"  {r['ticker']:<6} {r['status']}")
            continue
        head = (f"  {r['ticker']:<6} momentum {str(r.get('momentum')):>3}  "
                f"8-K {r.get('filed')}")
        L.append(head)
        for g in r['demand_guidance']:
            any_g = True
            L.append(f"         {g.get('period','')[:26]:<26} "
                     f"{g.get('metric','')[:34]:<34} {g.get('value','')[:60]}")
        if not r['demand_guidance']:
            L.append(f"         no demand-relevant guidance in that release"
                     f" ({r['other_guidance_count']} other items)")
    if not any_g:
        L.append('  No forward demand commitments found in the buyers\' '
                 'releases. Check their calls before concluding there are '
                 'none — issuers often guide verbally and file nothing.')
    L.append('')
    L.append('  suppliers, weakest first:')
    for s in b['suppliers_weakest_first'][:8]:
        L.append(f"    pos {s['position']:>2}  {s['stage'][:34]:<34} "
                 f"momentum {s['momentum']:>5}  rank {s['rank']}  "
                 f"{' '.join(s['members'][:5])}")
    return '\n'.join(L)


def render(p):
    if p.get('error'):
        return p['error'] + '\n  available: ' + ', '.join(p.get('available', []))
    L = [f"{p['chain']} — {p['stages_scored']} scoreable stages, "
         f"close {p['asof']}"
         + (f", vs {p['compared_with']}" if p.get('compared_with') else ''),
         f"  chain mean momentum {p.get('chain_mean')}  "
         f"({p.get('slope_reading', 'tilt not computed')})"]
    L.append('')
    L.append('  pos  stage                                  mom   chg   rank      members')
    for s in p['stages']:
        if s['momentum_mean_today'] is None:
            L.append(f"  {str(s['position'] or '-'):>3}  {s['stage'][:36]:<36}    "
                     f"-           (no scored members)")
            continue
        chg = s.get('momentum_change')
        chg_s = f"{chg:+5.1f}" if chg is not None else '    -'
        rank = (f"{s['rank_weakest_first']}/{s['of_stages_ranked']}"
                if s['rank_weakest_first'] else '   -')
        thin = '  THIN' if s['too_thin_to_cite'] else ''
        L.append(f"  {str(s['position'] or '-'):>3}  {s['stage'][:36]:<36} "
                 f"{s['momentum_mean_today']:>5.1f} {chg_s} {rank:>9}  "
                 f"{s['scored_members']}/{s['members_mapped']}{thin}")
    if p.get('biggest_fall'):
        b, g = p['biggest_fall'], p['best_stage']
        L.append('')
        if p.get('any_stage_rose'):
            L.append(f"  worst: {b['stage']} {b['momentum_change']:+.1f}"
                     f"   best: {g['stage']} {g['momentum_change']:+.1f}")
        else:
            L.append(f"  EVERY stage fell ({p['stages_down']} of "
                     f"{p['stages_moved']}). Worst {b['stage']} "
                     f"{b['momentum_change']:+.1f}; least bad {g['stage']} "
                     f"{g['momentum_change']:+.1f}. A chain-wide rotation, "
                     f"not a stage story.")
    return '\n'.join(L)


if __name__ == '__main__':
    args = sys.argv[1:]
    if not args:
        for c in list_chains():
            print('%-38s %2d stages   %s' % (c['name'], c['stages'],
                                             c.get('category') or ''))
        sys.exit(0)
    demand = '--demand' in args
    name = ' '.join(a for a in args if not a.startswith('--'))
    if demand:
        print('\n' + render_demand(demand_board(name)))
    else:
        print('\n' + render(movement(name)))
