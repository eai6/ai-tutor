"""Extract every tutor system prompt variant into design/prompts/<name>.md.

Each variant lives as a triple-quoted Python string constant inside either
the production prompt builders (apps/tutoring/prompts/{anthropic,gemini}.py)
or one of the A/B cycle wrapper scripts (scripts/run_ab_v{N}_cycle.py). The
A/B harness monkey-patches the wrapper constants over the production ones
at runtime — the production files are never edited as part of an A/B run.

This script reads the source files, pulls each constant body out by regex,
and writes one markdown file per variant under design/prompts/. The
markdown files are SECONDARY copies (human-readable, side-by-side
reviewable) — the wrapper scripts remain the source of truth.

Re-run this script any time you:
  - Add a new V{N+1}_TUTOR_SYSTEM_PROMPT_TEMPLATE in a new wrapper.
  - Edit an existing variant's text.
  - Want to refresh the size + line counts in the markdown headers.

Add a new variant by appending a dict to SPECS below.

Run with:  venv/bin/python scripts/extract_prompt_variants.py
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / 'design' / 'prompts'


@dataclass(frozen=True)
class VariantSpec:
    out_filename: str           # name under design/prompts/
    src_path: str               # path relative to repo root
    constant: str               # the triple-quoted constant to extract
    title: str                  # markdown H1
    description: str            # short paragraph for "What this is"
    note: Optional[str] = None  # optional second paragraph (results, caveats)


# Add new variants by appending a VariantSpec to this list. The script
# extracts them in order; order also drives the printed summary table.
SPECS: list[VariantSpec] = [
    VariantSpec(
        out_filename='v3_baseline_anthropic.md',
        src_path='apps/tutoring/prompts/anthropic.py',
        constant='TUTOR_SYSTEM_PROMPT_TEMPLATE',
        title='v3 baseline -- Anthropic (production)',
        description=(
            'The production Claude-shaped prompt currently shipping on `main`. '
            '~460 lines of XML-tagged constraints with internal contradictions '
            '(40-word cap vs worked-example scaffolding) -- see '
            '`design/SCIENCE_LEARNING_AUDIT_v3.md` H2. This is the prompt v4 '
            'was a rewrite of, and the implicit "control" against which '
            'v4/v5/v6 are compared in the A/B cycles.'
        ),
        note=(
            'Subject-pack injection (`apps/tutoring/prompts/injections/math.py` '
            'or `general.py`) is appended after this template by the builder '
            '-- not shown here.'
        ),
    ),
    VariantSpec(
        out_filename='v3_baseline_gemini.md',
        src_path='apps/tutoring/prompts/gemini.py',
        constant='GEMINI_TUTOR_SYSTEM_PROMPT_TEMPLATE',
        title='v3 baseline -- Gemini (production)',
        description=(
            'Gemini-native variant of the production prompt -- markdown-style '
            'rather than XML-tagged. The two providers run different prompts '
            'in production; A/B cycles v4-v6 unify them by patching both with '
            'the same template.'
        ),
        note=(
            'Subject-pack injection (e.g. `injections/math.py`) is appended '
            'after this template by the Gemini builder -- not shown here.'
        ),
    ),
    VariantSpec(
        out_filename='v4.md',
        src_path='scripts/run_ab_v4_cycle.py',
        constant='V4_TUTOR_SYSTEM_PROMPT_TEMPLATE',
        title='v4 -- slim rewrite, FINAL_REPORT v3 recommendations applied',
        description=(
            'First A/B-tested rewrite. Derived from '
            '`design/SCIENCE_LEARNING_AUDIT_v3.md` Section 4 (slim '
            'Gemini-compatible prompt) plus the 10 high-severity prompt edits '
            'from `ab-test-reports/FINAL_REPORT.md` (figure-ref ban, hedge '
            'probing, no verbatim re-pose, stem-variable carry, etc.). '
            'Patched into both Anthropic + Gemini builders by the v4 wrapper.'
        ),
        note=(
            'Result: Sonnet 4 mean 2.88 -> 2.98; Gemini 3 Flash mean 3.10 -> '
            '2.90. See `ab-test-reports-v4/FINAL_REPORT.md` for failure modes.'
        ),
    ),
    VariantSpec(
        out_filename='v5.md',
        src_path='scripts/run_ab_v5_cycle.py',
        constant='V5_TUTOR_SYSTEM_PROMPT_TEMPLATE',
        title=(
            'v5 -- addressing v4 themes (meta-leakage, silent pivot, '
            'diagnose-by-isomorph, worked-example skip)'
        ),
        description=(
            'Built to fix the four v4 high-severity themes: (A) meta-leakage '
            'of mode/tool names + JSON dumps, (B) silent pivot after student '
            'answers, (C) diagnose-by-isomorph on errors, (D) skipped worked '
            'example on novel multi-step calculations. New `<every_turn>` '
            'block forces "first sentence = evaluation"; new '
            '`<student_visible_output>` consolidated ban list; tier-4 '
            'feedback rewritten.'
        ),
        note=(
            'Result: Sonnet 4 mean 2.98 -> 3.10; Gemini 3 Flash mean 2.90 -> '
            '2.90. Side-effect: figure-ref + no-question regressions because '
            'consolidating rules into the ban list diluted their strength on '
            'Gemini Flash. v6 extracted them back out. See '
            '`ab-test-reports-v5/FINAL_REPORT.md`.'
        ),
    ),
    VariantSpec(
        out_filename='v6.md',
        src_path='scripts/run_ab_v6_cycle.py',
        constant='V6_TUTOR_SYSTEM_PROMPT_TEMPLATE',
        title=(
            'v6 -- figure-ref + must-end-with-question extracted to standalone '
            'blocks'
        ),
        description=(
            'Reverses the v5 consolidation regression: pulls `<figure_rules>` '
            'and `<must_end_with_question>` out of the '
            '`<student_visible_output>` ban list into their own emphatic '
            'standalone blocks. JSON / dev-field / self-talk / mode-name bans '
            'stay in the consolidated block (those held in v5). Strengthens '
            '`<every_turn>` rule 3 with redundancy.'
        ),
        note=(
            'Cycle also included engine changes (regen dedup penalty in '
            '`regen/self_retry.py`; LLM-judged template-repeat detection via '
            '`repeated_question.detect_template_repeat` + '
            '`exit_ticket_grader.JUDGE_TEMPLATE_REPEAT`). Result: Sonnet 4 '
            'mean 3.10 -> 3.27 (best of any cycle); Gemini 3 Flash mean 2.90 '
            '-> 3.10 (small n=2 due to 2 cell errors). See '
            '`ab-test-reports-v6/FINAL_REPORT.md`.'
        ),
    ),
    VariantSpec(
        out_filename='v7.md',
        src_path='scripts/run_ab_v7_cycle.py',
        constant='V7_TUTOR_SYSTEM_PROMPT_TEMPLATE',
        title=(
            'v7 -- structural restructure: valid-turn contract + branch '
            'templates + scoped wrong-answer policy'
        ),
        description=(
            'Response to `design/prompts/v6-prompt-feedback.md`. Replaces '
            'the v6 mix of overlapping blocks with: (1) `<valid_turn_contract>` '
            'of 7 mechanical rules first, (2) `<turn_algorithm>` + '
            '`<branch_templates>` (FEEDBACK / WORKED_EXAMPLE / PRACTICE / '
            'REMEDIATION / TEACH) as runtime control, (3) `<wrong_answer_policy>` '
            'with scoped 3-attempt progression (resolves v6 same-problem vs '
            'structurally-different collision), (4) `<media_contract>` that '
            'reframes `|||MEDIA:N|||` as a system-side marker stripped before '
            'display, (5) FEEDBACK step 1 mandates reading the bank grader '
            'verdict FIRST and never overriding it, (6) `<final_check>` for '
            'silent self-validation. Principles demoted to background; engine '
            'validators (no_question, figure_ref, repeated_question, '
            'same_template_repeat, regen dedup) are trusted, not duplicated '
            'in prose.'
        ),
        note=(
            'Engine fixes from v6 still active. Tested mid-tier first '
            '(Sonnet 4 + Gemini 3 Flash) per the AB_TESTING_PLAN canonical '
            'comparison. Result: <fill in after run>.'
        ),
    ),
]


def extract_triple_quoted(src: str, constant_name: str) -> tuple[str, int]:
    # Match: <CONSTANT> = """ ...body... """
    pattern = re.compile(
        rf'^{re.escape(constant_name)}\s*=\s*"""(.*?)"""\s*$',
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(src)
    if not m:
        raise ValueError(
            f"Could not find `{constant_name} = \"\"\"...\"\"\"` in source"
        )
    line = src[:m.start()].count('\n') + 1
    return m.group(1), line


def render_markdown(spec: VariantSpec, body: str, source_line: int) -> str:
    char_count = len(body)
    line_count = body.count('\n') + 1
    approx_tokens = char_count // 4

    parts: list[str] = []
    parts.append(f"# {spec.title}\n")
    parts.append('## Provenance\n')
    parts.append(f"- **Source**: `{spec.src_path}:{source_line}`")
    parts.append(f"- **Constant**: `{spec.constant}`")
    parts.append(
        f"- **Size**: {char_count:,} chars  ~{approx_tokens:,} tokens  "
        f"{line_count} lines"
    )
    parts.append('')
    parts.append('## What this is\n')
    parts.append(spec.description)
    parts.append('')
    if spec.note:
        parts.append('## Notes\n')
        parts.append(spec.note)
        parts.append('')
    parts.append('## Template\n')
    parts.append(
        'Interpolation tokens (single `{braces}`) are substituted at session '
        'start: `{tutor_name}`, `{institution_name}`, `{locale_context}`, '
        '`{language}`, `{grade_level}`, `{safety_prompt}`. Unknown tokens '
        'render as empty strings via `defaultdict(str)`.'
    )
    parts.append('')
    parts.append('```text')
    parts.append(body)
    parts.append('```')
    return '\n'.join(parts)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Extracting {len(SPECS)} variant(s) into {OUT_DIR.relative_to(REPO_ROOT)}/")
    print()

    rows: list[tuple[str, str, int, int]] = []  # (out, source, chars, lines)
    errors: list[str] = []

    for spec in SPECS:
        src_path = REPO_ROOT / spec.src_path
        if not src_path.exists():
            errors.append(f"{spec.out_filename}: source missing -> {spec.src_path}")
            continue
        src_text = src_path.read_text()
        try:
            body, source_line = extract_triple_quoted(src_text, spec.constant)
        except ValueError as exc:
            errors.append(f"{spec.out_filename}: {exc}")
            continue

        out_path = OUT_DIR / spec.out_filename
        out_path.write_text(render_markdown(spec, body, source_line))
        chars = len(body)
        lines = body.count('\n') + 1
        rows.append((spec.out_filename, f"{spec.src_path}:{source_line}", chars, lines))
        print(
            f"  {spec.out_filename:40s} "
            f"{chars:>7,} chars  {lines:>4} lines  <- {spec.src_path}"
        )

    print()
    if errors:
        print("ERRORS:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK -- {len(rows)} variant(s) written.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
