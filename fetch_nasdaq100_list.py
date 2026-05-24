#!/usr/bin/env python3
"""One-off: pull current NASDAQ 100 constituents from Wikipedia and write
tickers_nasdaq100.txt (one ticker per line, sorted). Re-runnable safely.
"""

import io
import os
import sys

import pandas as pd
import requests

URL = 'https://en.wikipedia.org/wiki/Nasdaq-100'
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   'tickers_nasdaq100.txt')

print(f'Fetching {URL} ...')
# Wikipedia blocks pandas' default user agent; fetch with a real one.
resp = requests.get(URL, headers={'User-Agent': 'Mozilla/5.0 (us-dashboard)'},
                    timeout=30)
resp.raise_for_status()
tables = pd.read_html(io.StringIO(resp.text))
print(f'  found {len(tables)} tables on the page')

# Find the constituents table: it has a "Ticker" or "Symbol" column.
constituents = None
for t in tables:
    cols = [str(c).strip().lower() for c in t.columns]
    if any(c in ('ticker', 'symbol') for c in cols):
        # And looks the right size (NASDAQ 100 = ~100 rows)
        if 80 <= len(t) <= 120:
            constituents = t
            break

if constituents is None:
    sys.exit('ERROR: could not find the NASDAQ 100 constituents table')

col = next(c for c in constituents.columns if str(c).strip().lower() in ('ticker', 'symbol'))
tickers = sorted({str(t).strip().upper() for t in constituents[col] if pd.notna(t)})
print(f'  extracted {len(tickers)} tickers')

with open(OUT, 'w', encoding='utf-8') as fh:
    fh.write('\n'.join(tickers) + '\n')
print(f'Wrote {OUT}')
