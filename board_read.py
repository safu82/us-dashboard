#!/usr/bin/env python3
"""What the boards say, computed rather than narrated.

The digest answers "what was reported". This answers "what the numbers did",
which is a different question and the one the site is actually for. A story
count tells you what newsrooms chose to write about; breadth and rank changes
tell you what the market did whether anybody wrote about it or not.

Four things, in the order they matter:

  regime    Where the benchmark's 20-session return sits against the +-2%
            switch the Funds board uses. A cross-sectional score cannot say
            what the market did, only where something stands inside it, so the
            direction has to be stated separately or every board reads green.

  breadth   Advancers against decliners, and the share of the universe above
            its 50-day average. Already computed nightly into market_health by
            step 8, so this reads it rather than recomputing it.

  movers    The largest momentum changes among individual stocks. A big price
            move is not the same as a change in standing; this is the second.

  funds     The largest changes in rank on the Funds board, which is where
            rotation shows up before anybody names it.

Everything here comes from tables the nightly chain has already written. No
network beyond Supabase, no price feed, no browser. It can therefore run inside
the scheduled job, which the X cross-check never can.
"""


def _page(sb, table, cols, f=None, size=1000):
    out, frm = [], 0
    while True:
        q = sb.table(table).select(cols)
        if f:
            q = f(q)
        rows = q.range(frm, frm + size - 1).execute().data or []
        out.extend(rows)
        if len(rows) < size:
            return out
        frm += size


def _band(r):
    """The three states the Funds board's regime line can be in."""
    if r is None:
        return 'unknown'
    if r <= -0.02:
        return 'down'
    if r >= 0.02:
        return 'up'
    return 'flat'


def board_read(sb, session, prev, n=5):
    """A dict of what the boards say on `session` against `prev`.

    Returns None rather than a half-filled shape when the session is not
    scored, so a caller can leave the section out instead of printing zeroes.
    """
    etf = {}
    for r in (sb.table('etf_snapshots')
              .select('symbol,snapshot_date,momentum_score,etf_rank,ret_20')
              .in_('snapshot_date', [session, prev]).execute().data or []):
        etf.setdefault(r['snapshot_date'], {})[r['symbol']] = r
    now, was = etf.get(session, {}), etf.get(prev, {})
    if not now:
        return None

    def ret20(d, sym):
        v = (d.get(sym) or {}).get('ret_20')
        return float(v) if v is not None else None

    spy, spy_prev = ret20(now, 'SPY'), ret20(was, 'SPY')
    qqq, qqq_prev = ret20(now, 'QQQ'), ret20(was, 'QQQ')

    # Distance to the next switch, so "flat" carries information. -0.03% and
    # +1.9% are both flat and mean quite different things.
    to_up = (0.02 - spy) if spy is not None else None
    to_down = (spy + 0.02) if spy is not None else None

    health = {r['snapshot_date']: r for r in
              (sb.table('market_health')
               .select('snapshot_date,universe_count,pct_above_50d,'
                       'pct_above_200d,new_highs,new_lows,advancers,decliners,'
                       'vix_close,sp_above_200d')
               .in_('snapshot_date', [session, prev]).execute().data or [])}
    h, hp = health.get(session) or {}, health.get(prev) or {}

    def num(d, k):
        v = d.get(k)
        return float(v) if v is not None else None

    # Funds: rank is already the board's own rank, so a change in it is the
    # Delta rank column rather than a proxy for it.
    fund_moves = []
    for sym, a in now.items():
        b = was.get(sym)
        if not b or a.get('etf_rank') is None or b.get('etf_rank') is None:
            continue
        fund_moves.append({
            'symbol': sym,
            'rank': a['etf_rank'], 'rank_prev': b['etf_rank'],
            'd_rank': b['etf_rank'] - a['etf_rank'],
            'mom': a.get('momentum_score'), 'mom_prev': b.get('momentum_score'),
            'ret20': ret20(now, sym), 'ret20_prev': ret20(was, sym),
        })
    fund_moves.sort(key=lambda x: -abs(x['d_rank']))

    # Stocks: two full pulls, because momentum lives per row and there is no
    # server-side way to diff two dates through PostgREST.
    cols = 'ticker,snapshot_date,momentum_score,rs_rank,bz_streak,close'
    rows = _page(sb, 'daily_stock_snapshots', cols,
                 lambda q: q.in_('snapshot_date', [session, prev])
                            .not_.is_('momentum_score', 'null'))
    by = {}
    for r in rows:
        by.setdefault(r['ticker'], {})[r['snapshot_date']] = r

    moves = []
    for tk, d in by.items():
        a, b = d.get(session), d.get(prev)
        if not a or not b:
            continue
        try:
            day = float(a['close']) / float(b['close']) - 1
        except (TypeError, ZeroDivisionError):
            day = None
        moves.append({
            'ticker': tk,
            'mom': a['momentum_score'], 'mom_prev': b['momentum_score'],
            'd_mom': a['momentum_score'] - b['momentum_score'],
            'rs': a.get('rs_rank'), 'rs_prev': b.get('rs_rank'),
            'bz': a.get('bz_streak'), 'day': day,
        })
    ups = sorted(moves, key=lambda x: -x['d_mom'])[:n]
    downs = sorted(moves, key=lambda x: x['d_mom'])[:n]

    return {
        'session': session, 'prev': prev,
        'regime': {
            'benchmark': 'SPY', 'ret20': spy, 'ret20_prev': spy_prev,
            'band': _band(spy), 'band_prev': _band(spy_prev),
            'to_up': to_up, 'to_down': to_down,
            'qqq': qqq, 'qqq_prev': qqq_prev, 'qqq_band': _band(qqq),
        },
        'breadth': {
            'universe': h.get('universe_count'),
            'advancers': h.get('advancers'), 'decliners': h.get('decliners'),
            'above50': num(h, 'pct_above_50d'), 'above50_prev': num(hp, 'pct_above_50d'),
            'above200': num(h, 'pct_above_200d'),
            'new_highs': h.get('new_highs'), 'new_lows': h.get('new_lows'),
            'vix': num(h, 'vix_close'), 'sp_above_200d': h.get('sp_above_200d'),
        },
        'risers': ups, 'fallers': downs,
        'fund_movers': fund_moves[:n],
        'n_funds': len(fund_moves), 'n_stocks': len(moves),
    }


def _pc(x, dp=1):
    return '—' if x is None else f'{x * 100:+.{dp}f}%'


def as_lines(b):
    """The same dict as Telegram-flavoured Markdown. Facts, no adjectives."""
    if not b:
        return []
    r, br = b['regime'], b['breadth']
    out = ['*The board:*']

    band = {'down': 'falling', 'up': 'rising', 'flat': 'flat'}[r['band']]
    line = (f"  SPY is {band} over 20 sessions ({_pc(r['ret20'])}"
            f" from {_pc(r['ret20_prev'])})")
    if r['band'] == 'flat' and r['to_up'] is not None:
        line += f" — {r['to_up'] * 100:.1f}pp from turning the banner green"
    out.append(line)
    if r['qqq'] is not None:
        where = {'down': 'still below -2%', 'up': 'above +2%',
                 'flat': 'in the flat band', 'unknown': ''}[r['qqq_band']]
        out.append(f"  QQQ {_pc(r['qqq'])} from {_pc(r['qqq_prev'])}"
                   + (f" ({where})" if where else ''))

    if br['advancers'] is not None:
        out.append(f"  {br['advancers']:,} advancers against {br['decliners']:,} "
                   f"decliners")
    if br['above50'] is not None:
        d = ('' if br['above50_prev'] is None
             else f" (from {br['above50_prev']:.1f}%)")
        out.append(f"  {br['above50']:.1f}% of the universe above its 50-day{d}")
    if br['vix'] is not None:
        out.append(f"  VIX {br['vix']:.2f}")
    out.append('')

    if b['risers']:
        out.append('*Gained the most standing:*')
        for m in b['risers']:
            out.append(f"  {m['ticker']:6} mom {m['mom_prev']}→{m['mom']} "
                       f"({m['d_mom']:+d}){'  ' + _pc(m['day']) if m['day'] is not None else ''}"
                       f"{'  streak ' + str(m['bz']) if m['bz'] else ''}")
        out.append('')
    if b['fallers']:
        out.append('*Lost the most:*')
        for m in b['fallers']:
            out.append(f"  {m['ticker']:6} mom {m['mom_prev']}→{m['mom']} "
                       f"({m['d_mom']:+d}){'  ' + _pc(m['day']) if m['day'] is not None else ''}")
        out.append('')

    big = [f for f in b['fund_movers'] if f['d_rank']]
    if big:
        out.append('*Funds that moved on the board:*')
        for f in big:
            out.append(f"  {f['symbol']:6} rank {f['rank_prev']}→{f['rank']} "
                       f"({f['d_rank']:+d})  mom {f['mom_prev']}→{f['mom']}")
        out.append('')
    return out
