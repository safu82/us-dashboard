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
import time
import urllib.error
import urllib.parse
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
from feed_entities import (build_patterns, clean_name, entities,
                           session_for)

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
    'MarketWatch':   'https://feeds.content.dowjones.io/public/rss/mw_marketpulse',
    'MarketWatch RT': 'https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines',
    'Investing.com': 'https://www.investing.com/rss/news_25.rss',
}

# Google News is an aggregator, not a publisher, and that is exactly why it is
# here: one request returns the same story as carried by Reuters, Bloomberg, the
# WSJ and a dozen others, which is the overlap the ranking depends on. Six
# hand-picked feeds could only ever produce six sources.
#
# The publisher is appended to each title as " - Reuters", so it is recovered
# and used as the SOURCE. Counting these as one source called "Google News"
# would collapse the very signal they are here to provide.
GOOGLE_NEWS = {
    'markets':  'stock market OR "Wall Street" OR Nasdaq OR "S&P 500"',
    'earnings': 'earnings report OR "quarterly results" OR guidance',
    'movers':   '"shares fall" OR "shares rise" OR "stock jumps" OR "stock sinks"',
}
GN_URL = ('https://news.google.com/rss/search?q={q}+when:1d'
          '&hl=en-US&gl=US&ceid=US:en')

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


def feed_items(name, url, from_aggregator=False):
    """Items from one feed. `from_aggregator` recovers the real publisher.

    Google News appends it to the title as " - Reuters", and it has to become
    the source: the whole ranking rests on how many INDEPENDENT outlets ran a
    story, and treating a hundred publishers as one feed would erase that.
    """
    try:
        root = ET.fromstring(fetch(url))
    except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, OSError) as e:
        print(f'  {name:16} FAILED {type(e).__name__} {getattr(e, "code", "")}')
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
        title = (it.findtext('title') or '').strip()
        src = name
        if from_aggregator:
            pub = it.findtext('source')
            if ' - ' in title:
                head, tail = title.rsplit(' - ', 1)
                # Only treat the tail as a publisher if it looks like one: a
                # few words, either capitalised or a bare domain. Requiring a
                # capital alone left "- marketscreener.com" stuck on the front
                # of headlines as though it were part of them.
                if (len(tail) <= 28 and len(tail.split()) <= 4
                        and (tail[:1].isupper() or re.match(r'^[a-z0-9.-]+\.[a-z]{2,}$', tail))):
                    title, pub = head.strip(), pub or tail.strip()
            if not pub:
                continue           # unattributable; it cannot be counted
            src = pub.strip()
        out.append({'title': title,
                    'desc': re.sub(r'<[^>]+>', ' ', it.findtext('description') or '')[:600],
                    'link': link, 'when': when, 'source': src})
    print(f'  {name:16} {len(out):3} items'
          + (f', {len({o["source"] for o in out})} publishers' if from_aggregator else ''))
    return out


def chase_movers(tickers, patterns, limit=14, min_move=0.05):
    """Ask about the names that moved but nobody we read wrote about.

    Broad feeds are a net: they catch whatever the wires chose to publish,
    which on 2026-10-23 meant every one of the ten biggest movers in the
    universe came back with no coverage at all — UWMC up 26%, LKQ down 22%,
    Robert Half up 21% — while the digest carried a GuruFocus template about a
    5% move. More feeds would not have fixed that; the wires simply do not
    push small and mid-caps into a general markets feed.

    Asking directly does fix it. A query per uncovered mover turns "no
    coverage" into "LKQ Announces Preliminary Third Quarter Results", and it
    costs one request each rather than another standing subscription.
    """
    r = (sb.table('daily_stock_snapshots').select('snapshot_date')
         .not_.is_('momentum_score', 'null')
         .order('snapshot_date', desc=True).limit(1).execute().data)
    if not r:
        return []
    session = r[0]['snapshot_date']
    prev = (sb.table('daily_stock_snapshots').select('snapshot_date')
            .lt('snapshot_date', session).not_.is_('momentum_score', 'null')
            .order('snapshot_date', desc=True).limit(1).execute().data)
    if not prev:
        return []
    prev = prev[0]['snapshot_date']

    def closes_for(d):
        out, frm = {}, 0
        while True:
            rows = (sb.table('daily_stock_snapshots').select('ticker,close')
                    .eq('snapshot_date', d).range(frm, frm + 999).execute().data or [])
            out.update({x['ticker']: x.get('close') for x in rows})
            if len(rows) < 1000:
                return out
            frm += 1000

    now, before = closes_for(session), closes_for(prev)
    funds = {e['symbol'] for e in
             (sb.table('etfs').select('symbol').execute().data or [])}
    covered = {m['entity'] for m in
               (sb.table('feed_mentions').select('entity')
                .eq('snapshot_date', session).execute().data or [])}

    moves = []
    for tk, c in now.items():
        if tk in funds or tk in covered:
            continue
        try:
            pct = float(c) / float(before.get(tk)) - 1
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if abs(pct) >= min_move:
            moves.append((abs(pct), pct, tk))
    moves.sort(reverse=True)
    moves = moves[:limit]
    if not moves:
        print('\nno uncovered movers to chase')
        return []

    names, frm = {}, 0
    while True:
        rows_n = (sb.table('us_stock_sectors').select('ticker,company_name')
                  .range(frm, frm + 999).execute().data or [])
        names.update({x['ticker']: x.get('company_name') for x in rows_n})
        if len(rows_n) < 1000:
            break
        frm += 1000
    print(f'\nchasing {len(moves)} uncovered movers on {session}:')
    rows = []
    for _, pct, tk in moves:
        base = clean_name(names.get(tk)) or tk
        q = urllib.parse.quote(f'"{base}" stock')
        items = feed_items(f'chase/{tk}', GN_URL.format(q=q), from_aggregator=True)
        hit = 0
        for it in items[:6]:
            text = f"{it['title']} {it['desc']}"
            # The query named this company, so the article is about it unless
            # the text says otherwise — but still require the name or cashtag
            # to appear, or a query for "Cable One" returns a price-quote page.
            full = patterns.get(tk, (None, None))[0]
            if not (f'${tk}' in text or f'({tk})' in text
                    or (full and full.search(text))):
                continue
            rows.append({'source': f'rss:{it["source"]}', 'source_type': 'rss',
                         'item_url': it['link'], 'item_title': it['title'][:400],
                         'published_at': it['when'].isoformat() if it['when'] else None,
                         'snapshot_date': session,
                         'entity': tk, 'entity_type': 'ticker',
                         'matched_on': 'chased', 'excerpt': it['title'][:300]})
            hit += 1
        print(f'  {tk:6} {pct*100:+6.1f}%  {hit} stories kept')
        time.sleep(1.2)
    return rows


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

    items = []
    for name, url in FEEDS.items():
        items += feed_items(name, url)
    for topic, q in GOOGLE_NEWS.items():
        items += feed_items(f'GoogleNews/{topic}',
                            GN_URL.format(q=urllib.parse.quote(q)),
                            from_aggregator=True)

    rows, skipped_old, no_entity = [], 0, 0
    seen_pub = set()
    # Every article seen this run, matched or not. The clean-up below has to
    # cover the ones that match NOTHING now: an article that used to resolve to
    # a ticker and no longer does would otherwise keep its old rows forever,
    # which is exactly how a Motley Fool piece about SpaceX stayed tagged as
    # Monster Beverage after the rule that did it was removed.
    fetched_urls = set()
    for it in items:
        if True:
            if it['when'] and it['when'] < cutoff:
                skipped_old += 1
                continue
            fetched_urls.add(it['link'])
            # Strip the publisher's own name before matching. Google News
            # repeats it in the description, and masthead words collide with
            # company names: "The Globe and Mail" tagged Globe Life on a
            # Spotify story, "Simply Wall Street" tagged Simply Good Foods on
            # six different companies' earnings. The byline is not the subject.
            text = f"{it['title']} {it['desc']}"
            pub = it.get('source') or ''
            if pub:
                text = re.sub(re.escape(pub), ' ', text, flags=re.I)
                for word in pub.split():
                    if len(word) > 3:
                        text = re.sub(r'(?<![A-Za-z])' + re.escape(word)
                                      + r'(?![A-Za-z])', ' ', text)
            ents = entities(text, tickers, patterns)
            if not ents:
                no_entity += 1
                continue
            sess = session_for(it['when'])
            seen_pub.add(it['source'])
            for ent, typ, how, exc in ents:
                rows.append({'source': f'rss:{it["source"]}', 'source_type': 'rss',
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
    rows += chase_movers(tickers, patterns)

    # The three Google News searches overlap by design — a big story shows up
    # under markets AND earnings AND movers — so the same (publisher, article,
    # entity) can arrive more than once. Postgres rejects a whole batch that
    # tries to upsert one key twice, so collapse them first.
    unique = {}
    for r in rows:
        unique[(r['source'], r['item_url'], r['entity'])] = r
    if len(unique) != len(rows):
        print(f'  collapsed {len(rows) - len(unique)} duplicate rows '
              '(same story under several searches)')
    rows = list(unique.values())

    # Batched small on purpose. PostgREST puts an `in` filter in the query
    # string, and a Google News link is ~700 characters of base64, so a hundred
    # of them is a 70KB URL and the server answers 400. Five keeps it well under
    # any limit. A failed batch is survivable — it leaves stale rows rather than
    # losing new ones — so it warns instead of aborting the run.
    urls = sorted(fetched_urls)
    failed = 0
    for i in range(0, len(urls), 5):
        try:
            sb.table('feed_mentions').delete().in_('item_url', urls[i:i + 5]).execute()
        except Exception as e:
            failed += 1
            if failed <= 3:
                print(f'  WARN delete batch failed ({type(e).__name__}); '
                      'stale rows for those articles may remain')
    for i in range(0, len(rows), 500):
        sb.table('feed_mentions').upsert(
            rows[i:i + 500], on_conflict='source,item_url,entity').execute()
    print(f'\nrewrote {len(rows)} rows across {len(urls)} articles')


if __name__ == '__main__':
    main()
