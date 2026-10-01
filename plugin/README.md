# Theme value-chain plugin — Phase 0

A local MCP server over the hand-curated theme value chains, so an LLM can
answer "who makes the chips for AI" or "which part of the nuclear trade is
working" from our own data.

**The point of Phase 0 is to find out whether the answers are any good.**
Nothing here is deployed, submitted or public. If the answers read as valuable,
we go to Phase 1 (public HTTPS endpoint). If they read as a table read aloud,
we stop, having spent an afternoon.

## What it exposes

| Tool | Answers |
|---|---|
| `list_themes` | what chains exist (18, across 6 categories) |
| `get_value_chain` | one chain, upstream → downstream, who sits where and why |
| `rank_links` | which stage of a chain is strongest right now |
| `compare_peers` | the names inside one stage, ranked like-for-like |

## Setup

```bash
pip install -r plugin/requirements.txt
```

Reads `SUPABASE_URL` and a key from the repo-root `.env`, same as every other
script here.

## Connect it

**Claude Code** — `.mcp.json` in the repo root already points at it. Restart
Claude Code in this directory and the four tools appear. Check with `/mcp`.

**Claude Desktop** — add to
`%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "theme-value-chains": {
      "command": "python",
      "args": ["C:\\Users\\Sarfaraz Khimani\\Documents\\us-dashboard\\plugin\\theme_server.py"]
    }
  }
}
```

Restart Claude Desktop afterwards.

**ChatGPT** — needs Developer Mode and a *remote* server, so it only works once
Phase 1 puts this behind HTTPS. Locally, run `python plugin/theme_server.py --http`
and point a tunnel at it if you want to try early.

## Questions worth asking it

These are the ones that decide whether Phase 1 is worth doing — they should
produce an answer you could not get from a stock screener:

- Who makes the chips for AI?
- Which part of the AI trade is hottest right now — chips, power, or software?
- Explain the obesity drug supply chain. Who benefits besides Lilly?
- Compare the AI memory stocks.
- What's in the nuclear value chain and which stage has momentum?
- Which companies are hurt by GLP-1 drugs?

The last one is the tell: `theme_members.note` carries editorial like
"Snacks — demand headwind (Hershey)". A screener cannot answer that.

## Design constraints (deliberate, carried into Phase 1)

- **Our own numbers only.** `momentum_score` is our 0-100 cross-sectional
  composite; the chain structure and per-name notes are our editorial. No
  prices, no OHLC, no fundamentals — so nothing third-party is redistributed.
- **Read-only, stateless.** No writes, no user data, nothing stored per caller.
- **Every payload carries `as_of` plus a `stale` flag**, so the model says
  "as of 29 Sep" rather than implying a live quote.
- **Ambiguous themes return candidates, not a guess.** Answering about the
  wrong chain is worse than asking.

## Before Phase 1

- Swap the service key for a read-only Supabase role (`SUPABASE_READONLY_KEY`).
  The server warns on startup while it is falling back — that key bypasses RLS
  and must never sit behind a public endpoint.
- Rate limiting.
- A domain you own, for the directory's domain verification.
