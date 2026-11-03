"""Stage 1 — the extractor, and the schema it fills.

Everything here is deterministic. Each field names the XBRL tags companies
actually use for it (they differ, so each field carries a candidate list), and
every value is pulled from facts whose accession number matches the filing we
asked for. That last part matters: it means every number in a note can be
traced to one document, and nothing can silently blend two quarters.

A model is NOT used at this stage. It cannot be — the whole point is that
these figures are as filed. The model's turn comes later, on the narrative
that XBRL does not carry.
"""
import io
import json

# ---------------------------------------------------------------------------
# The schema. duration = measured over a period (revenue); instant = measured
# at a date (cash). Order matters: the first tag a company reports wins.
# ---------------------------------------------------------------------------
DURATION = {
    'revenue': ['RevenueFromContractWithCustomerExcludingAssessedTax',
                'Revenues',
                'RevenueFromContractWithCustomerIncludingAssessedTax'],
    'operating_income': ['OperatingIncomeLoss'],
    # Every issuer picks a different one of these. Amazon uses the third.
    'pretax_income': [
        'IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest',
        'IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestmentsAndMinorityInterest',
        'IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments'],
    'tax_expense': ['IncomeTaxExpenseBenefit'],
    # The pair that exposes minority interest: ProfitLoss includes
    # noncontrolling interests, NetIncomeLoss is the parent's share.
    'net_income_total': ['ProfitLoss'],
    'net_income_attributable': ['NetIncomeLoss'],
    'noncontrolling_interest': [
        'NetIncomeLossAttributableToNoncontrollingInterest'],
    'eps_basic': ['EarningsPerShareBasic'],
    'eps_diluted': ['EarningsPerShareDiluted'],
    'shares_basic_wavg': ['WeightedAverageNumberOfSharesOutstandingBasic',
                          'WeightedAverageNumberOfSharesOutstanding'],
    'shares_diluted_wavg': ['WeightedAverageNumberOfDilutedSharesOutstanding'],
    'rd_expense': ['ResearchAndDevelopmentExpense'],
    'depreciation': ['DepreciationDepletionAndAmortization',
                     'DepreciationAmortizationAndAccretionNet',
                     'DepreciationAndAmortization'],
    'interest_expense': ['InterestExpense', 'InterestExpenseNonoperating',
                         'InterestIncomeExpenseNet'],
    'operating_cash_flow': ['NetCashProvidedByUsedInOperatingActivities'],
    # Amazon reports capex as "productive assets", not PP&E.
    'capex': ['PaymentsToAcquirePropertyPlantAndEquipment',
              'PaymentsToAcquireProductiveAssets'],
    'buybacks': ['PaymentsForRepurchaseOfCommonStock'],
    'dividends_paid': ['PaymentsOfDividendsCommonStock', 'PaymentsOfDividends'],
    'stock_issued': ['ProceedsFromIssuanceOfCommonStock'],
    'debt_issued': ['ProceedsFromIssuanceOfLongTermDebt',
                    'ProceedsFromIssuanceOfSeniorLongTermDebt'],
    # Unusual items. US GAAP has no "exceptional items" line, so these stand in.
    'restructuring': ['RestructuringCharges'],
    'impairment': ['AssetImpairmentCharges', 'GoodwillImpairmentLoss',
                   'ImpairmentOfIntangibleAssetsExcludingGoodwill'],
}

INSTANT = {
    'cash': ['CashAndCashEquivalentsAtCarryingValue'],
    'total_assets': ['Assets'],
    'equity_parent': ['StockholdersEquity'],
    'equity_incl_nci': [
        'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'],
    'long_term_debt': ['LongTermDebtNoncurrent', 'LongTermDebt'],
}

# dei: lives in its own namespace and carries the cover-page share count —
# the one that should be used for market cap, not a weighted average.
DEI_INSTANT = {'shares_outstanding_cover': ['EntityCommonStockSharesOutstanding']}


def _units(entry):
    """companyfacts nests values under a unit (USD, shares, USD/shares)."""
    for unit, rows in entry.get('units', {}).items():
        for row in rows:
            yield unit, row


def _pick(facts, ns, tags, accession, instant, period_end,
          want_ytd=False, latest_instant=False):
    """The value for these tags from one specific filing AND one period.

    Matching on accession alone is not enough, and getting this wrong is the
    first bug this pipeline produced. A 10-Q tags the prior year's comparatives
    under the same accession, so Alphabet's Q3-26 filing carries both the 2026
    and 2025 nine-month cash flows with identical span lengths. Picking "the
    shortest span" then chose between them arbitrarily and silently returned
    the 2025 figures — $112.31bn of operating cash flow instead of $42.29bn.

    So the end date is pinned to the period being reported. Only then does
    span length distinguish the quarter from the year to date.

    `latest_instant` is for the cover page, whose share count is stated as of
    a date near filing rather than period end.
    """
    space = facts.get('facts', {}).get(ns, {})
    for tag in tags:
        entry = space.get(tag)
        if not entry:
            continue
        hits = []
        for unit, row in _units(entry):
            if row.get('accn') != accession:
                continue
            if instant:
                if 'start' in row:
                    continue
                if not latest_instant and row.get('end') != period_end:
                    continue
                hits.append((None, row, unit, tag))
            else:
                if 'start' not in row:
                    continue
                if row.get('end') != period_end:
                    continue
                span = (_d(row['end']) - _d(row['start'])).days
                hits.append((span, row, unit, tag))
        if not hits:
            continue
        if instant:
            hits.sort(key=lambda h: h[1]['end'], reverse=True)
        else:
            # Shortest span = the quarter; longest = year to date.
            hits.sort(key=lambda h: h[0], reverse=want_ytd)
        span, row, unit, tag = hits[0]
        return {'value': row['val'], 'unit': unit, 'tag': tag,
                'start': row.get('start'), 'end': row.get('end'),
                'span_days': span, 'form': row.get('form'),
                'filed': row.get('filed'), 'accession': row.get('accn')}
    return None


def _d(s):
    from datetime import date
    y, m, dd = (int(x) for x in s.split('-'))
    return date(y, m, dd)


def extract(company_facts, filing):
    """Fill the schema for one filing. Returns the facts pack.

    Every field is either a dict carrying value + the tag and dates it came
    from, or None — meaning the company did not tag it in this filing. None is
    a real answer and must survive to the verifier, not be coerced to zero.
    """
    acc = filing['accession']
    pack = {
        'ticker': filing['ticker'],
        'form': filing['form'],
        'period_end': filing['period'],
        'filed': filing['filed'],
        'accession': acc,
        'source_url': filing['url'],
        'quarter': {},
        'ytd': {},
        'balance': {},
        'missing': [],
    }
    pe = filing['period']
    for field, tags in DURATION.items():
        q = _pick(company_facts, 'us-gaap', tags, acc, False, pe)
        y = _pick(company_facts, 'us-gaap', tags, acc, False, pe, want_ytd=True)
        pack['quarter'][field] = q
        pack['ytd'][field] = y
        if q is None:
            pack['missing'].append(field)
    for field, tags in INSTANT.items():
        v = _pick(company_facts, 'us-gaap', tags, acc, True, pe)
        pack['balance'][field] = v
        if v is None:
            pack['missing'].append(field)
    for field, tags in DEI_INSTANT.items():
        v = _pick(company_facts, 'dei', tags, acc, True, pe,
                  latest_instant=True)
        pack['balance'][field] = v
        if v is None:
            pack['missing'].append(field)
    return pack


def val(pack, section, field):
    """Value or None, without the caller unpacking dicts everywhere."""
    d = (pack.get(section) or {}).get(field)
    return d['value'] if d else None


def save(pack, path):
    with io.open(path, 'w', encoding='utf-8') as fh:
        json.dump(pack, fh, indent=1, sort_keys=True)
    return path
