#!/usr/bin/env python3
"""Build the expanded ~1,900-name US scan universe (hybrid S&P 1500 + NASDAQ overlay).

DRY-RUN BY DEFAULT: scrapes the S&P lists, filters + ranks the NASDAQ overlay,
prints the full breakdown, and writes a preview CSV next to this script — but
does NOT touch Supabase. Pass --write to upsert into us_stock_sectors.

Universe =
  S&P Composite 1500   S&P 500 + MidCap 400 + SmallCap 600
                       (GICS Sector + Sub-Industry scraped from Wikipedia)
  + ~400 NASDAQ overlay NASDAQ-listed common stocks NOT in the S&P core, ranked
                       by median daily dollar-volume; these are the names S&P
                       structurally excludes (foreign ADRs, recent IPOs,
                       unprofitable growth). GICS mapped from yfinance sector.
  + held extras        portfolio tickers outside the above.

Each row carries a `source` tag: sp500 / sp400 / sp600 / nasdaq_overlay / held.

See memory project-universe-expansion for the decision rationale.

Usage:
  python build_universe.py                       # full dry run (~15-30 min: the
                                                 #   overlay rank downloads ~3k tickers)
  python build_universe.py --limit-candidates 200  # FAST dry run to validate the
                                                 #   pipeline before the full pass
  python build_universe.py --overlay 400         # set overlay size (default 400)
  python build_universe.py --with-gics           # also fetch overlay GICS in dry run
  python build_universe.py --write               # COMMIT to us_stock_sectors

Env (only needed for --write, or to union live holdings): SUPABASE_URL,
SUPABASE_SERVICE_KEY in a .env in this directory.
"""

import argparse
import os
import re
import sys
import time
from io import StringIO
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# S&P Composite 1500 — three Wikipedia constituent tables, each id="constituents"
# and each carrying GICS Sector + GICS Sub-Industry columns.
SP_PAGES = {
    'sp500': 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies',
    'sp400': 'https://en.wikipedia.org/wiki/List_of_S%26P_400_companies',
    'sp600': 'https://en.wikipedia.org/wiki/List_of_S%26P_600_companies',
}

# NASDAQ Trader symbol directory (pipe-delimited, refreshed nightly).
NASDAQ_LISTED_URL = 'https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt'

OVERLAY_TARGET = 400        # how many NASDAQ-overlay names to keep
LOOKBACK = '3mo'            # window for the median-dollar-volume ranking
MIN_HISTORY_BARS = 45       # require ~3 months of bars (of ~63 in the window) so
                            # brand-new listings (e.g. a 10-day-old IPO) that
                            # can't feed the EMA200/weekly-RSI/RS stack are
                            # excluded — they re-qualify once seasoned.
BATCH_SIZE = 50             # tickers per yf.download call
SLEEP_BATCH = 2            # seconds between download batches

# Non-common issue types to drop from the NASDAQ list. NOTE: we deliberately do
# NOT drop "Depositary" — American Depositary Shares (ADRs) are exactly the
# foreign names we want in the overlay.
NONCOMMON_RE = re.compile(
    r'\b(warrant|warrants|unit|units|right|rights|preferred|preferreds|'
    r'debenture|debentures|notes?|convertible|when[- ]issued|'
    r'contingent value|subordinated)\b', re.I)

# Yahoo `.info` sector strings -> the 11 canonical GICS sectors used in
# us_stock_sectors (so the overlay classifies consistently with the S&P core).
YF_SECTOR_TO_GICS = {
    'Technology': 'Information Technology',
    'Healthcare': 'Health Care',
    'Financial Services': 'Financials',
    'Consumer Cyclical': 'Consumer Discretionary',
    'Consumer Defensive': 'Consumer Staples',
    'Industrials': 'Industrials',
    'Energy': 'Energy',
    'Basic Materials': 'Materials',
    'Real Estate': 'Real Estate',
    'Utilities': 'Utilities',
    'Communication Services': 'Communication Services',
}

# Portfolio-held tickers outside the indices — curated with GICS so everything
# held stays classified even if it never enters the S&P/NASDAQ cuts.
EXTRAS = [
    ('ARM',  'ARM Holdings plc',                   'Information Technology', 'Semiconductors'),
    ('TSM',  'Taiwan Semiconductor Manufacturing', 'Information Technology', 'Semiconductors'),
    ('SNDK', 'SanDisk Corporation',                'Information Technology', 'Semiconductors'),
    ('QQQ',  'Invesco QQQ Trust',                  'ETF',                    'Equity ETF'),
]


def yahoo_ticker(sym):
    """Wikipedia/NASDAQ use BRK.B; Yahoo uses BRK-B."""
    return str(sym).strip().upper().replace('.', '-')


def _pick(df, *cands):
    for c in cands:
        if c in df.columns:
            return c
    raise KeyError(f'none of {cands} in columns {list(df.columns)}')


def fetch_sp_constituents(source, url):
    """Scrape one S&P constituents table -> list of normalized rows."""
    r = requests.get(url, headers={'User-Agent': UA}, timeout=30)
    r.raise_for_status()
    tables = pd.read_html(StringIO(r.text), attrs={'id': 'constituents'})
    df = tables[0]
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[-1] if isinstance(c, tuple) else c for c in df.columns]
    t_col = _pick(df, 'Symbol', 'Ticker symbol', 'Ticker')
    n_col = _pick(df, 'Security', 'Company', 'Company Name', 'Name')
    s_col = _pick(df, 'GICS Sector', 'Sector')
    i_col = _pick(df, 'GICS Sub-Industry', 'GICS Sub Industry', 'Sub-Industry', 'Industry')
    rows = []
    for _, row in df.iterrows():
        rows.append({
            'ticker': yahoo_ticker(row[t_col]),
            'company_name': str(row[n_col]).strip(),
            'sector': str(row[s_col]).strip(),
            'industry': str(row[i_col]).strip(),
            'source': source,
        })
    return rows


def build_core():
    """S&P 500 -> 400 -> 600, deduped (precedence in that order)."""
    core, seen = [], set()
    for source, url in SP_PAGES.items():
        print(f'  scraping {source} ...', end=' ', flush=True)
        rows = fetch_sp_constituents(source, url)
        added = 0
        for row in rows:
            if row['ticker'] in seen:
                continue
            seen.add(row['ticker'])
            core.append(row)
            added += 1
        print(f'{added} new (raw {len(rows)})')
    return core, seen


def _nasdaq_text_via_https():
    r = requests.get(NASDAQ_LISTED_URL, headers={'User-Agent': UA}, timeout=30)
    r.raise_for_status()
    text = r.text or ''
    first = text.splitlines()[0] if text.strip() else ''
    if 'Symbol|Security Name' not in first:        # bot-block page returns HTML
        raise ValueError(f'unexpected response (header={first[:80]!r})')
    return text


def _nasdaq_text_via_ftp():
    from ftplib import FTP
    ftp = FTP('ftp.nasdaqtrader.com', timeout=30)
    ftp.login()
    lines = []
    ftp.retrlines('RETR SymbolDirectory/nasdaqlisted.txt', lines.append)
    ftp.quit()
    if not lines or 'Symbol|Security Name' not in lines[0]:
        raise ValueError('unexpected FTP content')
    return '\n'.join(lines)


def _fetch_nasdaq_listed_df():
    """Download + parse nasdaqlisted.txt. The HTTPS endpoint intermittently
    serves a bot-block HTML page when rate-limited, so we validate the header
    and fall back to the canonical FTP mirror (identical pipe format)."""
    text = None
    for attempt in range(1, 3):
        try:
            text = _nasdaq_text_via_https()
            break
        except Exception as e:
            print(f'  NASDAQ HTTPS attempt {attempt} failed: {e}')
            time.sleep(3 * attempt)
    if text is None:
        try:
            print('  falling back to NASDAQ FTP mirror ...')
            text = _nasdaq_text_via_ftp()
        except Exception as e:
            raise RuntimeError(f'NASDAQ list unavailable via HTTPS and FTP: {e}')
    df = pd.read_csv(StringIO(text), sep='|', dtype=str)
    if 'Symbol' not in df.columns:
        raise ValueError(f'no Symbol column; got {list(df.columns)}')
    return df


def fetch_nasdaq_candidates(core_set):
    """NASDAQ-listed common stocks not already in the S&P core."""
    df = _fetch_nasdaq_listed_df()
    # Drop the trailing "File Creation Time:" footer row.
    df = df[~df['Symbol'].astype(str).str.contains('File Creation', na=False)]
    # Structured flags: normal, non-test, non-ETF.
    df = df[(df.get('Test Issue') == 'N') &
            (df.get('ETF') == 'N') &
            (df.get('Financial Status', 'N') == 'N')]
    cands = []
    for _, row in df.iterrows():
        sym = str(row['Symbol']).strip()
        name = str(row.get('Security Name', ''))
        if not sym or NONCOMMON_RE.search(name):
            continue
        if not re.fullmatch(r'[A-Z]{1,5}', sym):   # plain common-stock symbols only
            continue
        tk = yahoo_ticker(sym)
        if tk in core_set:
            continue
        cands.append((tk, name.split(' - ')[0].strip()))
    # De-dup, keep first name seen.
    out, seen = [], set()
    for tk, name in cands:
        if tk not in seen:
            seen.add(tk)
            out.append((tk, name))
    return out


def _extract_cv(hist, ticker):
    if hist is None or hist.empty:
        return None
    cols = hist.columns
    if isinstance(cols, pd.MultiIndex):
        if ticker in cols.get_level_values(0):
            df = hist[ticker]
        else:
            return None
    else:
        df = hist
    if 'Close' not in df.columns or 'Volume' not in df.columns:
        return None
    return df[['Close', 'Volume']].dropna()


def rank_by_dollar_volume(candidates):
    """Median daily dollar-volume over LOOKBACK; returns [(ticker, $vol)] desc."""
    syms = [t for t, _ in candidates]
    dv = {}
    too_new = 0
    n_batches = (len(syms) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(syms), BATCH_SIZE):
        batch = syms[i:i + BATCH_SIZE]
        bnum = i // BATCH_SIZE + 1
        print(f'  ranking batch {bnum}/{n_batches} ({len(batch)}) ...', end=' ', flush=True)
        try:
            hist = yf.download(batch, period=LOOKBACK, interval='1d',
                               auto_adjust=False, progress=False,
                               threads=True, group_by='ticker')
        except Exception as e:
            print(f'download error: {e}')
            continue
        got = 0
        for t in batch:
            sub = _extract_cv(hist, t)
            if sub is None:
                continue
            if len(sub) < MIN_HISTORY_BARS:   # too new to feed the indicator stack
                too_new += 1
                continue
            med = (sub['Close'] * sub['Volume']).median()
            if pd.notna(med) and med > 0:
                dv[t] = float(med)
                got += 1
        print(f'{got} priced')
        if i + BATCH_SIZE < len(syms):
            time.sleep(SLEEP_BATCH)
    print(f'  ({too_new} candidates dropped: < {MIN_HISTORY_BARS} bars of history)')
    return sorted(dv.items(), key=lambda kv: kv[1], reverse=True)


def map_overlay_gics(tickers):
    """yfinance .info sector -> GICS for the chosen overlay names (slow: 1 call each)."""
    out = {}
    for j, t in enumerate(tickers, 1):
        sector, industry, name = 'Unknown', None, None
        try:
            info = yf.Ticker(t).info or {}
            sector = YF_SECTOR_TO_GICS.get(info.get('sector'), info.get('sector') or 'Unknown')
            industry = info.get('industry')
            name = info.get('shortName') or info.get('longName')
        except Exception:
            pass
        out[t] = (sector, industry, name)
        if j % 25 == 0:
            print(f'    gics {j}/{len(tickers)} ...', flush=True)
        time.sleep(0.3)
    return out


def read_held():
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_KEY')
    if not url or not key:
        return set()
    try:
        from supabase import create_client
        sb = create_client(url, key)
        return {h['ticker'] for h in (sb.table('holdings').select('ticker')
                .execute().data or []) if h.get('ticker')}
    except Exception as e:
        print(f'  WARN: could not read holdings ({e}); using EXTRAS only')
        return set()


def write_to_supabase(rows):
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_KEY')
    if not url or not key:
        sys.exit('ERROR: --write needs SUPABASE_URL + SUPABASE_SERVICE_KEY')
    if 'hcgyncghmcvylnrmcivj' in url:
        sys.exit('ERROR: SUPABASE_URL points at the India project — use the US project.')
    from supabase import create_client
    sb = create_client(url, key)
    now = datetime.now(timezone.utc).isoformat()
    payload = [{'ticker': r['ticker'], 'company_name': r['company_name'],
                'sector': r['sector'], 'industry': r['industry'],
                'source': r['source'], 'updated_at': now} for r in rows]
    for i in range(0, len(payload), 100):
        sb.table('us_stock_sectors').upsert(payload[i:i + 100], on_conflict='ticker').execute()
    cnt = sb.table('us_stock_sectors').select('ticker', count='exact').execute().count
    print(f'Wrote {len(payload)} rows. us_stock_sectors now has {cnt} rows.')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true', help='commit to us_stock_sectors')
    ap.add_argument('--overlay', type=int, default=OVERLAY_TARGET, help='overlay size')
    ap.add_argument('--with-gics', action='store_true', help='fetch overlay GICS even in dry run')
    ap.add_argument('--limit-candidates', type=int, default=0,
                    help='cap NASDAQ candidates ranked (fast smoke test; 0 = all)')
    args = ap.parse_args()
    dry = not args.write

    print('=' * 72)
    print('BUILD UNIVERSE  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
          '  [DRY RUN]' if dry else '  [WRITE]')
    print('=' * 72)

    print('S&P Composite 1500:')
    core, core_set = build_core()
    print(f'  core total (deduped): {len(core)}')

    print('NASDAQ overlay:')
    candidates = fetch_nasdaq_candidates(core_set)
    print(f'  {len(candidates)} NASDAQ common-stock candidates (post-filter, not in core)')
    if args.limit_candidates and args.limit_candidates < len(candidates):
        candidates = candidates[:args.limit_candidates]
        print(f'  --limit-candidates: ranking only the first {len(candidates)}')
    name_map = dict(candidates)
    ranked = rank_by_dollar_volume(candidates)
    chosen = ranked[:args.overlay]
    print(f'  selected top {len(chosen)} by median daily dollar-volume')

    # GICS enrichment for the overlay (always on --write; optional in dry run).
    gics = {}
    if args.write or args.with_gics:
        print('  mapping overlay GICS via yfinance ...')
        gics = map_overlay_gics([t for t, _ in chosen])

    overlay_rows = []
    for t, dv in chosen:
        sector, industry, nm = gics.get(t, ('(pending yfinance)', None, None))
        overlay_rows.append({
            'ticker': t, 'company_name': nm or name_map.get(t, t),
            'sector': sector, 'industry': industry,
            'source': 'nasdaq_overlay', 'dollar_volume': round(dv),
        })

    # Held extras (curated EXTRAS + any live holdings outside core/overlay).
    universe_set = core_set | {r['ticker'] for r in overlay_rows}
    held_rows = []
    for tk, nm, sec, ind in EXTRAS:
        if tk not in universe_set:
            held_rows.append({'ticker': tk, 'company_name': nm, 'sector': sec,
                              'industry': ind, 'source': 'held', 'dollar_volume': None})
            universe_set.add(tk)
    for tk in sorted(read_held() - universe_set):
        held_rows.append({'ticker': tk, 'company_name': tk, 'sector': 'Unknown',
                          'industry': None, 'source': 'held', 'dollar_volume': None})
        universe_set.add(tk)

    all_rows = ([dict(r, dollar_volume=None) for r in core] + overlay_rows + held_rows)

    # ── Summary ──────────────────────────────────────────────────────────────
    print('-' * 72)
    print('BREAKDOWN')
    by_source = {}
    for r in all_rows:
        by_source[r['source']] = by_source.get(r['source'], 0) + 1
    for s in ('sp500', 'sp400', 'sp600', 'nasdaq_overlay', 'held'):
        if by_source.get(s):
            print(f'  {s:16s} {by_source[s]:>5}')
    print(f'  {"TOTAL":16s} {len(all_rows):>5}')

    print('Core sector distribution (GICS):')
    sec_dist = {}
    for r in core:
        sec_dist[r['sector']] = sec_dist.get(r['sector'], 0) + 1
    for s, n in sorted(sec_dist.items(), key=lambda kv: kv[1], reverse=True):
        print(f'  {s:28s} {n:>5}')

    print(f'Top 25 NASDAQ overlay by median daily $-volume (of {len(chosen)} chosen):')
    for r in overlay_rows[:25]:
        print(f'  {r["ticker"]:8s} ${r["dollar_volume"]:>15,}  {r["company_name"][:40]}')

    # Preview CSV next to the script.
    csv_path = os.path.join(BASE, 'universe_preview.csv')
    pd.DataFrame(all_rows, columns=['ticker', 'company_name', 'sector',
                 'industry', 'source', 'dollar_volume']).to_csv(csv_path, index=False)
    print('-' * 72)
    print(f'Preview CSV: {csv_path}  ({len(all_rows)} rows)')

    if dry:
        print('DRY RUN — nothing written to Supabase. Re-run with --write to commit.')
    else:
        write_to_supabase(all_rows)


if __name__ == '__main__':
    main()
