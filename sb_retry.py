#!/usr/bin/env python3
"""Retry transient Supabase failures, for every query in the process.

Import it for the side effect, before any Supabase call:

    import sb_retry  # noqa: F401

Why a patch rather than a wrapper at each call site: the nightly chain talks to
Supabase from ten scripts across dozens of places, and the call that brought the
chain down on 2026-10-15 was an ordinary paged read inside a helper. A policy
that has to be remembered at every call site is one that will be missed at a
call site.

WHAT IS RETRIED — failures where the request never got a considered answer:
  * Cloudflare gateway errors in front of Supabase (502, 503, 504, 520-524),
    which is what happened on 2026-10-15: the pull of daily_stock_snapshots got
    an HTML "Bad gateway" page roughly 100 seconds in, and nine downstream
    steps were skipped for the night.
  * Connection and read timeouts, and protocol errors mid-response.
  * postgrest's "JSON could not be generated", which is what it raises when it
    tries to parse one of those HTML error pages as a response body.

WHAT IS NOT RETRIED — anything the server answered deliberately:
  * 4xx. A bad query or a bad key fails again identically, and retrying only
    delays the error by half a minute.
  * Statement timeout (57014). That means the query itself is too large, and
    the fix is keyset pagination rather than asking again.

ON WRITES: retrying a write that may already have landed is safe only when the
write is idempotent. Every write in the nightly chain is an upsert carrying an
on_conflict key, or a delete — both give the same result applied twice — so
this does not distinguish reads from writes. If a plain insert is ever added to
the chain, that assumption has to be revisited.
"""

import os
import random
import sys
import time

import httpx
from postgrest._sync import request_builder as _rb
from postgrest.exceptions import APIError

# Five attempts over roughly 25 seconds of backoff. Long enough to ride out the
# gateway blips that prompted this, short enough that a genuine outage still
# fails the run inside the job's timeout rather than burning it.
ATTEMPTS = int(os.environ.get('SB_RETRY_ATTEMPTS', '5'))
BASE_DELAY = float(os.environ.get('SB_RETRY_BASE_DELAY', '1.5'))
MAX_DELAY = float(os.environ.get('SB_RETRY_MAX_DELAY', '20'))

RETRY_STATUS = {408, 429, 502, 503, 504, 520, 521, 522, 523, 524}
RETRY_EXC = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
             httpx.WriteTimeout, httpx.PoolTimeout, httpx.ReadError,
             httpx.RemoteProtocolError)


def _status(err):
    """APIError.code is an int for HTTP statuses and a string like 'PGRST116'
    or '57014' for errors Postgres itself raised. Only the former are retried,
    so anything that will not parse as an int is treated as deliberate."""
    try:
        return int(getattr(err, 'code', None))
    except (TypeError, ValueError):
        return None


def transient(err):
    if isinstance(err, RETRY_EXC):
        return True
    if isinstance(err, APIError):
        if _status(err) in RETRY_STATUS:
            return True
        return 'JSON could not be generated' in (getattr(err, 'message', '') or '')
    return False


def _describe(err):
    if isinstance(err, APIError):
        return f'code={getattr(err, "code", "?")} {getattr(err, "message", "")}'[:120]
    return str(err)[:120]


def _wrap(fn):
    def execute(self, *args, **kwargs):
        for attempt in range(1, ATTEMPTS + 1):
            try:
                return fn(self, *args, **kwargs)
            except Exception as err:
                if attempt == ATTEMPTS or not transient(err):
                    raise
                delay = min(BASE_DELAY * 2 ** (attempt - 1), MAX_DELAY)
                delay *= 0.7 + 0.6 * random.random()   # jitter, so parallel
                #                                        callers do not resync
                # stderr and unbuffered: this has to show up in the Actions log
                # interleaved with the step that is stalling, not after it.
                print(f'  [sb_retry] {type(err).__name__} {_describe(err)} '
                      f'-> attempt {attempt}/{ATTEMPTS} failed, '
                      f'retrying in {delay:.1f}s', file=sys.stderr, flush=True)
                time.sleep(delay)
    execute.__wrapped__ = fn
    return execute


def _install():
    """Patch the builders that define execute; the rest inherit it."""
    done = []
    for name in ('SyncQueryRequestBuilder', 'SyncSingleRequestBuilder',
                 'SyncMaybeSingleRequestBuilder', 'SyncExplainRequestBuilder'):
        cls = getattr(_rb, name, None)
        if cls is None:
            continue
        fn = cls.__dict__.get('execute')
        if fn is None or hasattr(fn, '__wrapped__'):
            continue                      # inherited, or already patched
        cls.execute = _wrap(fn)
        done.append(name)
    return done


PATCHED = _install()

if not PATCHED:
    # Loud rather than silent: a postgrest upgrade that renames these classes
    # would otherwise quietly remove every retry in the nightly chain.
    print('WARNING: sb_retry patched nothing — postgrest internals may have '
          'changed; Supabase calls are NOT being retried', file=sys.stderr)


if __name__ == '__main__':
    print(f'sb_retry: patched {len(PATCHED)} builder(s): {", ".join(PATCHED)}')
    print(f'  attempts={ATTEMPTS} base={BASE_DELAY}s max={MAX_DELAY}s')
    print(f'  retry on HTTP {sorted(RETRY_STATUS)}')
    print(f'  retry on {", ".join(e.__name__ for e in RETRY_EXC)}')
