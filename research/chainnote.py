"""The chain note — the thing the single-company pipeline could not write.

A note on one company argues from inside that company. It cannot tell a
name-specific derate from a sector rotation, because it has nothing to
compare against. The SanDisk note reached a confident conclusion about memory
pricing when every one of the fifteen AI stages had fallen together, and the
memory stage had fallen less than most of them.

This writes from the chain instead:

  the board        where this chain sits among all eighteen
  the profile      every stage in supply-chain order, scored today, with how
                   far each has moved
  the demand side  what the buyers at the end of the chain have committed to
                   in writing

That last join is the one that matters. Suppliers de-rating while the
companies that buy from them guide capex higher is a disagreement with a
resolution, and it is invisible from inside any single name.

Written by the same discipline as the company note: arithmetic in code,
evidence only, no plumbing in the prose, and the critic reads it afterwards.
"""
import io
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chain as C

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except ImportError:
    pass

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
MODEL = 'claude-opus-5'
RATE_IN, RATE_OUT = 15.00, 75.00


PROMPT = """You are an analyst writing a note on an entire value chain, not
on one company. Write what a portfolio manager would act on.

WHAT THIS NOTE IS FOR

A chain note answers questions a single-stock note cannot: is this a rotation
or a market, where along the chain is money being paid, and do the buyers and
the suppliers agree about the future. Lead with whichever of those the
evidence answers most clearly.

The strongest material here is usually the DEMAND_BOARD set against the
supplier stages. When the companies at the buying end of a chain commit capex
and revenue growth in writing while the stages that sell to them trade in the
weak tail, that is a disagreement with a date on it. Say who is committing
what, and what the suppliers are priced at.

NEVER DO THESE

- Never mention packs, verification, schemas, coverage or provenance.
- Never cite a stage marked THIN or with fewer than three scored members.
- Never characterise a stage by naming selected members. Each stage carries
  a computed `spread`: weakest, strongest, median, how many below 20, and
  every member's momentum. Cite the spread. If you name members you must
  name the weakest alongside the strongest, because a stage recommended on
  its three best names while two are broken is a misrepresentation, not a
  summary.
- Never invent a figure. Everything numeric must be in the evidence.
- Never treat an absence of guidance as an absence of commitment — several
  issuers guide verbally and file nothing. If a buyer shows no guidance, say
  the filing carries none, not that the company said nothing.
- Never imply the guidance dates are simultaneous. They are not: say when a
  commitment was made if it is older than the move you are describing.

HANDLING THE NUMBERS

- Stage scores are mean momentum of scored members, 0-100, higher stronger,
  computed on the stated date. Changes are in points, not per cent.
- rs_rank: 1 is strongest of the whole universe.
- A chain mean is an average of stage means. Do not present it as precise.

STRUCTURE

## <Chain> — <a sentence stating the finding>

**What changed:** two or three sentences. The move, its breadth, and whether
this chain moved with the market or against it.

### Where the money went
The profile along the chain. Which stages held, which broke, and whether
there is a tilt from inputs toward buyers.

### What the buyers have committed
The demand board. Who guided what, when, and how that sits against the
suppliers' positioning.

### The disagreement
The core argument. If buyers and suppliers disagree, say which side the
evidence favours and what would resolve it. If they agree, say so and say
what that implies.

### Where to look
Three or four specific stages or names, each with a reason and a figure.

Under 800 words. Plain declarative sentences. Markdown.

EVIDENCE
"""


def build_context(chain_name, asof=None):
    prof = C.movement(chain_name, asof=asof)
    if prof.get('error'):
        return None, prof
    brd = C.board(asof=prof['asof'])
    dem = C.demand_board(chain_name, asof=prof['asof'])

    me = next((r for r in brd['chains']
               if r['chain'].lower() == prof['chain'].lower()), None)
    ctx = {
        'chain': prof['chain'],
        'as_of': prof['asof'],
        'compared_with': prof.get('compared_with'),
        'this_chain_on_the_board': me,
        'all_chains_weakest_first': brd['chains'],
        'chains_down_over_period': brd['chains_down'],
        'chains_ranked': brd['chains_ranked'],
        'chain_mean_momentum': prof.get('chain_mean'),
        'tilt_along_chain': prof.get('slope_reading'),
        'every_stage_fell': not prof.get('any_stage_rose', True),
        # Every scored member, never a slice. Truncating to eight hid two of
        # Semiconductor Equipment's ten and let a breadth claim be made about
        # a group the writer could not see in full.
        'stages': [
            {k: s.get(k) for k in
             ('position', 'stage', 'momentum_mean_today', 'momentum_then',
              'momentum_change', 'rank_weakest_first', 'of_stages_ranked',
              'scored_members', 'too_thin_to_cite', 'spread')}
            | {'members': [m for m in s['members'] if m['in_universe']]}
            for s in prof['stages']],
        'demand_board': dem.get('buyers'),
        'buyer_stages': dem.get('buyer_stages'),
        'suppliers_weakest_first': dem.get('suppliers_weakest_first'),
        'reading_notes': [
            'Momentum is a 0-100 cross-sectional percentile on the stated '
            'date; 50 is the market median. Changes are in points.',
            'Stage scores are the mean momentum of members scored that day, '
            'computed here, not the published portal stage score.',
            'Guidance in the demand board comes from each buyer\'s most '
            'recent 8-K, and those filings span several weeks. Check the '
            'filed date before setting a commitment against a price move.',
        ],
    }
    return ctx, None


def write(chain_name, model=MODEL, asof=None):
    import anthropic
    ctx, err = build_context(chain_name, asof)
    if err:
        return err
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model, max_tokens=5000,
        messages=[{'role': 'user',
                   'content': PROMPT + json.dumps(ctx, indent=1, default=str)}])
    note = ''.join(b.text for b in resp.content if b.type == 'text').strip()
    usage = {'model': model, 'input_tokens': resp.usage.input_tokens,
             'output_tokens': resp.usage.output_tokens,
             'cost_usd': round(resp.usage.input_tokens / 1e6 * RATE_IN
                               + resp.usage.output_tokens / 1e6 * RATE_OUT, 4)}
    slug = ctx['chain'].lower().replace(' ', '-').replace('&', 'and')
    stem = f"CHAIN_{slug}_{ctx['as_of']}"
    os.makedirs(OUT, exist_ok=True)
    with io.open(os.path.join(OUT, stem + '_note.md'), 'w',
                 encoding='utf-8') as fh:
        fh.write(note + f"\n\n---\nGenerated {date.today().isoformat()} from "
                        f"chain evidence. {usage['model']}, "
                        f"${usage['cost_usd']:.4f}.\n")
    with io.open(os.path.join(OUT, stem + '_context.json'), 'w',
                 encoding='utf-8') as fh:
        json.dump(ctx, fh, indent=1, default=str)
    return {'chain': ctx['chain'], 'note': note, 'usage': usage,
            'stem': stem}


if __name__ == '__main__':
    name = ' '.join(a for a in sys.argv[1:] if not a.startswith('--')) \
        or 'Artificial Intelligence'
    r = write(name)
    if r.get('error'):
        print(r['error'])
        print('available:', ', '.join(r.get('available', [])))
        sys.exit(1)
    print(r['note'])
    u = r['usage']
    print(f"\n[{u['input_tokens']:,} in / {u['output_tokens']:,} out, "
          f"${u['cost_usd']:.4f}]  -> {r['stem']}_note.md")
