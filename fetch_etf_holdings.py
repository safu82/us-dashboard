#!/usr/bin/env python3
"""ETF holdings, as deep as each issuer will give them.

Sources, in the order they are tried, because they give very different depth:

  SSGA (the SPDR funds)  complete daily holdings file. XLK comes back with all
                         166 positions and their weights.
  ARK                    complete daily CSV per fund.
  VanEck                 complete XLSX. SMH is the single most-found page on
                         the site — 868 impressions in six days on "smh
                         holdings" alone — and it was being served ten names
                         from Yahoo against the twenty-eight it holds.
  iShares                complete CSV, keyed by numeric product id.
  Yahoo (the rest)       only the top 10, covering roughly 60% of a fund by
                         weight. There is no deeper endpoint.

That difference is recorded per fund in `complete`, and it matters downstream:
with a full list we can say a name is NOT in a fund; with a top 10 we can only
ever say it IS one of the largest. Claiming absence from a top-10 list would be
wrong for every name in the fund's tail.

Every issuer here was found by asking the fund page what its own download link
is, not by guessing a URL. A guessed endpoint is the dangerous kind of wrong:
it returns an HTML page with a 200, parses to zero rows, and the fund silently
reads as holding nothing. So each parser returns None unless it found both a
ticker and a weight column, and the caller falls through to the next source.

Global X (8 funds) and First Trust (5) stay on Yahoo. Global X renders only
its top 10 into the page and keeps the rest behind an interaction; First
Trust's holdings page has thirteen tables and none of them carries a usable
header. Both are worth another look, neither is worth a brittle scraper.

Writes etf_holdings. Also reports how much of each fund sits outside our scan
universe, which is the input to deciding whether to widen it.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Usage: python fetch_etf_holdings.py [--report-only]
"""

import io
import os
import sys
import urllib.request
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv
from supabase import create_client

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))
URL, KEY = os.environ.get('SUPABASE_URL'), os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')
sb = create_client(URL, KEY)

SSGA = ('https://www.ssga.com/us/en/intermediary/library-content/products/'
        'fund-data/etfs/us/holdings-daily-us-en-{}.xlsx')
# ARK publish a plain daily CSV per fund at a stable path. Most other issuers
# (Global X, First Trust, iShares, Invesco) put their download behind a page
# that returns HTML to a plain request, so they would each need their own
# scraper; those fall through to Yahoo's top 10 below.
ARK = {
    'ARKF': 'ARK_FINTECH_INNOVATION_ETF_ARKF_HOLDINGS.csv',
    'ARKG': 'ARK_GENOMIC_REVOLUTION_ETF_ARKG_HOLDINGS.csv',
    'ARKQ': 'ARK_AUTONOMOUS_TECH._%26_ROBOTICS_ETF_ARKQ_HOLDINGS.csv',
    'ARKX': 'ARK_SPACE_EXPLORATION_%26_INNOVATION_ETF_ARKX_HOLDINGS.csv',
}
ARK_BASE = 'https://assets.ark-funds.com/fund-documents/funds-etf-csv/'
ARK_MAX_AGE_DAYS = 14
UA = {'User-Agent': 'Mozilla/5.0'}

# VanEck's own fund page links to this path. The segment after /etf/ is the
# asset class and it is not the same for every fund: SMH and MOO answer under
# /equity/, NLR and REMX answer HTML under every segment tried, so those two
# fall through to Yahoo rather than being guessed at.
VANECK = 'https://www.vaneck.com/us/en/etf/equity/{}/holdings/download/xlsx/'

# iShares serves latest-holdings.csv off the numeric product id; the slug in
# the middle of the path is ignored entirely, so only the id has to be right.
# These came from the product screener at
#   /us/product-screener/product-screener-v3.1.jsn
# which lists all 527 products. Hard-coded rather than fetched because that
# call is 1.9MB for seven lookups, and a fund missing from here falls through
# to Yahoo exactly as it does today.
#
# SLV is deliberately absent: it is a silver trust holding metal, not equities,
# and the endpoint 400s for it.
ISHARES = {'IBB': '239699', 'SOXX': '239705', 'IWM': '239710',
           'ICLN': '239738', 'IGV': '239771', 'ITA': '239502'}

SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                  ' (KHTML, like Gecko) Chrome/131.0 Safari/537.36',
    'Accept': '*/*', 'Accept-Language': 'en-US,en;q=0.9'})
# A ticker with a dot-suffix or non-letter characters is a foreign listing
# (6954.T, BHP.AX, CCO.TO) and will never be in a US scan universe.
US_LIKE = lambda t: t.isalpha() or (t.replace('-', '').isalpha() and len(t) <= 6)


def ssga_full(sym):
    """Complete holdings from the issuer, or None if this is not an SSGA fund."""
    try:
        req = urllib.request.Request(SSGA.format(sym.lower()), headers=UA)
        raw = urllib.request.urlopen(req, timeout=30).read()
    except Exception:
        return None
    for skip in (3, 4, 5, 6):
        try:
            df = pd.read_excel(io.BytesIO(raw), skiprows=skip)
        except Exception:
            continue
        cols = {str(c).strip(): c for c in df.columns}
        if 'Ticker' in cols and 'Weight' in cols:
            out = []
            for _, r in df.iterrows():
                t = str(r[cols['Ticker']]).strip().upper()
                w = pd.to_numeric(r[cols['Weight']], errors='coerce')
                if t and t not in ('NAN', '-', '') and pd.notna(w):
                    out.append((t, float(w) / 100.0))
            return out or None
    return None


def ark_full(sym):
    """Complete holdings from ARK's daily CSV, or None if not an ARK fund."""
    if sym not in ARK:
        return None
    try:
        req = urllib.request.Request(ARK_BASE + ARK[sym], headers=UA)
        txt = urllib.request.urlopen(req, timeout=30).read().decode('utf-8', 'replace')
        df = pd.read_csv(io.StringIO(txt))
    except Exception:
        return None
    cols = {str(c).strip().lower(): c for c in df.columns}
    if 'ticker' not in cols or 'weight (%)' not in cols:
        return None
    # ARK's file is only useful if it is current. It carries its own as-of date,
    # and a stale file would quietly mark live holdings as "not held".
    if 'date' in cols:
        d = pd.to_datetime(df[cols['date']].iloc[0], errors='coerce', format='mixed')
        if pd.notna(d) and (pd.Timestamp.today() - d).days > ARK_MAX_AGE_DAYS:
            print(f'      (ARK file dated {d.date()} — too old, falling back)', end=' ')
            return None
    out = []
    for _, r in df.iterrows():
        t = str(r[cols['ticker']]).strip().upper()
        # Weights arrive as strings like "9.65%".
        w = pd.to_numeric(str(r[cols['weight (%)']]).replace('%', '').strip(),
                          errors='coerce')
        if t and t not in ('NAN', '-', '') and pd.notna(w):
            out.append((t, float(w) / 100.0))
    return out or None


def _weights(df, tick_col, wt_col):
    """Rows of (TICKER, fraction) from a frame, or None if nothing parsed.

    Weights arrive as "17.42%" from VanEck and as 17.42 from iShares, so the
    percent sign is stripped and the result divided either way. A row without
    both a ticker and a number is dropped rather than defaulted: a holding at
    weight zero and a holding we failed to read must not look alike.
    """
    out = []
    for _, r in df.iterrows():
        t = str(r[tick_col]).strip().upper()
        w = pd.to_numeric(str(r[wt_col]).replace('%', '').replace(',', '').strip(),
                          errors='coerce')
        if t and t not in ('NAN', '-', '', '--') and pd.notna(w) and w > 0:
            out.append((t, float(w) / 100.0))
    return out or None


def _find_cols(df):
    """The ticker and weight columns, by name, or (None, None)."""
    tick = wt = None
    for c in df.columns:
        n = str(c).strip().lower()
        if tick is None and n in ('ticker', 'symbol', 'holding ticker'):
            tick = c
        if wt is None and ('% of net assets' in n or n.startswith('weight')
                           or n == 'net assets (%)'):
            wt = c
    return tick, wt


def vaneck_full(sym):
    """Complete holdings from VanEck's XLSX, or None if this is not one."""
    try:
        r = SESSION.get(VANECK.format(sym.lower()), timeout=40)
        # An unknown fund answers 200 with the marketing page, not a 404.
        if r.status_code != 200 or r.content[:4] != b'PK\x03\x04':
            return None
        df = pd.read_excel(io.BytesIO(r.content), skiprows=2)
    except Exception:
        return None
    tick, wt = _find_cols(df)
    if tick is None or wt is None:
        return None
    if 'Asset Class' in df.columns:
        df = df[df['Asset Class'].astype(str).str.contains('Stock|Equity', na=False)]
    return _weights(df, tick, wt)


def ishares_full(sym):
    """Complete holdings from iShares' CSV, or None if this is not one."""
    pid = ISHARES.get(sym)
    if not pid:
        return None
    try:
        r = SESSION.get(f'https://www.ishares.com/us/products/{pid}/x/'
                        f'latest-holdings.csv', timeout=40)
        if r.status_code != 200:
            return None
        txt = r.content.decode('utf-8-sig', 'replace')
    except Exception:
        return None
    # Eight lines of fund-level preamble sit above the header, and the count
    # has changed before, so the header is searched for rather than assumed.
    for skip in range(0, 14):
        try:
            df = pd.read_csv(io.StringIO(txt), skiprows=skip)
        except Exception:
            continue
        tick, wt = _find_cols(df)
        if tick is None or wt is None:
            continue
        if 'Asset Class' in df.columns:
            df = df[df['Asset Class'].astype(str).str.contains('Equity', na=False)]
        return _weights(df, tick, wt)
    return None


def yahoo_top(sym):
    try:
        h = yf.Ticker(sym).funds_data.top_holdings
        if h is None or not len(h):
            return None
        return [(str(i).strip().upper(), float(w))
                for i, w in zip(h.index, h['Holding Percent'])]
    except Exception:
        return None


def main():
    etfs = sb.table('etfs').select('symbol,theme_slug,benchmark_only').execute().data or []
    etfs = [e for e in etfs if not e.get('benchmark_only')]

    universe = set()
    frm = 0
    while True:
        rows = sb.table('us_stock_sectors').select('ticker').range(frm, frm + 999).execute().data or []
        universe.update(r['ticker'] for r in rows)
        if len(rows) < 1000:
            break
        frm += 1000

    print('=' * 78)
    print('ETF HOLDINGS  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 78)
    print(f'scan universe: {len(universe)} tickers\n')
    print(f'{"ETF":<6}{"src":<7}{"held":>5}{"cover":>7}{"inUni":>7}{"US-not-in-uni":>14}  examples')

    rows, stats = [], {'full': 0, 'top10': 0, 'none': 0}
    missing_us = {}
    today = datetime.now(timezone.utc).date().isoformat()
    for e in sorted(etfs, key=lambda r: r['symbol']):
        sym = e['symbol']
        hold = (ssga_full(sym) or ark_full(sym)
                or vaneck_full(sym) or ishares_full(sym))
        complete = hold is not None
        if hold is None:
            hold, complete = yahoo_top(sym), False
        if not hold:
            stats['none'] += 1
            print(f'{sym:<6}{"-":<7}{"-":>5}{"-":>7}{"-":>7}{"-":>14}  no holdings published')
            continue
        stats['full' if complete else 'top10'] += 1

        cover = sum(w for _, w in hold)
        in_uni = [t for t, _ in hold if t in universe]
        gap = [t for t, _ in hold if t not in universe and US_LIKE(t)]
        for t in gap:
            missing_us[t] = missing_us.get(t, 0) + 1
        print(f'{sym:<6}{"issuer" if complete else "top10":<7}{len(hold):>5}{cover*100:>6.0f}%'
              f'{len(in_uni):>7}{len(gap):>14}  {", ".join(gap[:5])}')

        for t, w in hold:
            rows.append({'etf': sym, 'ticker': t, 'weight': round(w, 6),
                         'complete': complete, 'as_of': today})

    print(f'\nsources: {stats["full"]} full issuer files · {stats["top10"]} Yahoo top-10 · '
          f'{stats["none"]} unavailable')
    print(f'distinct US-looking tickers held but NOT in our universe: {len(missing_us)}')
    if missing_us:
        top = sorted(missing_us.items(), key=lambda kv: -kv[1])[:25]
        print('  most widely held:', ', '.join(f'{t}({n})' for t, n in top))

    if '--report-only' in sys.argv:
        print('\n(report only — nothing written)')
        return
    for i in range(0, len(rows), 500):
        sb.table('etf_holdings').upsert(rows[i:i + 500], on_conflict='etf,ticker').execute()
    print(f'\nupserted {len(rows)} holding rows.')

    # A fund's holdings are a snapshot, not an accumulation. Upsert alone
    # never removes anything, so a fund that just moved from Yahoo's top 10 to
    # a full issuer file keeps its ten old rows alongside the new ones, and a
    # name the fund has since sold stays listed for ever. Every row written
    # above carries today's as_of; anything older, for a fund we actually
    # refreshed this run, is a leftover.
    #
    # Scoped to `touched` deliberately. A fund whose fetch failed wrote no
    # rows at all, and deleting by date alone would empty it rather than leave
    # yesterday's answer standing.
    touched = sorted({r['etf'] for r in rows})
    if touched:
        gone = sb.table('etf_holdings').delete() \
            .in_('etf', touched).lt('as_of', today).execute()
        print(f'removed {len(gone.data or [])} rows that this run did not refresh.')


if __name__ == '__main__':
    main()
