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

# Objects that move the boards but have no ticker in our universe.
MACRO = {
    'US10Y':  [r'10-?year (?:treasury|yield|note)', r'\b10Y\b', r'ten-?year yield'],
    'FED':    [r'\bFederal Reserve\b', r'\bFOMC\b', r'\brate (?:cut|hike)s?\b'],
    'BRENT':  [r'\bBrent\b.{0,20}crude', r'\bcrude oil\b', r'\boil prices?\b'],
    'GOLD':   [r'\bgold prices?\b', r'\bbullion\b'],
    'CPI':    [r'\bCPI\b', r'\binflation (?:data|report|print)\b'],
    'JOBS':   [r'\bjobless claims\b', r'\bpayrolls\b', r'\bunemployment rate\b'],
    'DRAM':   [r'\bDRAM\b', r'\bHBM\b', r'\bmemory (?:prices|chips)\b'],
}

# Company names that are also ordinary words. Matching these on text would tag
# every article containing the word, so they are reachable by cashtag only.
NAME_STOPLIST = {'target', 'gap', 'block', 'match', 'on', 'key', 'all', 'how',
                 'so', 'now', 'open', 'live', 'root', 'peak', 'sky', 'arc',
                 'ai', 'at', 'it', 'up', 'new', 'first', 'core', 'one', 'two',
                 'post', 'paper', 'science', 'global', 'national', 'general',
                 'american', 'united', 'capital', 'energy', 'health', 'metal',
                 # Surnames and nouns that are company first-words but occur far
                 # more often as themselves. "Trump Media" is a listed company;
                 # almost every story containing Trump is not about it.
                 'trump', 'trade', 'jones', 'stanley', 'robert', 'telephone',
                 'commerce', 'public', 'service', 'western', 'eastern', 'north',
                 'south', 'east', 'west', 'central', 'summit', 'liberty',
                 'premier', 'select', 'prime', 'pioneer', 'heritage', 'legacy'}

SUFFIX = re.compile(
    r'\s+(?:Inc|Corp|Corporation|Company|Co|Ltd|plc|Holdings?|Group|N\.V\.|'
    r'S\.A\.|LLC|L\.P\.|LP|Technologies|Technology|International|Limited|'
    r'Incorporated|Class\s+[A-Z])\.?,?\s*$', re.I)


def clean_name(name):
    """Strip corporate suffixes, repeatedly: 'Meta Platforms, Inc.' -> 'Meta Platforms'."""
    prev = None
    out = (name or '').strip().rstrip(',')
    while out != prev:
        prev = out
        out = SUFFIX.sub('', out).strip().rstrip(',')
    return out


def load_universe():
    """ticker -> compiled name pattern, plus the set of tickers we cover."""
    rows, frm = [], 0
    while True:
        r = (sb.table('us_stock_sectors').select('ticker,company_name')
             .range(frm, frm + 999).execute().data or [])
        rows.extend(r)
        if len(r) < 1000:
            break
        frm += 1000
    tickers, bases = set(), {}
    for r in rows:
        tk = (r.get('ticker') or '').strip()
        if not tk:
            continue
        tickers.add(tk)
        base = clean_name(r.get('company_name'))
        # Three characters or fewer is not a name, it is a coincidence waiting
        # to happen; and anything on the stoplist is an ordinary English word.
        if len(base) >= 4 and base.lower() not in NAME_STOPLIST:
            bases[tk] = base

    # The press almost never uses the registered name. "Meta Platforms" files
    # the accounts; the headline says "Meta stock sinks". So a short form is
    # needed, but only where it is unambiguous: "Meta" resolves to one company,
    # "General" resolves to Motors, Mills and Dynamics and must resolve to none.
    # Every word used in any company's name, and who uses it. A first word is
    # only safe as a short form if NO other company's name contains that word:
    # "Stanley" belongs to Stanley Black & Decker but lives inside Morgan
    # Stanley, so it tagged SWK on a Morgan Stanley earnings story. Likewise
    # "Trade" (The Trade Desk) inside "choppy trade" and "Jones" inside "Dow
    # Jones". Requiring the word to be unique across the whole universe removes
    # that class structurally rather than by listing offenders.
    word_owners = {}
    for tk, base in bases.items():
        for w in re.findall(r'[A-Za-z]{2,}', base):
            word_owners.setdefault(w.lower(), set()).add(tk)

    short = {}
    for tk, base in bases.items():
        if ' ' not in base:
            continue
        w = base.split()[0]
        if len(w) < 4 or w.lower() in NAME_STOPLIST:
            continue
        if word_owners.get(w.lower(), set()) == {tk}:
            short[tk] = w

    patterns = {}
    for tk, base in bases.items():
        full = re.compile(r'(?<![A-Za-z])' + re.escape(base) + r'(?![A-Za-z])')
        brief = None
        if tk in short:
            brief = re.compile(r'(?<![A-Za-z])' + re.escape(short[tk]) + r'(?![A-Za-z])')
        patterns[tk] = (full, brief)
    print(f'  short forms resolved unambiguously: {len(short)}')
    return tickers, patterns


# A short form is a first word, and first words are treacherous: "Trump Media"
# yields Trump, "Robert Half" yields Robert, "Telephone and Data Systems" yields
# Telephone. Each of those matched a story about politics, a Nobel prize and AI
# job losses respectively. Requiring market vocabulary nearby costs nothing on a
# real company story — they all say shares, earnings or revenue somewhere — and
# removes the whole class, rather than playing whack-a-mole with a stoplist.
MARKET_CONTEXT = re.compile(
    r'\b(shares?|stocks?|earnings|revenue|quarter(?:ly)?|guidance|EPS|profit|'
    r'investors?|analysts?|Nasdaq|NYSE|S&P|market cap|valuation|dividend|'
    r'upgrade[ds]?|downgrade[ds]?|buyback|IPO|forecast|outlook)\b', re.I)


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


def session_for(when):
    """The trading session a story belongs to, not the date it was filed.

    US cash equities open at 13:30 UTC. Anything filed before that belongs to
    the session that just finished: the daily recaps land around 03:00 UTC and
    are entirely about the previous close, and filing them under the new date
    would attribute yesterday's news to a session that has not happened. After
    the open, a story belongs to the day it was filed. Weekends roll back to the
    Friday for the same reason.
    """
    ts = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    d = ts.date()
    if ts.hour * 60 + ts.minute < 13 * 60 + 30:
        d -= timedelta(days=1)
    while d.weekday() > 4:
        d -= timedelta(days=1)
    return d


def entities(text, tickers, patterns):
    """[(entity, type, matched_on, excerpt)] for one story."""
    found = {}

    def add(ent, typ, how, at):
        if ent not in found:
            lo, hi = max(0, at - 60), at + 90
            found[ent] = (ent, typ, how, text[lo:hi].strip())

    for m in re.finditer(r'\$([A-Z]{1,5})\b', text):
        if m.group(1) in tickers:
            add(m.group(1), 'ticker', 'cashtag', m.start())
    has_market = bool(MARKET_CONTEXT.search(text))
    for tk, (full, brief) in patterns.items():
        m = full.search(text)
        if m:
            add(tk, 'ticker', 'company_name', m.start())
            continue
        # Short form only where the story is plainly about markets.
        if brief and has_market:
            m = brief.search(text)
            if m:
                add(tk, 'ticker', 'company_short', m.start())
    for key, pats in MACRO.items():
        for p in pats:
            m = re.search(p, text, re.I)
            if m:
                add(key, 'macro', 'macro_term', m.start())
                break
    return list(found.values())


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
    for i in range(0, len(rows), 500):
        sb.table('feed_mentions').upsert(
            rows[i:i + 500], on_conflict='source,item_url,entity').execute()
    print(f'\nupserted {len(rows)} rows')


if __name__ == '__main__':
    main()
