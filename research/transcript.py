"""Stage 3b — the call analyst.

The press release is what a company chose to write down. The call is where it
has to answer questions, and the gap between the two is often the finding.
Alphabet's Q3-26 release contained no guidance at all; the CFO gave 2026 capex
of $195-205bn on the call that evening. A pipeline reading only the release
would have missed the most consequential number of the quarter.

What this stage is actually for
-------------------------------
Not quotes. Quotes are easy and the press release already has the good ones.
The thing only a transcript can tell you is **which questions got a number and
which got a deflection**. The sharpest observation in the HFCL/STLTECH note is
exactly that: every one of ten analyst questions on HFCL's call was answered
with a figure, while STLTECH declined to disclose utilisation and realisation
— and the reader is being asked to pay twice the multiple for the company
disclosing less.

Alpha Vantage tags every segment with a speaker `title`, so management and
analysts separate deterministically. That makes question-and-answer pairing
free, and leaves the model only the judgment: was this answer responsive?

Tiering, as everywhere here:
  fetch + pair speakers      deterministic, cached, free after first call
  classify responsiveness    cheap model
  verify quotes + numbers    deterministic
"""
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pressrelease as PR          # reuse the shingle quote verifier

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except ImportError:
    pass

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')

# Deliberately a tier above the press-release extractor, and this is the
# clearest tier boundary found so far.
#
# Pulling a quote out of a document is extraction: Haiku does it faithfully
# and a substring check proves it. Deciding whether an answer was responsive
# is judgment, and Haiku is not stable at it — asked to assess Alphabet's
# seven exchanges it returned each one twice with DIFFERENT verdicts, giving
# 8 answered / 3 partial on one pass and 6 / 0 on the other. Either number
# could have gone into a note.
#
# The volume is tiny — one call per earnings call, maybe 80 a year — so the
# cost of moving up a tier is a few dollars annually against a judgment that
# is otherwise unusable.
MODEL = 'claude-sonnet-5'
RATE_IN, RATE_OUT = 3.00, 15.00
MAX_PAIRS = 14                    # calls rarely run longer; caps cost


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def quarter_for(period_end):
    """Alpha Vantage labels transcripts by calendar quarter of period end."""
    y, m, _ = (int(x) for x in period_end.split('-'))
    return f'{y}Q{(m - 1) // 3 + 1}'


def fetch(ticker, quarter, refresh=False):
    """Transcript segments, cached on disk.

    The free tier allows 25 calls a day and we need about 80 a year, so the
    limit is irrelevant — but caching still matters, because re-analysing a
    call during development should never cost a request.
    """
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f'{ticker.upper()}_{quarter}_transcript.json')
    if os.path.exists(path) and not refresh:
        return json.load(io.open(path, encoding='utf-8')), path, True

    key = os.environ.get('ALPHAVANTAGE_API_KEY')
    if not key:
        raise RuntimeError('ALPHAVANTAGE_API_KEY not set')
    url = ('https://www.alphavantage.co/query?' + urllib.parse.urlencode({
        'function': 'EARNINGS_CALL_TRANSCRIPT', 'symbol': ticker.upper(),
        'quarter': quarter, 'apikey': key}))
    req = urllib.request.Request(url, headers={'User-Agent': 'research/1.0'})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    if 'transcript' not in d:
        # Rate limits and empty coverage both arrive as a prose field, not an
        # error status. Surface the message rather than treating it as "no call".
        msg = d.get('Information') or d.get('Note') or d.get('Error Message')
        raise RuntimeError(f'no transcript for {ticker} {quarter}: '
                           f'{str(msg)[:200] if msg else list(d)}')
    json.dump(d, io.open(path, 'w', encoding='utf-8'), indent=1)
    return d, path, False


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------

def is_analyst(seg):
    return 'analyst' in (seg.get('title') or '').lower()


def is_operator(seg):
    return (seg.get('speaker') or '').strip().lower() == 'operator'


def qa_pairs(segs, limit=MAX_PAIRS):
    """Pair each analyst question with the management answers that follow.

    Deterministic: the speaker titles carry it. No model needed to know who
    asked and who replied, which is the expensive way to do this.
    """
    pairs, i = [], 0
    while i < len(segs) and len(pairs) < limit:
        s = segs[i]
        if not is_analyst(s):
            i += 1
            continue
        answers, j = [], i + 1
        while j < len(segs) and not is_analyst(segs[j]):
            if not is_operator(segs[j]):
                answers.append(segs[j])
            j += 1
        if answers:
            pairs.append({
                'analyst': s.get('speaker'), 'firm': s.get('title'),
                'question': (s.get('content') or '').strip(),
                'answered_by': [a.get('speaker') for a in answers],
                'answer': '\n'.join((a.get('content') or '') for a in answers).strip(),
            })
        i = j
    return pairs


NUMERIC = re.compile(
    r'(\$\s?\d|\d+(?:\.\d+)?\s?(?:%|percent|basis points|bps|billion|million|'
    r'bn|trillion)|\b\d{2,}\b)', re.I)


def has_number(text):
    """Did the answer contain a figure at all?

    Deterministic companion to the model's judgment. A reply with no digits
    anywhere cannot have answered a quantitative question, whatever it sounded
    like.
    """
    return bool(NUMERIC.search(text or ''))


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

PROMPT = """You are assessing an earnings-call exchange. For each pair, judge \
ONLY whether management's answer was responsive to what was actually asked.

Return JSON:

{"assessments": [
  {"i": 0,
   "asked_for_a_number": true,
   "gave_a_number": true,
   "responsiveness": "answered" | "partial" | "deflected" | "declined",
   "declined_phrase": "verbatim phrase where they declined, or empty",
   "subject": "three or four words"}
]}

Definitions, applied strictly:
- "asked_for_a_number": the analyst requested a quantity, rate, share, \
margin, timing or other figure.
- "gave_a_number": management supplied a figure responsive to THAT question. \
A number about something else does not count.
- "declined": management explicitly refused — "we don't disclose that", \
"not something we break out", "can't comment".
- "deflected": no refusal, but the question was not engaged — restating \
strategy, answering a different question.
- "partial": some of what was asked, not all.

Consistency rule you must not break: if responsiveness is "declined", \
"deflected" or "partial", then something specific was asked and withheld, so \
"asked_for_a_number" is true whenever the thing withheld was a quantity. A \
question about margins, growth rates, utilisation, mix or timing is asking \
for a number even when phrased conversationally.

Judge the exchange as it is. Do not be generous because the answer sounded \
confident, and do not be harsh because it was brief. Return JSON only.

EXCHANGES:
"""


PASSES = 3          # odd, so a majority always exists
AGREE_AT = 2        # a verdict needs this many of PASSES to be reported


def _one_pass(pairs, model):
    import anthropic
    client = anthropic.Anthropic()
    blob = []
    for i, p in enumerate(pairs):
        blob.append(f"--- {i} ---\nANALYST ({p['firm']}): {p['question'][:1200]}\n"
                    f"MANAGEMENT: {p['answer'][:2500]}")
    resp = client.messages.create(
        model=model, max_tokens=3000,
        messages=[{'role': 'user', 'content': PROMPT + '\n\n'.join(blob)}])
    raw = ''.join(b.text for b in resp.content if b.type == 'text').strip()
    raw = re.sub(r'^```(?:json)?|```$', '', raw, flags=re.M).strip()
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        out = {'assessments': []}
    cost = (resp.usage.input_tokens / 1e6 * RATE_IN
            + resp.usage.output_tokens / 1e6 * RATE_OUT)
    return out, resp.usage.input_tokens, resp.usage.output_tokens, cost


def classify(pairs, model=MODEL, passes=PASSES):
    """Judge each exchange several times and report how often it agreed.

    Responsiveness is the one thing in this pipeline that cannot be checked
    against a source. A quote either appears in the document or it does not;
    whether an answer was "partial" or "deflected" has no ground truth to
    compare against, and the model does not agree with itself about it —
    Sonnet moved the headline from 20% to 0% on two passes over identical
    input, and Haiku contradicted itself within a single response.

    Dropping the judgment would lose the most valuable thing a transcript
    offers. Reporting one pass as fact would be false precision. So it is
    measured instead: run it an odd number of times, report the verdict only
    where a majority agrees, and mark the rest UNSTABLE rather than guessing.

    Three passes cost about 15 cents per call, against maybe 80 calls a year.
    """
    from collections import Counter
    votes, tin, tout, cost = {}, 0, 0, 0.0
    for _ in range(passes):
        out, i_tok, o_tok, c = _one_pass(pairs, model)
        tin, tout, cost = tin + i_tok, tout + o_tok, cost + c
        seen = set()
        for a in (out.get('assessments') or []):
            i = a.get('i')
            if not isinstance(i, int) or i >= len(pairs) or i in seen:
                continue          # one judgment per exchange per pass
            seen.add(i)
            votes.setdefault(i, []).append(a)

    merged = []
    for i, vs in sorted(votes.items()):
        resp = Counter(v.get('responsiveness') for v in vs)
        top, n = resp.most_common(1)[0]
        asked = sum(1 for v in vs if v.get('asked_for_a_number'))
        # Prefer a declined_phrase from a pass that actually found one; they
        # are verifiable, so a single sighting is worth keeping and checking.
        phrase = next((v.get('declined_phrase') for v in vs
                       if v.get('declined_phrase')), '')
        merged.append({
            'i': i,
            'responsiveness': top if n >= AGREE_AT else 'UNSTABLE',
            'agreement': f'{n}/{len(vs)}',
            'votes': dict(resp),
            'asked_for_a_number': asked * 2 >= len(vs),
            'asked_agreement': f'{max(asked, len(vs)-asked)}/{len(vs)}',
            'declined_phrase': phrase,
            'subject': Counter(v.get('subject') for v in vs).most_common(1)[0][0],
        })
    usage = {'model': model, 'passes': passes, 'input_tokens': tin,
             'output_tokens': tout, 'cost_usd': round(cost, 5)}
    return {'assessments': merged}, usage


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def analyse(ticker, period_end, out_dir=None, refresh=False):
    q = quarter_for(period_end)
    d, path, cached = fetch(ticker, q, refresh)
    segs = d['transcript']
    full = '\n'.join(s.get('content') or '' for s in segs)
    pairs = qa_pairs(segs)
    judged, usage = classify(pairs) if pairs else ({'assessments': []},
                                                   {'cost_usd': 0})

    # One assessment per exchange, whatever the model returns. On Alphabet's
    # call it emitted every index twice, which inflated the tallies to 14
    # judgments over 7 questions and would have put a wrong ratio in a note.
    # An index appearing twice is a shape error, not new information.
    rows, seen, dup = [], set(), 0
    for a in (judged.get('assessments') or []):
        i = a.get('i')
        if not isinstance(i, int) or i >= len(pairs):
            continue
        if i in seen:
            dup += 1
            continue
        seen.add(i)
        p = pairs[i]
        # The model's call, cross-checked against whether digits exist at all.
        contains = has_number(p['answer'])
        resp = a.get('responsiveness')
        # Internal contradiction: management cannot decline, deflect or
        # partially answer a question that asked for nothing. The model
        # returned exactly this on Alphabet's call — "TPU system business
        # margins", marked declined, with asked_for_a_number false — which
        # would have undercounted the headline. Flag it rather than trust it.
        contradiction = (resp in ('declined', 'deflected', 'partial')
                         and not a.get('asked_for_a_number'))
        rows.append({**a, 'analyst': p['analyst'], 'firm': p['firm'],
                     'answered_by': p['answered_by'],
                     'answer_has_digits': contains,
                     'model_claim_conflicts': bool(a.get('gave_a_number')) and not contains,
                     'asked_flag_contradiction': contradiction,
                     'declined_verified': (
                         PR._norm(a.get('declined_phrase') or '') in PR._norm(full)
                         if a.get('declined_phrase') else None)})

    asked = [r for r in rows if r.get('asked_for_a_number')
             or r.get('asked_flag_contradiction')]
    gave = [r for r in asked if r.get('responsiveness') == 'answered']
    unstable = [r for r in rows if r.get('responsiveness') == 'UNSTABLE']
    out = {
        'ticker': ticker.upper(), 'quarter': q, 'period_end': period_end,
        'from_cache': cached, 'cache_path': path,
        'segments': len(segs), 'chars': len(full),
        'management_speakers': sorted({s.get('speaker') for s in segs
                                       if not is_analyst(s) and not is_operator(s)}),
        'questions': len(pairs), 'exchanges': rows,
        'score': {
            'quantitative_questions': len(asked),
            'answered_with_a_number': len(gave),
            'unstable': len(unstable),
            # No answer_rate. A ratio built on a judgment that moved from 20%
            # to 0% across two identical runs is false precision, and it would
            # read in a note as though it were measured.
            'declined': sum(1 for r in rows if r.get('responsiveness') == 'declined'),
            'deflected': sum(1 for r in rows if r.get('responsiveness') == 'deflected'),
            'partial': sum(1 for r in rows if r.get('responsiveness') == 'partial'),
            'answered': sum(1 for r in rows if r.get('responsiveness') == 'answered'),
            'model_contradictions': sum(1 for r in rows
                                        if r.get('asked_flag_contradiction')),
            'duplicate_assessments_dropped': dup,
        },
        'usage': usage,
    }
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with io.open(os.path.join(out_dir,
                     f'{ticker.upper()}_{period_end}_call.json'),
                     'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=1, ensure_ascii=False)
    return out


def render(o):
    s = o['score']
    L = [f"{o['ticker']} {o['quarter']} call — {o['segments']} segments, "
         f"{o['chars']:,} chars" + ('  (cached)' if o['from_cache'] else ''),
         f"  management: {', '.join(o['management_speakers'])}",
         f"  {o['questions']} analyst questions"]

    # What survives verification comes first, because it is the part a note
    # can actually stand on.
    declines = [r for r in o['exchanges']
                if r.get('declined_phrase') and r.get('declined_verified')]
    if declines:
        L.append(f"  VERIFIED REFUSALS ({len(declines)}):")
        for r in declines:
            L.append(f'    "{r["declined_phrase"][:120]}"')
            L.append(f"        on {r.get('subject')} — asked by "
                     f"{r.get('analyst')} ({r.get('firm')})")
    unver = [r for r in o['exchanges']
             if r.get('declined_phrase') and r.get('declined_verified') is False]
    for r in unver:
        L.append(f'    [NOT IN TRANSCRIPT] "{r["declined_phrase"][:90]}" — discard')

    L.append(f"  subjects management was pushed on: "
             + ', '.join(r.get('subject') for r in o['exchanges']
                         if r.get('responsiveness') in
                         ('declined', 'deflected', 'partial', 'UNSTABLE')))

    L.append(f"  responsiveness ({o['usage'].get('passes', 1)} passes, "
             f"majority required): answered {s['answered']}, partial "
             f"{s['partial']}, deflected {s['deflected']}, declined "
             f"{s['declined']}, unstable {s['unstable']}")
    for r in o['exchanges']:
        if r.get('responsiveness') == 'UNSTABLE':
            L.append(f"    [unstable {r.get('agreement')}] {r.get('subject')} "
                     f"— votes {r.get('votes')}")
    L.append(f"  cost ${o['usage'].get('cost_usd', 0):.5f}  "
             f"({o['usage'].get('passes')} passes, {o['usage']['model']})")
    return '\n'.join(L)


if __name__ == '__main__':
    OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
    args = sys.argv[1:] or ['GOOGL', '2026-09-30']
    for i in range(0, len(args), 2):
        print('\n' + render(analyse(args[i], args[i + 1], OUT)))
