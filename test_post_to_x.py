#!/usr/bin/env python3
"""Tests for post_to_x.py. Run: python test_post_to_x.py

Two things here are worth a permanent test.

The OAuth signature, because a wrong one fails as a flat 401 with nothing to
say which of the base string, the encoding, the parameter sort or the key was
wrong - and the X API is now billed per request, so debugging it live costs
money. X publishes a worked example with the nonce and timestamp pinned, so
the signature is reproducible; that example is the test.

The duplicate guards, because the failure they prevent is public. Matching on
exact text is not enough: the same company rendered into a slightly reworded
sentence is a different string and the same post to a reader.
"""
import re
import sys
import time
import urllib.parse

import post_to_x as P

FAILS = []


def check(name, got, want):
    ok = got == want
    print('%-46s %s' % (name, 'ok' if ok else 'FAIL  got=%r want=%r'
                        % (got, want)))
    if not ok:
        FAILS.append(name)


# -- the signature ---------------------------------------------------------
# https://docs.x.com/resources/fundamentals/authentication - worked example.
def test_signature():
    creds = {
        'api_key': 'xvz1evFS4wEEPTGEFPHBog',
        'api_secret': 'kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw',
        'access_token': '370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb',
        'access_secret': 'LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE',
    }
    query = {'include_entities': 'true',
             'status': 'Hello Ladies + Gentlemen, a signed OAuth request!'}

    real_uuid, real_time = P.uuid, P.time
    P.uuid = type('u', (), {'uuid4': staticmethod(lambda: type(
        'h', (), {'hex': 'kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg'})())})
    P.time = type('t', (), {'time': staticmethod(lambda: 1318622958)})
    try:
        hdr = P._auth_header(
            'POST', 'https://api.twitter.com/1.1/statuses/update.json',
            creds, query)
    finally:
        P.uuid, P.time = real_uuid, real_time

    sig = urllib.parse.unquote(
        re.search(r'oauth_signature="([^"]+)"', hdr).group(1))
    check('oauth signature matches the published example',
          sig, 'hCtSmYh+iHYCEqBWrE7C7hYmtUk=')
    for f in ('oauth_consumer_key', 'oauth_nonce', 'oauth_signature_method',
              'oauth_timestamp', 'oauth_token', 'oauth_version'):
        check('header carries ' + f, f + '=' in hdr, True)


# -- percent-encoding ------------------------------------------------------
def test_quote():
    # OAuth leaves ~ alone and escapes everything else outside unreserved.
    check('_q leaves ~ alone', P._q('a~b'), 'a~b')
    check('_q escapes +', P._q('a+b'), 'a%2Bb')
    check('_q escapes space', P._q('a b'), 'a%20b')


# -- the duplicate guards --------------------------------------------------
def _cand(**kw):
    base = {'type': 'no_coverage', 'auto': True, 'approved': True,
            'chars': 100, 'link': 'https://overnightarc.com/digest/x/',
            'tickers': ['PACB'], 'text': 'Pacific Biosciences rose 15.5%.'}
    base.update(kw)
    return base


def test_guards():
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    old = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                        time.gmtime(time.time() - 5 * 86400))
    led = {'x': {'id': '1', 'type': 'no_coverage',
                 'tickers': ['PACB', 'WEN'], 'at': now}}

    def picked(gen, ledger=None, already=None):
        p, _ = P.choose({'generated': gen}, ledger or {}, already or [])
        return len(p)

    reworded = _cand(text='Pacific Biosciences rose 15.5% yesterday and no '
                          'outlet wrote it up. A different sentence.')

    check('reworded post, same company, inside window',
          picked([reworded], led), 0)
    check('reworded post, same company, outside window',
          picked([reworded], {'x': dict(led['x'], at=old)}), 1)
    check('different company goes',
          picked([_cand(tickers=['ZZZ'], text='Other Co rose 12%.')], led), 1)
    check('exact text already on the timeline',
          picked([reworded], {}, [reworded['text']]), 0)
    check('link in the body is refused',
          picked([_cand(tickers=['YYY'],
                        text='See https://overnightarc.com/x/')]), 0)
    check('unapproved template is refused',
          picked([_cand(approved=False)]), 0)
    check('non-auto template is refused',
          picked([_cand(auto=False)]), 0)
    check('candidate with no link is refused',
          picked([_cand(link=None)]), 0)
    check('at most MAX_CANDIDATES go in one run',
          picked([_cand(tickers=['A%d' % i], text='Co %d rose 9%%.' % i)
                  for i in range(5)]), P.MAX_CANDIDATES)
    check('a ledger entry with a bad timestamp is ignored, not fatal',
          picked([reworded], {'x': dict(led['x'], at='not a date')}), 1)


# -- the cost estimate -----------------------------------------------------
def test_estimate():
    # Two candidates: two posts, two link replies, plus the timeline read.
    want = round(2 * P.COST_POST + 2 * P.COST_POST_URL
                 + P.COST_OWNED_READ * (P.RECENT + 1), 4)
    check('estimate for two candidates', P.estimate([_cand(), _cand()]), want)
    check('estimate stays under the run cap', want <= P.MAX_RUN_COST, True)


def test_fingerprint():
    check('fingerprint ignores case and spacing',
          P.fingerprint('A  b\nC') == P.fingerprint('a b c'), True)
    check('fingerprint separates different claims',
          P.fingerprint('a') == P.fingerprint('b'), False)


if __name__ == '__main__':
    for fn in (test_signature, test_quote, test_guards, test_estimate,
               test_fingerprint):
        print('\n-- %s' % fn.__name__)
        fn()
    print('\n%d failing' % len(FAILS))
    for f in FAILS:
        print('  ' + f)
    sys.exit(1 if FAILS else 0)
