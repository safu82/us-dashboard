"""Detect new posts from the belled accounts cheaply enough to poll often.

Why this exists
---------------
Replies to a third party cannot go through the API at all. X refuses them at
the policy level for pay-per-use apps:

    403 "You can only reply to or quote posts where you are mentioned or are
         the author."

Only Enterprise lifts that, so every reply still has to be typed into a live
browser session. But *detection* has no such restriction, and detection is the
part that has to run constantly. Visiting nine profiles in a browser costs
minutes and a great many tool calls; this costs one cheap request.

Why a List
----------
All eight belled accounts sit in the private "Analysts" list, so one call
covers them all. Polling each account separately would be eight times the
cost for the same answer.

Why two stages
--------------
X bills per post RETURNED, not per call, so max_results is the price dial.
A naive max_results=20 poll costs $0.10 every time, which at a five-minute
cadence is roughly $860 a month for mostly nothing.

So each poll first asks for exactly ONE post — the newest in the list, for
$0.005. If its id is not greater than the last one we looked at, there is
nothing new and we stop there. Only when the sentinel shows movement do we
pay for a fuller page. Something new appears roughly once an hour, so the
great majority of polls cost a single sentinel.

The list timeline does not support since_id (it allows only
[id, max_results, pagination_token, post.fields]), which is why the cursor is
kept here and the comparison done locally. Post ids are monotonic, so a
numeric compare is the whole filter.

Spend is accumulated into the state file as it happens, so the running cost
is measured rather than projected.

Usage
-----
    python watch_x.py --seed     record the current newest post, report nothing
    python watch_x.py            report posts newer than the last seen id
    python watch_x.py --spend    print measured spend so far and exit
    python watch_x.py --cost     print projected polling cost and exit

Output is deliberately terse and machine-readable. The first line is either
    NOTHING NEW
or
    NEW <n>
followed by one block per post. A caller that sees NOTHING NEW can stop
immediately without reading further.
"""
import argparse
import io
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LIST_ID = '2115703265988145657'          # private list: "Analysts"
STATE = '.x_watch_state.json'

# X pay-per-use: $0.005 per post returned. Confirmed against X's published
# rates 2026-11-01; if they move, this is the one number to change.
COST_PER_POST = 0.005

# How many posts to pull once the sentinel says something moved. Eight
# accounts rarely produce more than two or three posts inside a five-minute
# window, so this has generous headroom without being wasteful.
FETCH_ON_HIT = 5

# Retweets carry someone else's words and replies are fragments of a
# conversation we have not seen; neither is something our cross-section can
# answer. Quotes are kept — several of the posts worth replying to this week
# were quotes with the author's own argument on top.
SKIP_KINDS = {'retweeted', 'replied_to'}


def load_env():
    """Credentials live in .env locally and in the environment in CI."""
    if not os.path.exists('.env'):
        return
    for line in io.open('.env', encoding='utf-8-sig'):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_state():
    """The last post we have already looked at, and spend to date.

    A missing file is a first run. A file that exists but will not parse is
    not fine: silently treating it as empty would replay the whole timeline
    and invite replies to posts that are hours old.
    """
    if not os.path.exists(STATE):
        return {}
    with io.open(STATE, encoding='utf-8-sig') as fh:
        d = json.load(fh)
    if not isinstance(d, dict):
        raise ValueError(f'{STATE} is not an object; refusing to overwrite it')
    return d


def save_state(d):
    with io.open(STATE, 'w', encoding='utf-8') as fh:
        json.dump(d, fh, indent=1, sort_keys=True)


def spend(state, posts_returned):
    state['polls'] = state.get('polls', 0) + 1
    state['posts_billed'] = state.get('posts_billed', 0) + posts_returned
    state['spend_usd'] = round(state['posts_billed'] * COST_PER_POST, 4)


def age_minutes(created_at):
    fmt = '%Y-%m-%dT%H:%M:%S.%f%z' if '.' in created_at else '%Y-%m-%dT%H:%M:%S%z'
    t = datetime.strptime(created_at, fmt)
    return (datetime.now(timezone.utc) - t).total_seconds() / 60.0


def sentinel(creds, api):
    """Newest id in the list, for the price of one post."""
    out = api._call('GET', f'/lists/{LIST_ID}/tweets', creds,
                    query={'max_results': '1'})
    data = out.get('data') or []
    return (data[0]['id'] if data else None), len(data)


def fetch(creds, api, since_id, n):
    out = api._call('GET', f'/lists/{LIST_ID}/tweets', creds,
                    query={'max_results': str(n),
                           'tweet.fields': 'created_at,author_id,referenced_tweets',
                           'expansions': 'author_id'})
    raw = out.get('data') or []
    users = {u['id']: u['username']
             for u in ((out.get('includes') or {}).get('users') or [])}
    posts = []
    for t in raw:
        if since_id and int(t['id']) <= int(since_id):
            continue
        kinds = {r['type'] for r in (t.get('referenced_tweets') or [])}
        if kinds & SKIP_KINDS:
            continue
        posts.append({
            'id': t['id'],
            'author': users.get(t.get('author_id'), '?'),
            'age_min': age_minutes(t['created_at']),
            'text': t.get('text') or '',
            'quote': 'quoted' in kinds,
        })
    newest = max((t['id'] for t in raw), default=None, key=lambda s: int(s))
    return posts, newest, len(raw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seed', action='store_true',
                    help='record the newest post without reporting anything')
    ap.add_argument('--spend', action='store_true',
                    help='print measured spend so far and exit')
    ap.add_argument('--cost', action='store_true',
                    help='print projected polling cost and exit')
    args = ap.parse_args()

    if args.cost:
        # A hit costs the sentinel plus a page; misses cost the sentinel only.
        # Eight accounts produce roughly one post an hour between them.
        hit_rate = lambda mins: min(1.0, (mins / 60.0))
        print('assuming ~1 new post/hour across the eight accounts\n')
        for mins in (5, 10, 15, 30, 60):
            per_day = (24 * 60) / mins
            h = hit_rate(mins)
            per_poll = COST_PER_POST + h * FETCH_ON_HIT * COST_PER_POST
            print('every %2d min: %4.0f polls/day  $%4.2f/day  $%6.2f/month'
                  '   (24/7)' % (mins, per_day, per_day * per_poll,
                                 per_day * per_poll * 30))
        print()
        for mins in (5, 10):
            per_day = (14 * 60) / mins
            h = hit_rate(mins)
            per_poll = COST_PER_POST + h * FETCH_ON_HIT * COST_PER_POST
            print('every %2d min: %4.0f polls/day  $%4.2f/day  $%6.2f/month'
                  '   (14h active window)'
                  % (mins, per_day, per_day * per_poll,
                     per_day * per_poll * 30))
        return 0

    state = load_state()

    if args.spend:
        print('polls        %d' % state.get('polls', 0))
        print('posts billed %d' % state.get('posts_billed', 0))
        print('spend        $%.3f' % state.get('spend_usd', 0.0))
        print('since        %s' % state.get('first_poll', '(not recorded)'))
        return 0

    load_env()
    import post_to_x as api
    creds, why = api.credentials()
    if not creds:
        print('ERROR no credentials: %s' % why)
        return 1

    state.setdefault('first_poll', datetime.now(timezone.utc).isoformat())
    since = state.get('last_id')

    newest_id, billed = sentinel(creds, api)
    spend(state, billed)
    state['last_poll'] = datetime.now(timezone.utc).isoformat()

    if args.seed or not since:
        if newest_id:
            state['last_id'] = newest_id
        save_state(state)
        print('SEEDED at %s' % newest_id)
        return 0

    if not newest_id or int(newest_id) <= int(since):
        save_state(state)
        print('NOTHING NEW')
        return 0

    posts, newest, billed2 = fetch(creds, api, since, FETCH_ON_HIT)
    spend(state, billed2)
    if newest:
        state['last_id'] = newest
    save_state(state)

    if not posts:
        # The sentinel moved but everything was a retweet or a reply.
        print('NOTHING NEW')
        return 0

    posts.sort(key=lambda p: p['age_min'])
    print('NEW %d' % len(posts))
    for p in posts:
        print('---')
        print('id     %s' % p['id'])
        print('author @%s' % p['author'])
        print('age    %.1f min%s' % (p['age_min'], '  (quote)' if p['quote'] else ''))
        print('url    https://x.com/%s/status/%s' % (p['author'], p['id']))
        print('text   %s' % p['text'].replace('\n', '\n       '))
    return 0


if __name__ == '__main__':
    sys.exit(main())
