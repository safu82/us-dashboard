"""Stage 6 — the historian. What we said last time, and whether it held.

Two jobs, and the second is the one that matters.

1. Diff this quarter's evidence pack against the last one, so a note opens
   from what changed rather than restating the quarter from zero.

2. Keep a register of the falsifiable claims a note made, and re-test them
   when new evidence arrives. The most valuable section of the HFCL note is
   the one admitting four things the August note got wrong — defence
   overstated 5x, the Turnkey segment never mentioned, a net-debt-free claim
   wrong for the date cited, a risk since resolved. That section only exists
   because somebody went back and checked. Nothing about going back and
   checking should depend on remembering to.

A claim is only accepted if it is machine-testable: a metric, an operator, a
threshold and a date by which it should be true. "Management is credible" is
not a claim; "operating margin stays above 50% through FY27" is.

Deterministic. No model.
"""
import glob
import io
import json
import os
import re
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'out')
CLAIMS = os.path.join(HERE, 'claims.json')

# A change smaller than this is noise in a quarterly comparison.
MATERIAL_PCT = 10.0

OPS = {
    '>=': lambda a, b: a >= b,
    '>': lambda a, b: a > b,
    '<=': lambda a, b: a <= b,
    '<': lambda a, b: a < b,
    '==': lambda a, b: a == b,
    '!=': lambda a, b: a != b,
}


# ---------------------------------------------------------------------------
# a flat view of a pack, so claims can name one thing
# ---------------------------------------------------------------------------

def spans(facts_pack):
    """How many days each metric covers.

    Not cosmetic. A 10-Q's income statement is quarterly but its cash-flow
    statement is year-to-date, and "year to date" means different lengths in
    different fiscal quarters. SanDisk's April filing is fiscal Q3, so its
    operating cash flow spans nine months; the October filing is fiscal Q1,
    so its spans three. Comparing them gives "+40%" and means nothing.
    """
    out = {}
    src = {
        'revenue': 'revenue', 'operating_income': 'operating_income',
        'pretax_income': 'pretax_income', 'net_income': 'net_income_attributable',
        'eps_basic': 'eps_basic', 'eps_diluted': 'eps_diluted',
        'operating_cash_flow': 'operating_cash_flow', 'capex': 'capex',
        'buybacks': 'buybacks', 'stock_issued': 'stock_issued',
        'tax_expense': 'tax_expense', 'shares_basic': 'shares_basic_wavg',
        'shares_diluted': 'shares_diluted_wavg',
    }
    for name, field in src.items():
        d = (facts_pack.get('quarter') or {}).get(field)
        out[name] = d.get('span_days') if d else None
    # Derived metrics inherit the span of what they are built from.
    out['operating_margin'] = out['revenue']
    out['net_margin'] = out['revenue']
    out['free_cash_flow'] = out['operating_cash_flow']
    out['tax_rate'] = out['pretax_income']
    return out


def metrics(facts_pack, cross_pack=None):
    """Flatten facts + cross-section into named metrics.

    Claims reference these names. Derived values (margins, free cash flow)
    are computed here once rather than in every claim, so a claim about
    "operating_margin" cannot disagree with the note's own arithmetic.
    """
    def q(f):
        d = (facts_pack.get('quarter') or {}).get(f)
        return d['value'] if d else None

    def b(f):
        d = (facts_pack.get('balance') or {}).get(f)
        return d['value'] if d else None

    m = {
        'revenue': q('revenue'),
        'operating_income': q('operating_income'),
        'pretax_income': q('pretax_income'),
        'net_income': q('net_income_attributable'),
        'eps_basic': q('eps_basic'),
        'eps_diluted': q('eps_diluted'),
        'operating_cash_flow': q('operating_cash_flow'),
        'capex': q('capex'),
        'buybacks': q('buybacks'),
        'stock_issued': q('stock_issued'),
        'tax_expense': q('tax_expense'),
        'shares_basic': q('shares_basic_wavg'),
        'shares_diluted': q('shares_diluted_wavg'),
        'shares_cover': b('shares_outstanding_cover'),
        'cash': b('cash'),
        'total_assets': b('total_assets'),
        'long_term_debt': b('long_term_debt'),
    }
    rev, op, ni = m['revenue'], m['operating_income'], m['net_income']
    m['operating_margin'] = round(op / rev * 100, 2) if rev and op is not None else None
    m['net_margin'] = round(ni / rev * 100, 2) if rev and ni is not None else None
    if m['operating_cash_flow'] is not None and m['capex'] is not None:
        m['free_cash_flow'] = m['operating_cash_flow'] - m['capex']
    else:
        m['free_cash_flow'] = None
    pbt, tax = m['pretax_income'], m['tax_expense']
    m['tax_rate'] = round(tax / pbt * 100, 2) if pbt and tax is not None else None
    m['_spans'] = spans(facts_pack)

    if cross_pack and cross_pack.get('covered'):
        s = cross_pack['standing']
        m.update({
            'momentum': s['momentum'],
            'rs_rank': s['rs_rank'],
            'from_52w_high': s['from_52w_high'],
            'close': s['close'],
        })
        ch = (cross_pack.get('chain') or [None])[0]
        if ch:
            m['stage_rank_weakest_first'] = ch['stage_rank_weakest_first']
    return m


# ---------------------------------------------------------------------------
# finding prior packs
# ---------------------------------------------------------------------------

def packs_for(ticker):
    """Every stored evidence pack for a ticker, newest period first."""
    found = []
    for p in glob.glob(os.path.join(OUT, f'{ticker.upper()}_*_facts.json')):
        mm = re.match(rf'{ticker.upper()}_(\d{{4}}-\d{{2}}-\d{{2}})_(\w+)_facts\.json',
                      os.path.basename(p))
        if not mm:
            continue
        found.append({'period': mm.group(1), 'form': mm.group(2),
                      'facts': p, 'cross': p.replace('_facts.json', '_cross.json')})
    found.sort(key=lambda f: f['period'], reverse=True)
    return found


def _load(path):
    if not os.path.exists(path):
        return None
    with io.open(path, encoding='utf-8-sig') as fh:
        return json.load(fh)


def load_metrics(entry):
    f = _load(entry['facts'])
    c = _load(entry['cross'])
    return metrics(f, c) if f else None


# ---------------------------------------------------------------------------
# the diff
# ---------------------------------------------------------------------------

def diff(prev, curr):
    """Material movements between two quarters.

    Sign flips are always material regardless of size: free cash flow going
    from +$6bn to -$1bn is the story even though the percentage is modest.
    """
    ps, cs = prev.get('_spans') or {}, curr.get('_spans') or {}
    rows = []
    for k in sorted(set(prev) | set(curr)):
        if k == '_spans':
            continue
        a, b = prev.get(k), curr.get(k)
        if a is None and b is None:
            continue
        if a is None or b is None:
            rows.append({'metric': k, 'prev': a, 'curr': b,
                         'change_pct': None, 'material': True,
                         'why': 'appeared' if a is None else 'disappeared'})
            continue
        if a == b:
            continue
        # Refuse a percentage across different reporting spans. Reporting one
        # anyway is how a nine-month cash flow becomes "+40% on the quarter".
        sa, sb = ps.get(k), cs.get(k)
        if sa and sb and abs(sa - sb) > 20:
            rows.append({'metric': k, 'prev': a, 'curr': b,
                         'change_pct': None, 'material': True,
                         'comparable': False,
                         'why': f'NOT COMPARABLE: {sa}d vs {sb}d'})
            continue
        pct = ((b - a) / abs(a) * 100) if a else None
        flip = (a > 0) != (b > 0) and 0 not in (a, b)
        material = flip or (pct is not None and abs(pct) >= MATERIAL_PCT)
        rows.append({'metric': k, 'prev': a, 'curr': b,
                     'change_pct': round(pct, 1) if pct is not None else None,
                     'material': material, 'comparable': True,
                     'why': 'sign flip' if flip else ''})
    return rows


# ---------------------------------------------------------------------------
# the claims register
# ---------------------------------------------------------------------------

def load_claims():
    d = _load(CLAIMS)
    if d is None:
        return []
    if not isinstance(d, list):
        raise ValueError(f'{CLAIMS} must be a list of claims')
    return d


def save_claims(claims):
    with io.open(CLAIMS, 'w', encoding='utf-8') as fh:
        json.dump(claims, fh, indent=1, sort_keys=True)
    return CLAIMS


def add_claim(ticker, text, metric, op, value, horizon, source_note=''):
    """Register a falsifiable statement. Rejects anything untestable."""
    if op not in OPS:
        raise ValueError(f'operator must be one of {sorted(OPS)}')
    claims = load_claims()
    cid = f'{ticker.upper()}-{len(claims) + 1:03d}'
    claims.append({'id': cid, 'ticker': ticker.upper(),
                   'made_on': date.today().isoformat(),
                   'source_note': source_note, 'text': text,
                   'metric': metric, 'op': op, 'value': value,
                   'horizon': horizon, 'status': 'open'})
    save_claims(claims)
    return cid


def evaluate(ticker, curr_metrics, asof=None):
    """Re-test every open claim for a ticker against the newest evidence.

    Four outcomes. `unresolvable` is deliberately distinct from `broken`: a
    metric we cannot read is not a claim that failed, and collapsing the two
    would let the register quietly mark things wrong that were never checked.
    """
    asof = asof or date.today().isoformat()
    results = []
    for c in load_claims():
        if c['ticker'] != ticker.upper():
            continue
        got = curr_metrics.get(c['metric'])
        if got is None:
            verdict, detail = 'unresolvable', f"metric '{c['metric']}' not in this pack"
        else:
            ok = OPS[c['op']](got, c['value'])
            due = c.get('horizon') and asof >= c['horizon']
            if ok:
                verdict = 'held'
            elif due:
                verdict = 'broken'
            else:
                verdict = 'pending'
            detail = (f"{c['metric']} is {got:,} vs required {c['op']} "
                      f"{c['value']:,}" + ('' if due else
                      f" (horizon {c['horizon']} not reached)"))
        results.append(dict(c, verdict=verdict, observed=got, detail=detail,
                            checked_on=asof))
    return results


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def review(ticker):
    entries = packs_for(ticker)
    out = {'ticker': ticker.upper(), 'packs_held': len(entries)}
    if not entries:
        out['note'] = 'No stored packs. Run research/run.py first.'
        return out
    curr = load_metrics(entries[0])
    out['current_period'] = entries[0]['period']
    out['metrics'] = curr
    if len(entries) > 1:
        prev = load_metrics(entries[1])
        out['prior_period'] = entries[1]['period']
        out['diff'] = diff(prev, curr)
    else:
        out['note'] = ('Only one pack stored, so there is nothing to compare '
                       'against yet. Backfill a prior quarter with '
                       '--nth 1 to make the diff meaningful.')
    out['claims'] = evaluate(ticker, curr)
    return out


def render(r):
    L = [f"{r['ticker']} historian — {r['packs_held']} pack(s) stored"]
    if r.get('prior_period'):
        L.append(f"  comparing {r['prior_period']} -> {r['current_period']}")
        mat = [d for d in r['diff'] if d['material']]
        if not mat:
            L.append('  nothing moved materially')
        for d in mat:
            pct = f"{d['change_pct']:+.1f}%" if d['change_pct'] is not None else '—'
            L.append(f"    {d['metric']:<26} {_n(d['prev'])} -> {_n(d['curr'])}"
                     f"  {pct}" + (f"  [{d['why']}]" if d['why'] else ''))
    elif r.get('note'):
        L.append('  ' + r['note'])
    cl = r.get('claims') or []
    L.append(f"  claims on file: {len(cl)}")
    for c in cl:
        L.append(f"    [{c['verdict']:<12}] {c['text']}")
        L.append(f"                   {c['detail']}")
    return '\n'.join(L)


def _n(v):
    if v is None:
        return 'n/a'
    if isinstance(v, float) and abs(v) < 1000:
        return f'{v:,.2f}'
    if abs(v) >= 1e9:
        return f'{v/1e9:,.2f}bn'
    if abs(v) >= 1e6:
        return f'{v/1e6:,.1f}m'
    return f'{v:,.0f}'


if __name__ == '__main__':
    import sys
    for t in (sys.argv[1:] or ['SNDK']):
        print('\n' + render(review(t)))
