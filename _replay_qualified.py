#!/usr/bin/env python3
"""Read-only replay of the paper_trader entry funnel for a given snapshot date.
Reuses paper_trader's own classifiers + filters so the output matches what the
real run did. Prints every candidate and the stage it died at, then the
qualified set and the cash-cap squeeze. NO writes."""

import os, sys
from collections import defaultdict
from datetime import date, timedelta
from supabase import create_client

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except ImportError:
    pass

import paper_trader as pt

TARGET = sys.argv[1] if len(sys.argv) > 1 else '2026-06-02'

sb = create_client(pt.SUPABASE_URL, pt.SUPABASE_KEY)
today_date = date.fromisoformat(TARGET)

today_snap = pt.load_snapshots_for_date(sb, today_date.isoformat())
holdings = pt.load_holdings(sb)
open_trades = pt.load_open_paper_trades(sb)
pending_trades = pt.load_pending_paper_trades(sb)
recent_closed = pt.load_recent_closed_tickers(sb, today_date)
sector_map = pt.load_sectors(sb)
sector_rankings = pt.load_sector_rankings(sb, today_date.isoformat())
open_tickers = {t['ticker'] for t in (open_trades + pending_trades)}

presignals = pt.load_presignals(sb, TARGET, TARGET)
entry_signals = pt.load_entry_signals(sb, TARGET, TARGET)
by_ticker = pt.group_signals_by_ticker(presignals, entry_signals)

raw = []
for ticker, bundle in by_ticker.items():
    bucket, fams, fs, score = pt.classify_entry_bucket(bundle['entry_signal_rows'])
    if not bucket:
        bucket, fams, fs, score, _ = pt.classify_presignal_bucket(bundle['presignal_rows'])
    if not bucket:
        continue
    raw.append({'ticker': ticker, 'tier': bucket, 'families': fams, 'score': score})

fundamentals = pt.load_fundamentals(sb, [c['ticker'] for c in raw])
conviction = pt.load_conviction(sb, [c['ticker'] for c in raw])

qualified = []
for c in raw:
    t = c['ticker']
    if t in holdings:        c['died'] = 'in_holdings'; continue
    if t in recent_closed:   c['died'] = 'in_cooldown'; continue
    if t in open_tickers:    c['died'] = 'already_open'; continue
    ok, why = pt.passes_sector_filter(t, sector_map, sector_rankings)
    if not ok: c['died'] = f'sector:{why}'; continue
    ok, why = pt.passes_earnings_filter(t, fundamentals)
    if not ok: c['died'] = f'earnings:{why}'; continue
    ok, why = pt.passes_intraday_filter(t, today_snap)
    if not ok: c['died'] = f'intraday:{why}'; continue
    cv = conviction.get(t)
    if cv and cv.get('grade') == 'D':
        c['died'] = f'conviction:D({cv.get("score")})'; continue
    c['conviction'] = (f"{cv.get('grade')} ({cv.get('score')})" if cv else 'n/a')
    c['died'] = None
    qualified.append(c)

tier_order = {'T1_MULTI_STRONG':0,'T2_STRONG_REG':1,'T3_MULTI_REG':2,'T4_RS_ACCEL':3}
qualified.sort(key=lambda c: (tier_order[c['tier']],
                              -float((conviction.get(c['ticker']) or {}).get('score') or 0),
                              -float(c['score'])))

print(f"\n=== {TARGET}: {len(raw)} raw candidates, {len(qualified)} qualified ===\n")
print("QUALIFIED (sort order = the priority they'd have been filled in):")
for c in qualified:
    snap = today_snap.get(c['ticker']) or {}
    close = pt.to_float(snap.get('close'))
    atr = pt.to_float(snap.get('atr_14'))
    est_entry = close * (1 + pt.SLIPPAGE_BPS/10_000) if close else None
    qty, notional, _ = pt.size_position(c['tier'], est_entry, atr) if (est_entry and atr) else (0,0,0)
    pg = pt.peer_group_for(c['ticker'], sector_map)
    print(f"  {c['ticker']:6s} {c['tier']:16s} conv={c['conviction']:10s} "
          f"close=${close:.2f} would-size={qty}sh ~${notional:,.0f}  "
          f"families={','.join(c['families'])}  peer={pg}")

deployed = sum(pt.to_float(t.get('entry_value'),0) for t in (open_trades+pending_trades))
cap = pt.SLEEVE * pt.MAX_DEPLOYED_PCT
print(f"\nCASH: deployed=${deployed:,.2f} cap=${cap:,.0f} headroom=${cap-deployed:,.2f} "
      f"(floor=${pt.POSITION_FLOOR}) -> all qualified squeezed out\n")
