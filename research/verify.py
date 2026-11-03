"""Stage 2 — the verifier. The checklist, as code.

Each check is one question that caught something real in a note we are using
as the standard. None of them needs a model: they are arithmetic over facts
that arrived tagged, which makes them cheap, repeatable, and impossible to
hallucinate.

Four verdicts, and UNKNOWN is a first-class answer. A check that could not run
must say so rather than quietly pass — a silent pass is how a note ends up
asserting something nobody established.
"""
import io
import json

PASS, FLAG, FAIL, UNKNOWN = 'PASS', 'FLAG', 'FAIL', 'UNKNOWN'


def _q(pack, field):
    d = pack['quarter'].get(field)
    return (d['value'] if d else None), d


def _b(pack, field):
    d = pack['balance'].get(field)
    return (d['value'] if d else None), d


def _span(d):
    """Say which period a figure covers.

    A 10-Q's cash-flow statement is year-to-date, not quarterly, so the
    shortest span available for operating cash flow is nine months, not three.
    Reporting "this period" without saying which period is how a nine-month
    number gets read as a quarter.
    """
    if not d or d.get('span_days') is None:
        return ''
    n = d['span_days']
    label = ('the quarter' if n <= 100 else 'six months' if n <= 190 else
             'nine months' if n <= 285 else 'the year')
    return f" over {label} to {d['end']}"


def _money(x):
    if x is None:
        return 'n/a'
    a = abs(x)
    if a >= 1e9:
        return f'${x/1e9:,.2f}bn'
    if a >= 1e6:
        return f'${x/1e6:,.1f}m'
    return f'${x:,.0f}'


def check_share_count(pack):
    """Which share count should a multiple use?

    The cover-page count is the current one. A weighted average is a period
    artefact and understates a company that has been issuing stock. Using the
    wrong one is how a market cap ends up 5% wrong.
    """
    cover, cd = _b(pack, 'shares_outstanding_cover')
    basic, bd = _q(pack, 'shares_basic_wavg')
    if cover is None:
        return dict(
            id='share_count', verdict=UNKNOWN,
            question='Is a current share count available?',
            detail='No cover-page share count in the XBRL facts. This is the '
                   'expected result for a multi-class issuer: Alphabet, Meta '
                   'and Berkshire tag each share class against a dimensional '
                   'member, and the companyfacts API returns only '
                   'undimensioned facts. Market cap for this name must take '
                   'its share count from the filing cover or another source, '
                   'and no multiple should be computed until it does.')
    out = dict(id='share_count', verdict=PASS,
               question='Is a current share count available?',
               detail=f'Cover-page shares outstanding {cover:,.0f} '
                      f'(as of {cd["end"]}).',
               evidence=cd['tag'])
    if basic:
        gap = (cover - basic) / basic * 100
        out['detail'] += (f' Weighted-average basic for the quarter is '
                          f'{basic:,.0f}, a {gap:+.1f}% difference.')
        if abs(gap) >= 2:
            out['verdict'] = FLAG
            out['detail'] += (' Use the cover-page count for market cap; the '
                              'gap is large enough to move a multiple.')
    return out


def check_minority_interest(pack):
    """Is reported profit all the shareholders'?

    ProfitLoss includes noncontrolling interests; NetIncomeLoss is the
    parent's share. Where they differ, headline profit overstates what
    belongs to you — the HFCL case, where EPS was Rs 2.37 not Rs 2.58.
    """
    total, td = _q(pack, 'net_income_total')
    attrib, ad = _q(pack, 'net_income_attributable')
    nci, nd = _q(pack, 'noncontrolling_interest')
    if attrib is None:
        return dict(id='minority_interest', verdict=UNKNOWN,
                    question='Is profit attributable or total?',
                    detail='Net income attributable not tagged.')
    if total is None or abs(total - attrib) < 1:
        return dict(id='minority_interest', verdict=PASS,
                    question='Is profit attributable or total?',
                    detail=f'No noncontrolling interest. Attributable profit '
                           f'{_money(attrib)}.', evidence=ad['tag'])
    diff = total - attrib
    return dict(id='minority_interest', verdict=FLAG,
                question='Is profit attributable or total?',
                detail=f'Total profit {_money(total)} includes {_money(diff)} '
                       f'({diff/total*100:.1f}%) belonging to minorities. '
                       f'Use {_money(attrib)} for per-share work.',
                evidence=(nd or td)['tag'])


def check_eps_tie(pack):
    """Does EPS tie to profit over shares?

    It usually does. When it does not, something sits between net income and
    the common holder — preferred dividends most often. Alphabet's Q3-26 note
    flagged $298m of them; this check is how you find that without being told.
    """
    eps, ed = _q(pack, 'eps_basic')
    sh, sd = _q(pack, 'shares_basic_wavg')
    ni, nd = _q(pack, 'net_income_attributable')
    if None in (eps, sh, ni) or not sh:
        return dict(id='eps_tie', verdict=UNKNOWN,
                    question='Does basic EPS tie to profit / shares?',
                    detail='Needs EPS, weighted shares and attributable profit.')
    implied = eps * sh
    gap = implied - ni
    pct = abs(gap) / abs(ni) * 100 if ni else 0
    if pct < 1.0:
        return dict(id='eps_tie', verdict=PASS,
                    question='Does basic EPS tie to profit / shares?',
                    detail=f'EPS {eps} x {sh:,.0f} shares = {_money(implied)} '
                           f'vs attributable {_money(ni)}; ties within {pct:.1f}%.')
    return dict(id='eps_tie', verdict=FLAG,
                question='Does basic EPS tie to profit / shares?',
                detail=f'EPS implies {_money(implied)} but attributable profit '
                       f'is {_money(ni)}, a {_money(gap)} gap ({pct:.1f}%). '
                       f'Something sits between the two — preferred dividends '
                       f'or a participating-security allocation. Find it before '
                       f'using EPS.')


def check_dilution_gap(pack):
    """A wide basic-to-diluted gap is a live option or convertible overhang,
    and forward multiples should use the diluted count."""
    b, _ = _q(pack, 'eps_basic')
    d, dd = _q(pack, 'eps_diluted')
    if None in (b, d) or not b:
        return dict(id='dilution_gap', verdict=UNKNOWN,
                    question='How large is the basic-to-diluted gap?',
                    detail='Basic or diluted EPS not tagged.')
    gap = (b - d) / abs(b) * 100
    v = FLAG if gap >= 3 else PASS
    return dict(id='dilution_gap', verdict=v,
                question='How large is the basic-to-diluted gap?',
                detail=f'Basic {b}, diluted {d} — {gap:.1f}% dilution.'
                       + (' Use diluted for forward multiples.' if v == FLAG else ''))


def check_tax_rate(pack):
    """An effective rate far outside the normal band usually means a one-off
    — and a profit that is tax-flattered is not a repeatable profit."""
    tax, _ = _q(pack, 'tax_expense')
    pbt, pd = _q(pack, 'pretax_income')
    if None in (tax, pbt) or not pbt:
        return dict(id='tax_rate', verdict=UNKNOWN,
                    question='Is the effective tax rate normal?',
                    detail='Pre-tax income or tax expense not tagged.')
    rate = tax / pbt * 100
    v = PASS if 10 <= rate <= 35 else FLAG
    return dict(id='tax_rate', verdict=v,
                question='Is the effective tax rate normal?',
                detail=f'Tax {_money(tax)} on pre-tax {_money(pbt)} = '
                       f'{rate:.1f}%.' + ('' if v == PASS else
                       ' Outside the usual band — check for a one-off.'))


def check_capex_vs_ocf(pack):
    """Is the build self-funded?

    Capex above operating cash flow is the single most consequential thing in
    the Alphabet quarter: it turns a cash machine into a borrower.
    """
    ocf, od = _q(pack, 'operating_cash_flow')
    capex, cd = _q(pack, 'capex')
    if None in (ocf, capex):
        return dict(id='capex_vs_ocf', verdict=UNKNOWN,
                    question='Is capex covered by operating cash flow?',
                    detail='Operating cash flow or capex not tagged.')
    fcf = ocf - capex
    v = FLAG if fcf < 0 else PASS
    return dict(id='capex_vs_ocf', verdict=v,
                question='Is capex covered by operating cash flow?',
                detail=f'Operating cash flow {_money(ocf)}, capex '
                       f'{_money(capex)}, free cash flow {_money(fcf)}'
                       f'{_span(od)}.'
                       + ('' if v == PASS else
                          ' The build is not self-funded over this span.'))


def check_funding(pack):
    """Who paid for it — the business, the lenders, or new shareholders?

    Buybacks going to zero while stock is being issued is a capital-allocation
    reversal, and it is rarely the headline.
    """
    buy, bd = _q(pack, 'buybacks')
    iss, _ = _q(pack, 'stock_issued')
    debt, _ = _q(pack, 'debt_issued')
    bits = []
    if buy is not None:
        bits.append(f'buybacks {_money(buy)}')
    if iss is not None:
        bits.append(f'stock issued {_money(iss)}')
    if debt is not None:
        bits.append(f'debt raised {_money(debt)}')
    if not bits:
        return dict(id='funding', verdict=UNKNOWN,
                    question='How was the period funded?',
                    detail='No financing lines tagged in this filing.')
    v = PASS
    why = ''
    if iss and buy is not None and buy == 0:
        v, why = FLAG, ' Issuing stock with buybacks at zero.'
    elif iss and buy and iss > buy:
        v, why = FLAG, ' Issuing more stock than repurchasing.'
    return dict(id='funding', verdict=v,
                question='How was the period funded?',
                detail='; '.join(bits) + _span(bd) + '.' + why)


def check_unusual_items(pack):
    """The US analogue of an exceptional-items line. Present means the profit
    needs reading twice."""
    r, _ = _q(pack, 'restructuring')
    i, _ = _q(pack, 'impairment')
    found = [(n, v) for n, v in (('restructuring', r), ('impairment', i))
             if v]
    if not found:
        return dict(id='unusual_items', verdict=PASS,
                    question='Any restructuring or impairment?',
                    detail='None tagged. Note this is weaker than reading the '
                           'statement — only tagged items are visible here.')
    return dict(id='unusual_items', verdict=FLAG,
                question='Any restructuring or impairment?',
                detail='; '.join(f'{n} {_money(v)}' for n, v in found) + '.')


def check_margins(pack):
    """Is the profitability ordinary, exceptional, or impossible?

    Added after SanDisk's Q3-26 came back at a 79.6% operating margin and I
    assumed the extractor was broken. It was not — memory is in a historic
    upcycle and the figures tie to the price (annualised EPS puts the stock
    near 5x). But an extraction error looks exactly like a cycle peak, and
    nothing in the other checks distinguishes them, because they all test
    internal consistency and an error is usually internally consistent.

    So this check does not judge. It says "exceptional, confirm externally",
    which is the honest state of knowledge.
    """
    rev, rd = _q(pack, 'revenue')
    op, _ = _q(pack, 'operating_income')
    ni, _ = _q(pack, 'net_income_attributable')
    if rev is None or not rev:
        return dict(id='margins', verdict=UNKNOWN,
                    question='Is profitability in a normal range?',
                    detail='Revenue not tagged, so margins cannot be formed.')
    bits = [f'revenue {_money(rev)}{_span(rd)}']
    extreme = False
    if op is not None:
        m = op / rev * 100
        bits.append(f'operating margin {m:.1f}%')
        extreme |= m > 50 or m < -25
    if ni is not None:
        m = ni / rev * 100
        bits.append(f'net margin {m:.1f}%')
        extreme |= m > 45 or m < -25
    if ni is not None and ni > rev:
        return dict(id='margins', verdict=FAIL,
                    question='Is profitability in a normal range?',
                    detail='; '.join(bits) + '. Net income exceeds revenue — '
                           'either a very large one-off, or the extraction has '
                           'mixed periods. Do not use until resolved.')
    return dict(id='margins', verdict=FLAG if extreme else PASS,
                question='Is profitability in a normal range?',
                detail='; '.join(bits) + '.' + (
                    ' Exceptional for any industry — confirm against the '
                    'press release and the prior quarter before treating it '
                    'as a run rate.' if extreme else ''))


def check_coverage(pack):
    """What the filing did not tag. The honest edge of the pack."""
    missing = pack.get('missing') or []
    if not missing:
        return dict(id='coverage', verdict=PASS,
                    question='Did every schema field resolve?',
                    detail='All fields tagged.')
    return dict(id='coverage', verdict=FLAG,
                question='Did every schema field resolve?',
                detail=f'{len(missing)} field(s) not tagged in this filing: '
                       + ', '.join(sorted(missing))
                       + '. These must be read from the statement or left '
                         'explicitly unverified — never inferred.')


CHECKS = [check_share_count, check_minority_interest, check_eps_tie,
          check_dilution_gap, check_tax_rate, check_margins,
          check_capex_vs_ocf, check_funding, check_unusual_items,
          check_coverage]


def verify(pack):
    results = [c(pack) for c in CHECKS]
    tally = {}
    for r in results:
        tally[r['verdict']] = tally.get(r['verdict'], 0) + 1
    return {'ticker': pack['ticker'], 'form': pack['form'],
            'period_end': pack['period_end'], 'accession': pack['accession'],
            'source_url': pack['source_url'],
            'tally': tally, 'checks': results}


def render(report):
    lines = [f"{report['ticker']} {report['form']} — period {report['period_end']}",
             f"accession {report['accession']}",
             f"source {report['source_url']}", '']
    order = {FAIL: 0, FLAG: 1, UNKNOWN: 2, PASS: 3}
    for c in sorted(report['checks'], key=lambda c: order[c['verdict']]):
        lines.append(f"[{c['verdict']:<7}] {c['question']}")
        lines.append(f"           {c['detail']}")
    lines.append('')
    lines.append('  '.join(f'{k} {v}' for k, v in sorted(report['tally'].items())))
    return '\n'.join(lines)


def save(report, path):
    with io.open(path, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=1, sort_keys=True)
    return path
