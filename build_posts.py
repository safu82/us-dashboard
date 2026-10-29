#!/usr/bin/env python3
"""Candidate posts for @overnightarc, with the guards that make them postable.

Writes portal/data/posts.json and returns lines for the nightly Telegram
message, so the day's drafts are written before anyone is awake to write them.

Every candidate carries `auto`: true when the claim is a number from our own
data rendered into a fixed sentence, false when it needs somebody to agree with
an interpretation. Only the first kind can ever be published without a human,
and only then because the TEMPLATE was reviewed, not the output.

The guards exist because of what happened while these were being drafted by
hand on 2026-10-29. Five errors in one afternoon:

  - a post about our ranking method rather than about the market
  - Alphabet's fall blamed on capex, taken from one headline, when the source
    count across 29 outlets pointed at Search
  - Memory & Storage called "the weakest stage" when it ranks 12th of 16
  - a near-miss correcting somebody's after-hours figures with our close
  - a near-miss pairing our +1.34% Alphabet close with a -10% earnings story

Three of those are now structurally impossible here: no superlative is printed
without ranking the whole set, no move is printed without naming its session,
and nothing is generated from a single source.

The fourth guard matters most. "Nobody covered this" is a claim about ABSENCE,
and absence is exactly what the entity matcher gets wrong - on 2026-10-28 it
reported zero sources for Alphabet on the day Alphabet reported, while the WSJ,
Reuters and Bloomberg all carried it. So the no-coverage post is suppressed
entirely unless the matcher looks healthy for the session, and the mover must
have been chased individually and still come back empty.
"""
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'portal', 'data')
SITE = 'https://overnightarc.com'

LIMIT = 280
URL_RE = re.compile(r'https?://\S+|\b[a-z0-9.-]+\.(?:com|org|io|co)/\S*')

# A move has to be large enough that its absence from the wires is the story.
MIN_MOVE = 0.08
# A standing change has to be large enough to mean something.
MIN_DMOM = 15
# Below these the matcher is broken, not the news quiet. Observed range over
# the sessions to 2026-10-28 was 43-73 sources and 237-365 wire items.
MIN_SOURCES = 25
MIN_WIRE = 120
# A stage with fewer scored names than this cannot carry a comparison.
MIN_SCORED = 3


def cost(text):
    """Characters as X counts them. Every URL is 23, whatever its length."""
    return len(URL_RE.sub('x' * 23, text))


def load(name):
    p = os.path.join(DATA, name)
    if not os.path.isfile(p):
        return None
    with io.open(p, encoding='utf-8') as fh:
        return json.load(fh)


def pc(x, dp=1):
    return f'{x * 100:+.{dp}f}%'


def matcher_healthy(counts):
    """Did the news side work tonight? Returns (ok, why)."""
    if not counts:
        return False, 'no counts in digest'
    s, w = counts.get('sources') or 0, counts.get('wire') or 0
    if s < MIN_SOURCES:
        return False, f'only {s} sources, floor is {MIN_SOURCES}'
    if w < MIN_WIRE:
        return False, f'only {w} wire items, floor is {MIN_WIRE}'
    return True, f'{s} sources, {w} wire items'


def post_no_coverage(d, healthy, why):
    """The one nobody else can write, and the one most able to be wrong.

    Requires the matcher to look healthy AND the mover to have been chased
    individually. A mover sits in `minor` with headline None only after
    chase_movers has queried for that company by name and found nothing, so the
    silence has been tested rather than assumed.
    """
    if not healthy:
        return {'type': 'no_coverage', 'auto': False, 'text': None,
                'blocked': f'matcher health check failed: {why}'}

    silent = [m for m in (d.get('minor') or [])
              if not m.get('headline')
              and m.get('move') is not None
              and abs(m['move']) >= MIN_MOVE
              and (m.get('company') or '').strip()
              and m.get('mom') is not None]
    if not silent:
        return {'type': 'no_coverage', 'auto': False, 'text': None,
                'blocked': f'no uncovered mover above {MIN_MOVE:.0%}'}

    silent.sort(key=lambda m: -abs(m['move']))
    a = silent[0]
    srcs = (d.get('counts') or {}).get('sources')
    verb = 'rose' if a['move'] > 0 else 'fell'
    lines = [f"{a['company']} {verb} {abs(a['move']) * 100:.1f}% "
             f"on the {d['asOf']} session.", '',
             f"We read {srcs} separate outlets. Not one of them wrote "
             f"about it."]
    if len(silent) > 1:
        b = silent[1]
        v2 = 'rose' if b['move'] > 0 else 'fell'
        lines += ['', f"{b['company']} {v2} {abs(b['move']) * 100:.1f}%. "
                      f"Also nothing."]
    lines += ['', "The biggest movers are often the least covered — general "
                  "market feeds don't push mid-caps."]
    text = '\n'.join(lines)
    return {'type': 'no_coverage', 'auto': True, 'text': text,
            'chars': cost(text), 'tickers': [m['subject'] for m in silent[:2]],
            'link': f'{SITE}/digest/{d["asOf"]}/',
            'checks': [f'matcher healthy: {why}',
                       'mover chased individually and returned nothing',
                       f'move {abs(a["move"]):.1%} clears the {MIN_MOVE:.0%} floor']}


def post_standing(d):
    """The largest change in position, which is not the largest price move.

    Deliberately not automatic. A stock gaining 30 momentum points in a day is
    a fact, but every sentence worth writing around it is a reading of why.
    """
    b = d.get('board') or {}
    ups = [m for m in (b.get('risers') or []) if m.get('d_mom', 0) >= MIN_DMOM]
    downs = [m for m in (b.get('fallers') or []) if m.get('d_mom', 0) <= -MIN_DMOM]
    if not ups and not downs:
        return {'type': 'standing', 'auto': False, 'text': None,
                'blocked': f'nothing moved {MIN_DMOM} momentum points'}

    out = [f"Biggest changes in standing on the {d['asOf']} session — "
           f"momentum, not price:", '']
    for m in ups[:2]:
        out.append(f"{m['ticker']}  {m['mom_prev']}→{m['mom']} "
                   f"({m['d_mom']:+d}) on {pc(m['day'])}" if m.get('day') is not None
                   else f"{m['ticker']}  {m['mom_prev']}→{m['mom']} ({m['d_mom']:+d})")
    for m in downs[:2]:
        out.append(f"{m['ticker']}  {m['mom_prev']}→{m['mom']} "
                   f"({m['d_mom']:+d}) on {pc(m['day'])}" if m.get('day') is not None
                   else f"{m['ticker']}  {m['mom_prev']}→{m['mom']} ({m['d_mom']:+d})")
    out += ['', "A big move and a change in position are different things. "
                "This is the second."]
    text = '\n'.join(out)
    return {'type': 'standing', 'auto': False, 'text': text, 'chars': cost(text),
            'tickers': [m['ticker'] for m in (ups[:2] + downs[:2])],
            'link': f'{SITE}/',
            'checks': ['momentum deltas from our own scored rows, both sessions']}


def post_chain(chains):
    """A value chain comparison, with every superlative checked against all
    stages rather than against the two in front of it.

    A draft on 2026-10-29 called Memory & Storage "the weakest stage" when it
    ranks 12th of 16. Here the full set is ranked first and the wording is
    derived from the rank, so the claim cannot outrun the data.
    """
    if not chains:
        return {'type': 'chain', 'auto': False, 'text': None,
                'blocked': 'no chain data'}
    best = None
    for slug, ch in (chains.get('chains') or {}).items():
        stages = [L for L in ch['links']
                  if L.get('mom') is not None and not L.get('thin')
                  and (L.get('scored') or 0) >= MIN_SCORED and L.get('members')]
        if len(stages) < 4:
            continue
        stages.sort(key=lambda L: -L['mom'])
        top, bottom = stages[0], stages[-1]
        gap = top['mom'] - bottom['mom']
        if best is None or gap > best[0]:
            best = (gap, ch, stages, top, bottom)
    if not best:
        return {'type': 'chain', 'auto': False, 'text': None,
                'blocked': f'no chain has 4+ stages with {MIN_SCORED}+ scored names'}

    gap, ch, stages, top, bottom = best
    n, k = len(ch['links']), len(stages)

    def tk(L, howmany):
        return ' '.join('$' + m['ticker'] for m in L['members'][:howmany]
                        if not m.get('context'))

    def nm(L, short):
        return (L.get('short') or L['name']) if short else L['name']

    # Stage and chain names vary from "AI" to "Advanced Packaging & Test", so
    # one fixed wording fits some chains and overruns others. Step down
    # through progressively tighter forms and take the first that fits, rather
    # than dropping a good post because its subject has a long name.
    # Only name k when some stages were excluded. "7 stages; 7 have enough
    # scored names" is noise; "16 stages; 13 have enough" is the honest caveat
    # that stops "top" and "bottom" being read as covering all of them.
    scope = (f"{n} stages in our {ch['name']} value chain" if k == n else
             f"{n} stages in our {ch['name']} value chain; {k} have enough "
             f"scored names to rank")
    for tickers, short, closer in ((3, False, True), (3, True, True),
                                   (2, True, True), (2, True, False)):
        lines = [f"{scope}. They are not moving together.", '',
                 f"Top: {nm(top, short)} — {tk(top, tickers)} — {top['mom']}/100",
                 f"Bottom: {nm(bottom, short)} — {tk(bottom, tickers)} — "
                 f"{bottom['mom']}"]
        if closer:
            lines += ['', f"Money isn't moving into {ch['name']}. It's moving "
                          f"into parts of it."]
        text = '\n'.join(lines)
        if cost(text) <= LIMIT:
            break
    else:
        return {'type': 'chain', 'auto': False, 'text': None,
                'blocked': 'no wording of this chain fits 280 characters'}

    return {'type': 'chain', 'auto': True, 'text': text, 'chars': cost(text),
            'tickers': [m['ticker'] for m in top['members'][:tickers]],
            'link': f"{SITE}/themes/{ch['slug']}/",
            'checks': [f'{k} scoreable stages of {n} ranked in full',
                       f'thin stages excluded; each kept stage has '
                       f'{MIN_SCORED}+ scored names',
                       'the post states k, so "top" and "bottom" cannot be '
                       'read as covering stages we excluded',
                       'superlatives derived from the ranking, not asserted']}


def post_regime(d):
    """Only worth saying when the benchmark is near the switch."""
    b = (d.get('board') or {}).get('regime') or {}
    r, to_up = b.get('ret20'), b.get('to_up')
    if r is None or to_up is None:
        return {'type': 'regime', 'auto': False, 'text': None,
                'blocked': 'no regime figures'}
    if b.get('band') != 'flat' or to_up > 0.01:
        return {'type': 'regime', 'auto': False, 'text': None,
                'blocked': f'not near the switch ({to_up * 100:.1f}pp away)'}
    br = (d.get('board') or {}).get('breadth') or {}
    text = '\n'.join([
        f"SPY's 20-session return is {pc(r)} as of the {d['asOf']} close — "
        f"{to_up * 100:.1f}pp from turning our board's regime line green.",
        '',
        f"{br.get('advancers', 0):,} advancers against "
        f"{br.get('decliners', 0):,} decliners.",
        '',
        "A ranking says where something stands. It doesn't say which way the "
        "market went. That's why the line exists.",
    ])
    return {'type': 'regime', 'auto': True, 'text': text, 'chars': cost(text),
            'tickers': [], 'link': f'{SITE}/method/',
            'checks': ['ret20 and breadth read from our own scored session',
                       'session named explicitly, so no after-hours confusion']}


def build():
    d = load('digest.json')
    if not d or not d.get('asOf'):
        return [], []
    chains = load('chains.json')
    healthy, why = matcher_healthy(d.get('counts'))

    cands = [post_no_coverage(d, healthy, why), post_chain(chains),
             post_regime(d), post_standing(d)]

    good, blocked = [], []
    for c in cands:
        if not c.get('text'):
            blocked.append(c)
        elif c['chars'] > LIMIT:
            c['blocked'] = f'{c["chars"]} chars, over the {LIMIT} limit'
            blocked.append(c)
        else:
            good.append(c)
    return good, blocked


def as_lines(good, blocked):
    if not good and not blocked:
        return []
    out = ['*Drafts for @overnightarc:*']
    if not good:
        out.append('  _none passed their guards today_')
    for c in good:
        tag = 'auto-safe' if c['auto'] else 'needs your eye'
        out.append('')
        out.append(f"— _{c['type']} · {c['chars']}/{LIMIT} · {tag}_")
        out.append(c['text'])
        out.append(f"   reply with: {c['link']}")
    if blocked:
        out.append('')
        out.append('_held back: ' + '; '.join(
            f"{c['type']} ({c.get('blocked', 'no text')})" for c in blocked) + '_')
    return out


def main():
    good, blocked = build()
    payload = {'generated': good, 'blocked': blocked, 'limit': LIMIT}
    os.makedirs(DATA, exist_ok=True)
    with io.open(os.path.join(DATA, 'posts.json'), 'w',
                 encoding='utf-8', newline='\n') as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
    note = '\n'.join(as_lines(good, blocked)) or 'nothing to draft'
    print(note)
    print(f'\n  {len(good)} drafted, {len(blocked)} held back', file=sys.stderr)

    # Its own message rather than appended to the digest: the digest is already
    # near Telegram's 4,000-character ceiling and the drafts would be the part
    # that got truncated away.
    if '--telegram' in sys.argv and good:
        tok = os.environ.get('TELEGRAM_BOT_TOKEN')
        chat = os.environ.get('TELEGRAM_CHAT_ID')
        if not tok or not chat:
            print('telegram secrets not set', file=sys.stderr)
            return
        import urllib.parse
        import urllib.request
        body = urllib.parse.urlencode(
            {'chat_id': chat, 'parse_mode': 'Markdown',
             'text': note[:4000]}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f'https://api.telegram.org/bot{tok}/sendMessage', data=body),
            timeout=20)
        print('drafts sent to telegram', file=sys.stderr)


if __name__ == '__main__':
    main()
