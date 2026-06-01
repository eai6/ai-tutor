#!/usr/bin/env python
"""Audit script — heuristically surfaces user-facing English strings
in student-facing templates that AREN'T wrapped in ``{% trans %}`` /
``{% blocktrans %}``.

Run after M2 wrapping:

    python scripts/list_unwrapped_strings.py

Output lists candidate strings the audit caught with file:line. The
heuristic intentionally over-reports (e.g. text inside <style> blocks
will surface too); a human reviews the output and decides what to
wrap. The point is to make coverage gaps visible — not to perfectly
classify them.

Scope = the student-facing templates wrapped in M2:
  - templates/base.html
  - templates/tutoring/chat_tutor.html, catalog.html
  - templates/accounts/{landing,login,student_login,student_register,settings}.html

Out of scope (intentionally — admin / teacher pages stay English for v1):
  - templates/admin/, templates/dashboard/, templates/benchmark/,
    templates/help/, templates/safety/, templates/email/,
    templates/accounts/{bulk_student_upload,invite_*,staff_*,
    password_reset*}.html

Part of M2d of memory/portuguese_mozambique_pilot_plan.md.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Roots to scan.
BASE_DIR = Path(__file__).resolve().parent.parent
TARGETS = [
    BASE_DIR / 'templates' / 'base.html',
    BASE_DIR / 'templates' / 'tutoring' / 'chat_tutor.html',
    BASE_DIR / 'templates' / 'tutoring' / 'catalog.html',
    BASE_DIR / 'templates' / 'accounts' / 'landing.html',
    BASE_DIR / 'templates' / 'accounts' / 'login.html',
    BASE_DIR / 'templates' / 'accounts' / 'student_login.html',
    BASE_DIR / 'templates' / 'accounts' / 'student_register.html',
    BASE_DIR / 'templates' / 'accounts' / 'settings.html',
]
# M2-ext (2026-06-01): Also scan every template under templates/dashboard/ now
# that the teacher dashboard has been wrapped for Mozambique pilot.
TARGETS.extend(sorted((BASE_DIR / 'templates' / 'dashboard').rglob('*.html')))

# Heuristic patterns for visible text. We catch:
#   - Text content between > and < that starts with an uppercase letter
#     and has at least one space (likely a sentence/phrase, not a tag).
#   - placeholder="…" / title="…" / aria-label="…" attribute values.
_VISIBLE_TEXT = re.compile(
    r'>(\s*[A-Z][A-Za-z][^<>{]{4,})<',
    re.DOTALL,
)
_ATTR_TEXT = re.compile(
    r'\b(?:placeholder|title|aria-label|alt)="([^"{]{4,})"',
)

# Strings that are wrapped — we don't want to false-positive on them.
_WRAPPED_MARKERS = (
    '{% trans',
    '{% blocktrans',
    '|trans',
)

# Patterns that indicate the string is inside script / style — skip.
_SCRIPT_BLOCK = re.compile(
    r'<script\b[^>]*>.*?</script>',
    re.DOTALL | re.IGNORECASE,
)
_STYLE_BLOCK = re.compile(
    r'<style\b[^>]*>.*?</style>',
    re.DOTALL | re.IGNORECASE,
)

# Strings to ignore (proper nouns, brand names, JS-fragment garbage,
# Django filter calls, single words that aren't likely UI strings).
_IGNORE_TEXT = re.compile(
    r'^('
    r'\s*\d+'                       # Just numbers
    r'|AI Tutor'
    r'|Seychelles'
    r'|Tutor'                       # Brand fragments
    r'|.*\bDOCTYPE\b.*'
    r'|.*\{\{.*\}\}.*'              # Pure variable lookup (no surrounding text)
    r')$',
    re.IGNORECASE,
)


def _strip_blocks(text: str) -> str:
    """Strip <script> and <style> blocks so we don't false-positive on
    their content."""
    text = _SCRIPT_BLOCK.sub('', text)
    text = _STYLE_BLOCK.sub('', text)
    return text


def _is_already_wrapped(line: str) -> bool:
    return any(m in line for m in _WRAPPED_MARKERS)


def scan(path: Path) -> list[tuple[int, str]]:
    """Return [(line_number, candidate_string), ...] for unwrapped
    visible strings in ``path``."""
    if not path.exists():
        return []
    raw = path.read_text()
    stripped = _strip_blocks(raw)

    findings: list[tuple[int, str]] = []
    for i, line in enumerate(stripped.splitlines(), start=1):
        if _is_already_wrapped(line):
            continue
        for m in _VISIBLE_TEXT.finditer(line):
            text = m.group(1).strip()
            if _IGNORE_TEXT.match(text):
                continue
            findings.append((i, text[:80]))
        for m in _ATTR_TEXT.finditer(line):
            text = m.group(1).strip()
            if _IGNORE_TEXT.match(text):
                continue
            findings.append((i, f'(attr) {text[:80]}'))
    return findings


def main() -> int:
    total = 0
    for target in TARGETS:
        rel = target.relative_to(BASE_DIR)
        findings = scan(target)
        if findings:
            print(f'\n{rel}')
            for line_no, text in findings:
                print(f'  L{line_no}: {text}')
            total += len(findings)
    print(f'\n=== {total} candidate unwrapped string(s) found across {len(TARGETS)} templates ===')
    print('Heuristic — review and ignore false positives (variable-only `{{ x }}`,')
    print('JS-like fragments, single proper nouns). 0 in scope means M2 coverage is complete.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
