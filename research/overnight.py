"""Produce a finished note on a company that reports tonight, unattended.

    python research/overnight.py AMD

Earnings land after the US close, which is the middle of the night here. This
waits for the 8-K to appear on EDGAR, then runs the whole pipeline —
press release, filing stages, note, critic, revision, Word — and leaves a log
beside the output saying what it did and what it could not do.

Two things make this different from running the stages by hand.

It waits for the RIGHT filing, tested against the report date rather than
against whatever was newest when it started. The first live run started a
few minutes after the release had already been accepted, took that filing as
its baseline, and then waited for something newer that did not exist. So the
test is the filing's own date: any earnings 8-K stamped on or after the
report date is the release, whether it arrived before this script did or
after. EDGAR stamps in US Eastern and this runs from Dubai after the close,
where it is already the next day, so the comparison is made in Eastern.

It expects the XBRL to be missing. A 10-Q lands a day or more after the
release — AMD filed Q2's on the day after its 8-K — so on results night there
is no tagged data for the quarter just reported. The writer detects that and
withholds every trailing multiple rather than reaching for last quarter's
numbers; this script does not try to work around it, it just records that the
note was built from the release and consensus.
"""
import argparse
import io
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
OUT = os.path.join(HERE, 'out')

# EDGAR wants a contact address on every request. The pipeline is useless
# without it and the failure is a 403 several calls later, so fail here.
if not os.environ.get('EDGAR_UA'):
    os.environ['EDGAR_UA'] = 'overnightarc-research admin@overnightarc.com'

import edgar                                                  # noqa: E402

LOG_LINES = []


def log(msg):
    stamp = datetime.now().strftime('%H:%M:%S')
    line = f'[{stamp}] {msg}'
    LOG_LINES.append(line)
    print(line, flush=True)


def save_log(ticker):
    path = os.path.join(OUT, f'overnight_{ticker}_'
                             f'{datetime.now():%Y-%m-%d}.log')
    try:
        with io.open(path, 'a', encoding='utf-8') as fh:
            fh.write('\n'.join(LOG_LINES) + '\n')
    except OSError as e:
        print(f'could not write log: {e}')
    return path


def newest_earnings_8k(ticker):
    f = edgar.earnings_8k(ticker, limit=1)
    return f[0] if f else None


def wait_for_release(ticker, since, minutes, every):
    """Poll until an earnings 8-K filed on or after `since` is on file.

    The test is the filing's own date against the expected report date, not
    whether it differs from whatever was newest when this started. Comparing
    against the start state assumes the script got there first, and on the
    first live run it did not: the release had already been accepted, so the
    new filing was recorded as the baseline and the script sat waiting for a
    newer one that was never coming. A filing already on file is the result,
    not the previous quarter's.

    `since` is an EDGAR filing date (US Eastern), so the caller passes the
    report date in Eastern terms — here it is already tomorrow.
    """
    deadline = time.time() + minutes * 60
    attempt = 0
    while True:
        attempt += 1
        try:
            f = newest_earnings_8k(ticker)
        except Exception as e:                                # noqa: BLE001
            log(f'  poll {attempt}: EDGAR error, will retry — {e}')
            f = None
        if f and f['filed'] >= since:
            log(f'  poll {attempt}: release on file — {f["accession"]} '
                f'filed {f["filed"]}, items {f["items"]}')
            return f
        if f:
            log(f'  poll {attempt}: newest is {f["accession"]} filed '
                f'{f["filed"]}, before {since} — not the release yet')
        if time.time() + every > deadline:
            return None
        time.sleep(every)


def stage(name, fn, *a, **k):
    """Run one stage. A failure is logged and returned, never raised: a note
    without a transcript is worth having, and nothing here should take the
    whole run down with it."""
    log(f'{name} ...')
    try:
        r = fn(*a, **k)
        if isinstance(r, dict) and r.get('error'):
            log(f'  {name}: {r["error"]}')
        return r
    except Exception as e:                                    # noqa: BLE001
        log(f'  {name} FAILED — {type(e).__name__}: {e}')
        return {'error': str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('ticker')
    ap.add_argument('--wait', type=int, default=75,
                    help='minutes to wait for the 8-K (default 75)')
    ap.add_argument('--every', type=int, default=180,
                    help='seconds between polls (default 180)')
    ap.add_argument('--no-wait', action='store_true',
                    help='use the newest 8-K already on file')
    ap.add_argument('--since', default=None,
                    help='report date as EDGAR dates it, US Eastern '
                         '(default: today in Eastern)')
    a = ap.parse_args()
    t = a.ticker.upper()
    os.makedirs(OUT, exist_ok=True)
    log(f'=== overnight {t} — {datetime.now():%Y-%m-%d %H:%M} local ===')

    # EDGAR stamps filings in US Eastern. Run from Dubai after the close it is
    # already the next day locally, so local date would reject the release.
    since = a.since or (datetime.now(timezone.utc)
                        - timedelta(hours=5)).strftime('%Y-%m-%d')

    try:
        b = newest_earnings_8k(t)
        log(f'newest earnings 8-K on file: '
            f'{b["accession"] if b else "-"} (filed {b["filed"] if b else "-"})')
    except Exception as e:                                    # noqa: BLE001
        log(f'could not read EDGAR: {e}')
        save_log(t)
        return 1

    if a.no_wait:
        filing = b
        log('--no-wait: using the filing already on file')
    else:
        log(f'release is any earnings 8-K filed on or after {since}; '
            f'waiting up to {a.wait}m, polling every {a.every}s')
        filing = wait_for_release(t, since, a.wait, a.every)
        if not filing:
            log(f'NO RELEASE FOUND within {a.wait} minutes. Nothing written. '
                f'Either the company has not filed yet or it reports on a '
                f'different date than expected.')
            save_log(t)
            return 2

    # Consensus must be re-fetched. The cached copy was taken before the
    # print, so it carries the estimate with no actual beside it and the
    # note would have nothing to say about the beat.
    for tag in ('earnings', 'estimates'):
        p = os.path.join(HERE, 'cache', f'{t}_{tag}.json')
        if os.path.exists(p):
            try:
                shutil.move(p, p + '.pre_print')
                log(f'cleared cached {tag} (kept as .pre_print)')
            except OSError as e:
                log(f'could not clear cached {tag}: {e}')

    import pressrelease as PR
    rel = stage('press release', PR.read, t, out_dir=OUT)
    if isinstance(rel, dict) and not rel.get('error'):
        e = rel.get('extraction') or {}
        checks = rel.get('quote_checks') or []
        good = sum(1 for c in checks if c.get('verdict') in ('VERBATIM',
                                                             'SPLICED'))
        log(f'  {len(e.get("financials") or [])} figures, '
            f'{len(e.get("guidance") or [])} guidance, '
            f'{len(e.get("quotes") or [])} quotes; '
            f'{good}/{len(checks)} verified against the document')
        for g in (e.get('guidance') or []):
            log(f'  GUIDANCE {g.get("metric")} / {g.get("period")}: '
                f'{g.get("value")}')

    # Filing stages. The 10-Q for this quarter is probably not filed yet, in
    # which case this refreshes the prior one and the writer withholds the
    # trailing multiples. That is the intended outcome, not a failure.
    import run as R
    stage('filing stages', R.run, t)

    import writer as W
    w = stage('note', W.write, t)
    if isinstance(w, dict) and w.get('error'):
        save_log(t)
        return 3
    log(f'  {os.path.basename(w["note_path"])}  '
        f'${w["usage"]["cost_usd"]:.4f}')
    if w.get('warning'):
        log(f'  {w["warning"]}')

    import critic as K
    c = stage('critic', K.review, t)
    if isinstance(c, dict) and not c.get('error'):
        n = c.get('number_audit') or {}
        log(f'  {n.get("figures_checked")} figures, '
            f'{len(n.get("unmatched") or [])} unmatched, '
            f'{n.get("distinctive_figures")} distinctive')
        log(f'  {len(c.get("challenges") or [])} challenges, '
            f'${c.get("usage", {}).get("cost_usd", 0):.4f}')
        for ch in (c.get('challenges') or [])[:4]:
            log(f'    [{ch.get("severity")}] {ch.get("type")}: '
                f'{str(ch.get("problem"))[:110]}')
        if c.get('strongest_omission'):
            log(f'  omission: {str(c["strongest_omission"])[:150]}')

    final = w['note_path']
    if isinstance(c, dict) and not c.get('error'):
        r = stage('revision', W.revise, t)
        if isinstance(r, dict) and not r.get('error'):
            final = r.get('path') or final
            log(f'  {os.path.basename(final)}  '
                f'${r.get("usage", {}).get("cost_usd", 0):.4f}')

    import to_docx as D
    d = stage('word', D.convert, final)
    log(f'FINAL: {os.path.basename(d) if isinstance(d, str) else final}')
    log('=== done ===')
    save_log(t)
    return 0


if __name__ == '__main__':
    sys.exit(main())
