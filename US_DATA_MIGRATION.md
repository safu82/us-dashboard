# US Data in the India Supabase DB — Migration Inventory

_Audited 2026-05-19 against the live India Supabase project. Companion to
`US_DASHBOARD_BRIEF.md`. Row counts grow daily until cutover (the EOD snapshot
job keeps appending) — re-audit at migration time._

## How US data is tagged
Every table that holds US data uses the **`portfolio` enum column set to
`'US'`** (vs `'INDIAN'`). This is a clean, single-filter separator —
`WHERE portfolio = 'US'` extracts everything.

## US data to migrate (6 tables, ~610 rows)

| Table | US rows | What it is |
|---|---|---|
| `holdings` | 10 | Current US positions — AMZN, ARM, CRWD, GOOGL, MSFT, MU, NVDA, QQQ, SNDK, TSM |
| `transactions` | 21 | US buy/sell ledger — 10 tickers (incl. HACK, META — now fully exited) |
| `portfolio_snapshots` | 201 | US daily NAV history — the equity-curve series |
| `detailed_snapshots` | 170 | US per-holding enriched daily snapshots |
| `cash_flows` | 207 | US deposits/withdrawals — needed for XIRR |
| `portfolio_config` | 1 | One US config row |
| `dividends` | 0 | **None** — no US dividends recorded |

So the US "portfolio tracking" layer (snapshots + holdings + cash flows) *does*
exist and is worth carrying over for historical continuity. What does **not**
exist is any market-data or scanner layer.

## NOT present for US (build fresh — nothing to migrate)
- `daily_stock_snapshots`, `stock_fundamentals`, `live_prices` — **0 US rows**.
  US OHLC / fundamentals / live prices were never in Supabase; they live in the
  Google Sheets setup. The new US system builds these fresh (Alpaca + yfinance).
- All scanner tables (`entry_signals`, `market_alerts`, `presignal_scores`,
  `sector_rankings`, `earnings_calendar`, `ema_200_touch_tracking`), the paper
  tables, `indian_stock_sectors`, `zerodha_*`, `market_ratios` — India-only.

## Caveats / things to know before migrating
- **Transaction history is incomplete.** 21 rows cover 10 tickers, but AMZN and
  TSM are in `holdings` with *no* transactions — those positions were seeded
  directly (like India's hardcoded historical holdings). A FIFO rebuild from
  `transactions` alone will NOT reproduce the full book. Carry holdings over
  as-is; treat the txn ledger as partial.
- **`holdings.sector` is the same drifted free-text** as India had
  (CONSUMER CYCLICAL / IT / TECHNOLOGY / ETF — inconsistent). Do **not** carry
  these labels — the US system should classify via **GICS sectors** fresh.
- **`holdings.market_cap_crores` = 0** for all US rows (never populated, and
  "crores" is an INR unit anyway). Recompute in USD in the new system.
- Counts grow daily (snapshots + cash_flows) — migrate at cutover, re-audit then.

## Migration approach
The new US Supabase project is a *separate* project, so no cross-project SQL.
Per table: `SELECT ... WHERE portfolio = 'US'` → CSV → load into the new US
schema. The new US tables won't need a `portfolio` column at all (everything is
US), so drop it on load.

## India-side cleanup (AFTER US migration is verified)
Once the US data is confirmed in the new project, delete the `portfolio = 'US'`
rows from the India DB. This shrinks `holdings` 51 → 41, removes US rows from
`transactions` / snapshots / `cash_flows`, and means the India dashboard stops
showing US holdings entirely — retiring the `canonicalSector()` US fallback and
leaving the India system purely Indian. Do this as a deliberate, verified step,
not before the US side is live.
