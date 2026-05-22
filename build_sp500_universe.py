#!/usr/bin/env python3
"""Build the S&P 500 universe + GICS sector map.

- Fetches the current S&P 500 constituent list from Wikipedia, which carries
  the official GICS Sector and GICS Sub-Industry columns — one canonical
  taxonomy from day one (no free-text sector drift).
- Writes tickers_sp500.txt (Yahoo-format tickers, one per line) — the scan
  universe file.
- Upserts us_stock_sectors with ticker / company / GICS sector / sub-industry,
  plus the portfolio-held tickers that are not S&P 500 members so the
  dashboard and scanners can classify everything held.

Re-runnable — S&P 500 membership drifts over time.

Reads SUPABASE_URL + SUPABASE_SERVICE_KEY from env or a .env file in this
directory.
"""

import os
import sys
from io import StringIO
from datetime import datetime, timezone

import requests
import pandas as pd
from supabase import create_client
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: set SUPABASE_URL and SUPABASE_SERVICE_KEY in env or .env')
if 'hcgyncghmcvylnrmcivj' in URL:
    sys.exit('ERROR: SUPABASE_URL points at the India project — use the US project.')

WIKI = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# Portfolio-held tickers that are not S&P 500 members — added so everything
# held can be classified. Skipped automatically if already an index member.
EXTRAS = [
    ('ARM',  'ARM Holdings plc',                       'Information Technology', 'Semiconductors'),
    ('TSM',  'Taiwan Semiconductor Manufacturing',     'Information Technology', 'Semiconductors'),
    ('SNDK', 'SanDisk Corporation',                    'Information Technology', 'Semiconductors'),
    ('QQQ',  'Invesco QQQ Trust',                      'ETF',                    'Equity ETF'),
]


def yahoo_ticker(sym):
    """Wikipedia uses BRK.B / BF.B; Yahoo uses BRK-B / BF-B."""
    return sym.strip().upper().replace('.', '-')


print('Fetching S&P 500 constituents from Wikipedia...')
resp = requests.get(WIKI, headers={'User-Agent': UA}, timeout=30)
resp.raise_for_status()
df = pd.read_html(StringIO(resp.text))[0]   # first table = constituents

now = datetime.now(timezone.utc).isoformat()
rows = []
seen = set()
for _, r in df.iterrows():
    tk = yahoo_ticker(str(r['Symbol']))
    if tk in seen:
        continue
    seen.add(tk)
    rows.append({
        'ticker': tk,
        'company_name': str(r['Security']),
        'sector': str(r['GICS Sector']),
        'industry': str(r['GICS Sub-Industry']),
        'updated_at': now,
    })

sp500_tickers = sorted(seen)
print(f'  {len(sp500_tickers)} S&P 500 constituents')

# Universe file (pure S&P 500 — OHLC fetcher unions this with held extras)
with open(os.path.join(BASE, 'tickers_sp500.txt'), 'w', encoding='utf-8') as f:
    f.write('\n'.join(sp500_tickers) + '\n')
print(f'  tickers_sp500.txt written')

added_extras = []
for tk, name, sec, ind in EXTRAS:
    if tk not in seen:
        seen.add(tk)
        rows.append({'ticker': tk, 'company_name': name, 'sector': sec,
                     'industry': ind, 'updated_at': now})
        added_extras.append(tk)
print(f'  held extras added (not S&P 500 members): {added_extras or "none"}')

print(f'Upserting {len(rows)} rows into us_stock_sectors...')
sb = create_client(URL, KEY)
for i in range(0, len(rows), 100):
    sb.table('us_stock_sectors').upsert(rows[i:i + 100], on_conflict='ticker').execute()

count = sb.table('us_stock_sectors').select('ticker', count='exact').execute().count
print(f'Done. us_stock_sectors now has {count} rows.')
