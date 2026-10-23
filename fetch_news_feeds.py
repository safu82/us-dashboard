#!/usr/bin/env python3
"""Pull market news RSS, resolve what each story is about, write feed_mentions.

Why RSS rather than scraping commentary accounts: these feeds are published for
consumption, they need no key, no login and no session cookie, and they carry
the primary reporting that the commentary accounts repackage. Four of the items
in one widely-read daily recap on 2026-10-22 were traceable to these feeds the
same day. X handles are a second source worth adding on top for speed and
framing, but they are not the foundation.

What this is FOR is the join against daily_stock_snapshots, not the articles:

    momentum high, mentions zero      -> strong and unremarked, you are early
    mentions many, momentum fading    -> loud and weakening, you are late
    both rising                       -> confirmation

None of those three readings is available from either side alone.

Counting rule: one row per (article, entity), and a "mention count" means
DISTINCT SOURCES, never total rows. Finance media is an echo chamber — one
Bloomberg scoop becomes six stories within the hour — so counting articles
would measure syndication and call it consensus.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Usage: python fetch_news_feeds.py [--dry-run] [--days N]
"""

import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from dotenv import load_dotenv
from supabase import create_client

# Patches postgrest to retry transient Supabase failures (gateway
# errors, timeouts) instead of letting one kill the nightly chain.
import sb_retry  # noqa: F401
# What a story is ABOUT should not depend on where it came from, so the
# matching rules live in one place and the X collector imports the same ones.
from feed_entities import build_patterns, entities, session_for

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')
sb = create_client(URL, KEY)

# Verified reachable without a key on 2026-10-23. Yahoo Finance's feed 404s and
# GDELT rate-limits from a residential IP, so neither is here.
FEEDS = {
    'CNBC Markets':  'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258',
    'CNBC Earnings': 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135',
    'CNBC Tech':     'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910',
    'CNBC Finance':  'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664',
    'Nasdaq':        'https://www.nasdaq.com/feed/rssoutbound?category=Markets',
    'SeekingAlpha':  'https://seekingalpha.com/market_currents.xml',
}

def load_universe():
    """ticker -> compiled name patterns, built by the shared matcher."""
    rows, frm = [], 0
    while True:
        r = (sb.table('us_stock_sectors').select('ticker,company_name')
             .range(frm, frm + 999).execute().data or [])
        rows.extend(r)
        if len(r) < 1000:
            break
        frm += 1000
    tickers, patterns, n_short = build_patterns(rows)
    print(f'  short forms resolved unambiguously: {n_short}')
    return tickers, patterns


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (research)'})
    return urllib.request.urlopen(req, timeout=timeout).read()


def feed_items(name, url):
    try:
        root = ET.fromstring(fetch(url))
    except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, OSError) as e:
        print(f'  {name:15} FAILED {type(e).__name__} {getattr(e, "code", "")}')
        return []
    out = []
    for it in root.findall('.//item'):
        link = (it.findtext('link') or '').strip()
        if not link:
            continue
        when = None
        raw = it.findtext('pubDate') or it.findtext('{http://purl.org/dc/elements/1.1/}date')
        if raw:
            try:
                when = parsedate_to_datetime(raw)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                when = None
        out.append({'title': (it.findtext('title') or '').strip(),
                    'desc': re.sub(r'<[^>]+>', ' ', it.findtext('description') or '')[:600],
                    'link': link, 'when': when})
    print(f'  {name:15} {len(out):3} items')
    return out


def main():
    dry = '--dry-run' in sys.argv
    days = 3
    if '--days' in sys.argv:
        days = int(sys.argv[sys.argv.index('--days') + 1])
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    print('=' * 66)
    print('NEWS FEEDS  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 66)

    tickers, patterns = load_universe()
    print(f'universe: {len(tickers)} tickers, {len(patterns)} name patterns\n')

    rows, skipped_old, no_entity = [], 0, 0
    for name, url in FEEDS.items():
        for it in feed_items(name, url):
            if it['when'] and it['when'] < cutoff:
                skipped_old += 1
                continue
            text = f"{it['title']} {it['desc']}"
            ents = entities(text, tickers, patterns)
            if not ents:
                no_entity += 1
                continue
            sess = session_for(it['when'])
            for ent, typ, how, exc in ents:
                rows.append({'source': f'rss:{name}', 'source_type': 'rss',
                             'item_url': it['link'], 'item_title': it['title'][:400],
                             'published_at': it['when'].isoformat() if it['when'] else None,
                             'snapshot_date': sess.isoformat(),
                             'entity': ent, 'entity_type': typ,
                             'matched_on': how, 'excerpt': exc[:300]})

    print(f'\n{len(rows)} mentions | {no_entity} stories matched nothing '
          f'| {skipped_old} older than {days}d')

    by_entity = {}
    for r in rows:
        by_entity.setdefault(r['entity'], set()).add(r['source'])
    top = sorted(by_entity.items(), key=lambda kv: -len(kv[1]))[:12]
    print('\nmost-covered (distinct sources):')
    for ent, srcs in top:
        print(f'  {ent:8} {len(srcs)} sources')

    if dry:
        print('\n--dry-run: nothing written')
        return

    # Replace each article's entity set rather than merging into it. An upsert
    # alone can only add and update, so when the matcher improves, the rows it
    # used to get wrong stay behind: "Nasdaq takes a hit" kept tagging NDAQ long
    # after the rule that produced it was removed, and a stale exchange operator
    # sat at +7.5% in a digest about a selloff. Only articles seen in THIS run
    # are cleared, so a feed being down cannot wipe yesterday's good rows.
    urls = sorted({r['item_url'] for r in rows})
    for i in range(0, len(urls), 100):
        sb.table('feed_mentions').delete().in_('item_url', urls[i:i + 100]).execute()
    for i in range(0, len(rows), 500):
        sb.table('feed_mentions').upsert(
            rows[i:i + 500], on_conflict='source,item_url,entity').execute()
    print(f'\nrewrote {len(rows)} rows across {len(urls)} articles')


if __name__ == '__main__':
    main()
