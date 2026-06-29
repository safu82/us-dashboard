#!/usr/bin/env python3
"""Humanizing pass — strip AI tells from a finished newsletter draft.

The drafting model juggles facts, structure and voice at once and drifts back into
machine habits (bold everything, em-dashes, rule-of-three) by the back half. This
is a second, narrow LLM pass whose ONLY job is to line-edit the finished draft so
it reads like a sharp human wrote it — without changing a single number, ticker,
date, or table.

Pipeline position:
  synthesize_newsletter.py -> draft.md -> [humanize_newsletter.py] -> make_docx.py

Hard guards: tables and headings must come back byte-identical and the length must
stay sane, or the edit is rejected (written to *_humanized.md, original untouched).

Model: claude-opus-4-8, adaptive thinking, effort=medium, streamed.
Usage: python humanize_newsletter.py [path/to/newsletter_draft_YYYY-MM-DD.md]
Env: ANTHROPIC_API_KEY.
"""

import glob
import os
import re
import sys
from datetime import datetime, timezone

import anthropic
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

if not os.environ.get('ANTHROPIC_API_KEY'):
    sys.exit('ERROR: ANTHROPIC_API_KEY must be set in .env')

MODEL = 'claude-opus-4-8'

EDITOR = """You are a ruthless line editor. Your ONE job: make a finished markdown
newsletter read like a sharp, experienced human investor wrote it — not an AI.

ABSOLUTE PRESERVATION RULES (breaking any of these is total failure):
- Do NOT change any number, percentage, price, ticker symbol, date, or proper noun.
- Reproduce every markdown table EXACTLY, character for character. Never touch a
  line that starts with "|".
- Keep every heading (lines starting with "#") unchanged, in the same order.
- Keep the italic "*Methodology.*" footnote and any disclaimer lines verbatim.
- Do not add facts or claims. Do not drop a section. Do not change section meaning.

EDIT ONLY THE PROSE, to kill these AI tells:
- Banned constructions: "it's not just X, it's Y" / "this isn't about X, it's Y";
  "Here's the thing / the whole point"; "Let's dig in"; "Make no mistake"; "The
  bottom line"; "Read that again"; "Let that sink in"; a clause like "That's the
  signal/rotation/tell" used as a section-ending stinger; a rhetorical question
  answered in the next sentence; "X. Full stop." / "Period."; opening a section by
  restating its header; closing with "In short, …" or "The takeaway is …".
- The rule of three: AI defaults to "A, B, and C" triplets and three-part parallel
  sentences. Recast as one, two, or four items. Break the triplet rhythm.
- Em-dashes: at most one per paragraph; usually a comma or full stop is better.
- Bold: remove almost all of it. Leave bold on at most two or three genuinely
  surprising items in the entire issue. A bolded line in every section is a tell.
- Delete filler intensifiers: genuinely, truly, simply, really, quite, very,
  clearly, notably, importantly, crucially, fundamentally, exactly, precisely.
- Delete hedge stacks: "that said," "to be fair," "of course," "it's worth noting."
- Banned vocabulary: delve, tapestry, underscore(s), landscape, navigate
  (figuratively), realm, robust, leverage (verb), testament, pivotal, crucial,
  garner, boast, "stark reminder," "speaks volumes," "double-edged sword," "perfect
  storm," "in the world of," "when it comes to."
- Vary sentence and paragraph length. Let some sentences end flat, with no kicker.

Preserve the author's meaning and every data point. Output ONLY the edited markdown
document — no preamble, no commentary, no code fences."""


def table_lines(text):
    return sorted(l.strip() for l in text.splitlines() if l.lstrip().startswith('|'))


def headings(text):
    return [l.strip() for l in text.splitlines() if l.lstrip().startswith('#')]


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        cands = [p for p in glob.glob(os.path.join(BASE, 'newsletter_draft_*.md'))
                 if '_humanized' not in p]
        if not cands:
            sys.exit('No newsletter_draft_*.md found')
        path = sorted(cands)[-1]

    original = open(path, encoding='utf-8').read()
    print(f'Humanizing {os.path.basename(path)} ({len(original)} chars) with {MODEL} ...')

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=MODEL,
        max_tokens=20000,
        thinking={'type': 'adaptive'},
        output_config={'effort': 'medium'},
        system=[{'type': 'text', 'text': EDITOR, 'cache_control': {'type': 'ephemeral'}}],
        messages=[{'role': 'user',
                   'content': "Edit this newsletter draft. Output only the edited "
                              "markdown:\n\n" + original}],
    ) as stream:
        for _ in stream.text_stream:
            pass
        msg = stream.get_final_message()
    edited = "".join(b.text for b in msg.content if b.type == 'text').strip() + "\n"

    # ── guards: data must survive untouched ──────────────────────────────────
    problems = []
    if table_lines(original) != table_lines(edited):
        problems.append('table lines changed')
    if headings(original) != headings(edited):
        problems.append('headings changed/reordered')
    if not (0.6 * len(original) <= len(edited) <= 1.2 * len(original)):
        problems.append(f'length out of range ({len(edited)} vs {len(original)})')

    u = msg.usage
    if problems:
        alt = os.path.splitext(path)[0] + '_humanized.md'
        open(alt, 'w', encoding='utf-8').write(edited)
        print(f'\n[GUARD FAILED: {"; ".join(problems)}]')
        print(f'[original LEFT INTACT; humanized version saved for review -> {os.path.basename(alt)}]')
    else:
        try:
            open(path, 'w', encoding='utf-8').write(edited)
        except PermissionError:
            path = os.path.splitext(path)[0] + f'_{datetime.now(timezone.utc):%H%M%S}.md'
            open(path, 'w', encoding='utf-8').write(edited)
            print('\n(primary path locked — wrote to a timestamped copy instead)')
        print(f'\n[humanized in place -> {os.path.basename(path)}]')
    print(f'[tokens: in={u.input_tokens} out={u.output_tokens}]')


if __name__ == '__main__':
    main()
