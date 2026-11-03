"""Render a generated note as a Word document.

    python research/to_docx.py research/out/SNDK_2026-10-02_note.md

Notes are written as markdown because that is what the writer produces and
what diffs sensibly in git. This turns one into a .docx for reading and
sharing, preserving headings, bullets, tables and inline bold rather than
dumping plain text into a page.
"""
import io
import os
import re
import sys

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

BOLD = re.compile(r'\*\*(.+?)\*\*')
GREY = RGBColor(0x60, 0x60, 0x60)


def add_runs(p, text):
    """Write text into a paragraph, honouring **bold** spans."""
    pos = 0
    for m in BOLD.finditer(text):
        if m.start() > pos:
            p.add_run(text[pos:m.start()])
        p.add_run(m.group(1)).bold = True
        pos = m.end()
    if pos < len(text):
        p.add_run(text[pos:])


def add_table(doc, rows):
    """A markdown pipe table, minus its separator row."""
    body = [r for r in rows
            if not re.fullmatch(r'\s*\|?[\s:|-]+\|?\s*', r)]
    cells = [[c.strip() for c in r.strip().strip('|').split('|')] for r in body]
    if not cells:
        return
    width = max(len(r) for r in cells)
    t = doc.add_table(rows=len(cells), cols=width)
    t.style = 'Light Grid Accent 1'
    for i, row in enumerate(cells):
        for j in range(width):
            cell = t.cell(i, j)
            cell.text = ''
            add_runs(cell.paragraphs[0], row[j] if j < len(row) else '')
            if i == 0:
                for run in cell.paragraphs[0].runs:
                    run.bold = True


def convert(md_path, docx_path=None):
    docx_path = docx_path or os.path.splitext(md_path)[0] + '.docx'
    text = io.open(md_path, encoding='utf-8').read()

    doc = Document()
    normal = doc.styles['Normal']
    normal.font.name = 'Calibri'
    normal.font.size = Pt(10.5)

    lines = text.split('\n')
    i, buf = 0, []
    while i < len(lines):
        line = lines[i].rstrip()

        # Pipe table: gather the contiguous block.
        if line.lstrip().startswith('|'):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith('|'):
                rows.append(lines[i])
                i += 1
            add_table(doc, rows)
            continue

        if not line.strip():
            i += 1
            continue

        if line.startswith('### '):
            doc.add_heading(line[4:].strip(), level=3)
        elif line.startswith('## '):
            doc.add_heading(line[3:].strip(), level=2)
        elif line.startswith('# '):
            doc.add_heading(line[2:].strip(), level=1)
        elif re.fullmatch(r'-{3,}', line.strip()):
            # The footer rule. Everything after it is provenance, set small
            # and grey so it reads as a stamp rather than as content.
            i += 1
            while i < len(lines):
                tail = lines[i].strip()
                if tail:
                    p = doc.add_paragraph()
                    r = p.add_run(tail)
                    r.font.size = Pt(8)
                    r.font.color.rgb = GREY
                    r.italic = True
                i += 1
            break
        elif re.match(r'\s*[-*]\s+', line):
            p = doc.add_paragraph(style='List Bullet')
            add_runs(p, re.sub(r'^\s*[-*]\s+', '', line))
        elif re.match(r'\s*\d+\.\s+', line):
            p = doc.add_paragraph(style='List Number')
            add_runs(p, re.sub(r'^\s*\d+\.\s+', '', line))
        else:
            p = doc.add_paragraph()
            add_runs(p, line)
        i += 1

    doc.save(docx_path)
    return docx_path


if __name__ == '__main__':
    targets = sys.argv[1:]
    if not targets:
        import glob
        here = os.path.dirname(os.path.abspath(__file__))
        targets = [p for p in sorted(glob.glob(os.path.join(
            here, 'out', '*_note.md')))
            # Word leaves a binary ~$ owner file beside anything it has open.
            if not os.path.basename(p).startswith('~$')]
    for t in targets:
        out = convert(t)
        print('%s  ->  %s  (%.1f KB)'
              % (os.path.basename(t), out, os.path.getsize(out) / 1024))
