#!/usr/bin/env python3
"""Newsletter synthesis — the capstone of the weekly pipeline.

Feeds the deterministic data pack + market events + macro news + ticker news
(+ optional previous issue and open predictions) to Claude, with the style guide
distilled from the Athena reference issues as the system prompt, and produces a
full Athena-voice draft for the human editor to refine.

Pipeline position:
  build_data_pack.py  ─┐
  detect_market_events ┤→ artifacts ─→ synthesize_newsletter.py ─→ draft.md ─→ YOU edit
  fetch_macro_news     ┤
  fetch_ticker_news    ─┘

Model: claude-opus-4-8, adaptive thinking, effort=high, streamed (long output).

Env: ANTHROPIC_API_KEY (required) + the artifact files in the repo root.
Usage: python synthesize_newsletter.py
"""

import json
import os
import sys
from datetime import datetime, timezone

import anthropic
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

if not os.environ.get('ANTHROPIC_API_KEY'):
    sys.exit('ERROR: ANTHROPIC_API_KEY must be set in .env')

MODEL = 'claude-opus-4-8'
MAX_TOKENS = 20000


def read_text(name, required=True):
    path = os.path.join(BASE, name)
    if not os.path.exists(path):
        if required:
            sys.exit(f'ERROR: {name} not found — run the upstream pipeline step first')
        return None
    return open(path, encoding='utf-8').read()


def read_json(name, required=True):
    txt = read_text(name, required)
    return json.loads(txt) if txt else None


def main():
    style_guide = read_text('newsletter_style_guide.md')
    data_pack = read_text('newsletter_data_pack.md')          # build_data_pack.py
    events = read_json('market_events.json')                  # detect_market_events.py
    macro = read_json('macro_news.json')                      # fetch_macro_news.py
    ticker = read_json('ticker_news.json', required=False)    # fetch_ticker_news.py (optional)
    prev_issue = read_text('previous_issue.md', required=False)        # last week's final, optional
    predictions = read_json('open_predictions.json', required=False)   # ledger, optional

    issue_date = events.get('latest_date') or datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Assemble the week's inputs. Trim the events list to the high-impact head so
    # the prompt leads with what actually moved.
    top_events = events.get('events', [])[:30]

    parts = [
        f"Today's issue date: {issue_date}. Write the weekly newsletter for the US market.",
        "\n\n===== DATA PACK (Where We Stand, RS Movers, Watchlist — use numbers verbatim) =====\n",
        data_pack,
        "\n\n===== MARKET EVENTS (what moved, ranked by impact x breadth — pick the lead + themes from the top) =====\n",
        json.dumps(top_events, indent=2),
        "\n\n===== MACRO NEWS (dominant macro/geopolitics themes by coverage volume) =====\n",
        json.dumps(macro.get('topics', []), indent=2),
    ]
    if ticker and ticker.get('by_ticker'):
        parts += ["\n\n===== TICKER NEWS (why specific moved names moved, with sentiment) =====\n",
                  json.dumps({'by_ticker': ticker['by_ticker'],
                              'sector_news': ticker.get('sector_news', {})}, indent=2)]
    if prev_issue:
        parts += ["\n\n===== LAST WEEK'S ISSUE (for continuity + voice; do not repeat it) =====\n", prev_issue]
    if predictions:
        parts += ["\n\n===== OPEN PREDICTIONS TO GRADE (grade each honestly near the top) =====\n",
                  json.dumps(predictions, indent=2)]
    parts.append("\n\nWrite the complete newsletter in markdown now, following the style guide exactly.")

    user_content = "".join(p for p in parts if p)

    client = anthropic.Anthropic()
    print(f'Synthesizing newsletter for {issue_date} with {MODEL} ...')

    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={'type': 'adaptive'},
        output_config={'effort': 'high'},
        system=[{'type': 'text', 'text': style_guide,
                 'cache_control': {'type': 'ephemeral'}}],
        messages=[{'role': 'user', 'content': user_content}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end='', flush=True)
        msg = stream.get_final_message()

    draft = "".join(b.text for b in msg.content if b.type == 'text')
    out_path = os.path.join(BASE, f'newsletter_draft_{issue_date}.md')
    open(out_path, 'w', encoding='utf-8').write(draft)
    u = msg.usage
    print(f'\n\n[draft written to {out_path}]')
    print(f'[tokens: in={u.input_tokens} cache_read={getattr(u, "cache_read_input_tokens", 0)} '
          f'out={u.output_tokens}]')


if __name__ == '__main__':
    main()
