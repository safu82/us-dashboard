"""Stage 8 — the writer.

The writer never reads a filing. It sees only the packs the earlier stages
produced, which is what makes a note auditable: every figure in it exists as
a field somewhere, with an accession number behind it.

Arithmetic happens here, in code, before the model is called. Asking a model
to multiply a share count by a price and divide by earnings invites a wrong
number that reads perfectly; computing it first and handing over the result
removes the opportunity. What is left for the model is the only thing it is
actually better at than code: deciding what the evidence means.

The hard rule in the prompt is that a figure the packs do not contain cannot
appear in the note. "We could not verify this" is always an acceptable
sentence, and several of the checks exist precisely to produce it.
"""
import glob
import io
import json
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import historian as H

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except ImportError:
    pass

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
MODEL = 'claude-opus-5'
RATE_IN, RATE_OUT = 15.00, 75.00


# ---------------------------------------------------------------------------
# gather
# ---------------------------------------------------------------------------

def _load(path):
    if not path or not os.path.exists(path):
        return None
    with io.open(path, encoding='utf-8-sig') as fh:
        return json.load(fh)


def _newest(pattern):
    hits = sorted(glob.glob(os.path.join(OUT, pattern)))
    return hits[-1] if hits else None


def gather(ticker):
    """Every pack we hold for a ticker, newest of each kind."""
    t = ticker.upper()
    packs = {
        'facts': _load(_newest(f'{t}_*_10Q_facts.json'))
                 or _load(_newest(f'{t}_*_10K_facts.json')),
        'checks': _load(_newest(f'{t}_*_10Q_checks.json'))
                  or _load(_newest(f'{t}_*_10K_checks.json')),
        'cross': _load(_newest(f'{t}_*_10Q_cross.json')),
        'release': _load(_newest(f'{t}_*_8K_release.json')),
        'call': _load(_newest(f'{t}_*_call.json')),
    }
    packs['history'] = H.review(t)

    # One price for everything priced.
    #
    # The cross-section pack deliberately carries the close on the FILING
    # date, because its job is to say where the name stood when the results
    # landed. Valuing the company today against that price put two closes in
    # one note — $1,150.89 in the multiples and $1,075.08 in the levels
    # table, seven per cent apart, both presented as current.
    #
    # So levels are fetched first and their close, which is the latest
    # session, is the only price any multiple is built on.
    try:
        import crosssection as X
        packs['levels'] = X.levels(t)
    except Exception as e:                                    # noqa: BLE001
        packs['levels'] = {'error': str(e)}

    lv = packs.get('levels') or {}
    close = lv.get('close')
    if close is None and packs.get('cross', {}).get('covered'):
        close = packs['cross']['standing'].get('close')
    try:
        import estimates as E
        packs['estimates'] = E.pack(t, close)
    except Exception as e:                                    # noqa: BLE001
        packs['estimates'] = {'error': str(e)}

    # The chain this name sits in. Without it a note cannot tell a
    # name-specific de-rate from a sector rotation, and will guess. The
    # SanDisk note concluded "a name-specific derate does" hold while every
    # one of the fifteen stages in its chain had fallen, memory by less than
    # most of them. It had no way to know.
    chain_name = None
    for ch in ((packs.get('cross') or {}).get('chain') or []):
        chain_name = ch.get('chain')
        break
    if chain_name:
        try:
            import chain as C
            packs['chain_profile'] = C.movement(chain_name)
            packs['chain_board'] = C.board()
            packs['chain_demand'] = C.demand_board(chain_name, max_new=0)
        except Exception as e:                                # noqa: BLE001
            packs['chain_error'] = str(e)
    return packs


# ---------------------------------------------------------------------------
# arithmetic — in code, never in the model
# ---------------------------------------------------------------------------

def derive(packs):
    """Everything computable, computed once, with its basis stated.

    Each entry carries how it was reached so the note can say "annualised
    from one quarter" rather than implying a trailing twelve months we never
    measured.
    """
    f, x = packs.get('facts'), packs.get('cross')
    d, notes = {}, []
    if not f:
        return d, ['No facts pack: nothing can be computed.']

    def q(k):
        v = (f.get('quarter') or {}).get(k)
        return v['value'] if v else None

    def b(k):
        v = (f.get('balance') or {}).get(k)
        return v['value'] if v else None

    eps_d, eps_b = q('eps_diluted'), q('eps_basic')
    rev, op, ni = q('revenue'), q('operating_income'), q('net_income_attributable')
    shares_cover = b('shares_outstanding_cover')
    shares_dil = q('shares_diluted_wavg')
    close = (x or {}).get('standing', {}).get('close') if x and x.get('covered') else None

    d['period_end'] = f.get('period_end')
    d['accession'] = f.get('accession')
    if eps_d is not None:
        d['eps_diluted_quarter'] = eps_d
        d['eps_annualised'] = round(eps_d * 4, 2)
        notes.append('EPS annualised is the latest quarter x4, not a trailing '
                     'twelve months. For a cyclical business that is a strong '
                     'assumption and should be stated as one.')
    if close and shares_cover:
        d['market_cap'] = round(close * shares_cover)
        d['share_count_basis'] = 'cover page'
    elif close and shares_dil:
        d['market_cap'] = round(close * shares_dil)
        d['share_count_basis'] = 'weighted-average diluted (no cover count)'
        notes.append('Market cap uses a weighted-average share count because '
                     'the cover-page count is not in the XBRL facts. Treat it '
                     'as approximate.')
    # Two P/Es, because they are two different questions and here they differ
    # by 8%. Per-share divides today's price by what a share earned, using the
    # diluted count inside EPS. Whole-company divides market cap, built on the
    # cover-page count, by total profit. The gap between them is the share
    # count moving — SanDisk bought back $5bn in the quarter, so the cover
    # count (142.3m) is below the quarter's diluted average (154m).
    #
    # Reporting only one would hide that. Reporting a single blended figure
    # would be wrong in both directions.
    if close and eps_d:
        d['pe_price_over_annualised_eps'] = round(close / (eps_d * 4), 2)
        d['pe_basis_per_share'] = ('close / (diluted quarterly EPS x4); '
                                   'dilution is inside EPS')
    if d.get('market_cap') and ni:
        d['pe_mcap_over_annualised_net_income'] = round(
            d['market_cap'] / (ni * 4), 2)
        d['pe_basis_whole_company'] = ('market cap on the cover-page share '
                                       'count / (quarterly net income x4)')
    if (d.get('pe_price_over_annualised_eps')
            and d.get('pe_mcap_over_annualised_net_income')):
        notes.append(
            'The two P/E figures differ because the share count fell between '
            'the quarter and the cover date. Cite one and say which; do not '
            'average them.')
    if rev:
        if op is not None:
            d['operating_margin_pct'] = round(op / rev * 100, 1)
        if ni is not None:
            d['net_margin_pct'] = round(ni / rev * 100, 1)
    ocf, capex = q('operating_cash_flow'), q('capex')
    if ocf is not None and capex is not None:
        d['free_cash_flow'] = ocf - capex
    cash, debt = b('cash'), b('long_term_debt')
    if cash is not None and debt is not None:
        d['net_cash'] = cash - debt
    # A buyback means nothing at its absolute size; it means something as a
    # share of what the company is worth and of what it earned in cash. The
    # writer will reach for both ratios whether or not they are supplied, and
    # arithmetic done in prose is arithmetic nobody checked.
    buy = q('buybacks')
    if buy:
        d['buybacks_quarter'] = buy
        if d.get('market_cap'):
            d['buybacks_pct_of_market_cap'] = round(buy / d['market_cap'] * 100, 2)
        if d.get('free_cash_flow'):
            d['buybacks_pct_of_free_cash_flow'] = round(
                buy / d['free_cash_flow'] * 100, 1)
    return d, notes


# ---------------------------------------------------------------------------
# the note
# ---------------------------------------------------------------------------

PROMPT = """You are an analyst writing an internal note from a prepared
evidence pack. Write the note a portfolio manager would act on.

WHAT A NOTE IS

A note argues. It takes the evidence and reaches a view, states what would
break that view, and says what to do. A recital of the quarter's figures is
not a note — the reader can open the filing. Your value is entirely in what
you make of the numbers, not in repeating them.

Before writing, find the one thing in this evidence that is not obvious from
reading the press release. It is usually a disagreement: between what the
company earned and what the market is paying, between rising estimates and
falling relative strength, between a headline and the line beneath it, or
between this quarter's trend and last quarter's. Lead with that. Everything
else in the note exists to support or qualify it.

NEVER DO THESE

- Never mention verification, checks, packs, schemas, tags, coverage scores
  or data provenance in the prose. "Checked and passed", "the pack flags",
  "verification returns SPLICED", "seven schema fields are not tagged" — all
  forbidden. That machinery decides what you are ALLOWED to say; it is not
  something the reader sees. If a figure failed verification, simply do not
  use it.
- Never pad a sentence with an instruction to yourself ("Use diluted").
- Never list what you could not verify as a section. If something material is
  genuinely unknowable, work it into the argument in one clause — "the
  balance sheet does not disclose X, so the Y case rests on Z" — and only
  where it changes the conclusion.
- Never invent a figure. Everything numeric must exist in the evidence.
- Never state a price target you have not built from consensus EPS in the
  evidence, and always say which EPS and which multiple produced it.
- Never characterise the peer group by naming selected members. Each chain
  stage carries `spread_including_subject` — weakest, strongest, median and
  every member's momentum. Use it. If you argue a move is group-wide or
  name-specific, that claim must be consistent with the spread, including
  the members that cut against it.

PLACE THE MOVE BEFORE EXPLAINING IT

CHAIN_CONTEXT holds every stage of the chain this company sits in, how far
each moved, and where the chain ranks among all eighteen. Read it before
attributing anything to the company.

A de-rate is only name-specific if the chain did not de-rate with it. If
every stage in the chain fell, the company's own margins, guidance and
capital allocation did not cause its fall, and a note explaining the fall
through them is explaining the wrong thing. Say how much is chain, how much
is stage, and how much is left over for the name — in that order.

DEMAND_FROM_BUYERS holds what the companies at the downstream end of the
chain have committed to in writing. If this name is a supplier, that is the
demand side of its own argument. A mean-reversion or peak-cycle case that
never engages with what the buyers have committed is one-sided and will be
treated as such.

HANDLING THE NUMBERS

- Arithmetic is already done. Use the DERIVED and FORWARD_MULTIPLES blocks;
  do not recompute, do not produce a ratio that is not supplied.
- Say which basis a multiple is on — consensus FY, or a quarter annualised.
  Annualising one quarter of a cyclical business is a strong assumption and
  should be named as one, once, not repeatedly.
- Cross-sectional standing is end-of-day on a stated date. Name it once.
- Where consensus dispersion is wide, the forward multiple is a range, not a
  number. Say so.

STRUCTURE

## <TICKER> — <a sentence stating the finding, not describing the company>

**Verdict:** BUY / ACCUMULATE / HOLD / AVOID, then two or three sentences on
why, and the level or event that would change it. Be decisive. If the
evidence genuinely does not support a view, say what is missing that would.

### The argument
Three or four paragraphs. This is the note. Use the quarter, the estimates,
the positioning and management's words as material for a case, not as
sections to fill.

### Against this
The strongest case for the other side, honestly put. One paragraph.

### Levels and valuation
A compact table of the price map and forward multiples supplied in the
evidence.

### What would change the view
Four or five specific, checkable triggers. Each must name a figure or a
level, not a sentiment.

Under 800 words. Plain declarative sentences. No hedging stacks, no "it is
worth noting", no summary paragraph at the end. Markdown.

EVIDENCE
"""


def build_context(packs, derived, derive_notes):
    """Trim the packs to what a writer needs, keeping provenance."""
    f, c, x = packs.get('facts'), packs.get('checks'), packs.get('cross')
    ctx = {'derived': derived, 'derivation_caveats': derive_notes}
    if f:
        ctx['filing'] = {'form': f.get('form'), 'period_end': f.get('period_end'),
                         'filed': f.get('filed'), 'accession': f.get('accession'),
                         'source_url': f.get('source_url'),
                         'fields_not_tagged': f.get('missing')}
        ctx['quarter_figures'] = {
            k: v['value'] for k, v in (f.get('quarter') or {}).items() if v}
        ctx['balance_figures'] = {
            k: v['value'] for k, v in (f.get('balance') or {}).items() if v}
        ctx['reporting_spans_days'] = {
            k: v.get('span_days') for k, v in (f.get('quarter') or {}).items() if v}
    if c:
        # Verification governs what may be said; it is not content. Only
        # failures reach the writer, and only so it avoids leaning on a
        # figure that did not stand up — never so it can narrate the check.
        bad = [ck for ck in c.get('checks', [])
               if ck['verdict'] in ('FAIL', 'UNKNOWN')]
        if bad:
            ctx['do_not_rely_on'] = [
                {'issue': ck['question'], 'why': ck['detail']} for ck in bad]
    est = packs.get('estimates') or {}
    if est.get('surprises'):
        ctx['surprise_history'] = est['surprises']
    if est.get('consensus'):
        ctx['forward_consensus'] = [c_ for c_ in est['consensus']
                                    if c_.get('horizon') == 'fiscal year']
    if est.get('forward_multiples'):
        ctx['forward_multiples'] = est['forward_multiples']
    cp = packs.get('chain_profile') or {}
    if cp.get('stages'):
        brd = (packs.get('chain_board') or {}).get('chains') or []
        me = next((r for r in brd
                   if r['chain'].lower() == cp['chain'].lower()), None)
        ctx['chain_context'] = {
            'chain': cp['chain'],
            'compared_with': cp.get('compared_with'),
            'chain_mean_momentum': cp.get('chain_mean'),
            'every_stage_in_chain_fell': not cp.get('any_stage_rose', True),
            'stages_down': cp.get('stages_down'),
            'stages_moved': cp.get('stages_moved'),
            'this_chain_on_the_board': me,
            'chains_ranked': (packs.get('chain_board') or {}).get('chains_ranked'),
            'chains_down': (packs.get('chain_board') or {}).get('chains_down'),
            'stages': [{k: s.get(k) for k in
                        ('position', 'stage', 'momentum_mean_today',
                         'momentum_change', 'rank_weakest_first',
                         'of_stages_ranked', 'too_thin_to_cite', 'spread')}
                       for s in cp['stages']],
        }
    dem = (packs.get('chain_demand') or {}).get('buyers') or []
    buyers = [b for b in dem if b.get('demand_guidance')]
    if buyers:
        ctx['demand_from_buyers'] = {
            'buyer_stages': (packs.get('chain_demand') or {}).get('buyer_stages'),
            'commitments': buyers,
            'caveat': ('These are the buyers at the downstream end of this '
                       'name\'s chain. Filing dates differ and some are weeks '
                       'older than the price move; check filed before setting '
                       'a commitment against a de-rating. A buyer with no '
                       'guidance listed filed none - it does not mean none '
                       'was given, as several guide only on the call.'),
        }

    lv = packs.get('levels') or {}
    if lv.get('levels'):
        ctx['price_levels'] = lv
        ctx['valuation_price'] = {
            'close': lv.get('close'), 'as_of': lv.get('asof'),
            'note': 'Every multiple in this pack is built on this price. '
                    'It is the latest session, which is later than the '
                    'filing date. Quote this close and this date.'}
    if x and x.get('covered'):
        st = dict(x['standing'])
        # Rename so it cannot be mistaken for today's price.
        st['close_on_filing_date'] = st.pop('close', None)
        ctx['cross_section'] = {
            'as_of_FILING_DATE': x['asof'], 'universe': x['universe_scored'],
            **st, 'cohort': x['cohort'],
            'movement': x.get('movement'), 'chain': x.get('chain'),
            'staleness': x.get('chain_staleness'),
            'note': ('This is where the name stood when the results landed. '
                     'Do not price anything off close_on_filing_date — use '
                     'valuation_price.')}
    elif x:
        ctx['cross_section'] = {'covered': False, 'note': x.get('note')}
    r = packs.get('release')
    if r and not (r.get('extraction') or {}).get('error'):
        e = r['extraction']
        ctx['press_release'] = {
            'source': r.get('source_url'), 'filed': r.get('filed'),
            'quotes': e.get('quotes'), 'guidance': e.get('guidance'),
            'absent_from_this_document': e.get('absent_from_this_document')
                                         or e.get('notable_absences'),
            'quote_verification': r.get('quote_checks')}
    cl = packs.get('call')
    if cl:
        ctx['earnings_call'] = {
            'quarter': cl.get('quarter'),
            'questions': cl.get('questions'),
            'verified_refusals': [
                {'phrase': r_['declined_phrase'], 'subject': r_.get('subject'),
                 'analyst': r_.get('analyst'), 'firm': r_.get('firm')}
                for r_ in cl.get('exchanges', [])
                if r_.get('declined_phrase') and r_.get('declined_verified')],
            'responsiveness': cl.get('score'),
            'caveat': ('Responsiveness is a model judgment voted over several '
                       'passes. Counts are indicative; anything marked '
                       'UNSTABLE had no majority. Do not quote a ratio.')}
    h = packs.get('history') or {}
    if h.get('diff'):
        ctx['vs_prior_filing'] = {
            'prior_period': h.get('prior_period'),
            'changes': [d for d in h['diff'] if d['material']]}
    if h.get('claims'):
        ctx['prior_claims'] = h['claims']
    return ctx


def write(ticker, model=MODEL):
    import anthropic
    packs = gather(ticker)
    if not packs.get('facts'):
        return {'ticker': ticker, 'error': 'no facts pack; run research/run.py'}
    derived, notes = derive(packs)
    ctx = build_context(packs, derived, notes)

    client = anthropic.Anthropic()
    # max_tokens covers reasoning as well as the note. At 4,000 the model
    # spent nearly all of it thinking about the chain evidence and produced
    # 1,694 characters before the cap cut it mid-word, which looks like a
    # short note rather than a truncated one.
    resp = client.messages.create(
        model=model, max_tokens=16000,
        messages=[{'role': 'user',
                   'content': PROMPT + json.dumps(ctx, indent=1, default=str)}])
    note = ''.join(b.text for b in resp.content if b.type == 'text').strip()
    usage = {'model': model, 'input_tokens': resp.usage.input_tokens,
             'output_tokens': resp.usage.output_tokens,
             'cost_usd': round(resp.usage.input_tokens / 1e6 * RATE_IN
                               + resp.usage.output_tokens / 1e6 * RATE_OUT, 4)}
    stem = f"{ticker.upper()}_{derived.get('period_end')}"
    body = (note + f"\n\n---\nGenerated {date.today().isoformat()} from "
                   f"evidence packs. {usage['model']}, "
                   f"${usage['cost_usd']:.4f}.\n")

    def save(path, text, as_json=False):
        """Never lose a paid-for note to a file lock.

        Word holds an exclusive handle on anything open in it, and these
        notes are written to be read in Word. Falling back to a sibling name
        costs the reader one rename; failing here costs a regeneration.
        """
        try:
            with io.open(path, 'w', encoding='utf-8') as fh:
                json.dump(text, fh, indent=1, default=str) if as_json \
                    else fh.write(text)
            return path, None
        except PermissionError:
            alt = path.replace('.', '.NEW.', 1)
            with io.open(alt, 'w', encoding='utf-8') as fh:
                json.dump(text, fh, indent=1, default=str) if as_json \
                    else fh.write(text)
            return alt, f'{os.path.basename(path)} was locked (open in Word?)'

    note_path, warn = save(os.path.join(OUT, stem + '_note.md'), body)
    ctx_path, _ = save(os.path.join(OUT, stem + '_context.json'), ctx, True)
    return {'ticker': ticker.upper(), 'note': note, 'usage': usage,
            'note_path': note_path, 'warning': warn,
            'context_fields': sorted(ctx)}


REVISE_PROMPT = """You wrote the note below. A critic has audited it against
the same evidence and raised the challenges listed. Rewrite the note.

How to treat each challenge:

- A factual or basis error — a figure on the wrong basis, a direction
  reversed, a comparison between incompatible measures — must be corrected.
  No exceptions and no defending it.
- A one-sided argument must be answered by engaging the other side from the
  evidence, not by adding a disclaimer.
- An omission the critic calls strongest must appear in the note.
- A challenge you believe is wrong may stand, but then the note must say in
  one clause why the reading it uses is the right one.

Do not simply append caveats. Rewrite the argument so the corrections are
part of it. If the corrections change the conclusion, change the conclusion —
a verdict that survives only because you ignored a correction is worthless.

Keep the structure, the length limit and every rule from the original brief:
no mention of verification, packs, coverage scores or provenance; every
figure from the evidence; one price for all multiples.

Return the revised note as markdown and nothing else.

ORIGINAL NOTE
"""


def revise(ticker, model=MODEL):
    """Rewrite a note against the critic's challenges."""
    import anthropic
    import glob as _g
    t = ticker.upper()
    notes = _g.glob(os.path.join(OUT, f'{t}_*_note*.md'))
    if not notes:
        return {'error': f'no note for {t}'}
    note_path = max(notes, key=os.path.getmtime)
    crit_path = os.path.join(OUT, f'{t}_critique.json')
    if not os.path.exists(crit_path):
        return {'error': f'no critique for {t}; run research/critic.py first'}
    note = io.open(note_path, encoding='utf-8').read()
    crit = json.load(io.open(crit_path, encoding='utf-8'))
    ctx_path = re.sub(r'_note(?:\.NEW)?\.md$', '_context.json', note_path)
    ctx = json.load(io.open(ctx_path, encoding='utf-8'))

    payload = (REVISE_PROMPT + note
               + '\n\nCHALLENGES\n'
               + json.dumps({'challenges': crit.get('challenges'),
                             'verdict_assessment': crit.get('verdict_assessment'),
                             'strongest_omission': crit.get('strongest_omission'),
                             'unmatched_figures': crit.get('number_audit', {})
                             .get('unmatched')}, indent=1, default=str)
               + '\n\nEVIDENCE\n' + json.dumps(ctx, indent=1, default=str))

    client = anthropic.Anthropic()
    resp = client.messages.create(model=model, max_tokens=16000,
                                  messages=[{'role': 'user', 'content': payload}])
    revised = ''.join(b.text for b in resp.content if b.type == 'text').strip()
    usage = {'model': model, 'input_tokens': resp.usage.input_tokens,
             'output_tokens': resp.usage.output_tokens,
             'cost_usd': round(resp.usage.input_tokens / 1e6 * RATE_IN
                               + resp.usage.output_tokens / 1e6 * RATE_OUT, 4)}
    stem = os.path.basename(note_path).replace('_note', '_note.v2') \
                                      .replace('.NEW', '')
    path = os.path.join(OUT, stem)
    with io.open(path, 'w', encoding='utf-8') as fh:
        fh.write(revised + f"\n\n---\nRevised against {len(crit.get('challenges') or [])}"
                           f" critic challenges. {usage['model']}, "
                           f"${usage['cost_usd']:.4f}.\n")
    return {'ticker': t, 'note': revised, 'path': path, 'usage': usage,
            'challenges_addressed': len(crit.get('challenges') or [])}


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if '--revise' in sys.argv:
        for t in (args or ['SNDK']):
            r = revise(t)
            if r.get('error'):
                print(f"{t}: {r['error']}")
                continue
            print(r['note'])
            print(f"\n[addressed {r['challenges_addressed']} challenges, "
                  f"${r['usage']['cost_usd']:.4f}] -> "
                  f"{os.path.basename(r['path'])}")
        sys.exit(0)
    for t in (args or ['SNDK']):
        r = write(t)
        if r.get('error'):
            print(f"{t}: {r['error']}")
            continue
        print(r['note'])
        u = r['usage']
        if r.get('warning'):
            print(f"\n! {r['warning']} — written to "
                  f"{os.path.basename(r['note_path'])} instead")
        print(f"\n[{u['input_tokens']:,} in / {u['output_tokens']:,} out, "
              f"${u['cost_usd']:.4f}]  -> {os.path.basename(r['note_path'])}")

