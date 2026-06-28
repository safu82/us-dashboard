#!/usr/bin/env python3
"""Macro / geopolitics news sweep -> macro_news.json.

Sweeps a curated set of perennial market-moving macro themes (Fed, inflation,
oil, jobs, trade, geopolitics, rates/dollar, the tape) via a news provider, and
ranks them by COVERAGE VOLUME x SOURCE BREADTH — a strong proxy for "how
market-moving is this story this week" (a big story gets covered by many outlets).
The week's dominant macro narrative surfaces as the highest-scoring theme.

This is the macro half of the news engine; the ticker/sector half (driven by
market_events.json) uses a paid/ticker provider later. Source is swappable via
news_providers.get_provider().

Output: macro_news.json — top themes + representative headlines, for the LLM
synthesis layer to turn into the newsletter's macro sections.
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

# Perennial market-moving macro themes. The week's big story shows up as the
# highest-volume theme; the geopolitics bucket catches breaking shocks (oil, war).
MACRO_TOPICS = [
    ('Fed & rates',        '"Federal Reserve" OR FOMC OR "interest rate" OR "rate cut" OR "rate hike"'),
    ('Inflation',          'inflation OR CPI OR "consumer prices" OR PPI OR "core inflation"'),
    ('Oil & energy',       '"oil prices" OR "crude oil" OR OPEC OR "energy prices" OR Brent'),
    ('US economy & jobs',  '"jobs report" OR unemployment OR recession OR "labor market" OR "economic growth"'),
    ('Trade & tariffs',    'tariffs OR "trade war" OR "trade deal" OR "export controls"'),
    ('Geopolitics',        'Iran OR Israel OR "Strait of Hormuz" OR Ukraine OR "Middle East" OR sanctions'),
    ('Rates & dollar',     '"Treasury yields" OR "10-year Treasury" OR "US dollar" OR "bond market"'),
    ('The tape',           '"stock market" OR "S&P 500" OR Nasdaq OR "Wall Street" OR "market selloff"'),
]

MAX_ARTICLES = 75
TIMESPAN = '7d'
HEADLINES_PER_TOPIC = 5
SLEEP = 5.0           # GDELT throttles bursts; ~5s between queries stays clear


def main():
    provider = get_provider('gdelt')
    print(f'Macro news sweep via {provider.name} ({TIMESPAN}, {len(MACRO_TOPICS)} themes)\n')

    results = []
    for label, query in MACRO_TOPICS:
        arts = provider.fetch_articles(query, max_articles=MAX_ARTICLES, timespan=TIMESPAN)
        domains = {a.domain for a in arts if a.domain}
        # Dedupe to one headline per domain (keep relevance order), newest-ish first.
        seen, top = set(), []
        for a in arts:
            if a.domain in seen:
                continue
            seen.add(a.domain)
            top.append(a)
            if len(top) >= HEADLINES_PER_TOPIC:
                break
        # Coverage volume + source breadth = market-relevance proxy.
        score = len(arts) + len(domains) * 1.5
        results.append({
            'topic': label,
            'query': query,
            'score': round(score, 1),
            'article_count': len(arts),
            'distinct_domains': len(domains),
            'top_headlines': [{'title': a.title, 'domain': a.domain,
                               'date': a.date, 'url': a.url} for a in top],
        })
        print(f'  {label:<20} vol={len(arts):>3}  domains={len(domains):>3}  score={score:>6.1f}')
        time.sleep(SLEEP)

    results.sort(key=lambda r: r['score'], reverse=True)

    out = {'generated': datetime.now(timezone.utc).isoformat(),
           'provider': provider.name, 'timespan': TIMESPAN, 'topics': results}
    out_path = os.path.join(BASE, 'macro_news.json')
    open(out_path, 'w', encoding='utf-8').write(json.dumps(out, indent=2))

    print('\nDominant macro themes this week (by coverage volume x breadth):')
    for r in results:
        if not r['top_headlines']:
            continue
        print(f'\n  ▶ {r["topic"]}  (score {r["score"]})')
        for h in r['top_headlines'][:3]:
            print(f'      - {h["title"][:90]}  [{h["domain"]}, {h["date"]}]')
    print(f'\n[written to {out_path}]')


if __name__ == '__main__':
    main()
