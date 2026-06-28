#!/usr/bin/env python3
"""Ticker / sector news -> ticker_news.json.

Explains WHY the names/sectors flagged by detect_market_events.py moved, using
Alpha Vantage NEWS_SENTIMENT (per-ticker relevance + sentiment) behind the
swappable provider interface.

Alpha Vantage treats multiple `tickers` as an AND/intersection (NVDA,DFTX returns
only articles mentioning BOTH), so tickers MUST be queried one at a time. Free tier
= 25 req/day, so we cap at MAX_TICKERS individual calls + TOP_THEMES topic calls
(~14 total) — fits the daily budget at a weekly cadence.

Usage: python fetch_ticker_news.py [max_tickers]   (default 12; pass small N to test)

Input: market_events.json (from detect_market_events.py).
Output: ticker_news.json for the LLM synthesis layer.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

from news_providers import get_provider

BASE = os.path.dirname(os.path.abspath(__file__))

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

MAX_TICKERS = 12       # individual AV calls (override via argv for cheap testing)
TOP_THEMES = 2
PER_TICKER = 3
PER_SECTOR = 4
MIN_RELEVANCE = 0.1
AV_GAP = 13.0          # seconds between Alpha Vantage calls (free tier = 5/min)

# Our GICS sectors -> Alpha Vantage NEWS_SENTIMENT topic slugs.
SECTOR_TO_AV_TOPIC = {
    'Information Technology': 'technology',
    'Communication Services': 'technology',
    'Health Care': 'life_sciences',
    'Financials': 'finance',
    'Energy': 'energy_transportation',
    'Utilities': 'energy_transportation',
    'Industrials': 'manufacturing',
    'Materials': 'manufacturing',
    'Real Estate': 'real_estate',
    'Consumer Discretionary': 'retail_wholesale',
    'Consumer Staples': 'retail_wholesale',
}


def main():
    max_tickers = int(sys.argv[1]) if len(sys.argv) > 1 else MAX_TICKERS
    ev_path = os.path.join(BASE, 'market_events.json')
    if not os.path.exists(ev_path):
        sys.exit('market_events.json not found — run detect_market_events.py first')
    ev = json.load(open(ev_path, encoding='utf-8'))
    events = ev.get('events', [])
    week_ago = ev.get('week_ago', '')          # recency floor (AV returns latest)

    # Tickers from stock/earnings events (dedupe, keep highest-impact order).
    tickers = []
    for e in events:
        if e.get('ticker') and e['kind'] in ('stock', 'earnings') and e['ticker'] not in tickers:
            tickers.append(e['ticker'])
    tickers = tickers[:max_tickers]

    themes = [e['sector'] for e in events
              if e['kind'] == 'theme' and e.get('sector')][:TOP_THEMES]

    av = get_provider('alphavantage')
    by_ticker, sector_news = {}, {}

    # 1) One call PER ticker (AV multi-ticker = AND/intersection, so no batching).
    print(f'Alpha Vantage: {len(tickers)} individual ticker calls ...')
    for i, tk in enumerate(tickers):
        if i:
            time.sleep(AV_GAP)
        arts = av.fetch_articles(tk, max_articles=50, timespan='7d')
        picks = []
        for a in arts:
            if week_ago and a.date and a.date < week_ago:    # keep last ~week
                continue
            rel, lab, sc = None, None, None
            for ts in (a.ticker_sentiment or []):
                if ts.get('ticker') == tk:
                    try:
                        rel = float(ts.get('relevance_score') or 0)
                    except (TypeError, ValueError):
                        rel = 0
                    lab, sc = ts.get('ticker_sentiment_label'), ts.get('ticker_sentiment_score')
                    break
            if rel is None or rel < MIN_RELEVANCE:
                continue
            picks.append({'title': a.title, 'url': a.url, 'domain': a.domain, 'date': a.date,
                          'relevance': round(rel, 3), 'sentiment': lab, 'sentiment_score': sc})
        picks.sort(key=lambda x: x['relevance'], reverse=True)
        if picks:
            by_ticker[tk] = picks[:PER_TICKER]
    print(f'  -> news for {len(by_ticker)}/{len(tickers)} tickers')

    # 2) Topic calls for the dominant sector themes.
    for i, sec in enumerate(themes):
        topic = SECTOR_TO_AV_TOPIC.get(sec)
        if not topic:
            continue
        time.sleep(AV_GAP)
        print(f'Alpha Vantage: topic call "{topic}" for {sec} ...')
        arts = av.fetch_articles('', max_articles=50, timespan='7d', topics=topic)
        seen, picks = set(), []
        for a in arts:
            if week_ago and a.date and a.date < week_ago:
                continue
            if a.domain in seen:
                continue
            seen.add(a.domain)
            picks.append({'title': a.title, 'url': a.url, 'domain': a.domain,
                          'date': a.date, 'sentiment': a.sentiment})
            if len(picks) >= PER_SECTOR:
                break
        sector_news[sec] = picks
        print(f'  -> {len(picks)} headlines for {sec}')

    out = {'generated': datetime.now(timezone.utc).isoformat(),
           'provider': av.name, 'tickers': tickers, 'themes': themes,
           'by_ticker': by_ticker, 'sector_news': sector_news}
    out_path = os.path.join(BASE, 'ticker_news.json')
    open(out_path, 'w', encoding='utf-8').write(json.dumps(out, indent=2))

    print('\nTicker news (top stories per moved name):')
    for tk in tickers:
        arts = by_ticker.get(tk)
        if not arts:
            continue
        print(f'\n  {tk}:')
        for a in arts[:2]:
            print(f'      - {a["title"][:88]}  [{a["domain"]}, {a["date"]}, {a["sentiment"]}]')
    print(f'\n[written to {out_path}]')


if __name__ == '__main__':
    main()
