#!/usr/bin/env python3
"""Cases the entity matcher has got wrong, kept so it cannot get them wrong again.

No network and no database: patterns are built from a handful of rows written
out literally. Company names are copied exactly as `us_stock_sectors` holds
them — plain trading names, not legal ones. Using the legal names here made
three cases fail for reasons that had nothing to do with what was being tested.

    python test_feed_entities.py
"""
import sys

from feed_entities import build_patterns, entities

ROWS = [
    {'ticker': 'GS', 'company_name': 'Goldman Sachs'},
    {'ticker': 'MS', 'company_name': 'Morgan Stanley'},
    {'ticker': 'JEF', 'company_name': 'Jefferies'},
    {'ticker': 'C', 'company_name': 'Citigroup'},
    {'ticker': 'JPM', 'company_name': 'JPMorgan Chase'},
    {'ticker': 'PIPR', 'company_name': 'Piper Sandler'},
    {'ticker': 'MTX', 'company_name': 'Minerals Technologies'},
    {'ticker': 'COIN', 'company_name': 'Coinbase'},
    {'ticker': 'EFSC', 'company_name': 'Enterprise Financial Services'},
    {'ticker': 'LMT', 'company_name': 'Lockheed Martin'},
    {'ticker': 'JBLU', 'company_name': 'JetBlue'},
    {'ticker': 'VZ', 'company_name': 'Verizon'},
    {'ticker': 'NDAQ', 'company_name': 'Nasdaq'},
    {'ticker': 'DJT', 'company_name': 'Trump Media & Technology Group'},
    # Exchange-format names: the qualifier sits in brackets at the end.
    {'ticker': 'GOOGL', 'company_name': 'Alphabet Inc. (Class A)'},
    {'ticker': 'GOOG', 'company_name': 'Alphabet Inc. (Class C)'},
    {'ticker': 'FOXA', 'company_name': 'Fox Corporation (Class A)'},
    {'ticker': 'FOX', 'company_name': 'Fox Corporation (Class B)'},
    {'ticker': 'KO', 'company_name': 'Coca-Cola Company (The)'},
    {'ticker': 'HD', 'company_name': 'Home Depot (The)'},
    {'ticker': 'TTD', 'company_name': 'Trade Desk (The)'},
    {'ticker': 'LLY', 'company_name': 'Lilly (Eli)'},
    {'ticker': 'MSFT', 'company_name': 'Microsoft'},
    {'ticker': 'NWSA', 'company_name': 'News Corp (Class A)'},
    {'ticker': 'NWS', 'company_name': 'News Corp (Class B)'},
    {'ticker': 'RRC', 'company_name': 'Range Resources'},
    {'ticker': 'CACC', 'company_name': 'Credit Acceptance Corporation'},
]

# (headline, tickers that should be tagged). Every one of these is a real
# headline that appeared in a feed, except where marked as a control.
CASES = [
    # --- research houses quoted as commentators (2026-10-26) --------------
    # GS took the top slot with seven sources. One was about Goldman.
    ('The Goldman Sachs Group (NYSE:GS) Stock Jumps 2% After Analyst Upgrade', {'GS'}),
    ('BEML, BDL, HAL, BEL, MTAR shares rise up to 5%; Macquarie, Goldman Sachs, '
     'others pick top bets', set()),
    ('Paytm shares rise 2% after Q2 results. What are Jefferies, Goldman Sachs, '
     'and others saying?', set()),
    ('The AI trade is driving global markets more than oil, Goldman Sachs says', set()),
    ('Trend hedge funds saw worst week since 2024 as AI stocks fell, Goldman says', set()),
    ('Goldman Sachs names top EU construction stocks amid market headwinds', set()),
    ("Price Action: ITC Hotels shares rise 5% as Jefferies maintains 'buy'", set()),
    ('Urban Company shares rise 5% as Morgan Stanley sees up to 40% upside', set()),
    ('China stocks could slide 15% before bottom, Morgan Stanley says', set()),
    ('Morgan Stanley declares the U.S. stock market correction is nearing its end', set()),

    # The house named last, as the origin of the action. Nothing follows it,
    # so a trailing-verb test alone would let it through.
    ('Enterprise Financial Services Shares Rise After Upgrade From Piper Sandler',
     {'EFSC'}),
    ('Lockheed Martin Shares Rise After Morgan Stanley Upgrade', {'LMT'}),
    ('Coinbase Shares Fall After Morgan Stanley Downgrade', {'COIN'}),

    # --- the same houses as genuine subjects: all must survive -----------
    ('Morgan Stanley Q3 earnings beat as trading revenue surges', {'MS'}),
    ('Goldman Sachs shares fall 3% after missing on fixed income revenue', {'GS'}),
    ('Goldman Sachs Q3 profit tops estimates on trading strength', {'GS'}),
    ('Jefferies posts record investment banking quarter', {'JEF'}),
    ('Piper Sandler shares fall 4% after Q3 revenue miss', {'PIPR'}),
    ('$GS upgraded to Buy at Baird', {'GS'}),

    # --- "Minerals" is a noun every mining headline contains (2026-10-26) --
    ('Alvo Minerals (ASX:ALV) Shares Fall After Boa Vista EM Survey Update', set()),
    ('Connected Minerals (ASX:CML) Shares Fall as Niobium Data Is Released', set()),

    # --- older regressions, kept because they were expensive -------------
    ('Nasdaq takes a hit on AI spending fears', set()),
    ('Trump approval rating slips in latest poll', set()),

    # --- names the exchange stores with a bracketed qualifier (2026-10-28) ---
    # Alphabet scored ZERO mentions on the day it reported, while Microsoft
    # scored fifteen. Three faults stacked: the bracket defeated the suffix
    # stripper, the dual share class defeated the short-form uniqueness rule,
    # and the press writes "Google" either way.
    ('Alphabet beats on cloud revenue as Google Cloud grows 34%', {'GOOGL'}),
    ('Google parent Alphabet lifts capex guidance again', {'GOOGL'}),
    ('Google shares jump after earnings', {'GOOGL'}),
    ('Alphabet Inc. (NASDAQ:GOOGL) Q3 earnings beat', {'GOOGL'}),
    # Stripping every suffix would reduce these to "Fox" and "News" - three
    # letters, and an ordinary noun. The cleaner stops one step short, which is
    # also what the press writes.
    ('Fox Corporation shares fall after advertising miss', {'FOXA'}),
    ('Fox shares fall after advertising miss', set()),
    ('News Corp reports a jump in digital subscriptions', {'NWSA'}),
    ('In other news, shares were mixed', set()),
    # Ordinary market vocabulary that happens to be a company's first word.
    ('The stock traded in a narrow range all session', set()),
    ('Credit markets tightened sharply on the news', set()),
    ('Range Resources beat on production guidance', {'RRC'}),
    # Hyphenated first word: splitting on letters gave "coca" and "cola" and
    # never "coca-cola", so the uniqueness lookup refused the short form.
    ('Coca-Cola raises full-year outlook on pricing', {'KO'}),
    ('Home Depot cuts guidance as big-ticket demand softens', {'HD'}),
    ('Lilly stock climbs on obesity drug data', {'LLY'}),
    # "Trade" is stoplisted, so this one still needs its full name - and now
    # has a usable one rather than "Trade Desk (The)".
    ('Trade Desk shares sink on slowing ad spend', {'TTD'}),
    # The alias must not fire on the ordinary word.
    ('How to google a stock ticker', set()),

    # --- controls: nothing above may disturb an ordinary story -----------
    ('JetBlue Loss Widens on 57% Fuel Surge, but Stock Jumps 11%', {'JBLU'}),
    ('Verizon lifts annual forecasts, signs $2 billion AI deal with AWS', {'VZ'}),
    ('Microsoft Azure revenue surges 48% to $33.2B, beating expectations', {'MSFT'}),
]


def main():
    tickers, patterns, _ = build_patterns(ROWS)
    failed = 0
    for text, expect in CASES:
        got = {e[0] for e in entities(text, tickers, patterns) if e[1] == 'ticker'}
        if got != expect:
            failed += 1
            print(f'FAIL  got={",".join(sorted(got)) or "-"}  '
                  f'want={",".join(sorted(expect)) or "-"}\n      {text}')
    print(f'{len(CASES) - failed}/{len(CASES)} pass')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
