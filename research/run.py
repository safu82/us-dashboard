"""Stages 0-2 end to end for one ticker.

    python research/run.py GOOGL
    python research/run.py AMZN SNDK --form 10-Q

Writes the facts pack and the verification report into research/out/ so the
later stages — and a human — can read exactly what the note was built from.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import edgar
import facts as F
import verify as V

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')

# The cross-section needs Supabase; the filing stages need only EDGAR. Keep
# the import soft so a machine with SEC access but no database still gets the
# filing half rather than failing outright.
try:
    import crosssection as X
    HAVE_X = True
except Exception as _e:                                   # noqa: BLE001
    HAVE_X, X_ERR = False, str(_e)


def run(ticker, form='10-Q', which=0, ctx=None):
    os.makedirs(OUT, exist_ok=True)
    filings = edgar.recent_filings(ticker, forms=(form,), limit=which + 1)
    if not filings:
        print(f'{ticker}: no {form} found')
        return None
    filing = filings[which]
    print(f"\n{'='*70}\n{ticker}  {filing['form']}  period {filing['period']}  "
          f"filed {filing['filed']}\n{'='*70}")

    cf = edgar.company_facts(ticker)
    pack = F.extract(cf, filing)
    report = V.verify(pack)

    stem = f"{ticker}_{filing['period']}_{filing['form'].replace('-', '')}"
    F.save(pack, os.path.join(OUT, stem + '_facts.json'))
    V.save(report, os.path.join(OUT, stem + '_checks.json'))
    print(V.render(report))
    written = [stem + '_facts.json', stem + '_checks.json']

    if HAVE_X:
        asof, uni, sd = ctx if ctx else (None, None, None)
        # The pack must carry the standing as it was when this filing landed,
        # not as it is today — otherwise a backfilled quarter gets stamped
        # with current momentum and every historical diff reads as flat.
        filing_asof = X.nearest_date(filing['filed'])
        if filing_asof is None:
            # Snapshot history does not reach this filing. Say so and leave
            # the cross-section out. Substituting today's standing would
            # stamp current momentum onto an old quarter — the same
            # vintage-mixing error the packs are built to prevent.
            cross = {'ticker': ticker, 'covered': False,
                     'asof': None, 'filed': filing['filed'],
                     'note': (f"No snapshot on or before {filing['filed']}. "
                              f"daily_stock_snapshots begins later than this "
                              f"filing, so no cross-sectional claim can be "
                              f"made about it.")}
            print(f"\n  cross-section unavailable: no snapshot on or before "
                  f"{filing['filed']}")
        elif filing_asof != asof:
            print(f"\n  (cross-section as of {filing_asof}, the session on or "
                  f"before this filing)")
            cross = X.cross_section(ticker, filing_asof, None, sd)
        else:
            cross = X.cross_section(ticker, asof, uni, sd)
        X.save(cross, os.path.join(OUT, stem + '_cross.json'))
        written.append(stem + '_cross.json')
        if cross.get('covered'):
            print()
            print(X.render(cross))
    else:
        print(f'\n(cross-section skipped: {X_ERR})')

    print('\nwritten: ' + '\n         '.join('research/out/' + w for w in written))
    return report


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('tickers', nargs='+')
    ap.add_argument('--form', default='10-Q')
    ap.add_argument('--nth', type=int, default=0,
                    help='0 = most recent filing of that form')
    a = ap.parse_args()
    # Pull the universe and the stage table once, not per ticker.
    ctx = None
    if HAVE_X:
        d = X.latest_date()
        ctx = (d, X.universe(d), X._stage_table())
    for t in a.tickers:
        try:
            run(t.upper(), a.form, a.nth, ctx)
        except Exception as e:
            print(f'{t}: FAILED — {e}')
