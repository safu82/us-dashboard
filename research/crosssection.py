"""Stage 4 — the cross-section. Where a name sits against everything else.

This is the half of a note that filings cannot provide and most research does
not have. A 10-Q tells you SanDisk earned $8.1bn on $11.0bn of revenue. Only
the cross-section tells you the market has that name at momentum 5 of 100 and
rs_rank 1,773 of 1,928 — i.e. priced as a cycle peak, not a growth story. The
two together are the finding; neither alone is.

Deterministic throughout. SQL plus one JSON file, no model, no tokens.

Conventions that are easy to get backwards, so they are stated once here:
  rs_rank        1 = strongest. A climbing name's number FALLS.
  momentum_score 0-100 percentile, higher = stronger. 50 is the median.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (.env auto-loaded).
"""
import io
import json
import os
import sys

from dotenv import load_dotenv
from supabase import create_client

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE, '.env'))

URL = os.environ.get('SUPABASE_URL')
KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not URL or not KEY:
    sys.exit('ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')
sb = create_client(URL, KEY)

CHAINS = os.path.join(BASE, 'portal', 'data', 'chains.json')
MIN_STAGE_NAMES = 3     # never cite a stage thinner than this
WEEK_TD, MONTH_TD = 5, 21

# Read the archive, not daily_stock_snapshots. The operational table keeps a
# 100-day rolling window, which is shorter than the gap between two filings —
# so a quarter-on-quarter cross-section is impossible against it by
# construction. The archive holds the same rows and never prunes.
TABLE = 'daily_snapshot_archive'
LIVE = 'daily_stock_snapshots'


# ---------------------------------------------------------------------------
# universe
# ---------------------------------------------------------------------------

def _max_date(table):
    r = (sb.table(table).select('snapshot_date')
         .order('snapshot_date', desc=True).limit(1).execute())
    return r.data[0]['snapshot_date'] if r.data else None


def latest_date():
    return _max_date(TABLE)


def health():
    """Is the archive keeping up with the live table?

    fetch_ohlc writes the archive then prunes the live window, so the two
    should agree on the newest session. If the archive is behind, a night's
    archive step failed and that history is about to be lost — which is worth
    saying loudly rather than quietly reading stale data.
    """
    a, l = _max_date(TABLE), _max_date(LIVE)
    return {'archive_latest': a, 'live_latest': l,
            'in_sync': a == l,
            'warning': None if a == l else
            (f'daily_snapshot_archive is at {a} but {LIVE} is at {l}. The '
             f'nightly archive step has not run or failed; sessions pruned '
             f'before it does are lost permanently.')}


def nearest_date(on_or_before):
    """The last session at or before a date.

    An evidence pack for the April filing must carry April's standing, not
    today's. Backfilling without this would stamp current momentum onto a
    six-month-old quarter and make every historical diff read as flat.
    """
    r = (sb.table(TABLE).select('snapshot_date')
         .lte('snapshot_date', on_or_before)
         .order('snapshot_date', desc=True).limit(1).execute())
    return r.data[0]['snapshot_date'] if r.data else None


def levels(ticker, asof=None):
    """The price map: where the stock sits against the lines people watch.

    Read from the live table rather than the archive, because moving averages
    are only meaningful at the current date and the archive deliberately does
    not carry them. Each level is returned with its distance from the close,
    which is the form a note uses it in.
    """
    asof = asof or _max_date(LIVE)
    r = (sb.table(LIVE).select(
            'close,high_52w,low_52w,ema_20,ema_50,ema_200,atr_14,rsi_14,'
            'vol_ratio,snapshot_date')
         .eq('ticker', ticker.upper()).eq('snapshot_date', asof)
         .limit(1).execute())
    if not r.data:
        return None
    d = r.data[0]
    close = d.get('close')
    if not close:
        return None
    out = {'asof': asof, 'close': close, 'levels': []}
    for label, key in (('52-week high', 'high_52w'), ('200-day EMA', 'ema_200'),
                       ('50-day EMA', 'ema_50'), ('20-day EMA', 'ema_20'),
                       ('52-week low', 'low_52w')):
        v = d.get(key)
        if v:
            out['levels'].append({
                'level': label, 'price': round(float(v), 2),
                'from_close_pct': round((float(v) - close) / close * 100, 1)})
    out['levels'].sort(key=lambda x: -x['price'])
    atr = d.get('atr_14')
    if atr:
        out['atr_14'] = round(float(atr), 2)
        out['atr_pct_of_close'] = round(float(atr) / close * 100, 2)
    for k in ('rsi_14', 'vol_ratio'):
        if d.get(k) is not None:
            out[k] = round(float(d[k]), 2)
    return out


def _page(table, columns, filters, page=1000):
    out, frm = [], 0
    while True:
        q = sb.table(table).select(columns)
        for f in filters:
            q = f(q)
        res = q.range(frm, frm + page - 1).execute()
        if not res.data:
            break
        out.extend(res.data)
        if len(res.data) < page:
            break
        frm += page
    return out


def universe(asof):
    """Every scored name on one date. One pull, reused by every lookup."""
    rows = _page(TABLE,
                 'ticker,close,momentum_score,rs_rank,high_52w,low_52w,'
                 'peer_group,peer_percentile,sector_percentile,bz_streak,'
                 'rank_slope',
                 [lambda q: q.eq('snapshot_date', asof),
                  lambda q: q.not_.is_('momentum_score', 'null')])
    return {r['ticker']: r for r in rows}


def history(ticker, limit=40):
    """Recent sessions for one name, newest first — for rank movement."""
    r = (sb.table(TABLE)
         .select('snapshot_date,close,momentum_score,rs_rank')
         .eq('ticker', ticker).order('snapshot_date', desc=True)
         .limit(limit).execute())
    return r.data or []


# ---------------------------------------------------------------------------
# value chains
# ---------------------------------------------------------------------------

def _stage_table():
    """Every stage with at least MIN_STAGE_NAMES scored members, ranked
    weakest first. Ranking the whole set is what lets a note say "24th
    weakest of 94" instead of an unanchored adjective."""
    with io.open(CHAINS, encoding='utf-8-sig') as fh:
        doc = json.load(fh)
    raw = doc['chains']
    chains = list(raw.values()) if isinstance(raw, dict) else raw
    stages = []
    for ch in chains:
        for L in ch.get('links', []):
            if L.get('thin') or (L.get('scored') or 0) < MIN_STAGE_NAMES:
                continue
            stages.append({
                'chain': ch['name'],
                'stage': L['name'],
                'mom': L['mom'],
                'scored': L['scored'],
                'members': [m for m in L.get('members', [])
                            if not m.get('context')],
            })
    stages.sort(key=lambda s: s['mom'])
    for i, s in enumerate(stages, 1):
        s['rank_weakest_first'] = i
    return doc.get('asOf'), stages


def stages_for(ticker, stages):
    hits = []
    for s in stages:
        if any(m.get('ticker') == ticker for m in s['members']):
            hits.append(s)
    return hits


# ---------------------------------------------------------------------------
# the pack
# ---------------------------------------------------------------------------

def _pct_from(close, ref):
    if not close or not ref:
        return None
    return round((close - ref) / ref * 100, 1)


def cross_section(ticker, asof=None, uni=None, stage_data=None):
    ticker = ticker.upper()
    asof = asof or latest_date()
    uni = uni if uni is not None else universe(asof)
    chains_asof, stages = stage_data or _stage_table()

    pack = {'ticker': ticker, 'asof': asof, 'chains_asof': chains_asof,
            'universe_scored': len(uni), 'covered': ticker in uni}

    if not pack['covered']:
        # A watchlist name we do not score is a real and common case. Say so
        # plainly; a note must not imply cross-sectional context it lacks.
        pack['note'] = (f'{ticker} is not in the scored universe on {asof}. '
                        f'No cross-sectional claim can be made about it.')
        return pack

    r = uni[ticker]
    mom, rank = r['momentum_score'], r['rs_rank']

    # Standing
    pack['standing'] = {
        'close': r['close'],
        'momentum': mom,
        'rs_rank': rank,
        'rs_rank_of': len(uni),
        'from_52w_high': _pct_from(r['close'], r['high_52w']),
        'from_52w_low': _pct_from(r['close'], r['low_52w']),
        'peer_group': r.get('peer_group'),
        'peer_percentile': r.get('peer_percentile'),
        'sector_percentile': r.get('sector_percentile'),
        'bz_streak': r.get('bz_streak'),
    }

    # Cohort: how many names share this condition. "46 of 1,928 at momentum
    # 5 or below" is the sentence this exists to support.
    at_or_below = sum(1 for v in uni.values()
                      if v['momentum_score'] is not None
                      and v['momentum_score'] <= mom)
    # Names the subject actually sits beside — a tight band around its own
    # score, not the floor of the market. Sorting from the floor made every
    # name "keep company" with the same dozen micro-caps, which is true of
    # nothing except the floor itself.
    band = sorted(
        [t for t, v in uni.items()
         if v['momentum_score'] is not None and t != ticker
         and abs(v['momentum_score'] - mom) <= 2],
        key=lambda t: (abs(uni[t]['momentum_score'] - mom), t))
    pack['cohort'] = {
        'at_or_below_momentum': at_or_below,
        'share_pct': round(at_or_below / len(uni) * 100, 2),
        'band_width': 2,
        'band_size': len(band),
        'band_names': band[:20],
    }

    # Movement. The GOOG note's "down 423 places in a week" is this.
    h = history(ticker)
    def back(n):
        return h[n] if len(h) > n else None
    mv = {}
    for label, n in (('1w', WEEK_TD), ('1m', MONTH_TD)):
        prev = back(n)
        if not prev:
            continue
        mv[f'rs_rank_{label}_ago'] = prev['rs_rank']
        mv[f'momentum_{label}_ago'] = prev['momentum_score']
        if prev['rs_rank'] is not None and rank is not None:
            # Positive = improved (rank number fell).
            mv[f'rank_change_{label}'] = prev['rs_rank'] - rank
    mv['sessions_available'] = len(h)
    pack['movement'] = mv

    # Value chain
    # chains.json supplies MEMBERSHIP only. Momentum for every peer is taken
    # from the same database row as the subject's, because the file is
    # rebuilt nightly and can lag the snapshot — on 2026-11-02 it still held
    # 10-30 scores, so GOOGL read 10 as a subject and 11 as a peer. One
    # vintage per pack, or the numbers quietly disagree with each other.
    stale = bool(chains_asof and chains_asof != asof)
    hits = stages_for(ticker, stages)
    pack['chain'] = []
    for s in hits:
        peers = []
        for m in s['members']:
            t = m.get('ticker')
            if t == ticker:
                continue
            row = uni.get(t)
            peers.append({'ticker': t,
                          'momentum': row['momentum_score'] if row else None,
                          'rs_rank': row['rs_rank'] if row else None,
                          'in_universe': bool(row)})
        peers.sort(key=lambda m: (m['momentum'] is None, m['momentum']))
        # The distribution, computed, so a note cannot describe the stage by
        # picking the members that suit it. A SanDisk note argued its fall
        # was group-wide while quietly omitting NTAP, the one peer that had
        # held up.
        import chain as _C
        peer_spread = _C.spread(peers + [{'ticker': ticker, 'momentum': mom}])
        pack['chain'].append({
            'spread_including_subject': peer_spread,
            'chain': s['chain'], 'stage': s['stage'],
            'stage_momentum_asof_chains': s['mom'],
            'stage_scored': s['scored'],
            'stage_rank_weakest_first': s['rank_weakest_first'],
            'stage_rank_of': len(stages),
            'peers': peers,
        })
    if stale:
        pack['chain_staleness'] = (
            f'Stage membership and stage momentum come from chains.json as of '
            f'{chains_asof}, while prices and ranks are {asof}. Peer momentum '
            f'has been re-read from the {asof} snapshot; the stage-level score '
            f'has not and is {chains_asof} vintage.')
    if not hits:
        pack['chain_note'] = (f'{ticker} is not mapped to any value-chain '
                              f'stage with {MIN_STAGE_NAMES}+ scored names.')

    # Peer group from the scanner (distinct from the hand-mapped chains)
    pg = r.get('peer_group')
    if pg:
        group = [v for v in uni.values() if v.get('peer_group') == pg]
        group.sort(key=lambda v: v['rs_rank'] or 10 ** 9)
        pack['peer_group'] = {
            'name': pg, 'members': len(group),
            'strongest': [{'ticker': v['ticker'], 'momentum': v['momentum_score'],
                           'rs_rank': v['rs_rank']} for v in group[:5]],
            'weakest': [{'ticker': v['ticker'], 'momentum': v['momentum_score'],
                         'rs_rank': v['rs_rank']} for v in group[-5:]],
        }
    return pack


def render(p):
    if not p.get('covered'):
        return f"{p['ticker']}: {p.get('note')}"
    s, out = p['standing'], []
    out.append(f"{p['ticker']} cross-section — close {p['asof']}")
    out.append(f"  close {s['close']:,.2f}   momentum {s['momentum']}/100   "
               f"rs_rank {s['rs_rank']:,} of {s['rs_rank_of']:,}")
    out.append(f"  {s['from_52w_high']:+.1f}% from 52w high, "
               f"{s['from_52w_low']:+.1f}% from low"
               + (f"   peer group {s['peer_group']}" if s['peer_group'] else ''))
    c = p['cohort']
    out.append(f"  cohort: {c['at_or_below_momentum']:,} of "
               f"{p['universe_scored']:,} names ({c['share_pct']}%) at "
               f"momentum {s['momentum']} or below")
    if c['band_names']:
        out.append(f"    sits beside ({c['band_size']} names within "
                   f"+/-{c['band_width']}): {' '.join(c['band_names'][:12])}")
    m = p.get('movement') or {}
    if 'rank_change_1w' in m:
        out.append(f"  rank 1w ago {m['rs_rank_1w_ago']:,} "
                   f"({m['rank_change_1w']:+,} places)"
                   + (f", 1m ago {m['rs_rank_1m_ago']:,} "
                      f"({m['rank_change_1m']:+,})"
                      if 'rank_change_1m' in m else ''))
    for ch in p.get('chain', []):
        out.append(f"  chain: {ch['chain']} / {ch['stage']} — stage momentum "
                   f"{ch['stage_momentum_asof_chains']} ({p['chains_asof']}), "
                   f"{ch['stage_rank_weakest_first']} weakest of "
                   f"{ch['stage_rank_of']} ({ch['stage_scored']} names)")
        out.append('    peers: ' + ', '.join(
            f"{x['ticker']} {x['momentum'] if x['in_universe'] else 'n/s'}"
            for x in ch['peers']))
    if p.get('chain_note'):
        out.append('  ' + p['chain_note'])
    if p.get('chain_staleness'):
        out.append('  ! ' + p['chain_staleness'])
    return '\n'.join(out)


def save(pack, path):
    with io.open(path, 'w', encoding='utf-8') as fh:
        json.dump(pack, fh, indent=1, sort_keys=True)
    return path


if __name__ == '__main__':
    tickers = [t.upper() for t in sys.argv[1:]] or ['SNDK']
    d = latest_date()
    uni = universe(d)
    sd = _stage_table()
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
    os.makedirs(out_dir, exist_ok=True)
    for t in tickers:
        pack = cross_section(t, d, uni, sd)
        print('\n' + render(pack))
        save(pack, os.path.join(out_dir, f'{t}_{d}_cross.json'))

