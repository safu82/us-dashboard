"""Stage 3 — the press release reader. The first stage that uses a model.

Transcripts of earnings calls are the obvious source for management
commentary and the hardest to get: usually paywalled, inconsistently
formatted, often transcribed by hand. The 8-K exhibit 99.1 is not. It is the
earnings press release, it is free on EDGAR, it is filed within minutes of
the numbers, and it carries the CEO and CFO prepared quotes plus whatever
guidance the company chose to put in writing.

So this stage takes the free source first. A transcript stage can be added
later without changing anything downstream.

Three steps, and the tiering is the point:

  1. Fetch and flatten the exhibit        deterministic, free
  2. Extract quotes and guidance          cheap model
  3. Verify every quote against the text  deterministic, free

Step 3 is what makes step 2 safe. A model asked for verbatim quotes will
occasionally produce a plausible sentence that is not in the document, and
no amount of prompting reliably stops it. Checking each returned quote
against the source costs nothing and turns "probably accurate" into
"verified, or flagged".
"""
import io
import json
import os
import re
import sys
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edgar

# ANTHROPIC_API_KEY lives in the repo's .env alongside the Supabase and X
# credentials, and the SDK only reads the process environment.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except ImportError:
    pass

# Extraction is high-volume, low-judgment work: exactly the tier that should
# never touch a frontier model. Rates are per million tokens; confirm against
# current pricing before trusting the cost line.
MODEL = 'claude-haiku-4-5-20251001'
RATE_IN, RATE_OUT = 1.00, 5.00
MAX_CHARS = 60000       # press releases run long; the quotes are near the top


# ---------------------------------------------------------------------------
# 1. HTML -> text
# ---------------------------------------------------------------------------

class _Text(HTMLParser):
    """Flatten filing HTML, keeping table rows legible.

    Financial tables carry the segment detail, and collapsing them into a run
    of loose numbers destroys which figure belongs to which line. Rows are
    emitted pipe-separated so both a human and a model can still read them.
    """
    SKIP = {'script', 'style', 'head'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.cell, self.row = [], [], []
        self.in_table, self.skip = 0, 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == 'table':
            self.in_table += 1
        elif tag == 'tr' and self.in_table:
            self.row = []
        elif tag in ('td', 'th') and self.in_table:
            self.cell = []
        elif tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == 'table':
            self.in_table = max(0, self.in_table - 1)
            self.parts.append('\n')
        elif tag in ('td', 'th') and self.in_table:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = []
        elif tag == 'tr' and self.in_table:
            cells = [c for c in self.row if c]
            if cells:
                self.parts.append('  | ' + ' | '.join(cells) + '\n')
            self.row = []

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_table:
            self.cell.append(data)
        else:
            self.parts.append(data)


def to_text(html):
    p = _Text()
    p.feed(html)
    txt = ''.join(p.parts)
    txt = txt.replace('\xa0', ' ')
    txt = re.sub(r'[ \t]+', ' ', txt)
    txt = re.sub(r'\n\s*\n\s*\n+', '\n\n', txt)
    return txt.strip()


# ---------------------------------------------------------------------------
# 2. extraction
# ---------------------------------------------------------------------------

PROMPT = """You are extracting from a company earnings press release (SEC \
8-K exhibit 99.1). Extract only. Do not analyse, summarise, or judge.

Return JSON exactly in this shape:

{
  "quotes": [
    {"speaker": "full name", "role": "title as stated or empty",
     "text": "the quotation, verbatim and complete",
     "topic": "one of: results, demand, margins, capital, guidance, \
strategy, other"}
  ],
  "guidance": [
    {"metric": "what is being guided", "period": "which period",
     "value": "the figure or range as stated",
     "verbatim": "the sentence it came from, word for word"}
  ],
  "segments": ["segment names that have their own reported figures"],
  "topics": ["subjects the release actually discusses"],
  "absent_from_this_document": ["things an earnings release often states \
that THIS DOCUMENT does not contain. Phrase every entry as a fact about the \
document, never about the company. Write 'no capex guidance in this \
release', not 'the company gave no capex guidance' — many issuers guide \
verbally on the call and put nothing in writing, so an absence here is \
evidence about the document only. Only list what you are confident is \
absent."]
}

Rules:
- Quotations must be copied character for character from the document. Never \
paraphrase, never tidy, never join two sentences that are not adjacent.
- If the release contains no management quotation, return an empty list. An \
empty list is correct and useful; an invented quote is not.
- Guidance means a forward statement the company makes about its own future \
figures. A historical result is not guidance.
- Return JSON and nothing else.

DOCUMENT:
"""


def extract(text, model=MODEL):
    """Pull quotes and guidance with a cheap model. Returns (pack, usage)."""
    import anthropic
    client = anthropic.Anthropic()
    body = text[:MAX_CHARS]
    resp = client.messages.create(
        model=model, max_tokens=4000,
        messages=[{'role': 'user', 'content': PROMPT + body}])
    raw = ''.join(b.text for b in resp.content if b.type == 'text').strip()
    raw = re.sub(r'^```(?:json)?|```$', '', raw, flags=re.M).strip()
    try:
        pack = json.loads(raw)
    except json.JSONDecodeError as e:
        pack = {'error': f'model did not return JSON: {e}', 'raw': raw[:2000]}
    usage = {
        'model': model,
        'input_tokens': resp.usage.input_tokens,
        'output_tokens': resp.usage.output_tokens,
        'cost_usd': round(resp.usage.input_tokens / 1e6 * RATE_IN
                          + resp.usage.output_tokens / 1e6 * RATE_OUT, 5),
        'chars_sent': len(body),
        'chars_truncated': max(0, len(text) - len(body)),
    }
    return pack, usage


# ---------------------------------------------------------------------------
# 3. verification
# ---------------------------------------------------------------------------

def _norm(s):
    """Compare on words alone. Filings are full of non-breaking spaces,
    curly quotes and soft hyphens that differ between the HTML and any
    sensible rendering of it, and none of that changes whether the sentence
    is in the document."""
    s = (s or '').replace('’', "'").replace('‘', "'")
    s = s.replace('“', '"').replace('”', '"')
    s = s.replace('—', '-').replace('–', '-')
    return re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()


SHINGLE = 8          # words per window
VERBATIM_AT = 0.95   # coverage above this is a faithful quotation
PARTIAL_AT = 0.60    # below this, treat as unlocated


def _coverage(needle, hay):
    """How much of the quote is in the document, and in what shape.

    A plain substring test is too strict, and wrongly so. Press releases
    routinely interrupt a quotation with its attribution —

        "...growth in our data center business," said David Goeckeler,
        Chairman and CEO of Sandisk, "we are executing on the strategy..."

    Stitching those halves together is correct quoting, not invention, but
    the quote then never appears as one contiguous run.

    Coverage alone cannot tell the two cases apart, so the shape of the
    misses decides it. One splice breaks exactly the SHINGLE-1 windows that
    straddle it, and they are consecutive. Rewording scatters its misses.
    So: contiguous short runs of failure mean a spliced quotation and are
    faithful; scattered failures mean the words were changed.

    Returns (coverage, window count, number of splice points, scattered?).
    """
    w = needle.split()
    if not w:
        return 0.0, 0, 0, False
    if len(w) <= SHINGLE:
        return (1.0 if needle in hay else 0.0), 1, 0, needle not in hay
    windows = [' '.join(w[i:i + SHINGLE]) for i in range(len(w) - SHINGLE + 1)]
    miss = [i for i, s in enumerate(windows) if s not in hay]
    cov = 1 - len(miss) / len(windows)
    if not miss:
        return cov, len(windows), 0, False
    runs, start, prev = [], miss[0], miss[0]
    for i in miss[1:]:
        if i != prev + 1:
            runs.append((start, prev))
            start = i
        prev = i
    runs.append((start, prev))
    # A splice can break at most SHINGLE-1 consecutive windows. Anything
    # longer is missing text, not a join.
    scattered = any((b - a + 1) > SHINGLE - 1 for a, b in runs)
    return cov, len(windows), len(runs), scattered


def verify_quotes(pack, text):
    """Every quotation must be findable in the source document.

    This is the check that makes a cheap model usable here. A quote that
    cannot be located is not necessarily invented — it may be reworded — but
    either way it must not reach a note unmarked.
    """
    hay = _norm(text)
    results = []

    def judge(cov, splices, scattered):
        if cov >= VERBATIM_AT:
            return 'VERBATIM'
        if not scattered and cov >= PARTIAL_AT:
            # Every gap is short and contiguous: the quote was spliced around
            # its attribution, which is faithful quoting.
            return 'SPLICED'
        return 'PARTIAL' if cov >= PARTIAL_AT else 'NOT FOUND'

    for q in (pack.get('quotes') or []):
        needle = _norm(q.get('text'))
        if not needle:
            results.append({'speaker': q.get('speaker'), 'verdict': 'EMPTY',
                            'coverage': 0.0})
            continue
        cov, n, splices, scattered = _coverage(needle, hay)
        results.append({'speaker': q.get('speaker'), 'topic': q.get('topic'),
                        'chars': len(q.get('text') or ''),
                        'coverage': round(cov, 3), 'windows': n,
                        'splices': splices, 'scattered': scattered,
                        'verdict': judge(cov, splices, scattered)})
    for g in (pack.get('guidance') or []):
        cov, n, splices, scattered = _coverage(_norm(g.get('verbatim')), hay)
        results.append({'guidance': g.get('metric'),
                        'coverage': round(cov, 3), 'splices': splices,
                        'verdict': judge(cov, splices, scattered)})
    return results


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def read(ticker, out_dir=None, model=MODEL):
    filings = edgar.earnings_8k(ticker, limit=1)
    if not filings:
        return {'ticker': ticker, 'error': 'no 8-K with Item 2.02 found'}
    f = filings[0]
    ex = edgar.earnings_exhibit(f)
    if not ex:
        return {'ticker': ticker, 'error': 'no exhibit found in the 8-K'}

    html = edgar.fetch_document(ex['url'])
    text = to_text(html)
    pack, usage = extract(text, model)
    checks = verify_quotes(pack, text)

    out = {'ticker': ticker.upper(), 'form': '8-K', 'items': f['items'],
           'filed': f['filed'], 'period': f['period'],
           'accession': f['accession'], 'exhibit': ex['name'],
           'source_url': ex['url'], 'text_chars': len(text),
           'extraction': pack, 'quote_checks': checks, 'usage': usage}

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        stem = f"{ticker.upper()}_{f['period']}_8K"
        with io.open(os.path.join(out_dir, stem + '_release.json'), 'w',
                     encoding='utf-8') as fh:
            json.dump(out, fh, indent=1, ensure_ascii=False)
        with io.open(os.path.join(out_dir, stem + '_release.txt'), 'w',
                     encoding='utf-8') as fh:
            fh.write(text)
    return out


def render(o):
    if o.get('error'):
        return f"{o['ticker']}: {o['error']}"
    e, u = o['extraction'], o['usage']
    L = [f"{o['ticker']} 8-K items {o['items']} filed {o['filed']}",
         f"  exhibit {o['exhibit']}  ({o['text_chars']:,} chars of text)"]
    if e.get('error'):
        L.append('  EXTRACTION FAILED: ' + e['error'])
        return '\n'.join(L)
    by = {}
    for c in o['quote_checks']:
        by[c['verdict']] = by.get(c['verdict'], 0) + 1
    L.append(f"  quotes {len(e.get('quotes') or [])}   "
             f"guidance {len(e.get('guidance') or [])}   "
             f"verification " + ', '.join(f'{k} {v}' for k, v in sorted(by.items())))
    for q in (e.get('quotes') or []):
        chk = next((c for c in o['quote_checks']
                    if c.get('speaker') == q.get('speaker')
                    and c.get('topic') == q.get('topic')), {})
        L.append(f"    [{chk.get('verdict','?'):<9} {chk.get('coverage',0):.0%}] "
                 f"{q.get('speaker')} ({q.get('topic')}): "
                 f"\"{(q.get('text') or '')[:140]}...\"")
    for g in (e.get('guidance') or []):
        L.append(f"    GUIDANCE  {g.get('metric')} / {g.get('period')}: "
                 f"{g.get('value')}")
    absent = e.get('absent_from_this_document') or e.get('notable_absences')
    if absent:
        # Labelled explicitly. Alphabet's Q3-26 release contained no guidance
        # while the CFO guided 2026 capex to $195-205bn on the call the same
        # evening, so "absent" here must never be read as "the company was
        # silent".
        L.append('  absent from THIS DOCUMENT (not necessarily unsaid): '
                 + '; '.join(absent))
    L.append(f"  cost ${u['cost_usd']:.5f}  ({u['input_tokens']:,} in / "
             f"{u['output_tokens']:,} out, {u['model']})"
             + (f"  [{u['chars_truncated']:,} chars truncated]"
                if u['chars_truncated'] else ''))
    return '\n'.join(L)


if __name__ == '__main__':
    OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
    for t in (sys.argv[1:] or ['SNDK']):
        print('\n' + render(read(t.upper(), OUT)))
