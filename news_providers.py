#!/usr/bin/env python3
"""Pluggable news-provider interface for the newsletter.

The retrieval layer is built behind this interface so the free source used now
(GDELT, keyless) can be swapped for a paid one (Benzinga / Polygon) later without
touching the callers. Every provider returns a list[Article].

Providers:
  - GDELTProvider  : keyless, global news, great for macro / geopolitics.
  - (future) AlphaVantageProvider, BenzingaProvider, ... for ticker/sector news.
"""

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

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
    summary: str = ''
    ticker_sentiment: object = None   # provider-specific per-ticker relevance/sentiment


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


class AlphaVantageProvider(NewsProvider):
    """Alpha Vantage NEWS_SENTIMENT — ticker/sector news with per-ticker relevance
    and sentiment. Best for explaining WHY specific names moved. Free tier is
    25 req/day, so callers should BATCH tickers into one comma-separated query.
    `query` = comma-separated tickers (e.g. 'NVDA,MU'); pass topics= for a topic
    feed (e.g. 'technology', 'life_sciences')."""
    name = 'alphavantage'
    URL = 'https://www.alphavantage.co/query'

    def __init__(self):
        self.key = os.environ.get('ALPHAVANTAGE_API_KEY')
        if not self.key:
            raise RuntimeError('ALPHAVANTAGE_API_KEY not set')

    def fetch_articles(self, query, max_articles=200, timespan='7d', topics=None, since=None):
        # NOTE: do NOT send time_from by default — AV silently returns an empty
        # feed for ticker queries when time_from is set. We omit it (AV returns
        # the latest, relevance-sorted) and filter recency in the caller instead.
        params = {'function': 'NEWS_SENTIMENT', 'apikey': self.key,
                  'sort': 'RELEVANCE', 'limit': min(max_articles, 1000)}
        if query:
            params['tickers'] = query
        if topics:
            params['topics'] = topics
        if since:
            params['time_from'] = since        # YYYYMMDDTHHMM, opt-in only
        try:
            r = requests.get(self.URL, params=params, headers={'User-Agent': UA}, timeout=30)
            data = r.json()
        except Exception as e:
            print(f'  AlphaVantage request failed: {e}')
            return []
        feed = data.get('feed')
        if feed is None:
            # Free-tier limit / bad request return Information / Note / Error Message.
            msg = (data.get('Information') or data.get('Note')
                   or data.get('Error Message') or str(data)[:140])
            print(f'  AlphaVantage: no feed — {msg[:140]}')
            return []
        return [self._parse(x, query) for x in feed if x.get('title')]

    @staticmethod
    def _parse(x, query):
        tp = x.get('time_published', '')      # 20260626T134500
        d = f'{tp[0:4]}-{tp[4:6]}-{tp[6:8]}' if len(tp) >= 8 else ''
        try:
            sent = float(x.get('overall_sentiment_score'))
        except (TypeError, ValueError):
            sent = None
        return Article(title=(x.get('title') or '').strip(), url=x.get('url', ''),
                       domain=x.get('source', ''), date=d, query=query,
                       sentiment=sent, summary=(x.get('summary') or '').strip(),
                       ticker_sentiment=x.get('ticker_sentiment') or [])


def get_provider(name='gdelt'):
    if name == 'gdelt':
        return GDELTProvider()
    if name in ('alphavantage', 'av'):
        return AlphaVantageProvider()
    raise ValueError(f'unknown news provider: {name!r}')
