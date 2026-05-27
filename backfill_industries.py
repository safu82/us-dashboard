#!/usr/bin/env python3
"""Fill us_stock_sectors.industry for rows where it is NULL, normalizing
yfinance's Yahoo-style industry labels to the GICS Sub-Industry naming
already in the table (so all rows merge into the same peer-group buckets).

yfinance returns labels like "Software - Application" or "Internet Retail";
this script maps them to the GICS Sub-Industry strings used by S&P 500 data
loaded from Wikipedia (e.g., "Application Software", "Broadline Retail").

Idempotent — only updates rows that are still NULL.
"""

import os
import sys
import time

import yfinance as yf
from supabase import create_client
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

# Maps Yahoo's industry labels to the GICS Sub-Industry strings already used
# by the table (sourced from Wikipedia's S&P 500 list). Extend as needed.
YAHOO_TO_GICS = {
    'Software - Application':         'Application Software',
    'Software - Infrastructure':      'Systems Software',
    'Internet Retail':                'Broadline Retail',
    'Beverages - Non-Alcoholic':      'Soft Drinks & Non-alcoholic Beverages',
    'Beverages - Wineries & Distilleries': 'Distillers & Vintners',
    'Engineering & Construction':     'Construction & Engineering',
    'Semiconductor Equipment & Materials': 'Semiconductor Materials & Equipment',
    'Specialty Business Services':    'Research & Consulting Services',
    'Information Technology Services':'IT Consulting & Other Services',
    'Drug Manufacturers - General':   'Pharmaceuticals',
    'Drug Manufacturers - Specialty & Generic': 'Pharmaceuticals',
    'Medical Devices':                'Health Care Equipment',
    'Medical Instruments & Supplies': 'Health Care Supplies',
    'Diagnostics & Research':         'Life Sciences Tools & Services',
    'Auto Manufacturers':             'Automobile Manufacturers',
    'Auto Parts':                     'Automotive Parts & Equipment',
    'Internet Content & Information': 'Interactive Media & Services',
    'Communication Equipment':        'Communications Equipment',
    'Computer Hardware':              'Technology Hardware, Storage & Peripherals',
    'Consumer Electronics':           'Technology Hardware, Storage & Peripherals',
}


def normalize_industry(yahoo_label):
    """Translate a yfinance industry string to GICS Sub-Industry naming."""
    if not yahoo_label:
        return None
    return YAHOO_TO_GICS.get(yahoo_label.strip(), yahoo_label.strip())


def main():
    sb = create_client(URL, KEY)
    rows = (sb.table('us_stock_sectors')
              .select('ticker,company_name,sector,industry')
              .is_('industry', 'null')
              .execute().data or [])
    print(f'Rows missing industry: {len(rows)}')
    if not rows:
        return

    for r in rows:
        tk = r['ticker']
        try:
            info = yf.Ticker(tk).info or {}
            ind_yahoo = info.get('industry') or info.get('industryDisp')
            ind = normalize_industry(ind_yahoo)
            if not ind:
                print(f'  {tk:6s}  no industry from yfinance, skipping')
                continue
            sb.table('us_stock_sectors').update(
                {'industry': ind}
            ).eq('ticker', tk).execute()
            arrow = f'{ind_yahoo} -> {ind}' if ind_yahoo != ind else ind
            print(f'  {tk:6s}  {r["sector"]:30s} {arrow}')
            time.sleep(0.4)  # be polite to Yahoo
        except Exception as e:
            print(f'  {tk:6s}  ERROR: {e}')

    # Re-check
    remaining = (sb.table('us_stock_sectors').select('ticker', count='exact')
                   .is_('industry', 'null').execute())
    print(f'Done. Still NULL: {remaining.count}')


if __name__ == '__main__':
    main()
