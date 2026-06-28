#!/usr/bin/env python3
"""Pluggable news-provider interface for the newsletter.

The retrieval layer is built behind this interface so the free source used now
(GDELT, keyless) can be swapped for a paid one (Benzinga / Polygon) later without
touching the callers. Every provider returns a list[Article].

Providers:
  - GDELTProvider  : keyless, global news, great for macro / geopolitics.
  - (future) AlphaVantageProvider, BenzingaProvider, ... for ticker/sector news.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import requests

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


@dataclass
class Article:
    title: str
    url: str
    domain: str
    date: str                    # YYYY-MM-DD
    source_country: str = ''
    query: str = ''
    # forward-compat fields some providers supply (Alpha Vantage, Benzinga):
    tone: float = None
    sentiment: float = None
    relevance: float = None


class NewsProvider(ABC):
    name = 'base'

    @abstractmethod
    def fetch_articles(self, query, max_articles=75, timespan='7d'):
        """Return list[Article] for a query over the lookback window."""
        raise NotImplementedError


class GDELTProvider(NewsProvider):
    """GDELT 2.0 DOC API — keyless, indexes worldwide online news. Best for the
    macro / geopolitics backdrop (Fed, oil, inflation, conflicts)."""
    name = 'gdelt'
    URL = 'https://api.gdeltproject.org/api/v2/doc/doc'

    def fetch_articles(self, query, max_articles=75, timespan='7d', attempts=4):
        params = {
            'query': f'({query}) sourcelang:english',
            'mode': 'ArtList',
            'format': 'json',
            'timespan': timespan,
            'maxrecords': min(max_articles, 250),
            'sort': 'HybridRel',
        }
        for a in range(1, attempts + 1):
            wait = 3 * a
            try:
                r = requests.get(self.URL, params=params,
                                 headers={'User-Agent': UA}, timeout=30)
                if r.status_code == 429:        # GDELT throttles bursts hard
                    wait = 15 * a
                    raise RuntimeError('HTTP 429 (rate limited)')
                if r.status_code != 200 or not r.text.strip():
                    raise RuntimeError(f'HTTP {r.status_code}')
                # GDELT returns a plain-text error (e.g. "phrase is too short") or
                # an HTML page instead of JSON on a bad query — don't retry those.
                if not r.text.lstrip().startswith('{'):
                    print(f'  GDELT rejected {query!r}: {r.text.strip()[:80]}')
                    return []
                arts = (r.json() or {}).get('articles') or []
                return [self._parse(x, query) for x in arts if x.get('title')]
            except Exception as e:
                if a < attempts:
                    time.sleep(wait)
                    continue
                print(f'  GDELT fetch failed for {query!r}: {e}')
                return []

    @staticmethod
    def _parse(x, query):
        sd = x.get('seendate', '')           # e.g. 20260626T134500Z
        d = f'{sd[0:4]}-{sd[4:6]}-{sd[6:8]}' if len(sd) >= 8 else ''
        return Article(title=(x.get('title') or '').strip(),
                       url=x.get('url', ''), domain=x.get('domain', ''),
                       date=d, source_country=x.get('sourcecountry', ''),
                       query=query)


def get_provider(name='gdelt'):
    if name == 'gdelt':
        return GDELTProvider()
    raise ValueError(f'unknown news provider: {name!r}')
