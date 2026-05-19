# US Dashboard — Project Brief

_Prepared 2026-05-19. Drop this into the new US repo and open a fresh Claude Code
session in that directory._

## Goal
Build a **live** US-equities portfolio dashboard + scanner system, mirroring the
existing India system (repo: `portfolio-alerts`) as closely as possible.

## Current state (to be replaced)
- Runs on Google Sheets + `GoogleFinance()` + an always-on proxy server.
- Only an EOD portfolio snapshot, per-stock snapshots, and a Telegram EOD
  portfolio-performance alert. No real scanners.
- The new build replaces **all** of this — the Google Sheets and the proxy
  server go away entirely.

## Owner context
- Based in IST. Actively watches the US session live until ~3 PM EST
  (~1:30 AM IST). **Live ticks during market hours are a hard requirement** —
  this is NOT an EOD-only system.

## Architecture decisions (locked)
- **Separate Supabase project** — do NOT touch the India DB. US tables only.
  Migrate the existing US holdings out of the India `holdings` table into here
  (this also cleans up the India side).
- **Separate GitHub repo**, public (free unlimited Actions minutes).
- **Separate Railway service** — always-on worker, US market hours, polls the
  live feed and writes `us_live_prices`. Mirrors `zerodha_rest_updater_railway.py`.
- **Live data + OHLC:** Alpaca free tier (IEX real-time feed + REST history).
  IEX-feed coverage is fine for a liquid large-cap book and an S&P 500 universe.
- **Fundamentals:** yfinance (good for US data; there is no Screener.in
  equivalent for the US).
- **Universe:** S&P 500 (the Nifty-500 analog).
- **Sectors:** GICS (11 standard sectors; yfinance provides sector/industry
  per stock).
- **Currency:** USD.
- **Timezone:** all scheduling in ET, and it MUST handle US daylight saving —
  the IST↔ET offset shifts twice a year (India has no DST).

## Open decisions (resolve early)
- **Where the US portfolio is held** — determines transaction ingestion:
  Interactive Brokers has an API; Indian platforms (INDmoney / Vested / Groww)
  generally mean manual entry or a statement-import flow (like the India HDFC
  contract-note ingester).
- **Final feed pick:** Alpaca (recommended) vs Finnhub vs a broker API.

## Phasing
1. **Data layer** — US Supabase schema, Alpaca OHLC ingestion, EOD snapshots,
   the live-price Railway worker. Replace Google Sheets.
2. **Dashboard port** — fork India `index.html` as the template; swap data
   sources, tables, currency. Much of the JS (XIRR, charts, conviction logic,
   FIFO) is market-agnostic and reuses directly.
3. **Scanners** — entry signals, RS ranking, breadth, sector strength.
4. **Paper trading** — later.

## Reference
The India system lives in the `portfolio-alerts` repo — use it as the
structural template for tables, scanners, dashboard layout, and the Railway
worker pattern.

## Lessons carried from the India build
- **GitHub scheduled crons are unreliable** — delayed or dropped under load,
  and the first run after a cron change is often skipped. Use off-the-hour
  cron minutes, and manually trigger a workflow after any schedule change.
- **One canonical source of truth for sector classification from day one** —
  do not let a free-text sector column drift (the India `holdings.sector`
  column drifted into duplicate labels and had to be cleaned up).
- Keep the live-data worker in its own process/service — never share it.
