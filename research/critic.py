"""Stage 9 — the critic. Attack the note before anyone acts on it.

Two layers, and the first one is the reason this works.

NUMBER AUDIT (deterministic, free)
    Pull every figure out of the note, pull every figure out of the evidence
    it was written from, and match them. Anything in the note that is not in
    the evidence is either derived — in which case the note must show the
    derivation — or invented. No model is needed to find them, and no model
    can talk its way past the result.

    This matters more than it sounds. A model writing from a pack does not
    usually hallucinate wildly; it slips a digit, carries a figure from the
    wrong period, or quietly computes something it was told not to. All three
    look completely plausible in prose and all three are caught here.

CHALLENGE (model)
    Given the note, the evidence and the unmatched figures, argue against the
    note: claims the evidence does not support, one-sided reasoning, a
    confident verdict on balanced facts.

The SNDK note's verdict moved between identical runs, which is exactly the
failure a critic exists to surface. It does not fix instability — nothing
does — but it makes the note say which parts rest on judgment rather than
evidence, and that is actionable where a flipping verdict is not.
"""
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except ImportError:
    pass

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
MODEL = 'claude-opus-5'
RATE_IN, RATE_OUT = 15.00, 75.00

# Two figures match if they agree to within this. Notes round (79.59 -> 79.6)
# and that is not an error.
TOL = 0.01

SCALE = {'bn': 1e9, 'billion': 1e9, 'b': 1e9,
         'm': 1e6, 'million': 1e6, 'mn': 1e6,
         'trillion': 1e12, 'tn': 1e12, 'k': 1e3}

# Figures too common to carry information. Flagging "4" or "2026" produces
# noise that buries the real findings.
IGNORE = {0, 1, 2, 3, 4, 5, 10, 100}
YEARS = set(range(1990, 2101))

NUM = re.compile(r"""
    (?P<neg>[-−+])?\s*\$?\s*
    (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*(?P<scale>bn|billion|million|mn|trillion|tn|[bmk])?\b
""", re.X | re.I)


def _vals(text):
    """Every figure in a string, scaled to its natural magnitude."""
    out = []
    for m in NUM.finditer(text or ''):
        raw = m.group('num').replace(',', '')
        try:
            v = float(raw)
        except ValueError:
            continue
        s = (m.group('scale') or '').lower()
        if s in SCALE:
            v *= SCALE[s]
        if m.group('neg') in ('-', '−'):
            v = -v
        out.append((v, m.group(0).strip()))
    return out


def context_numbers(ctx):
    """Every figure anywhere in the evidence, including inside strings.

    Guidance arrives as prose - "$12.2 billion to $12.6 billion" - so walking
    only the numeric leaves would miss most of what a note legitimately
    quotes.
    """
    seen = set()

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, bool):
            return
        elif isinstance(o, (int, float)):
            seen.add(float(o))
        elif isinstance(o, str):
            for v, _ in _vals(o):
                seen.add(v)
    walk(ctx)
    # Percentages are stored either way round; accept both readings.
    for v in list(seen):
        if 0 < abs(v) < 1:
            seen.add(v * 100)
    return seen


def _close(a, b, tol=TOL):
    # The epsilon is not slack. A figure rounded to the decimal shown lands
    # exactly on the allowance, and computing the same ratio two ways puts it
    # a last-bit over: 3.1 against 3.05 failed by 4e-19.
    return abs(a - b) / max(abs(a), abs(b), 1e-9) <= tol + 1e-12


# A note writing 3.1 for 3.05 is correct to the decimal it shows, but it sits
# 1.6% away and the flat tolerance reported it as unsupported. The allowance
# is half a unit of the last digit actually written, as a fraction of the
# figure, so precision earns tightness: 79.6 is held to 0.06%, 3.1 to 1.6%.
# Capped, because an integer would otherwise license several per cent.
MAX_ALLOW = 0.05


def _allow(token):
    m = NUM.search(token or '')
    if not m:
        return TOL
    raw = m.group('num').replace(',', '')
    try:
        num = abs(float(raw))
    except ValueError:
        return TOL
    if not num:
        return TOL
    dp = len(raw.split('.')[1]) if '.' in raw else 0
    # Scale cancels: half a unit at the written precision is the same
    # fraction of 5.1 whether it means 5.1 or $5.1bn.
    return max(TOL, min(0.5 * 10 ** -dp / num, MAX_ALLOW))


def _matches(v, pool, tol=TOL):
    """Does this figure appear in the evidence, allowing for how notes write?

    Three accommodations, each from a false positive this produced:

    Sign. A note says a stock "fell 1,497 places" where the pack holds
    -1497. The direction is in the prose, not the digits.

    Rounding. $5.136bn is written as $5.1bn, which is 0.7% away and was
    being flagged as unsupported. Notes round to the digits they show and
    that is not an error.

    Scale. The same quantity appears as 0.734, 73.4 and 73.4% depending on
    where it sits.
    """
    av = abs(v)
    for p in pool:
        ap = abs(p)
        if p == v or _close(av, ap, tol):
            return True
        for alt in (av * 1e9, av / 1e9, av * 1e6, av / 1e6,
                    av * 1e3, av / 1e3, av * 100, av / 100):
            if ap and _close(ap, alt, tol):
                return True
    return False


FOOTER = re.compile(r'\n-{3,}\s*\nGenerated .*$', re.S)


def _sigfigs(token):
    m = NUM.search(token or '')
    if not m:
        return 0
    return len(m.group('num').replace(',', '').replace('.', '').lstrip('0'))


def audit_numbers(note, ctx):
    """Figures in the note with no counterpart in the evidence.

    A clean result is worth only as much as the figures it cleared. Matching
    a two-digit percentage against three hundred evidence figures proves
    almost nothing: measured against this pack, 62% of arbitrary values
    between 1 and 200 find some counterpart. Matching $1,075.08 proves a
    great deal. So the audit also counts how many figures carried enough
    digits for the match to mean something, and that count is what a clean
    pass should be read against.
    """
    pool = context_numbers(ctx)
    # The provenance stamp carries the model's own cost and a date. Auditing
    # it reports the price of the note as an unsupported claim.
    body = FOOTER.sub('', note)
    unmatched, checked, distinctive = [], 0, 0
    for line in body.split('\n'):
        if line.strip().startswith('|') and set(line.strip()) <= set('|-: '):
            continue
        for v, token in _vals(line):
            if v in IGNORE or (v in YEARS and float(v).is_integer()):
                continue
            checked += 1
            if _sigfigs(token) >= 4:
                distinctive += 1
            if not _matches(v, pool, _allow(token)):
                unmatched.append({'value': v, 'as_written': token,
                                  'line': line.strip()[:160]})
    # One entry per distinct figure; a target repeated four times is one
    # problem, not four.
    dedup, seen = [], set()
    for u in unmatched:
        if u['value'] in seen:
            continue
        seen.add(u['value'])
        dedup.append(u)
    return {'figures_checked': checked, 'unmatched': dedup,
            'distinctive_figures': distinctive,
            'evidence_figures': len(pool)}


PROMPT = """You are auditing a research note against the evidence it was
written from. Your job is to attack it. A note that survives you is one
someone can act on; a note you wave through and which is wrong is your
failure, not the writer's.

You are given the NOTE, the EVIDENCE, and UNMATCHED_FIGURES — numbers that
appear in the note with no counterpart in the evidence. Each unmatched figure
is either legitimately derived (in which case the note must show the
derivation in its own text) or it is unsupported.

Return JSON:

{"challenges": [
  {"severity": "high" | "medium" | "low",
   "type": "unsupported_figure" | "wrong_basis" | "overstated" |
           "one_sided" | "contradicted_by_evidence" | "unstated_assumption",
   "quote": "the exact sentence or fragment from the note",
   "problem": "one sentence on what is wrong",
   "fix": "what the note should say instead"}],
 "verdict_assessment": {
   "verdict_in_note": "what it concluded",
   "evidence_supports_it": true | false | "partially",
   "why": "one or two sentences"},
 "strongest_omission": "the single most important thing the evidence \
contains that the note failed to use, or empty if none"}

Judge hard on these in particular:

- A figure used on the wrong basis: a quarter annualised and presented as
  trailing, a price from one date used with data from another, a margin
  computed over a different span.
- One-sided reasoning. If the evidence contains a demand side and the note
  argues only supply, or vice versa, that is a high-severity challenge.
- A confident verdict on balanced evidence. Say so plainly.
- Anything stated as fact that the evidence marks unstable, unverified or
  absent.

Return AT MOST 8 challenges, most severe first. Twenty is not a review, it is
a transcript of your reading, and nobody acts on it. Keep 'problem' and 'fix'
to one sentence each.

Do not challenge style. Do not invent problems to appear rigorous — an empty
challenge list is a legitimate result.

Return JSON only, and keep it parseable: inside any field value use single
quotes, never double quotes, because a quoted fragment from the note
containing its own double quotes will break the response. Keep each quote
field under 200 characters.
"""


def challenge(note, ctx, unmatched, model=MODEL):
    import anthropic
    client = anthropic.Anthropic()
    payload = {
        'NOTE': note,
        'UNMATCHED_FIGURES': unmatched,
        'EVIDENCE': ctx,
    }
    # A thorough critique of a long note runs well past 4k tokens, and a
    # truncated response is unparseable JSON rather than a short critique.
    resp = client.messages.create(
        model=model, max_tokens=16000,
        messages=[{'role': 'user',
                   'content': PROMPT + json.dumps(payload, indent=1,
                                                  default=str)}])
    raw = ''.join(b.text for b in resp.content if b.type == 'text').strip()
    raw = re.sub(r'^```(?:json)?|```$', '', raw, flags=re.M).strip()
    try:
        out = json.loads(raw)
    except json.JSONDecodeError as e:
        out = {'error': f'critic did not return JSON: {e}',
               'stop_reason': resp.stop_reason,
               'hint': ('response hit max_tokens' if resp.stop_reason ==
                        'max_tokens' else 'malformed output'),
               'raw': raw[:1200]}
    usage = {'model': model, 'input_tokens': resp.usage.input_tokens,
             'output_tokens': resp.usage.output_tokens,
             'cost_usd': round(resp.usage.input_tokens / 1e6 * RATE_IN
                               + resp.usage.output_tokens / 1e6 * RATE_OUT, 4)}
    return out, usage


def review(ticker, note_path=None, ctx_path=None):
    """Audit a note against THE CONTEXT IT WAS WRITTEN FROM.

    Pairing matters and getting it wrong is silent. Globbing for the newest
    note and the newest context independently matched an older note to a
    newer pack, and several figures were reported as unsupported purely
    because they had been written from different evidence. The context is
    therefore derived from the note's own filename, and a missing partner is
    an error rather than a fallback.
    """
    import glob as _g
    t = ticker.upper()
    if note_path is None:
        cands = _g.glob(os.path.join(OUT, f'{t}_*_note*.md'))
        if not cands:
            raise FileNotFoundError(f'no note for {t}; run research/writer.py')
        note_path = max(cands, key=os.path.getmtime)   # newest by time, not name
    if ctx_path is None:
        # Notes acquire suffixes — .NEW when the original was locked, .v2
        # after revision — while the evidence they were written from does
        # not change. Strip any suffix to find the partner.
        ctx_path = re.sub(r'_note(?:\.[A-Za-z0-9]+)?\.md$',
                          '_context.json', note_path)
        if not os.path.exists(ctx_path):
            # The writer falls back to a .NEW name only for the file that was
            # locked, so the pair can legitimately differ by that suffix.
            alt = re.sub(r'_note(?:\.NEW)?\.md$', '_context.json', note_path)
            if os.path.exists(alt):
                ctx_path = alt
            else:
                raise FileNotFoundError(
                    f'no context beside {os.path.basename(note_path)} — '
                    f'cannot audit a note against evidence it was not '
                    f'written from')
    note = io.open(note_path, encoding='utf-8').read()
    ctx = json.load(io.open(ctx_path, encoding='utf-8'))

    nums = audit_numbers(note, ctx)
    ch, usage = challenge(note, ctx, nums['unmatched'])
    out = {'ticker': t, 'note': os.path.basename(note_path),
           'context': os.path.basename(ctx_path),
           'number_audit': nums, **ch, 'usage': usage}
    with io.open(os.path.join(OUT, f'{t}_critique.json'), 'w',
                 encoding='utf-8') as fh:
        json.dump(out, fh, indent=1, default=str)
    return out


def render(r):
    a = r['number_audit']
    L = [f"CRITIC — {r['ticker']}  ({r['note']})",
         f"  number audit: {a['figures_checked']} figures in "
         f"the note, {len(a['unmatched'])} with no match in "
         f"{a['evidence_figures']} evidence figures",
         # Said out loud, because a bare zero reads as stronger than it is.
         f"    {a.get('distinctive_figures', 0)} of them carry four or more "
         f"digits; below that a match against a pool this dense is weak "
         f"evidence, so read a clean pass against that count"]
    for u in r['number_audit']['unmatched'][:12]:
        L.append(f"    {u['as_written']:<14} {u['line'][:104]}")
    if r.get('error'):
        L.append('  ' + r['error'])
        return '\n'.join(L)
    va = r.get('verdict_assessment') or {}
    L.append(f"  verdict: {va.get('verdict_in_note')} — evidence supports it: "
             f"{va.get('evidence_supports_it')}")
    if va.get('why'):
        L.append(f"    {va['why']}")
    chs = r.get('challenges') or []
    order = {'high': 0, 'medium': 1, 'low': 2}
    for c in sorted(chs, key=lambda c: order.get(c.get('severity'), 9)):
        L.append(f"  [{c.get('severity','?'):<6}] {c.get('type')}")
        L.append(f"    \"{(c.get('quote') or '')[:150]}\"")
        L.append(f"    problem: {c.get('problem')}")
        L.append(f"    fix:     {c.get('fix')}")
    if r.get('strongest_omission'):
        L.append(f"  STRONGEST OMISSION: {r['strongest_omission']}")
    L.append(f"  cost ${r['usage']['cost_usd']:.4f}")
    return '\n'.join(L)


if __name__ == '__main__':
    for t in (sys.argv[1:] or ['SNDK']):
        print('\n' + render(review(t)))
