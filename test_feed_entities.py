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

    # --- controls: nothing above may disturb an ordinary story -----------
    ('JetBlue Loss Widens on 57% Fuel Surge, but Stock Jumps 11%', {'JBLU'}),
    ('Verizon lifts annual forecasts, signs $2 billion AI deal with AWS', {'VZ'}),
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
