#!/usr/bin/env python3
"""Publish the approved drafts from posts.json to @overnightarc.

build_posts.py decides WHAT may go out. This decides whether it actually
goes, and is deliberately the duller of the two: it adds no wording of its
own beyond a fixed lead for the link reply, and it refuses anything
build_posts.py did not mark both `auto` and `approved`.

Money
-----
The X API has had no free tier since 2026-02-06; it is pay-per-use, billed
against credits bought in the developer console. As of 2026-10-29:

    plain post              $0.015
    post containing a link  $0.200     <- raised from $0.01 on 2026-04-20
    reading a post          $0.005 ($0.001 for your own)

So the link reply costs thirteen times the post it hangs off, and a loop
that posts in error is expensive rather than merely embarrassing. Hence:

  - nothing posts without --live; the default is a dry run that prints
    exactly what would be sent and what it would cost
  - MAX_REQUESTS caps one run no matter what posts.json contains
  - MAX_RUN_COST refuses the run outright if the estimate exceeds it

Not posting twice
-----------------
The workflow is stateless and the ledger is committed by a LATER step, so a
run that posts and then fails before the commit would leave no local trace
and repost tomorrow. The authority is therefore X itself: we read our own
recent posts and skip any text already there. The local ledger is only a
fast path, and is allowed to be wrong in the safe direction.

Authentication is OAuth 1.0a user context - four values that do not expire,
rather than a refresh-token dance a nightly job would have to persist.
"""
import base64
import calendar
import hashlib
import hmac
import io
import json
import os
import re
import sys
import time
import urllib.parse
import uuid

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, 'portal', 'data')
POSTS = os.path.join(DATA, 'posts.json')
LEDGER = os.path.join(DATA, 'posted.json')

API = 'https://api.x.com/2'
TIMEOUT = 30

# Rates in US dollars, for the estimate printed before anything is sent.
COST_POST = 0.015
COST_POST_URL = 0.200
COST_OWNED_READ = 0.001

# One run sends at most two candidates, each a post plus its reply.
MAX_CANDIDATES = 2
MAX_REQUESTS = 4
# A run estimated above this refuses rather than proceeds. Two candidates with
# replies is $0.43; anything near a dollar means something is wrong upstream.
MAX_RUN_COST = 0.60

# How many of our own posts to read back when checking for duplicates. The
# account posts twice a day, so twenty covers well over a week.
RECENT = 20
# A company may not headline the same template twice inside this many days,
# however the sentence around it has been reworded.
SUBJECT_DAYS = 3

URL_RE = re.compile(r'https?://\S+')

# The one piece of wording this module owns. Keyed by template so the reply
# says something about what the reader is about to get, rather than "link:".
REPLY_LEAD = {
    'chain': 'Every stage, the companies we map to it, and why each one is '
             'there:',
    'no_coverage': 'What every outlet did carry last night, ranked by how '
                   'many of them carried it:',
}
REPLY_LEAD_DEFAULT = 'The full board behind this:'


# --------------------------------------------------------------------------
# OAuth 1.0a
# --------------------------------------------------------------------------

def _q(s):
    """Percent-encoding as OAuth defines it, which is not urlencode's."""
    return urllib.parse.quote(str(s), safe='~')


def _auth_header(method, url, creds, query=None):
    """Sign a request. The JSON body is deliberately not part of the base
    string: OAuth 1.0a only signs form-encoded bodies, and X's v2 endpoints
    take JSON, so signing it would produce a signature X cannot reproduce."""
    oauth = {
        'oauth_consumer_key': creds['api_key'],
        'oauth_nonce': uuid.uuid4().hex,
        'oauth_signature_method': 'HMAC-SHA1',
        'oauth_timestamp': str(int(time.time())),
        'oauth_token': creds['access_token'],
        'oauth_version': '1.0',
    }
    signed = dict(oauth)
    signed.update(query or {})
    base = '&'.join([
        method.upper(),
        _q(url),
        _q('&'.join(f'{_q(k)}={_q(signed[k])}' for k in sorted(signed))),
    ])
    key = f"{_q(creds['api_secret'])}&{_q(creds['access_secret'])}"
    oauth['oauth_signature'] = base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return 'OAuth ' + ', '.join(
        f'{_q(k)}="{_q(v)}"' for k, v in sorted(oauth.items()))


def credentials():
    """The four OAuth 1.0a values, or None with a reason."""
    names = {'api_key': 'X_API_KEY', 'api_secret': 'X_API_SECRET',
             'access_token': 'X_ACCESS_TOKEN',
             'access_secret': 'X_ACCESS_SECRET'}
    creds, missing = {}, []
    for k, env in names.items():
        v = (os.environ.get(env) or '').strip()
        if not v:
            missing.append(env)
        creds[k] = v
    if missing:
        return None, 'not set: ' + ', '.join(missing)
    return creds, None


# --------------------------------------------------------------------------
# API calls
# --------------------------------------------------------------------------

def _call(method, path, creds, query=None, body=None):
    url = API + path
    headers = {'Authorization': _auth_header(method, url, creds, query)}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    r = requests.request(method, url, headers=headers, params=query,
                         json=body, timeout=TIMEOUT)
    if r.status_code == 429:
        raise RuntimeError('rate limited by X (429); not retrying in a '
                           'nightly job')
    if r.status_code >= 400:
        raise RuntimeError(f'{method} {path} -> {r.status_code} {r.text[:400]}')
    return r.json() if r.content else {}


def own_recent_texts(creds):
    """Texts of our own recent posts. The authoritative duplicate check."""
    me = _call('GET', '/users/me', creds)
    uid = (me.get('data') or {}).get('id')
    if not uid:
        raise RuntimeError(f'could not read own user id: {me}')
    got = _call('GET', f'/users/{uid}/tweets', creds,
                query={'max_results': str(RECENT)})
    return uid, [t.get('text', '') for t in (got.get('data') or [])]


def send(creds, text, reply_to=None):
    body = {'text': text}
    if reply_to:
        body['reply'] = {'in_reply_to_tweet_id': reply_to}
    out = _call('POST', '/tweets', creds, body=body)
    tid = (out.get('data') or {}).get('id')
    if not tid:
        raise RuntimeError(f'post returned no id: {out}')
    return tid


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def fingerprint(text):
    """Stable identity for a post. Whitespace and case vary with wording
    tweaks; the claim does not."""
    return hashlib.sha256(
        re.sub(r'\s+', ' ', (text or '').strip().lower()).encode()).hexdigest()


def load_ledger():
    try:
        with io.open(LEDGER, encoding='utf-8') as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except (IOError, ValueError):
        return {}


def save_ledger(led):
    os.makedirs(DATA, exist_ok=True)
    with io.open(LEDGER, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(led, fh, indent=1, ensure_ascii=False, sort_keys=True)


def reply_text(cand):
    lead = REPLY_LEAD.get(cand['type'], REPLY_LEAD_DEFAULT)
    return f"{lead} {cand['link']}"


def estimate(candidates):
    """Dollars this run would spend, reads included."""
    n = len(candidates)
    reads = COST_OWNED_READ * (RECENT + 1)
    return round(n * COST_POST + n * COST_POST_URL + reads, 4)


def recent_subjects(ledger, days=SUBJECT_DAYS):
    """{(type, ticker)} posted within the window.

    Exact-text matching is not enough on its own. The no-coverage template
    renders the same company into a slightly different sentence as the
    wording evolves, so two posts about Pacific Biosciences on consecutive
    days are different strings and the same post to a reader.
    """
    cutoff = time.time() - days * 86400
    out = set()
    for rec in ledger.values():
        if not isinstance(rec, dict):
            continue
        try:
            # timegm, not mktime: the stamp is UTC and mktime would read it
            # as local, shifting the window by the machine's offset.
            when = calendar.timegm(time.strptime(rec.get('at', ''),
                                                 '%Y-%m-%dT%H:%M:%SZ'))
        except ValueError:
            continue
        if when < cutoff:
            continue
        for tk in rec.get('tickers') or []:
            out.add((rec.get('type'), tk))
    return out


def choose(payload, ledger, already):
    """The candidates that may go, and why each rejected one may not."""
    seen = {fingerprint(t) for t in already}
    seen.update(ledger.keys())
    subjects = recent_subjects(ledger)
    picked, skipped = [], []
    for c in payload.get('generated') or []:
        why = None
        if not c.get('text'):
            why = 'no text'
        elif not c.get('auto'):
            why = 'template needs a human (auto=false)'
        elif not c.get('approved'):
            why = 'template not approved for unattended posting'
        elif not c.get('link'):
            why = 'no link to reply with'
        elif fingerprint(c['text']) in seen:
            why = 'already posted'
        elif URL_RE.search(c['text']):
            why = 'post body contains a link; the link belongs in the reply'
        else:
            clash = sorted({t for t in (c.get('tickers') or [])
                            if (c.get('type'), t) in subjects})
            if clash:
                why = (f"{', '.join(clash)} already posted as "
                       f"{c['type']} in the last {SUBJECT_DAYS} days")
        if why:
            skipped.append((c.get('type', '?'), why))
            continue
        picked.append(c)
        seen.add(fingerprint(c['text']))
        for tk in c.get('tickers') or []:
            subjects.add((c['type'], tk))
        if len(picked) >= MAX_CANDIDATES:
            break
    return picked, skipped


# --------------------------------------------------------------------------

def telegram(lines):
    tok = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat = os.environ.get('TELEGRAM_CHAT_ID')
    if not tok or not chat:
        return
    try:
        requests.post(f'https://api.telegram.org/bot{tok}/sendMessage',
                      data={'chat_id': chat, 'parse_mode': 'Markdown',
                            'text': '\n'.join(lines)[:4000],
                            'disable_web_page_preview': 'true'},
                      timeout=20)
    except requests.RequestException as e:
        print(f'telegram failed: {e}', file=sys.stderr)


def seed():
    """Record today's drafts as already posted, without sending anything.

    Used once, on 2026-10-29: both approved templates had gone out by hand
    that day, and the no-coverage one in wording that no longer matches what
    the script generates. Without this the next run would have reposted
    Pacific Biosciences in a different sentence.
    """
    with io.open(POSTS, encoding='utf-8') as fh:
        payload = json.load(fh)
    led = load_ledger()
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    n = 0
    for c in payload.get('generated') or []:
        if not (c.get('text') and c.get('auto') and c.get('approved')):
            continue
        fp = fingerprint(c['text'])
        if fp in led:
            continue
        led[fp] = {'id': None, 'type': c['type'],
                   'tickers': c.get('tickers') or [], 'at': now,
                   'note': 'posted by hand; recorded so it is not repeated'}
        n += 1
    save_ledger(led)
    print(f'{n} recorded as already posted', file=sys.stderr)
    return 0


def main():
    live = '--live' in sys.argv
    if '--seed' in sys.argv:
        return seed()

    try:
        with io.open(POSTS, encoding='utf-8') as fh:
            payload = json.load(fh)
    except (IOError, ValueError) as e:
        print(f'no drafts to post: {e}', file=sys.stderr)
        return 0

    # A dry run needs no credentials: its whole point is to show what would
    # go out, which is a question about posts.json, not about the account.
    creds, why = credentials()
    if not creds and live:
        # Not an error: the job runs whether or not posting is switched on.
        print(f'X credentials {why}; nothing posted.', file=sys.stderr)
        return 0

    ledger = load_ledger()

    already = []
    if live:
        try:
            _, already = own_recent_texts(creds)
        except (RuntimeError, requests.RequestException) as e:
            print(f'could not read own timeline, refusing to post: {e}',
                  file=sys.stderr)
            return 1

    picked, skipped = choose(payload, ledger, already)

    for t, why in skipped:
        print(f'  skip {t}: {why}', file=sys.stderr)
    if not picked:
        print('nothing to post', file=sys.stderr)
        return 0

    cost = estimate(picked)
    print(f'{len(picked)} to post, estimated ${cost:.3f}', file=sys.stderr)
    if cost > MAX_RUN_COST:
        print(f'refusing: estimate exceeds ${MAX_RUN_COST:.2f} cap',
              file=sys.stderr)
        return 1

    if not live:
        for c in picked:
            print(f"\n--- {c['type']} ({c['chars']} chars) ---")
            print(c['text'])
            print(f'  reply: {reply_text(c)}')
        print(f'\nDRY RUN - pass --live to send. Estimated ${cost:.3f}.')
        return 0

    sent, requests_made, spent = [], 0, COST_OWNED_READ * (RECENT + 1)
    for c in picked:
        if requests_made + 2 > MAX_REQUESTS:
            print('request cap reached', file=sys.stderr)
            break
        try:
            tid = send(creds, c['text'])
            requests_made += 1
            spent += COST_POST
        except (RuntimeError, requests.RequestException) as e:
            print(f"failed to post {c['type']}: {e}", file=sys.stderr)
            continue

        # The post is out. Record it before the reply, so a reply that fails
        # cannot cause the post to be sent again tomorrow.
        ledger[fingerprint(c['text'])] = {
            'id': tid, 'type': c['type'],
            'tickers': c.get('tickers') or [],
            'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
        save_ledger(ledger)

        rid = None
        try:
            rid = send(creds, reply_text(c), reply_to=tid)
            requests_made += 1
            spent += COST_POST_URL
        except (RuntimeError, requests.RequestException) as e:
            print(f"posted {c['type']} but its reply failed: {e}",
                  file=sys.stderr)

        sent.append((c['type'], tid, rid))

    if not sent:
        return 1

    lines = ['*Posted to @overnightarc:*']
    for t, tid, rid in sent:
        lines.append(f"— {t} — https://x.com/overnightarc/status/{tid}"
                     + ('' if rid else '  _(link reply failed)_'))
    lines.append('')
    lines.append(f'_approx ${spent:.2f} of API credit_')
    print('\n'.join(lines))
    telegram(lines)
    return 0


if __name__ == '__main__':
    sys.exit(main())
