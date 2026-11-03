"""Stage 5 — estimates, surprises and the valuation build.

Without consensus there is no beat, no miss, no surprise and no forward
multiple, and a note collapses into a recital of the filing. This supplies
the three things that let it say something instead:

  surprise history   did the quarter beat, and is the beat growing or fading
  forward consensus  what the street expects, how many analysts, how wide
  revision trend     which way estimates have moved over 90 days

The revision trend is the part most often missing elsewhere and the most
useful. A stock whose estimates are rising while its relative strength
collapses is telling you something different from one where both fall
together, and only the pair distinguishes a de-rating from a downgrade.

All arithmetic happens here. The writer receives finished figures.
"""
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except ImportError:
    pass

BASE = 'https://www.alphavantage.co/query'
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
PAUSE = 1.5          # free tier wants about one request a second


def _call(params, ticker, tag, refresh=False):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f'{ticker.upper()}_{tag}.json')
    if os.path.exists(path) and not refresh:
        return json.load(io.open(path, encoding='utf-8'))
    key = os.environ.get('ALPHAVANTAGE_API_KEY')
    if not key:
        raise RuntimeError('ALPHAVANTAGE_API_KEY not set')
    url = BASE + '?' + urllib.parse.urlencode(dict(params, apikey=key))
    req = urllib.request.Request(url, headers={'User-Agent': 'research/1.0'})
    time.sleep(PAUSE)
    with urllib.request.urlopen(req, timeout=45) as r:
        d = json.loads(r.read())
    for w in ('Note', 'Information', 'Error Message'):
        if w in d:
            raise RuntimeError(f'{ticker} {tag}: {str(d[w])[:180]}')
    json.dump(d, io.open(path, 'w', encoding='utf-8'), indent=1)
    return d


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def surprises(ticker, n=8, refresh=False):
    """Reported vs expected, most recent first, with the trend in the beat."""
    d = _call({'function': 'EARNINGS', 'symbol': ticker.upper()},
              ticker, 'earnings', refresh)
    rows = []
    for r in (d.get('quarterlyEarnings') or [])[:n]:
        rows.append({
            'period': r.get('fiscalDateEnding'),
            'reported_eps': _f(r.get('reportedEPS')),
            'estimated_eps': _f(r.get('estimatedEPS')),
            'surprise': _f(r.get('surprise')),
            'surprise_pct': _f(r.get('surprisePercentage')),
        })
    beats = [r for r in rows if r['surprise_pct'] is not None]
    streak = 0
    for r in beats:
        if r['surprise_pct'] > 0:
            streak += 1
        else:
            break
    out = {'quarters': rows, 'beat_streak': streak}
    if len(beats) >= 2:
        out['latest_surprise_pct'] = beats[0]['surprise_pct']
        out['prior_surprise_pct'] = beats[1]['surprise_pct']
        out['surprise_narrowing'] = (
            abs(beats[0]['surprise_pct']) < abs(beats[1]['surprise_pct']))
    return out


def consensus(ticker, refresh=False):
    """Forward estimates by horizon, with how far they have been revised."""
    d = _call({'function': 'EARNINGS_ESTIMATES', 'symbol': ticker.upper()},
              ticker, 'estimates', refresh)
    out = []
    for e in (d.get('estimates') or []):
        avg = _f(e.get('eps_estimate_average'))
        ago90 = _f(e.get('eps_estimate_average_90_days_ago'))
        row = {
            'date': e.get('date'), 'horizon': e.get('horizon'),
            'eps_avg': avg,
            'eps_high': _f(e.get('eps_estimate_high')),
            'eps_low': _f(e.get('eps_estimate_low')),
            'analysts': _f(e.get('eps_estimate_analyst_count')),
            'eps_avg_90d_ago': ago90,
            'revenue_avg': _f(e.get('revenue_estimate_average')),
        }
        if avg is not None and ago90:
            row['eps_revision_90d_pct'] = round((avg - ago90) / abs(ago90) * 100, 1)
        if row['eps_high'] and row['eps_low'] and avg:
            # How much the street disagrees. A wide band on a cyclical name
            # means the multiple depends on whose number you take.
            row['dispersion_pct'] = round(
                (row['eps_high'] - row['eps_low']) / abs(avg) * 100, 1)
        out.append(row)
    out.sort(key=lambda r: r['date'] or '')
    return out


def valuation(close, cons, horizons=('fiscal year',)):
    """Forward multiples on consensus, at a stated price."""
    rows = []
    for c in cons:
        if c['horizon'] not in horizons or not c['eps_avg'] or not close:
            continue
        row = {'date': c['date'], 'analysts': c['analysts'],
               'eps_avg': c['eps_avg'],
               'pe_on_consensus': round(close / c['eps_avg'], 2)}
        if c.get('eps_high'):
            row['pe_on_high'] = round(close / c['eps_high'], 2)
        if c.get('eps_low'):
            row['pe_on_low'] = round(close / c['eps_low'], 2)
        row['eps_revision_90d_pct'] = c.get('eps_revision_90d_pct')
        rows.append(row)
    return rows


def pack(ticker, close=None, refresh=False):
    out = {'ticker': ticker.upper(), 'close_used': close}
    try:
        out['surprises'] = surprises(ticker, refresh=refresh)
    except Exception as e:
        out['surprises_error'] = str(e)
    try:
        cons = consensus(ticker, refresh=refresh)
        out['consensus'] = cons
        if close:
            out['forward_multiples'] = valuation(close, cons)
    except Exception as e:
        out['consensus_error'] = str(e)
    return out


def render(p):
    L = [f"{p['ticker']} estimates"]
    s = p.get('surprises') or {}
    if s.get('quarters'):
        L.append(f"  surprise history (beat streak {s.get('beat_streak')}):")
        for r in s['quarters'][:5]:
            # The quarter a company is about to report is already in the feed,
            # carrying consensus with no actual beside it. That row is the
            # most interesting one tonight and the least printable: render it
            # as pending rather than formatting None.
            if r['estimated_eps'] is None:
                continue
            if r['reported_eps'] is None:
                L.append(f"    {r['period']}  reported        -  "
                         f"est {r['estimated_eps']:>8}   not yet reported")
                continue
            L.append(f"    {r['period']}  reported {r['reported_eps']:>8}  "
                     f"est {r['estimated_eps']:>8}  "
                     f"{r['surprise_pct']:+.1f}%")
        if s.get('surprise_narrowing') is not None:
            L.append('    beat is ' + ('narrowing' if s['surprise_narrowing']
                                       else 'widening'))
    for c in (p.get('consensus') or []):
        if c['horizon'] != 'fiscal year':
            continue
        rev = (f"  revised {c['eps_revision_90d_pct']:+.1f}% in 90d"
               if c.get('eps_revision_90d_pct') is not None else '')
        L.append(f"  FY to {c['date']}: EPS {c['eps_avg']:.2f} "
                 f"({int(c['analysts'] or 0)} analysts, "
                 f"range {c['eps_low']:.0f}-{c['eps_high']:.0f}){rev}")
    for m in (p.get('forward_multiples') or []):
        L.append(f"    at {p['close_used']:,.2f}: {m['pe_on_consensus']}x "
                 f"consensus, {m['pe_on_low']}x on the low estimate, "
                 f"{m['pe_on_high']}x on the high")
    for k in ('surprises_error', 'consensus_error'):
        if p.get(k):
            L.append(f"  {k}: {p[k]}")
    return '\n'.join(L)


if __name__ == '__main__':
    import crosssection as X
    for t in (sys.argv[1:] or ['SNDK']):
        c = None
        try:
            uni = X.universe(X.latest_date())
            r = uni.get(t.upper())
            c = r['close'] if r else None
        except Exception:
            pass
        print('\n' + render(pack(t, c)))
