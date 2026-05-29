#!/usr/bin/env python3
"""Yahoo Finance Live Price Updater — Railway worker.

Always-on worker (Procfile `worker`). During the US regular session it polls
the Yahoo v8 chart endpoint for every held ticker plus the index/benchmark
symbols and upserts them into `live_prices`. This replaces the old Google
Sheets + proxy-server price path.

Port of the India `zerodha_rest_updater_railway.py`, minus the Zerodha auth
layer (Yahoo needs no token) and the paper-trading fill/stop logic (Phase 4).

Scheduling is ET-aware via `America/New_York`, so US daylight-saving shifts
are handled automatically.

Env vars (a .env file in the repo root is auto-loaded for local runs;
on Railway set them as service variables):
  SUPABASE_URL
  SUPABASE_SERVICE_KEY
"""

import os
import time
import traceback
from datetime import date, datetime, timezone

import requests
import pytz
from supabase import create_client
from dotenv import load_dotenv

# Paper-trader constants + helpers, imported so the inline fill / stop / MFE
# checks below stay 1:1 with the EOD scanner. Any tier-sizing change in
# paper_trader.py flows here automatically.
from paper_trader import (
    SLIPPAGE_BPS, STOP_ATR_MULT,
    size_position, trading_days_between, to_float, to_int,
)

load_dotenv()

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise SystemExit('SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
et = pytz.timezone('America/New_York')

POLL_REGULAR  = 15      # seconds between price cycles during regular session
POLL_EXTENDED = 60      # slower polling during pre/post-market (thin liquidity)
TICKER_REFRESH = 300    # re-check holdings/transactions/paper-trades every 5 min
PENDING_MAX_TRADING_DAYS = 2   # pending paper trades that can't fill within 2td expire
AH_PM_ALERT_THRESHOLD = 2.0    # % move in PM/AH on a held ticker => alert
AH_PM_ALERT_DEDUPE_HOURS = 12  # don't re-log the same ticker within this window

# Index / benchmark symbols streamed alongside holdings (Yahoo symbols).
# SPY is included so we have an S&P 500 read during pre/post-market — the
# raw ^GSPC index doesn't trade extended hours (computed from constituents).
INDEX_TICKERS = ['^GSPC', '^IXIC', '^DJI', '^VIX', 'SPY']

YAHOO_HOSTS = ['https://query1.finance.yahoo.com',
               'https://query2.finance.yahoo.com']
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def is_market_open():
    """US regular session: 09:30-16:00 ET, Mon-Fri.
    Used to gate paper-trader fills + intraday stops (regular session only)."""
    now = datetime.now(et)
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return start <= now <= end


def current_session_phase():
    """Returns 'REGULAR' / 'PRE' / 'POST' / 'CLOSED'. Extended hours = 4-9:30 ET
    and 16-20 ET. Weekends + overnight = CLOSED."""
    now = datetime.now(et)
    if now.weekday() >= 5:
        return 'CLOSED'
    h, m = now.hour, now.minute
    minutes = h * 60 + m
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return 'REGULAR'
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return 'PRE'
    if 16 * 60 <= minutes < 20 * 60:
        return 'POST'
    return 'CLOSED'


def get_streaming_tickers():
    """Holdings + transaction-ledger tickers + paper-trade tickers + index symbols.

    paper_trades pending rows need a live tick so paper_fill_pending.py can fill
    them at the D1 open. Open paper rows need a live tick so the Algo tab can
    show live unrealised P&L (and so the future intraday stop watcher works).
    Closed paper rows are excluded — no further pricing needed."""
    tickers = set(INDEX_TICKERS)
    try:
        for h in (supabase.table('holdings').select('ticker').execute().data or []):
            if h.get('ticker'):
                tickers.add(h['ticker'])
        for t in (supabase.table('transactions').select('ticker').execute().data or []):
            if t.get('ticker'):
                tickers.add(t['ticker'])
        for p in (supabase.table('paper_trades').select('ticker')
                          .in_('status', ['pending', 'open']).execute().data or []):
            if p.get('ticker'):
                tickers.add(p['ticker'])
    except Exception as e:
        print(f'WARN: could not load tickers from Supabase: {e}')
    return tickers


def fetch_quote(session, symbol):
    """Fetch one quote from the Yahoo v8 chart endpoint, with extended-hours.
    Returns dict(price, prev_close, volume, pre, post, state) or None on failure.

    Uses interval=1m + range=1d + includePrePost=true. Yahoo's v8 meta does NOT
    expose preMarketPrice / postMarketPrice (only regularMarketPrice), so we
    walk the 1-minute bars (which span 4 AM -> 8 PM ET when includePrePost is
    set) and pick the latest close in each session window.
    """
    for host in YAHOO_HOSTS:
        try:
            url = f'{host}/v8/finance/chart/{symbol}?interval=1m&range=1d&includePrePost=true'
            r = session.get(url, timeout=10)
            if r.status_code != 200:
                continue
            j = r.json()
            result = j['chart']['result'][0]
            meta = result['meta']
            timestamps = result.get('timestamp') or []
            closes = (result.get('indicators', {}).get('quote', [{}])[0].get('close') or [])
            prev = meta.get('chartPreviousClose') or meta.get('previousClose')
            regular = meta.get('regularMarketPrice')
            volume = meta.get('regularMarketVolume') or 0
            if prev is None or regular is None:
                continue

            # Walk bars from newest -> oldest, classify by ET wall-clock window,
            # capture latest close in each (regular / pre / post)
            latest_regular = None
            latest_pre = None
            latest_post = None
            for ts, c in zip(reversed(timestamps), reversed(closes)):
                if c is None:
                    continue
                dt_et = datetime.fromtimestamp(ts, tz=et)
                if dt_et.weekday() >= 5:
                    continue
                mins = dt_et.hour * 60 + dt_et.minute
                if 9 * 60 + 30 <= mins < 16 * 60:
                    if latest_regular is None:
                        latest_regular = float(c)
                elif 4 * 60 <= mins < 9 * 60 + 30:
                    if latest_pre is None:
                        latest_pre = float(c)
                elif 16 * 60 <= mins < 20 * 60:
                    if latest_post is None:
                        latest_post = float(c)
                if latest_regular is not None and latest_pre is not None and latest_post is not None:
                    break

            # Use the bar-derived regular price if we have one (more current than
            # meta.regularMarketPrice which may be stale by the session)
            if latest_regular is not None:
                regular_now = latest_regular
            else:
                regular_now = float(regular)

            # Determine LTP based on the current phase
            phase_now = current_session_phase()
            if phase_now == 'PRE' and latest_pre is not None:
                ltp = latest_pre
            elif phase_now == 'POST' and latest_post is not None:
                ltp = latest_post
            else:
                ltp = regular_now

            return {
                'price': float(ltp),
                'prev_close': float(prev),
                'volume': int(volume),
                'pre':   latest_pre,
                'post':  latest_post,
                'state': meta.get('marketState') or phase_now,
                'regular': regular_now,
            }
        except Exception:
            continue
    return None


def fetch_and_update_prices(session, tickers, phase):
    """Fetch every ticker, upsert the batch into live_prices.
    `phase` is the local session phase (REGULAR/PRE/POST/CLOSED) used to label
    the row; pre/post prices come from Yahoo's meta when available."""
    updates = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for symbol in tickers:
        q = fetch_quote(session, symbol)
        if not q:
            continue
        day_change = q['price'] - q['prev_close']
        day_change_pct = (day_change / q['prev_close'] * 100) if q['prev_close'] else 0
        # Pre/post change % relative to the regular previous close
        pre  = q.get('pre')
        post = q.get('post')
        pre_chg_pct  = ((pre  - q['prev_close']) / q['prev_close'] * 100) if (pre  and q['prev_close']) else None
        post_chg_pct = ((post - q['prev_close']) / q['prev_close'] * 100) if (post and q['prev_close']) else None
        # market_state: prefer Yahoo's classification, fall back to our local phase
        state = q.get('state') or phase

        updates.append({
            'ticker': symbol,
            'price': round(q['price'], 4),
            'day_change': round(day_change, 4),
            'day_change_pct': round(day_change_pct, 4),
            'prev_close': round(q['prev_close'], 4),
            'volume': q['volume'],
            'pre_market_price':       round(pre,  4) if pre  is not None else None,
            'pre_market_change_pct':  round(pre_chg_pct,  4) if pre_chg_pct  is not None else None,
            'post_market_price':      round(post, 4) if post is not None else None,
            'post_market_change_pct': round(post_chg_pct, 4) if post_chg_pct is not None else None,
            'market_state': state,
            'updated_at': now_iso,
        })
    if updates:
        supabase.table('live_prices').upsert(updates, on_conflict='ticker').execute()
    return len(updates)


# ─── Paper-trade fill + intraday stop monitor + MFE/MAE tracker ──────────
# Mirrors India's zerodha_rest_updater_railway.py architecture. On each price
# tick cycle (every POLL_INTERVAL seconds during market hours):
#   1) fill any pending paper_trades at the latest LTP
#   2) close any open paper_trade whose LTP <= current_stop
#   3) update per-trade MFE/MAE high-water marks
# Keeps paper-trade state always-fresh; the nightly paper_trader.py only has
# to handle EOD partials, trails, time stops, equity snapshot, and the daily
# entry scan.

def _expire_pending(tr, today_, reason):
    supabase.table('paper_trades').update({
        'status': 'closed',
        'exit_date': today_.isoformat(),
        'exit_reason': reason,
        'exit_price': None,
        'total_pnl': 0,
        'total_pnl_pct': 0,
        'current_quantity': 0,
        'updated_at': datetime.utcnow().isoformat(),
    }).eq('id', tr['id']).execute()


def _close_paper_at_ltp(tr, ltp):
    """Close an open paper trade at LTP — used when LTP <= current_stop intraday."""
    entry_price = float(tr['entry_price'])
    initial_qty = int(tr['initial_quantity'])
    initial_stop = float(tr['initial_stop'])
    current_qty = int(tr['current_quantity'])
    if current_qty <= 0:
        return
    realised_pnl_before = float(tr.get('realised_pnl') or 0)
    realised_qty_before = int(tr.get('realised_qty') or 0)
    trail_armed = bool(tr.get('trail_armed'))
    breakeven_armed = bool(tr.get('breakeven_armed'))
    partials_taken = int(tr.get('partials_taken') or 0)
    partials = tr.get('partials') or []

    exit_chunk_pnl = (ltp - entry_price) * current_qty
    realised_pnl = realised_pnl_before + exit_chunk_pnl
    realised_qty = realised_qty_before + current_qty

    entry_value = entry_price * initial_qty
    total_pnl_pct = (realised_pnl / entry_value * 100) if entry_value else 0
    risk_per_share = entry_price - initial_stop
    r_mult = ((realised_pnl / initial_qty) / risk_per_share
              if (initial_qty and risk_per_share > 0) else None)

    today_et = datetime.now(et).date()
    try:
        entry_date_ = date.fromisoformat(tr['entry_date'])
        holding_days = trading_days_between(entry_date_, today_et)
    except Exception:
        holding_days = 0

    supabase.table('paper_trades').update({
        'exit_date': today_et.isoformat(),
        'exit_price': round(ltp, 4),
        'exit_reason': 'trail_stop' if trail_armed else 'stop',
        'total_pnl': round(realised_pnl, 2),
        'total_pnl_pct': round(total_pnl_pct, 3),
        'r_multiple': round(r_mult, 3) if r_mult is not None else None,
        'holding_days': holding_days,
        'current_quantity': 0,
        'realised_pnl': round(realised_pnl, 2),
        'realised_qty': realised_qty,
        'partials': partials,
        'partials_taken': partials_taken,
        'breakeven_armed': breakeven_armed,
        'trail_armed': trail_armed,
        'status': 'closed',
        'updated_at': datetime.utcnow().isoformat(),
    }).eq('id', tr['id']).execute()


def fill_pending_paper_trades():
    """Fill any PENDING paper_trades using the latest live_prices tick as the
    D1 entry. Expires rows older than PENDING_MAX_TRADING_DAYS. Idempotent."""
    try:
        resp = (supabase.table('paper_trades').select('*')
                  .eq('status', 'pending').eq('mode', 'paper').execute())
        pending = resp.data or []
        if not pending:
            return 0

        tickers = [r['ticker'] for r in pending]
        prices_resp = (supabase.table('live_prices').select('ticker, price')
                         .in_('ticker', tickers).execute())
        price_map = {r['ticker']: to_float(r.get('price'))
                     for r in (prices_resp.data or [])}

        today_et = datetime.now(et).date()
        filled = 0
        for tr in pending:
            ticker = tr['ticker']
            try:
                scan_date = date.fromisoformat(tr['entry_date'])
                age_td = trading_days_between(scan_date, today_et)

                live_price = price_map.get(ticker)
                if not live_price or live_price <= 0:
                    if age_td > PENDING_MAX_TRADING_DAYS:
                        _expire_pending(tr, today_et, 'fill_expired')
                        print(f"  ⏳ PAPER EXPIRED: {ticker} (age={age_td}td, no live price)")
                    continue

                tier = tr['strategy_tier']
                entry_atr = to_float(tr.get('entry_atr'))
                if not entry_atr or entry_atr <= 0:
                    _expire_pending(tr, today_et, 'fill_rejected_missing_atr')
                    continue

                entry_price = live_price * (1 + SLIPPAGE_BPS / 10_000)
                qty, _, _ = size_position(tier, entry_price, entry_atr)
                if qty == 0:
                    _expire_pending(tr, today_et, 'fill_rejected_position_floor')
                    print(f"  ❌ PAPER FILL REJECT: {ticker} qty=0 @ {entry_price:.2f}")
                    continue

                initial_stop = entry_price - STOP_ATR_MULT * entry_atr
                initial_risk = qty * (entry_price - initial_stop)

                supabase.table('paper_trades').update({
                    'entry_date': today_et.isoformat(),
                    'entry_price': round(entry_price, 4),
                    'initial_quantity': qty,
                    'current_quantity': qty,
                    'entry_value': round(qty * entry_price, 2),
                    'initial_risk': round(initial_risk, 2),
                    'initial_stop': round(initial_stop, 4),
                    'current_stop': round(initial_stop, 4),
                    'status': 'open',
                    'updated_at': datetime.utcnow().isoformat(),
                }).eq('id', tr['id']).execute()
                filled += 1
                print(f"  ✅ PAPER FILL: {ticker} @ {entry_price:.2f} qty={qty} stop={initial_stop:.2f} ({tier})")
            except Exception as e:
                print(f"  ⚠️  Fill failed for {ticker}: {e}")
        return filled
    except Exception as e:
        print(f"⚠️  fill_pending_paper_trades error: {e}")
        return 0


def check_paper_stops():
    """Walk open paper trades, exit at LTP if LTP <= current_stop."""
    try:
        trades_resp = (supabase.table('paper_trades').select('*')
                         .eq('status', 'open').eq('mode', 'paper').execute())
        open_trades = trades_resp.data or []
        if not open_trades:
            return 0

        tickers = [t['ticker'] for t in open_trades]
        prices_resp = (supabase.table('live_prices').select('ticker, price')
                         .in_('ticker', tickers).execute())
        price_map = {r['ticker']: float(r.get('price') or 0)
                     for r in (prices_resp.data or [])}

        closed = 0
        for tr in open_trades:
            ltp = price_map.get(tr['ticker'], 0)
            current_stop = float(tr.get('current_stop') or 0)
            if ltp <= 0 or current_stop <= 0:
                continue
            if ltp > current_stop:
                continue
            try:
                _close_paper_at_ltp(tr, ltp)
                closed += 1
                print(f"  🛑 PAPER STOP: {tr['ticker']} @ {ltp:.2f} (stop={current_stop:.2f}, tier={tr.get('strategy_tier')})")
            except Exception as e:
                print(f"  ⚠️  Failed to close paper trade {tr['ticker']}: {e}")
        return closed
    except Exception as e:
        print(f"⚠️  check_paper_stops error: {e}")
        return 0


def update_paper_excursions():
    """Update max_unrealized_pct / min_unrealized_pct on open paper trades
    whenever the live LTP prints a new extreme. EOD batch (paper_trader.py)
    will fold today's bar_high/bar_low into the same columns."""
    try:
        trades_resp = (supabase.table('paper_trades')
                         .select('id, ticker, entry_price, max_unrealized_pct, min_unrealized_pct')
                         .eq('status', 'open').eq('mode', 'paper').execute())
        open_trades = trades_resp.data or []
        if not open_trades:
            return 0

        tickers = [t['ticker'] for t in open_trades]
        prices_resp = (supabase.table('live_prices').select('ticker, price')
                         .in_('ticker', tickers).execute())
        price_map = {r['ticker']: float(r.get('price') or 0)
                     for r in (prices_resp.data or [])}

        updates = 0
        now_iso = datetime.utcnow().isoformat()
        for tr in open_trades:
            ltp = price_map.get(tr['ticker'], 0)
            entry = float(tr.get('entry_price') or 0)
            if ltp <= 0 or entry <= 0:
                continue
            unr_pct = (ltp - entry) / entry * 100

            stored_max = tr.get('max_unrealized_pct')
            stored_min = tr.get('min_unrealized_pct')
            stored_max = float(stored_max) if stored_max is not None else None
            stored_min = float(stored_min) if stored_min is not None else None

            patch = {}
            if stored_max is None or unr_pct > stored_max:
                patch['max_unrealized_pct'] = round(unr_pct, 3)
            if stored_min is None or unr_pct < stored_min:
                patch['min_unrealized_pct'] = round(unr_pct, 3)
            if not patch:
                continue
            patch['updated_at'] = now_iso
            try:
                supabase.table('paper_trades').update(patch).eq('id', tr['id']).execute()
                updates += 1
            except Exception as e:
                print(f"  ⚠️  Excursion update failed for {tr['ticker']}: {e}")
        return updates
    except Exception as e:
        print(f"⚠️  update_paper_excursions error: {e}")
        return 0


def check_ah_pm_earnings_alerts(phase):
    """During PRE/POST phase, if any held ticker has moved >= AH_PM_ALERT_THRESHOLD
    in extended hours vs prev regular close, INSERT into `alerts` table with
    alert_type='earnings_reaction'. Idempotent within AH_PM_ALERT_DEDUPE_HOURS
    via an existence check on the same (ticker, alert_type, alert_date).
    """
    if phase not in ('PRE', 'POST'):
        return 0
    try:
        # Universe = real holdings only (not paper). Earnings hits matter for
        # capital at risk; paper picks are informational.
        held = (supabase.table('holdings').select('ticker').execute().data or [])
        held_tickers = {h['ticker'] for h in held if h.get('ticker')}
        if not held_tickers:
            return 0

        # Pull current live_prices for held tickers
        prices_resp = (supabase.table('live_prices')
                         .select('ticker, pre_market_price, pre_market_change_pct, '
                                 'post_market_price, post_market_change_pct, prev_close')
                         .in_('ticker', list(held_tickers)).execute())

        today_iso = datetime.now(et).date().isoformat()
        logged = 0
        for r in (prices_resp.data or []):
            ticker = r['ticker']
            if phase == 'PRE':
                px = r.get('pre_market_price'); chg = r.get('pre_market_change_pct')
            else:
                px = r.get('post_market_price'); chg = r.get('post_market_change_pct')
            if px is None or chg is None:
                continue
            if abs(float(chg)) < AH_PM_ALERT_THRESHOLD:
                continue

            # Dedupe: skip if we already logged an earnings_reaction for this
            # ticker today (or within DEDUPE_HOURS).
            existing = (supabase.table('alerts')
                          .select('id')
                          .eq('ticker', ticker)
                          .eq('alert_type', 'earnings_reaction')
                          .eq('alert_date', today_iso)
                          .limit(1).execute())
            if existing.data:
                continue

            direction = 'up' if float(chg) > 0 else 'down'
            try:
                supabase.table('alerts').insert({
                    'ticker': ticker,
                    'alert_type': 'earnings_reaction',
                    'alert_date': today_iso,
                    'direction': direction,
                    'close': round(float(px), 4),
                    'metadata': {
                        'move_pct': round(float(chg), 3),
                        'market_state': phase,
                        'prev_regular_close': float(r.get('prev_close') or 0),
                        'detected_at': datetime.utcnow().isoformat(),
                    },
                }).execute()
                logged += 1
                arrow = '↑' if direction == 'up' else '↓'
                print(f"  📣 EARNINGS REACTION: {ticker} {arrow}{abs(float(chg)):.2f}% ({phase}) @ {float(px):.2f}")
            except Exception as e:
                print(f"  ⚠️  Failed to log alert for {ticker}: {e}")
        return logged
    except Exception as e:
        print(f"⚠️  check_ah_pm_earnings_alerts error: {e}")
        return 0


def main():
    print('=' * 60)
    print('YAHOO LIVE PRICE UPDATER - Railway worker')
    print('=' * 60)
    session = requests.Session()
    session.headers.update({'User-Agent': UA})

    tickers = None
    last_session_date = None
    last_ticker_refresh = 0.0

    while True:
        try:
            now = datetime.now(et)
            phase = current_session_phase()

            # Stream during regular session AND pre/post-market. Sleep only when
            # both windows are closed (overnight + weekends). Paper-trader hooks
            # are still gated on REGULAR phase below.
            if phase == 'CLOSED':
                print(f'{phase} - {now:%Y-%m-%d %H:%M %Z} - waiting...')
                time.sleep(60)
                continue

            # New session — (re)load the ticker set. Use the date when we first
            # see PRE (start of trading day) rather than REGULAR so we have the
            # universe ready for 4 AM ticks.
            if last_session_date != now.date():
                print(f'\n=== New market day: {now.date()} ({phase}) ===')
                tickers = get_streaming_tickers()
                print(f'Streaming {len(tickers)} tickers')
                last_session_date = now.date()
                last_ticker_refresh = time.time()

            # Periodic ticker refresh — picks up new holdings/paper-trades intraday.
            if time.time() - last_ticker_refresh > TICKER_REFRESH:
                refreshed = get_streaming_tickers()
                if refreshed != tickers:
                    added = sorted(refreshed - tickers)
                    dropped = sorted(tickers - refreshed)
                    if added:
                        print(f'+ added tickers: {added}')
                    if dropped:
                        print(f'- dropped tickers: {dropped}')
                    tickers = refreshed
                last_ticker_refresh = time.time()

            n = fetch_and_update_prices(session, tickers, phase)

            # Paper-trader functions: REGULAR phase ONLY (India parity — algo
            # never acts on PM/AH prices, those are display-only on the dashboard).
            filled = stopped = excursions = 0
            if phase == 'REGULAR' and n > 0:
                filled = fill_pending_paper_trades()
                stopped = check_paper_stops()
                excursions = update_paper_excursions()

            # Earnings-reaction alerts on real holdings during PRE/POST hours
            ah_alerts = 0
            if phase in ('PRE', 'POST') and n > 0:
                ah_alerts = check_ah_pm_earnings_alerts(phase)

            suffix_parts = []
            if filled:     suffix_parts.append(f'filled_paper={filled}')
            if stopped:    suffix_parts.append(f'stopped_paper={stopped}')
            if excursions: suffix_parts.append(f'excursions={excursions}')
            if ah_alerts:  suffix_parts.append(f'ah_alerts={ah_alerts}')
            suffix = (', ' + ', '.join(suffix_parts)) if suffix_parts else ''
            print(f'{now:%H:%M:%S} ET [{phase}] - updated {n}/{len(tickers)} prices{suffix}')

            # Slower polling outside regular session — PM/AH liquidity is thin and
            # there's no point hammering Yahoo every 15s for sparse ticks.
            interval = POLL_REGULAR if phase == 'REGULAR' else POLL_EXTENDED
            time.sleep(interval)

        except KeyboardInterrupt:
            print('\nShutting down.')
            break
        except Exception as e:
            print(f'ERROR in main loop: {e} - retrying in 30s')
            time.sleep(30)


if __name__ == '__main__':
    main()
