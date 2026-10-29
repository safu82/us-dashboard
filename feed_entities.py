#!/usr/bin/env python3
"""Resolve what a piece of market text is about. Shared by every feed collector.

One copy, because the matching rules were arrived at by being wrong three times
and a second copy would drift from the first. The RSS and X collectors differ in
how they fetch; what a story is ABOUT should not depend on where it came from.

The rules, and the mistake each one exists to prevent:

  cashtag        $META. Trivial, and the only rule that is never wrong.
  company_name   The full registered name. Specific enough to stand alone.
  company_short  The first word. NOT optional — the press writes "Meta stock
                 sinks", never "Meta Platforms", so without this the single
                 biggest story of 2026-10-22 went untagged. Also the dangerous
                 one: the first attempt tagged DJT on Trump's approval rating,
                 RHI on a Nobel prize awarded to Robert Hall, and TDS on
                 telephone switch operators. Two constraints below fix that.
  macro_term     Objects that move the boards but have no ticker.
"""

import re
from datetime import datetime, timedelta, timezone

# Objects that move the boards but have no ticker in our universe.
MACRO = {
    'US10Y':  [r'10-?year (?:treasury|yield|note)', r'\b10Y\b', r'ten-?year yield'],
    'FED':    [r'\bFederal Reserve\b', r'\bFOMC\b', r'\brate (?:cut|hike)s?\b'],
    'BRENT':  [r'\bBrent\b.{0,20}crude', r'\bcrude oil\b', r'\boil prices?\b'],
    'GOLD':   [r'\bgold prices?\b', r'\bbullion\b'],
    'CPI':    [r'\bCPI\b', r'\binflation (?:data|report|print)\b'],
    'JOBS':   [r'\bjobless claims\b', r'\bpayrolls\b', r'\bunemployment rate\b'],
    'DRAM':   [r'\bDRAM\b', r'\bHBM\b', r'\bmemory (?:prices|chips)\b'],
}

# Company names that are also ordinary words, or that occur far more often as
# themselves than as the company. "Trump Media" is listed; almost no story
# containing Trump is about it.
NAME_STOPLIST = {'target', 'gap', 'block', 'match', 'on', 'key', 'all', 'how',
                 'so', 'now', 'open', 'live', 'root', 'peak', 'sky', 'arc',
                 'ai', 'at', 'it', 'up', 'new', 'first', 'core', 'one', 'two',
                 'post', 'paper', 'science', 'global', 'national', 'general',
                 'american', 'united', 'capital', 'energy', 'health', 'metal',
                 'trump', 'trade', 'jones', 'stanley', 'robert', 'telephone',
                 'commerce', 'public', 'service', 'western', 'eastern', 'north',
                 'south', 'east', 'west', 'central', 'summit', 'liberty',
                 'premier', 'select', 'prime', 'pioneer', 'heritage', 'legacy',
                 # Index and exchange words that are also listed companies.
                 # "Nasdaq takes a hit on AI spending fears" is about the index,
                 # not about Nasdaq Inc, and tagging NDAQ there put a +7.5%
                 # exchange operator at the top of a digest about a selloff.
                 'nasdaq', 'dow', 'russell', 'invesco',
                 # Company names that are also the adjectives financial writers
                 # reach for. "Monster third-quarter earnings" is a description,
                 # not Monster Beverage, and it put MNST third in a digest on a
                 # story about SpaceX.
                 'monster', 'titan', 'paramount', 'sterling', 'superior',
                 'apex', 'pinnacle', 'element', 'signature', 'catalyst',
                 'momentum', 'velocity', 'advance', 'standard', 'universal',
                 # Minerals Technologies cleans down to "Minerals", which is a
                 # noun every mining headline on earth contains. It collected
                 # two ASX juniors on 2026-10-26 - Alvo Minerals and Connected
                 # Minerals - and reported them as a US specialty chemicals
                 # company.
                 'minerals',
                 # First words that are ordinary market vocabulary, so the
                 # market-context gate does nothing to contain them: a stock
                 # trades in a RANGE, CREDIT markets tighten, an OPTION expires,
                 # a figure comes in UNDER. Each of these is a real company's
                 # first word - Range Resources, Credit Acceptance, Option Care
                 # Health, Under Armour, Delta Air Lines, Daily Journal, Star
                 # Bulk - and each keeps its full name; only the short form
                 # goes. False negatives are the cheaper error here.
                 'news', 'under', 'credit', 'delta', 'daily', 'option',
                 'range', 'star'}

# Banks and brokerages are quoted inside other companies' stories constantly:
# "Goldman says", "what Jefferies is saying", "Morgan Stanley sees 40% upside".
# The matcher cannot tell the subject of a story from the house holding an
# opinion about it, and the digest ranks by how many separate outlets carried an
# item - so a name that appears in everybody else's story wins by construction,
# not by mattering.
#
# On 2026-10-26 Goldman Sachs took the top slot with seven sources. One was
# about Goldman. The others were an Indian defence basket, Paytm twice, EU
# construction stocks, hedge fund returns and the AI trade. Every Indian
# article in that digest entered through a broker name; not one through its own
# subject. Fixing the attribution removes the geography problem with it.
RESEARCH_HOUSES = {'GS', 'MS', 'JPM', 'C', 'BAC', 'WFC', 'JEF', 'UBS', 'DB',
                   'BCS', 'LAZ', 'EVR', 'RJF', 'PIPR', 'SF', 'HLI', 'PJT',
                   'NMR', 'MC', 'COWN', 'BMO', 'RY', 'TD', 'CS'}

# Said OF the house, so the house is the source of a view and not the story.
# Searched over a short tail because headlines are short; over a paragraph this
# would reach into an unrelated clause.
HOUSE_COMMENTARY = re.compile(
    r'\b(say|says|said|saying|sees|seeing|saw|names|named|expects?|warns?|'
    r'declares?|maintains?|reiterates?|initiates?|resumes?|reinstates?|'
    r'upgrades?|downgrades?|upgrade|downgrade|raises?|lowers?|lifts?|cuts?|'
    r'picks?|flags?|likes?|prefers?|boosts?|slashes?|starts?|rates?|'
    r'analysts?|strategists?|note|call|target)\b', re.I)

# Said BEFORE the house, same conclusion.
HOUSE_ATTRIBUTION = re.compile(
    r'\b(what\s+(?:are|is|do|does|did)\b|according to\b|analysts?\s+at\b|'
    r'strategists?\s+at\b|per\s+a\s+note\b|citing\b)', re.I)

# The house named last, as the origin of the action: "Shares Rise After Upgrade
# From Piper Sandler". Nothing follows it, so the trailing-verb test finds
# nothing to object to and the mention survives with the house credited as the
# subject of a story about somebody else.
HOUSE_TRAILING = re.compile(
    r'\b(?:upgrade|downgrade|rating|ratings|coverage|initiation|initiated|'
    r'target|note|call|recommendation|outlook|buy|sell|hold|overweight|'
    r'underweight|neutral)\w*\s+(?:from|at|by)\s+$', re.I)

# The house is unambiguously the subject. Checked first, because a genuine
# story about a bank is full of the same words - "Goldman Sachs Group (NYSE:GS)
# Stock Jumps 2% After Analyst Upgrade" contains both "analyst" and "upgrade".
HOUSE_SUBJECT_TAIL = re.compile(
    r'^\W{0,12}(?:Group\W{0,4})?(shares?|stock|stocks|Q[1-4]|earnings|revenue|'
    r'results|profits?|EPS|guidance|beats?|misses?|falls?|rises?|jumps?|'
    r'slides?|gains?|drops?|sinks?|surges?|tumbles?|posts?|reports?)\b', re.I)

# Requiring market vocabulary near a short-form match costs nothing on a real
# company story — they all say shares, earnings or revenue somewhere — and
# removes the political and general-news class wholesale.
MARKET_CONTEXT = re.compile(
    r'\b(shares?|stocks?|earnings|revenue|quarter(?:ly)?|guidance|EPS|profit|'
    r'investors?|analysts?|Nasdaq|NYSE|S&P|market cap|valuation|dividend|'
    r'upgrade[ds]?|downgrade[ds]?|buyback|IPO|forecast|outlook)\b', re.I)

SUFFIX = re.compile(
    r'\s+(?:Inc|Corp|Corporation|Company|Co|Ltd|plc|Holdings?|Group|N\.V\.|'
    r'S\.A\.|LLC|L\.P\.|LP|Technologies|Technology|International|Limited|'
    r'Incorporated|Class\s+[A-Z])\.?,?\s*$', re.I)

# The exchange listing format parks a qualifier in brackets at the end:
# "Alphabet Inc. (Class A)", "Coca-Cola Company (The)", "Lilly (Eli)". SUFFIX
# anchors to the end of the string, so the closing bracket stopped it matching
# and nothing was stripped at all - leaving a name no headline ever contains.
PARENTHETICAL = re.compile(r'\s*\([^)]*\)\s*$')

# Names the press uses that are not the registered name. Deliberately tiny: it
# is a list of genuine exceptions, not a place to paper over matching bugs.
# "Google" is the one that matters - Alphabet's filings say Alphabet and
# essentially no headline does.
ALIASES = {
    'GOOGL': ['Google'],
    'META': ['Facebook'],
}

MARKET_OPEN_UTC_MIN = 13 * 60 + 30     # 13:30 UTC


def clean_name(name):
    """'Alphabet Inc. (Class A)' -> 'Alphabet'. Repeats: these stack.

    Brackets come off first and unconditionally - a trailing qualifier is never
    part of the name anybody writes.

    Suffixes then come off one at a time, and only while the result is still a
    name. Stripping to the end produced 'News Corp (Class A)' -> 'News' and
    'Fox Corporation (Class A)' -> 'Fox': an ordinary noun that would match a
    large share of all headlines, and three letters shared with a news network,
    an actor and an animal. Where the last strip would ruin the name, the
    longer form is kept - and 'News Corp' and 'Fox Corporation' are what the
    press writes anyway.
    """
    out = (name or '').strip().rstrip(',')
    prev = None
    while out != prev:
        prev = out
        out = PARENTHETICAL.sub('', out).strip().rstrip(',')
    prev = None
    while out != prev:
        prev = out
        cand = SUFFIX.sub('', out).strip().rstrip(',')
        if cand == out:
            break
        if len(cand) < 4 or cand.lower() in NAME_STOPLIST:
            break
        out = cand
    return out


def _words(base):
    """Every word a name could be claimed by.

    Whitespace tokens are kept whole so "Coca-Cola" is one word. Splitting on
    letters alone produced "coca" and "cola" and never "coca-cola", so the
    uniqueness lookup below found nothing and refused the short form to a
    company whose name is on a billion cans.

    The alphabetic fragments are indexed too, so a different company holding
    "Cola" still blocks the short form rather than silently sharing it.
    """
    out = set()
    for tok in base.split():
        t = tok.strip('.,&()').lower()
        if len(t) >= 2:
            out.add(t)
        for frag in re.findall(r'[A-Za-z]{2,}', tok):
            out.add(frag.lower())
    return out


def build_patterns(rows):
    """rows of {ticker, company_name} -> (tickers, {ticker: (full, short|None)})."""
    tickers, bases = set(), {}
    for r in rows:
        tk = (r.get('ticker') or '').strip()
        if not tk:
            continue
        tickers.add(tk)
        base = clean_name(r.get('company_name'))
        # Three characters or fewer is not a name, it is a coincidence waiting
        # to happen.
        if len(base) >= 4 and base.lower() not in NAME_STOPLIST:
            bases[tk] = base

    # Share classes are one company, not two. Alphabet is listed twice, as
    # GOOGL and GOOG, and the uniqueness rule below - which exists to stop an
    # ambiguous first word being claimed - saw "Alphabet" owned by two tickers
    # and refused a short form to both. The protection fired on the one case it
    # should not: two tickers that are the same issuer. On 2026-10-28 that left
    # the largest company to report with zero mentions while Microsoft had 15.
    #
    # Tickers cleaning to the same base are the same company, so one carries
    # the name. The longest ticker wins, which is reliably the class people
    # quote: GOOGL over GOOG, FOXA over FOX, NWSA over NWS, UAA over UA. The
    # others stay in `tickers`, so a cashtag still finds them.
    by_base = {}
    for tk, base in bases.items():
        by_base.setdefault(base.lower(), []).append(tk)
    folded = 0
    for tks in by_base.values():
        if len(tks) > 1:
            keep = sorted(tks, key=lambda t: (-len(t), t))[0]
            for t in tks:
                if t != keep:
                    bases.pop(t, None)
                    folded += 1

    # A first word is only safe as a short form if NO other company's name
    # contains that word. "Stanley" belongs to Stanley Black & Decker but lives
    # inside Morgan Stanley; "Trade" (The Trade Desk) inside "choppy trade";
    # "Jones" inside "Dow Jones". Uniqueness across the universe removes the
    # class structurally instead of by listing offenders.
    word_owners = {}
    for tk, base in bases.items():
        for w in _words(base):
            word_owners.setdefault(w, set()).add(tk)
        for a in ALIASES.get(tk, []):
            for w in _words(a):
                word_owners.setdefault(w, set()).add(tk)

    short = {}
    for tk, base in bases.items():
        if ' ' not in base:
            continue
        w = base.split()[0]
        key = w.strip('.,&()').lower()
        if len(key) < 4 or key in NAME_STOPLIST:
            continue
        if word_owners.get(key, set()) == {tk}:
            short[tk] = w

    patterns = {}
    for tk, base in bases.items():
        names = [base] + ALIASES.get(tk, [])
        full = re.compile(r'(?<![A-Za-z])(?:'
                          + '|'.join(re.escape(n) for n in names)
                          + r')(?![A-Za-z])')
        brief = (re.compile(r'(?<![A-Za-z])' + re.escape(short[tk]) + r'(?![A-Za-z])')
                 if tk in short else None)
        patterns[tk] = (full, brief)
    return tickers, patterns, len(short)


def is_commentary(text, tk, at, end):
    """True when a research house is the source of a view, not the subject.

    Subject evidence wins outright: an explicit exchange-qualified ticker or a
    cashtag settles it, and so does the name sitting directly against the words
    a story uses about its own subject. Only when there is none of that does the
    commentary test get to drop the mention.

    Everything that is not a research house returns False, so the ordinary path
    through the matcher is unchanged.
    """
    if tk not in RESEARCH_HOUSES:
        return False
    if re.search(r'(?:NYSE|NASDAQ|AMEX|NYSEARCA)\s*:\s*' + re.escape(tk) + r'\b',
                 text, re.I):
        return False
    if re.search(r'\$' + re.escape(tk) + r'\b', text):
        return False
    if HOUSE_SUBJECT_TAIL.match(text[end:end + 34]):
        return False
    if HOUSE_COMMENTARY.search(text[end:end + 40]):
        return True
    before = text[max(0, at - 60):at]
    return bool(HOUSE_ATTRIBUTION.search(before) or HOUSE_TRAILING.search(before))


def entities(text, tickers, patterns):
    """[(entity, type, matched_on, excerpt)] for one piece of text."""
    found = {}

    def add(ent, typ, how, at):
        if ent not in found:
            lo, hi = max(0, at - 60), at + 90
            found[ent] = (ent, typ, how, text[lo:hi].strip())

    for m in re.finditer(r'\$([A-Z]{1,5})\b', text):
        if m.group(1) in tickers:
            add(m.group(1), 'ticker', 'cashtag', m.start())
    has_market = bool(MARKET_CONTEXT.search(text))
    for tk, (full, brief) in patterns.items():
        m = full.search(text)
        if m:
            if not is_commentary(text, tk, m.start(), m.end()):
                add(tk, 'ticker', 'company_name', m.start())
            continue
        if brief and has_market:
            m = brief.search(text)
            if m and not is_commentary(text, tk, m.start(), m.end()):
                add(tk, 'ticker', 'company_short', m.start())
    for key, pats in MACRO.items():
        for p in pats:
            m = re.search(p, text, re.I)
            if m:
                add(key, 'macro', 'macro_term', m.start())
                break
    return list(found.values())


def session_for(when):
    """The trading session a post belongs to, not the date it was filed.

    US cash equities open at 13:30 UTC. Anything filed before that belongs to
    the session that just finished: the daily recaps land near 03:00 UTC and are
    entirely about the previous close, so filing them under the new date would
    attribute yesterday's news to a session that has not happened. Weekends roll
    back to the Friday for the same reason.
    """
    ts = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    d = ts.date()
    if ts.hour * 60 + ts.minute < MARKET_OPEN_UTC_MIN:
        d -= timedelta(days=1)
    while d.weekday() > 4:
        d -= timedelta(days=1)
    return d
