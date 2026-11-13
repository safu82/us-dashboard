"""Stage 0 — the watcher. Finds filings; fetches the numbers as filed.

The important discovery here is that for US filers we need far less model work
than expected. Every 10-Q and 10-K is tagged in XBRL, and the SEC publishes
those tags through a free JSON API. So revenue, share count, minority interest,
cash flow and tax all arrive as *structured data traceable to an accession
number* — no extraction, no hallucination risk, no tokens.

That leaves the model only the things XBRL genuinely does not carry: narrative
guidance, segment commentary, and management's language on the call.

SEC access policy requires a real contact in the User-Agent and allows about
10 requests a second. Set EDGAR_UA before using this, e.g.

    setx EDGAR_UA "overnightarc-research you@example.com"
"""
import io
import json
import os
import re
import time
import urllib.request

UA = os.environ.get('EDGAR_UA', '').strip()
BASE = 'https://data.sec.gov'
TICKER_MAP = 'https://www.sec.gov/files/company_tickers.json'
PAUSE = 0.15          # stay well inside the 10/sec limit


class EdgarError(RuntimeError):
    pass


def _get(url):
    if not UA:
        raise EdgarError(
            'Set EDGAR_UA to "name you@example.com" — the SEC rejects or '
            'throttles requests without a real contact in the User-Agent.')
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept-Encoding': 'gzip, deflate',
        'Host': url.split('/')[2],
    })
    time.sleep(PAUSE)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        if r.headers.get('Content-Encoding') == 'gzip':
            import gzip
            raw = gzip.decompress(raw)
    return json.loads(raw)


_cik_cache = {}


def cik_for(ticker):
    """SEC's own ticker->CIK map. Padded to the 10 digits their API wants."""
    global _cik_cache
    if not _cik_cache:
        data = _get(TICKER_MAP)
        for row in data.values():
            _cik_cache[row['ticker'].upper()] = row['cik_str']
    t = ticker.upper()
    if t not in _cik_cache:
        raise EdgarError(f'{ticker} is not in the SEC ticker map')
    return '%010d' % _cik_cache[t]


def recent_filings(ticker, forms=('10-Q', '10-K', '8-K'), limit=20):
    """Most recent filings, newest first. This is the watcher's eye.

    Cheap enough to poll: one request per ticker, no model, no tokens. A
    scheduled job compares the top accession against what it saw last time.
    """
    cik = cik_for(ticker)
    data = _get(f'{BASE}/submissions/CIK{cik}.json')
    r = data['filings']['recent']
    out = []
    for i in range(len(r['accessionNumber'])):
        if r['form'][i] not in forms:
            continue
        acc = r['accessionNumber'][i]
        # `dir` as well as `url`, because filing_files() and earnings_exhibit()
        # key on it. Only earnings_8k() used to set it, so every other form was
        # unreachable through them: a 6-K raised KeyError: 'dir', which quietly
        # put every foreign private issuer out of reach — Camtek and Nova file
        # 6-K and 20-F, never an 8-K or a 10-Q.
        folder = (f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/'
                  f"{acc.replace('-', '')}")
        out.append({
            'ticker': ticker.upper(),
            'cik': cik,
            'form': r['form'][i],
            'items': (r.get('items') or [''] * len(r['form']))[i],
            'filed': r['filingDate'][i],
            'period': r['reportDate'][i],
            'accession': acc,
            'primary_doc': r['primaryDocument'][i],
            'dir': folder,
            'url': f"{folder}/{r['primaryDocument'][i]}",
        })
        if len(out) >= limit:
            break
    return out


def company_facts(ticker):
    """Every XBRL fact the company has ever filed, by tag."""
    return _get(f'{BASE}/api/xbrl/companyfacts/CIK{cik_for(ticker)}.json')


def earnings_8k(ticker, limit=4):
    """8-Ks announcing results, newest first.

    An 8-K declares which Items it reports, and Item 2.02 is "Results of
    Operations and Financial Condition" — the earnings release. Filtering on
    that is exact and free, where guessing from the filing date would catch
    every unrelated 8-K a company files.
    """
    cik = cik_for(ticker)
    data = _get(f'{BASE}/submissions/CIK{cik}.json')
    r = data['filings']['recent']
    out = []
    for i in range(len(r['accessionNumber'])):
        if r['form'][i] != '8-K':
            continue
        if '2.02' not in (r.get('items') or [''] * len(r['form']))[i]:
            continue
        acc = r['accessionNumber'][i]
        out.append({
            'ticker': ticker.upper(), 'cik': cik, 'form': '8-K',
            'items': r['items'][i],
            'filed': r['filingDate'][i], 'period': r['reportDate'][i],
            'accession': acc, 'primary_doc': r['primaryDocument'][i],
            'dir': (f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/'
                    f"{acc.replace('-', '')}"),
        })
        if len(out) >= limit:
            break
    return out


def filing_files(filing):
    """Every document in a filing's folder."""
    idx = _get(filing['dir'] + '/index.json')
    return (idx.get('directory') or {}).get('item', [])


# An earnings release is filed as exhibit 99.1. Issuers name the file almost
# but not quite consistently, so match a pattern rather than a literal.
EX991 = re.compile(r'ex[-_]?99[._-]?1|exhibit[-_]?99[._-]?1', re.I)

# EDGAR's own index and header pages, which live in the filing folder beside
# the documents and carry no content: 0001178913-26-005000-index.html,
# -index-headers.html, and the accession .txt wrapper.
INDEX_ARTIFACT = re.compile(r'-index(-headers)?\.html?$|^\d{10}-\d{2}-\d{6}\.txt$', re.I)


def earnings_exhibit(filing):
    """The EX-99.1 press release in an earnings 8-K, or None.

    Returns the chosen filename alongside the URL so a note can say which
    document a quote came from, rather than just which filing.
    """
    files = filing_files(filing)
    htm = [f for f in files
           if f['name'].lower().endswith(('.htm', '.html', '.txt'))
           # EDGAR generates its own index and header pages inside every
           # filing folder and reports them at size 0. They were slipping
           # through the fallback below — the exclusion tested for
           # '-index.htm' and they are named '-index.html' — so the "largest
           # remaining exhibit" could be an empty index page. Camtek's 6-K
           # then read as 1,344 characters of cover text instead of the
           # 21,321-character results release underneath it.
           and int(f.get('size') or 0) > 0
           and not INDEX_ARTIFACT.search(f['name'])]
    named = [f for f in htm if EX991.search(f['name'])]
    if not named:
        # Fall back to the largest exhibit that is not the cover itself.
        others = [f for f in htm
                  if f['name'].lower() != filing['primary_doc'].lower()]
        if not others:
            # Nothing but the cover, which is how a 6-K or a 20-F is built:
            # the results are IN the primary document, not beside it. Say so
            # by returning None so the caller reads filing['url'] instead.
            return None
        named = [max(others, key=lambda f: int(f.get('size') or 0))]
    f = named[0]
    return {'name': f['name'], 'size': int(f.get('size') or 0),
            'url': f"{filing['dir']}/{f['name']}",
            'matched_pattern': bool(EX991.search(f['name']))}


def fetch_document(url):
    """Raw bytes of a filing document, decoded as text."""
    if not UA:
        raise EdgarError('Set EDGAR_UA before fetching documents.')
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Accept-Encoding': 'gzip, deflate',
        'Host': url.split('/')[2]})
    time.sleep(PAUSE)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        if r.headers.get('Content-Encoding') == 'gzip':
            import gzip
            raw = gzip.decompress(raw)
    for enc in ('utf-8', 'cp1252', 'latin-1'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')


def save(obj, path):
    with io.open(path, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, indent=1, sort_keys=True, default=str)
    return path


if __name__ == '__main__':
    import sys
    for t in (sys.argv[1:] or ['GOOGL']):
        print('\n=== %s ===' % t.upper())
        for f in recent_filings(t, forms=('10-Q', '10-K'), limit=4):
            print('  %-5s filed %s  period %s  %s'
                  % (f['form'], f['filed'], f['period'], f['accession']))
