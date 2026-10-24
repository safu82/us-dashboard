#!/usr/bin/env python3
"""Collect posts from a short list of market accounts into feed_mentions.

Second source, not the foundation. The RSS wires carry the primary reporting;
these accounts add speed (the daily recaps land ~03:00 UTC, hours before the
morning write-ups) and framing (which part of a print mattered). Counting is by
DISTINCT SOURCE for exactly this reason — several of these accounts relay the
same scoop within the hour, and counting posts would measure syndication.

The five were chosen to do five different jobs, and they parse very differently:

  amitisinvesting  8.8k-char numbered daily recap, ticker-dense
  KobeissiLetter   macro one-liners, ZERO cashtags — flows, rates, deficits
  MelvinInvests    semis and memory supply chain, often Samsung/SK Hynix,
                   which are not US-listed and resolve to no ticker
  eWhispers        earnings calendars, almost pure cashtag lists
  CharlieBilello   market statistics, no cashtags, highly checkable claims

So cashtag extraction alone would cover about a third of this. The shared
matcher in feed_entities handles the rest.

AUTHENTICATION. X serves nothing useful logged out, so this needs a session.
Run `python fetch_x_feeds.py --login` ONCE: a browser opens, you log in by hand,
and the session is saved to x_state.json. That file is a live credential — it is
gitignored, and in CI it belongs in a secret, not the repo. No password is ever
read, stored or typed by this script.

A NOTE ON TERMS. Automated collection is against X's terms of service, and the
account carrying the session is the one at risk. That argues for a short list
polled once a day, which is what this is, rather than broad scraping. The RSS
collector is the part with no such exposure, and it is deliberately the one the
morning note can run without.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY.
Usage:
  python fetch_x_feeds.py --login          # once, interactive
  python fetch_x_feeds.py [--dry-run] [--days N] [--headed]
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

import sb_retry  # noqa: F401
from feed_entities import build_patterns, entities, session_for

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))
STATE = os.path.join(BASE, 'x_state.json')

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')

# role decides how a post is USED, which matters more than who wrote it.
#
#   recap     split on its numbered items, entity-match each, and let them
#             raise a story's source count in the digest. Only works for
#             accounts that actually name tickers.
#   macro     stored as a note. Tested against real posts, KobeissiLetter
#             resolved to zero entities on five of five - buybacks at a record
#             $1.3tn and market cap at 350% of GDP name no company - so
#             counting entities would simply discard him.
#   calendar  cashtags are companies DUE TO REPORT, not companies in the news.
#             Forward-looking, and the only forward-looking thing we have.
HANDLES = {
    'amitisinvesting': {'role': 'recap',    'about': 'daily numbered recap'},
    'KobeissiLetter':  {'role': 'macro',    'about': 'flows, rates, valuation'},
    'MelvinInvests':   {'role': 'recap',    'about': 'semis and memory chain'},
    'eWhispers':       {'role': 'calendar', 'about': 'who reports, and when'},
    'CharlieBilello':  {'role': 'macro',    'about': 'market statistics'},
}

PER_HANDLE = 25          # posts wanted before scrolling stops
MAX_SCROLLS = 30
SCROLL_WAIT = 1.4        # X's virtualiser needs a beat to render the next page
BETWEEN_HANDLES = 6      # seconds; politeness, and it looks less like a bot
MAX_EXPANDS = 6          # permalinks opened per handle to recover long posts

# Collect what is rendered. X virtualises the timeline, so this runs after each
# scroll and dedupes by permalink rather than reading the DOM once at the end.
COLLECT_JS = """
() => {
  const out = [];
  for (const a of document.querySelectorAll('article[data-testid="tweet"]')) {
    const t = a.querySelector('time');
    if (!t) continue;                      // no timestamp => promoted
    let href = (t.closest('a')?.getAttribute('href') || '');
    const m = href.match(/^\\/[^/]+\\/status\\/\\d+/);
    if (!m) continue;
    const metric = n => {
      const e = a.querySelector(`[data-testid="${n}"]`);
      return e ? e.innerText.trim().split('\\n')[0] : '';
    };
    out.push({
      href: m[0],
      when: t.getAttribute('datetime'),
      likes: metric('like'),
      truncated: !!a.querySelector('[data-testid="tweet-text-show-more-link"]'),
      text: [...a.querySelectorAll('[data-testid="tweetText"]')]
              .map(e => e.innerText).join('\\n')
              .replace(/\\n\\$([A-Z.]+)\\n/g, ' $$$1 '),
    });
  }
  return out;
}
"""

# On a permalink, read the first article only: replies are other people's.
FULL_JS = """
() => {
  const a = document.querySelector('article[data-testid="tweet"]');
  if (!a) return null;
  return [...a.querySelectorAll('[data-testid="tweetText"]')]
           .map(e => e.innerText).join('\\n')
           .replace(/\\n\\$([A-Z.]+)\\n/g, ' $$$1 ');
}
"""


def do_login():
    """Save a session by hand. See the note on Google sign-in below.

    Google refuses OAuth from a browser it detects as automated — "this browser
    or app may not be secure" — which is a deliberate security control on their
    side, not a bug to work around. So this does nothing to disguise itself. If
    your X account signs in through Google, this path will not work, and the
    answer is to drop it rather than to defeat the check.
    """
    from playwright.sync_api import sync_playwright
    print('A browser will open on X.\n')
    print("  Sign in with X's OWN form — handle and password.")
    print('  "Sign in with Google" will be refused: Google blocks sign-in from')
    print('  an automated browser on purpose, and this makes no attempt to')
    print('  look like anything else.\n')
    with sync_playwright() as p:
        b = p.chromium.launch(channel='chrome', headless=False)
        ctx = b.new_context()
        page = ctx.new_page()
        page.goto('https://x.com/login')
        try:
            page.wait_for_url('**/home', timeout=300000)
        except Exception:
            print('Did not reach the home timeline within 5 minutes; '
                  'nothing saved.')
            b.close()
            return 1
        ctx.storage_state(path=STATE)
        b.close()
    print(f'Saved session to {STATE}')
    print('That file is a live credential. It is gitignored; keep it out of '
          'anywhere shared, and put it in a secret if this ever runs in CI.')
    return 0


def collect(page, handle, cutoff):
    page.goto(f'https://x.com/{handle}', wait_until='domcontentloaded')
    page.wait_for_timeout(2600)
    seen = {}
    stale = 0
    for _ in range(MAX_SCROLLS):
        if len(seen) >= PER_HANDLE:
            break
        before = len(seen)
        for p in page.evaluate(COLLECT_JS) or []:
            if p['href'] not in seen:
                seen[p['href']] = p
        page.mouse.wheel(0, 2200)
        page.wait_for_timeout(int(SCROLL_WAIT * 1000))
        for p in page.evaluate(COLLECT_JS) or []:
            if p['href'] not in seen:
                seen[p['href']] = p
        if len(seen) == before:
            stale += 1
            if stale >= 6:
                break
        else:
            stale = 0

    posts = []
    for p in seen.values():
        try:
            when = datetime.fromisoformat(p['when'].replace('Z', '+00:00'))
        except (TypeError, ValueError, AttributeError):
            continue
        # A pinned post can be two years old and would otherwise be counted as
        # today's news on every single run.
        if when < cutoff:
            continue
        p['dt'] = when
        posts.append(p)
    posts.sort(key=lambda x: x['dt'], reverse=True)

    # The timeline truncates long posts, and the long ones are the recaps where
    # most of the tickers live — the 2026-10-22 recap was 8.8k characters and
    # named twenty-odd names. Recovering a handful is worth the extra loads.
    expanded = 0
    for p in posts:
        if expanded >= MAX_EXPANDS or not p.get('truncated'):
            continue
        try:
            page.goto('https://x.com' + p['href'], wait_until='domcontentloaded')
            page.wait_for_timeout(2200)
            full = page.evaluate(FULL_JS)
            if full and len(full) > len(p['text']):
                p['text'] = full
                expanded += 1
        except Exception as e:
            print(f'    expand failed {p["href"]}: {type(e).__name__}')
    return posts, expanded


def main():
    if '--login' in sys.argv:
        return do_login()
    if not URL or not KEY:
        sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')
    if not os.path.exists(STATE):
        sys.exit(f'No saved session at {STATE}.\n'
                 'Run:  python fetch_x_feeds.py --login')

    dry = '--dry-run' in sys.argv
    days = 2
    if '--days' in sys.argv:
        days = int(sys.argv[sys.argv.index('--days') + 1])
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    sb = create_client(URL, KEY)
    print('=' * 66)
    print('X FEEDS  ', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    print('=' * 66)

    rows, frm = [], 0
    while True:
        r = (sb.table('us_stock_sectors').select('ticker,company_name')
             .range(frm, frm + 999).execute().data or [])
        rows.extend(r)
        if len(r) < 1000:
            break
        frm += 1000
    tickers, patterns, n_short = build_patterns(rows)
    print(f'universe: {len(tickers)} tickers, {n_short} short forms\n')

    from playwright.sync_api import sync_playwright
    out, no_entity = [], 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless='--headed' not in sys.argv)
        ctx = browser.new_context(storage_state=STATE,
                                  viewport={'width': 1280, 'height': 2000})
        page = ctx.new_page()
        notes = []
        for i, (handle, cfg) in enumerate(HANDLES.items()):
            role = cfg['role']
            try:
                posts, expanded = collect(page, handle, cutoff)
            except Exception as e:
                print(f'  {handle:17} FAILED {type(e).__name__}: {str(e)[:90]}')
                continue
            hits, kept = 0, 0
            for p in posts:
                url = 'https://x.com' + p['href']
                sess = session_for(p['dt']).isoformat()

                if role == 'calendar':
                    syms = sorted({m for m in re.findall(r'\$([A-Z]{1,5})\b', p['text'])
                                   if m in tickers})
                    if not syms:
                        continue
                    notes.append({'handle': handle, 'url': url,
                                  'posted_at': p['dt'].isoformat(),
                                  'snapshot_date': sess, 'kind': 'calendar',
                                  'text': p['text'][:900], 'tickers': syms,
                                  'item_no': 0})
                    kept += 1
                    continue

                if role == 'macro':
                    # Kept whole and unparsed. The claim IS the content; there
                    # is no company to attach it to, and splitting it would
                    # leave a number with nothing around it.
                    notes.append({'handle': handle, 'url': url,
                                  'posted_at': p['dt'].isoformat(),
                                  'snapshot_date': sess, 'kind': 'macro',
                                  'text': p['text'][:900], 'tickers': None,
                                  'item_no': 0})
                    kept += 1
                    continue

                # role == 'recap'. A numbered recap is a dozen separate events
                # sharing one URL, so split it: each item becomes its own
                # candidate story and can be matched, counted and merged with
                # whatever the wires carried. Stored whole as well, because the
                # items a wire never ran are the reason to read him at all.
                items = re.split(r'\n(?=\d{1,2}\.\s)', p['text'])
                numbered = len(items) > 2
                for n, body in enumerate(items):
                    body = body.strip()
                    if len(body) < 40:
                        continue
                    if numbered:
                        notes.append({'handle': handle, 'url': url,
                                      'posted_at': p['dt'].isoformat(),
                                      'snapshot_date': sess, 'kind': 'recap',
                                      'text': body[:900], 'tickers': None,
                                      'item_no': n + 1})
                        kept += 1
                    ents = entities(body, tickers, patterns)
                    if not ents:
                        no_entity += 1
                        continue
                    hits += 1
                    for ent, typ, how, exc in ents:
                        out.append({
                            'source': f'x:{handle}', 'source_type': 'x',
                            # Items of one post need distinct URLs or they
                            # collide on the (source, url, entity) key.
                            'item_url': url + (f'#item{n}' if numbered else ''),
                            'item_title': body[:400],
                            'published_at': p['dt'].isoformat(),
                            'snapshot_date': sess,
                            'entity': ent, 'entity_type': typ,
                            'matched_on': how, 'excerpt': exc[:300]})
            print(f'  {handle:17} {len(posts):3} posts, {expanded} expanded, '
                  f'{hits} tagged, {kept} notes  ({cfg["about"]})')
            if i < len(HANDLES) - 1:
                time.sleep(BETWEEN_HANDLES)
        browser.close()

    print(f'\n{len(out)} mentions, {len(notes)} notes | '
          f'{no_entity} items matched nothing')
    by_kind = {}
    for n in notes:
        by_kind[n['kind']] = by_kind.get(n['kind'], 0) + 1
    print('notes by kind: ' + (', '.join(f'{k}={v}' for k, v in sorted(by_kind.items()))
                               or 'none'))

    if dry:
        print('\n--dry-run: nothing written')
        return 0
    for i in range(0, len(out), 500):
        sb.table('feed_mentions').upsert(
            out[i:i + 500], on_conflict='source,item_url,entity').execute()
    for i in range(0, len(notes), 200):
        sb.table('x_notes').upsert(
            notes[i:i + 200], on_conflict='url,item_no').execute()
    print(f'\nupserted {len(out)} mentions and {len(notes)} notes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
