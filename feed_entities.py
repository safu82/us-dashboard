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
                 'nasdaq', 'dow', 'russell', 'invesco'}

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

MARKET_OPEN_UTC_MIN = 13 * 60 + 30     # 13:30 UTC


def clean_name(name):
    """'Meta Platforms, Inc.' -> 'Meta Platforms'. Repeats: suffixes stack."""
    prev = None
    out = (name or '').strip().rstrip(',')
    while out != prev:
        prev = out
        out = SUFFIX.sub('', out).strip().rstrip(',')
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

    # A first word is only safe as a short form if NO other company's name
    # contains that word. "Stanley" belongs to Stanley Black & Decker but lives
    # inside Morgan Stanley; "Trade" (The Trade Desk) inside "choppy trade";
    # "Jones" inside "Dow Jones". Uniqueness across the universe removes the
    # class structurally instead of by listing offenders.
    word_owners = {}
    for tk, base in bases.items():
        for w in re.findall(r'[A-Za-z]{2,}', base):
            word_owners.setdefault(w.lower(), set()).add(tk)

    short = {}
    for tk, base in bases.items():
        if ' ' not in base:
            continue
        w = base.split()[0]
        if len(w) < 4 or w.lower() in NAME_STOPLIST:
            continue
        if word_owners.get(w.lower(), set()) == {tk}:
            short[tk] = w

    patterns = {}
    for tk, base in bases.items():
        full = re.compile(r'(?<![A-Za-z])' + re.escape(base) + r'(?![A-Za-z])')
        brief = (re.compile(r'(?<![A-Za-z])' + re.escape(short[tk]) + r'(?![A-Za-z])')
                 if tk in short else None)
        patterns[tk] = (full, brief)
    return tickers, patterns, len(short)


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
            add(tk, 'ticker', 'company_name', m.start())
            continue
        if brief and has_market:
            m = brief.search(text)
            if m:
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
