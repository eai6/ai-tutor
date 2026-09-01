"""Convert the Country Adoption Playbook .docx into documentation partials.

The playbook is authored in Word by the people who own the argument, not in
this repository. Rather than transcribe it by hand — which goes stale the
first time a paragraph is edited — this script reads the .docx and writes one
HTML fragment per top-level section into templates/docs/sections/.

Run it again whenever docs/AI_Tutor_Country_Adoption_Playbook.docx changes:

    venv/bin/python scripts/build_playbook_docs.py

Two outputs, both committed, so a deployment needs neither the .docx nor this
script:

    templates/docs/sections/<slug>.html   the prose
    apps/docs/_index.py                   each section's h2 anchors, and the
                                          set of words its prose contains

The split is deliberate. Everything generated is derived from the Word file
and must never be hand-edited; the section index in apps/docs/playbook.py —
order, grouping, one-line summaries — is hand-written, because those are
editorial decisions the document does not carry.

Word constructs and what they become:

    Heading1                 the section boundary (one file each)
    Heading2 / Heading3      h2 / h3, with a slug id so a heading is linkable
    ListBullet / ListNumber  ul / ol, consecutive runs merged
    Courier New runs         pre.doc-code — these are shell commands and the
                             two ASCII diagrams, and w:br carries their newlines
    a 1x1 table              aside.doc-note — the boxed asides; the first
                             paragraph is the label, the rest is the body
    any other table          table.doc-table, first row as the header
"""
from __future__ import annotations

import html
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'

REPO = Path(__file__).resolve().parent.parent
DOCX = REPO / 'docs' / 'AI_Tutor_Country_Adoption_Playbook.docx'
OUT = REPO / 'ai_tutor' / 'templates' / 'docs' / 'sections'
INDEX_MODULE = REPO / 'ai_tutor' / 'apps' / 'docs' / '_index.py'

# Heading1 text -> output slug. Hand-mapped rather than slugified: the file
# names are URLs, and "6.  What it costs, and what drives the cost" does not
# make one. A heading missing from here stops the build instead of silently
# producing an unreachable page.
SLUGS = {
    '1.  Executive summary': 'executive-summary',
    '2.  What is actually being adopted': 'what-is-adopted',
    '3.  The five layers of adaptation': 'five-layers',
    '4.  Four models of adoption': 'adoption-models',
    '5.  Data sovereignty: the honest account': 'data-sovereignty',
    '6.  What it costs, and what drives the cost': 'costs',
    '7.  Cost sharing and sustainability': 'cost-sharing',
    '8.  Evidence, evaluation and quality assurance': 'evidence',
    '9.  A phased adoption roadmap': 'roadmap',
    '10.  Teacher training and support': 'teacher-training',
    '11.  Governance and what the country must provide': 'governance',
    '12.  Risks and how they are managed': 'risks',
    'A.  Country onboarding checklist': 'onboarding-checklist',
    'B.  Language and locale adaptation': 'language-locale',
    'C.  Curriculum ingestion and the content pipeline': 'curriculum-pipeline',
    'D.  National structures: grades, schools, terminology, thresholds': 'national-structures',
    'E.  Hosting the platform': 'hosting',
    'F.  Quality gates before a cohort': 'quality-gates',
    'G.  Operating the platform': 'operations',
    'H.  Integration surface': 'integration',
    'Appendix 1 — Case study: what a second-country adaptation actually changed': 'case-study',
    'Appendix 2 — Questions to answer before Phase 1': 'before-phase-1',
    'Appendix 3 — Glossary': 'glossary',
}

# Everything up to and including the table of contents is replaced by the
# documentation index page, which is a card grid rather than a bullet list.
SKIP_SECTIONS = {'Contents'}


def slugify(text: str) -> str:
    text = re.sub(r'[^a-z0-9]+', '-', text.lower())
    return text.strip('-')[:60] or 'section'


def para_style(p) -> str:
    st = p.find(f'{W}pPr/{W}pStyle')
    return st.get(f'{W}val') if st is not None else 'Normal'


def run_text(run) -> str:
    """Run text with w:br as a newline and w:tab as spaces.

    The breaks matter: every shell block in the document is one paragraph
    whose lines are separated by w:br, so dropping them concatenates
    `cd ai-tutor` onto `docker run` and produces a command nobody can run.
    """
    out = []
    for node in run:
        tag = node.tag[len(W):]
        if tag == 't':
            out.append(node.text or '')
        elif tag == 'br':
            out.append('\n')
        elif tag == 'tab':
            out.append('    ')
    return ''.join(out)


def is_code(p) -> bool:
    runs = [r for r in p.findall(f'{W}r') if run_text(r).strip()]
    if not runs:
        return False
    return all(
        (r.find(f'{W}rPr/{W}rFonts') is not None
         and 'Courier' in (r.find(f'{W}rPr/{W}rFonts').get(f'{W}ascii') or ''))
        for r in runs
    )


def toggled(run, prop: str) -> bool:
    """True if a run carries <w:b/> or <w:i/> as ON.

    Word writes the OFF state as an element too — <w:b w:val="0"/> — so
    presence alone is not the answer. Reading it that way marked the entire
    document bold and italic, because the body style sets both explicitly off.
    """
    node = run.find(f'{W}rPr/{W}{prop}')
    if node is None:
        return False
    return (node.get(f'{W}val') or 'true').lower() not in ('0', 'false', 'none')


def inline(p) -> str:
    """Paragraph text as HTML, keeping bold and italic runs."""
    parts = []
    for r in p.findall(f'{W}r'):
        text = run_text(r)
        if not text:
            continue
        esc = html.escape(text).replace('\n', '<br>')
        if toggled(r, 'b'):
            esc = f'<strong>{esc}</strong>'
        if toggled(r, 'i'):
            esc = f'<em>{esc}</em>'
        parts.append(esc)
    return ''.join(parts).strip()


def plain(p) -> str:
    return ''.join(run_text(r) for r in p.findall(f'{W}r')).strip()


def cell_html(tc) -> str:
    """A table cell. Mostly one paragraph; a few carry a list."""
    out, buf = [], []

    def flush():
        if buf:
            out.append('<ul>' + ''.join(f'<li>{i}</li>' for i in buf) + '</ul>')
            buf.clear()

    for p in tc.findall(f'{W}p'):
        text = inline(p)
        if not text:
            continue
        if para_style(p).startswith('ListBullet'):
            buf.append(text)
        else:
            flush()
            out.append(text)
    flush()
    return '<br>'.join(out) if len(out) > 1 else (out[0] if out else '')


def render_table(tbl) -> str:
    rows = tbl.findall(f'{W}tr')
    if not rows:
        return ''

    # A one-cell table is the document's boxed aside, not tabular data. The
    # first paragraph is its label; everything after is the body.
    if len(rows) == 1 and len(rows[0].findall(f'{W}tc')) == 1:
        paras = [inline(p) for p in rows[0].find(f'{W}tc').findall(f'{W}p')]
        paras = [p for p in paras if p]
        if not paras:
            return ''
        label = paras[0]
        body = ''.join(f'<p>{b}</p>' for b in paras[1:])
        return (f'<aside class="doc-note">'
                f'<p class="doc-note__label">{label}</p>{body}</aside>')

    head = rows[0]
    head_cells = ''.join(
        f'<th scope="col">{cell_html(tc)}</th>' for tc in head.findall(f'{W}tc')
    )
    body_rows = []
    for tr in rows[1:]:
        cells = tr.findall(f'{W}tc')
        # First cell is the row's subject — a row header, so a screen reader
        # announces "NAT gateway, US$ per month, 33" rather than a bare number.
        tds = [f'<th scope="row">{cell_html(cells[0])}</th>'] if cells else []
        tds += [f'<td>{cell_html(tc)}</td>' for tc in cells[1:]]
        body_rows.append('<tr>' + ''.join(tds) + '</tr>')
    return (
        '<div class="doc-table-wrap" tabindex="0" role="region">'
        f'<table class="doc-table"><thead><tr>{head_cells}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table></div>'
    )


def render_body(elements) -> tuple[str, list[dict]]:
    """Section elements -> (html, on-page contents entries)."""
    out: list[str] = []
    toc: list[dict] = []
    list_tag: str | None = None
    seen: set[str] = set()

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f'</{list_tag}>')
            list_tag = None

    def anchor(text: str) -> str:
        base = slugify(text)
        slug, n = base, 2
        while slug in seen:
            slug, n = f'{base}-{n}', n + 1
        seen.add(slug)
        return slug

    for el in elements:
        tag = el.tag[len(W):]
        if tag == 'tbl':
            close_list()
            out.append(render_table(el))
            continue
        if tag != 'p':
            continue

        style = para_style(el)
        text = inline(el)
        if not text:
            continue

        if style in ('Heading2', 'Heading3'):
            close_list()
            level = 2 if style == 'Heading2' else 3
            slug = anchor(plain(el))
            out.append(f'<h{level} id="{slug}">{text}</h{level}>')
            if level == 2:
                toc.append({'id': slug, 'title': plain(el)})
            continue

        if style.startswith('ListBullet') or style.startswith('ListNumber'):
            want = 'ul' if style.startswith('ListBullet') else 'ol'
            if list_tag != want:
                close_list()
                out.append(f'<{want} class="doc-list">')
                list_tag = want
            out.append(f'<li>{text}</li>')
            continue

        close_list()
        if is_code(el):
            code = html.escape(''.join(run_text(r) for r in el.findall(f'{W}r')).strip('\n'))
            out.append(f'<pre class="doc-code"><code>{code}</code></pre>')
        else:
            out.append(f'<p>{text}</p>')

    close_list()
    return '\n'.join(out), toc


WORD = re.compile(r"[a-z0-9][a-z0-9._/-]+")


def words(markup: str) -> str:
    """The distinct words in a section, for the index page's search.

    Deduplicated and sorted: the search asks "does this section contain this
    string", never "how often" or "where", so positions and repeats are 45 KB
    of nothing. What survives is what makes searching "NAT gateway" or
    "pgvector" land on the right section rather than on nothing.
    """
    text = html.unescape(re.sub(r'<[^>]+>', ' ', markup)).lower()
    return ' '.join(sorted(set(WORD.findall(text))))


def main() -> int:
    if not DOCX.exists():
        print(f'missing source document: {DOCX}', file=sys.stderr)
        return 1

    body = ET.fromstring(zipfile.ZipFile(DOCX).read('word/document.xml')).find(f'{W}body')

    sections: list[tuple[str, list]] = []
    current: list = []
    title = ''
    for el in body:
        tag = el.tag[len(W):]
        if tag == 'p' and para_style(el) == 'Heading1':
            if title:
                sections.append((title, current))
            title, current = plain(el), []
        elif tag in ('p', 'tbl'):
            current.append(el)
    if title:
        sections.append((title, current))

    OUT.mkdir(parents=True, exist_ok=True)
    written, index = 0, []
    for title, elements in sections:
        if title in SKIP_SECTIONS:
            continue
        slug = SLUGS.get(title)
        if slug is None:
            print(f'no slug mapped for heading: {title!r}', file=sys.stderr)
            return 1
        markup, toc = render_body(elements)
        (OUT / f'{slug}.html').write_text(
            '{# Generated by scripts/build_playbook_docs.py — edit the .docx, not this. #}\n'
            + markup + '\n',
            encoding='utf-8',
        )
        written += 1
        index.append((slug, title, toc, words(markup)))

    lines = [
        '"""Generated by scripts/build_playbook_docs.py — do not edit.',
        '',
        "HEADINGS  each section's h2 anchors, so a section page renders its own",
        '          contents list server-side rather than assembling it in the browser.',
        'WORDS     the distinct words in each section, served as JSON to the index',
        '          page so its search reaches the prose and not only the headings.',
        '',
        'Regenerate by rerunning the script against the .docx.',
        '"""',
        '',
        'HEADINGS = {',
    ]
    for slug, _title, toc, _blob in index:
        lines.append(f'    {slug!r}: [')
        for entry in toc:
            lines.append(f'        ({entry["id"]!r}, {entry["title"]!r}),')
        lines.append('    ],')
    lines.append('}')
    lines.append('')
    lines.append('WORDS = {')
    for slug, _title, _toc, blob in index:
        lines.append(f'    {slug!r}: {blob!r},')
    lines.append('}')
    INDEX_MODULE.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    print(f'wrote {written} sections to {OUT.relative_to(REPO)}')
    print(f'wrote headings and word index to {INDEX_MODULE.relative_to(REPO)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
