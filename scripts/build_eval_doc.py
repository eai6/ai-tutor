"""Generate AI_Tutor_Eval_Harness.docx — a standalone explainer of the
eval harness built in evals/.

Run:
    python scripts/build_eval_doc.py [--output <path>]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / 'AI_Tutor_Eval_Harness.docx'


def add_title(doc, text):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(22)


def add_subtitle(doc, text):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run.italic = True
    run.font.size = Pt(12)
    run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)


def add_para(doc, text, *, bold=False, italic=False):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = bold
    run.italic = italic
    return p


def add_bullet(doc, text):
    doc.add_paragraph(text, style='List Bullet')


def add_code(doc, text):
    """Mono-spaced inline block. python-docx has no real code block style, so
    we approximate with the 'No Spacing' style + Courier."""
    p = doc.add_paragraph(style='No Spacing')
    run = p.add_run(text)
    run.font.name = 'Courier New'
    run.font.size = Pt(9)


def add_table(doc, header, rows):
    table = doc.add_table(rows=1 + len(rows), cols=len(header))
    table.style = 'Light Grid Accent 1'
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    hdr_cells = table.rows[0].cells
    for i, h in enumerate(header):
        hdr_cells[i].text = h
        for para in hdr_cells[i].paragraphs:
            for run in para.runs:
                run.bold = True
    for r, row in enumerate(rows, start=1):
        for c, val in enumerate(row):
            table.rows[r].cells[c].text = str(val)


def build(output_path: Path) -> Path:
    doc = Document()

    # -----------------------------------------------------------------
    # Title page block
    # -----------------------------------------------------------------
    add_title(doc, 'AI Tutor Evaluation Harness')
    add_subtitle(doc,
                 'A curated, repo-checked-in regression suite for the conversational tutor')

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run('Nyansapo Labs — AI Tutor (ai-tutor)')
    run.font.size = Pt(11)
    run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run('Branch: pixeldesignlabs-dev   ·   Phases 1–6 shipped')
    run.font.size = Pt(10)
    run.font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    doc.add_paragraph()

    # -----------------------------------------------------------------
    # 1. Executive Summary
    # -----------------------------------------------------------------
    doc.add_heading('1. Executive Summary', level=1)
    add_para(doc,
        'The AI Tutor Evaluation Harness is a curated, version-controlled test '
        'suite that lets us run the entire conversational tutor against a fixed '
        'set of scenarios and obtain a single comparable score in one command. '
        'It is the regression signal we did not have before: every time we '
        'change a prompt, tweak a judge, or swap a model, we can re-run the '
        'suite and see — quantitatively — whether the change helped, hurt, or '
        'left things unchanged.')

    add_para(doc,
        'The harness exercises the tutor across five distinct student personas '
        '(struggler, average, capable, probe-resistant, non-responder), two '
        'subjects (math and geography), and two execution modes (single-turn '
        'response evaluation and full multi-turn session simulation). Scoring '
        'happens through three layers — deterministic assertions, '
        'judge-derived labels from the production judge pipeline, and an '
        'LLM-as-judge rubric — so every scenario produces both a pass/fail '
        'verdict and a rich diagnostic trail explaining why.')

    add_para(doc,
        'The dataset (23 scenarios at the time of writing) lives in YAML files '
        'inside the repo. Same git SHA in, same scenario set out, forever. A '
        'single Django management command runs the suite and writes a dated '
        'JSON result blob; a companion report tool computes pass-rate '
        'comparisons between any two runs, surfacing newly passing and newly '
        'failing scenarios with the specific assertions that flipped.')

    # -----------------------------------------------------------------
    # 2. The Problem
    # -----------------------------------------------------------------
    doc.add_heading('2. The Problem It Solves', level=1)

    add_para(doc,
        'Before this harness existed, the AI Tutor had two related but '
        'distinct quality systems, neither of which could answer the question '
        '"did this code change make the tutor better or worse?"')

    add_para(doc, 'apps/benchmark/ — production sampling', bold=True)
    add_para(doc,
        'Pulls real student turns from production into BenchmarkItem snapshots '
        'and asks a human reviewer (Edward) to label them. This produces '
        'ground-truth labels grounded in real usage, but it requires real '
        'pilot traffic to feed it and human-in-the-loop annotation per item. '
        'You cannot use it to ask "did the change I made five minutes ago '
        'regress anything" because the dataset shifts every time you sample.')

    add_para(doc, 'apps/tutoring/student_sim/ — synthetic traffic generation', bold=True)
    add_para(doc,
        'Drives the tutor end-to-end using LLM-backed student personas, '
        'producing sessions that look like real conversations. But the '
        'simulator only produces traffic; it does not score it. Its output '
        'feeds back into the production-sampling benchmark, which means it '
        'still needs human labels.')

    add_para(doc, 'The gap', bold=True)
    add_para(doc,
        'What we lacked was an automated regression test for the tutor: same '
        'inputs every run, automatic scoring against pre-defined expected '
        'behaviour, one command to run, one number out. That is what the eval '
        'harness provides. It complements the other two — it does not replace '
        'them — and fills the specific role of catching regressions before '
        'they reach pilot students.')

    # -----------------------------------------------------------------
    # 3. The Solution
    # -----------------------------------------------------------------
    doc.add_heading('3. The Solution: How the Harness Works', level=1)

    add_para(doc,
        'The harness is a small, self-contained Python package under the '
        'evals/ directory at the repository root. It reuses the production '
        'tutor engine, the production judge pipeline, and the existing '
        'persona-driven simulator as building blocks. What is new is the '
        'orchestration: how scenarios are defined, how the tutor is invoked '
        'against them, and how the result is scored and persisted.')

    doc.add_heading('3.1 The Dataset', level=2)
    add_para(doc,
        'Every scenario lives in its own YAML file under '
        'evals/dataset/<category>/. The file is the single source of truth: '
        'it defines the persona to use, the lesson to teach against, an '
        'optional canned conversation history, the student input that '
        'triggers the tutor response under test, and the assertions that '
        'define what a "passing" response looks like.')

    add_para(doc, 'A representative scenario YAML:', italic=True)
    add_code(doc, """\
id: math_correct_advance_001
persona: average
subject: math
lesson_id: 1137
mode: single_turn

seed_history:
  - role: tutor
    text: "Four angles around a point measure 60°, 75°, 80°, and x. Find x."

student_turn: "145 — i did 60+75+80=215 then 360-215=145"

assertions:
  must_not_contain_phrase:
    - "let me check"
    - "are you sure"
    - "walk me through"
  must_not_label: [WRONG_VERDICT, ASK_WORKING]

rubric:
  - "Confirms '145' is correct — briefly, without effusive praise"
  - "Advances the lesson rather than re-probing the working"
  - "Does NOT second-guess a correct answer"
pass_threshold: 0.7
""")

    add_para(doc,
        'Because scenarios are plain YAML committed to the repository, the '
        'dataset is reproducible by construction. Two runs against the same '
        'git SHA will exercise the exact same scenarios in the exact same '
        'order. When a regression appears, the responsible commit is '
        'identifiable from the SHA stamped on the result blob.')

    doc.add_heading('3.2 Two Execution Modes', level=2)

    add_para(doc, 'single_turn', bold=True)
    add_para(doc,
        'The runner creates a fresh TutorSession in the local database, '
        'injects the scenario\'s seed_history as past SessionTurn rows, and '
        'calls ConversationalTutor.respond(student_turn) exactly once. The '
        'tutor produces a single response, the production judge pipeline '
        'fires automatically against that response, and the result is scored. '
        'Single-turn scenarios are fast (one LLM call to the tutor, plus '
        'judges, plus the rubric) and deterministic enough to make up the '
        'bulk of the suite.')

    add_para(doc, 'multi_turn', bold=True)
    add_para(doc,
        'The runner delegates to apps.tutoring.student_sim.simulate_session, '
        'which spins up a real persona LLM and lets it have a full '
        'conversation with the tutor — opening turn, persona reply, tutor '
        'response, persona reply, and so on until the session terminates '
        '(completed, exit ticket, deadlock, or max turns). The runner then '
        'reads back every SessionTurn from the database, derives per-turn '
        'labels via apps.benchmark.autopopulate, and scores the whole '
        'trajectory. Multi-turn scenarios catch session-level failure modes '
        '(banned-opener loops, premature advance, repetition) that no '
        'single-turn check can see.')

    doc.add_heading('3.3 The Three Scoring Layers', level=2)

    add_para(doc,
        'Every scenario is scored through up to three composed layers. The '
        'overall scenario verdict is PASS only when all applicable layers '
        'pass.')

    add_table(doc,
        ['Layer', 'What it checks', 'Cost'],
        [
            ['1 — Deterministic',
             'Phrase presence/absence, paragraph count, whether the response '
             'ends in a question, max paragraph count, etc. Pure Python '
             'string operations.',
             'Free'],
            ['2 — Judge-derived labels',
             'The production judge pipeline (apps.tutoring.judges.unified) '
             'fires automatically when the tutor responds. Its output is '
             'mapped to the 30-label vocabulary via '
             'apps.benchmark.autopopulate.derive_suggested_labels. Scenarios '
             'assert which labels must/must-not appear.',
             'Same as a prod turn'],
            ['3 — LLM-as-judge rubric',
             'A small, pinned model (Claude Haiku 4.5 by default, temperature 0) '
             'scores each rubric item from 0.0 to 1.0. The weighted mean must '
             'meet the scenario\'s pass_threshold. Catches behaviours layers '
             '1+2 cannot articulate ("did the tutor adapt its strategy", '
             '"was the tone appropriate", etc.).',
             '~$0.001 per scenario'],
        ])

    add_para(doc,
        'For multi-turn scenarios the layer-3 rubric is fed the entire '
        'session transcript and asked to score each rubric item across the '
        'whole interaction rather than against one response.')

    doc.add_heading('3.4 The Frozen Lesson Fixtures', level=2)
    add_para(doc,
        'Reproducibility requires that the curriculum content the tutor '
        'teaches against stays stable across runs. To achieve this the '
        'harness ships a small extraction script (evals/fixtures/extract.py) '
        'that parses prod_content_dump.sql, picks a handful of lessons '
        '(currently 4: two math, two geography) along with all their steps '
        'and exit-ticket questions, reparents them to a dedicated synthetic '
        '"Eval Harness" institution, and emits Django fixture JSON. The '
        'fixtures are committed to the repository and loaded into the dev '
        'database via manage.py loaddata. The eval-only institution and '
        'simulator-bot user use high-range primary keys (≥ 999000) to avoid '
        'collisions with anything else a developer might have created '
        'locally.')

    doc.add_heading('3.5 The Report', level=2)
    add_para(doc,
        'Each run writes a JSON result blob to evals/runs/ named with a '
        'UTC timestamp and the git short-SHA. The companion '
        'python -m evals.report tool reads these blobs and prints a '
        'readable summary of the run: overall pass rate, per-persona '
        'pass rate, per-failure-category breakdown (derived from scenario '
        'tags), and the list of failing scenarios with the specific '
        'assertions that flipped.')

    add_para(doc,
        'When invoked with --diff, the report tool computes a comparison '
        'between two runs: pass-rate delta per persona and per tag, lists '
        'of newly passing scenarios, lists of newly failing scenarios (with '
        '⚠ markers and root-cause hints), and any scenarios that appear in '
        'one run but not the other. This is the regression-detection '
        'mechanism: the answer to "did my change break anything?" is one '
        'command away.')

    # -----------------------------------------------------------------
    # 4. The Persona × Situation Matrix
    # -----------------------------------------------------------------
    doc.add_heading('4. The Persona × Situation Matrix', level=1)
    add_para(doc,
        'The dataset is deliberately structured as a matrix: each of the '
        'five personas is exercised against each of several known failure '
        'modes. Not every cell in the matrix is meaningful (a non-responder '
        'cannot, by definition, "push back on a tutor error"), so we author '
        'the cells that map to concrete failure categories from the project '
        'eval-benchmark vocabulary (memory/eval_benchmark_v2_simplified.md).')

    add_para(doc,
        'The five personas, in their behavioural profile:')

    add_table(doc,
        ['Persona', 'Behaviour', 'Stresses the engine on…'],
        [
            ['STRUGGLER', 'Misreads, arithmetic slips, partial work, asks for help, ~30% accuracy',
             'Remediation flow, working-request handling, scaffolding'],
            ['AVERAGE', 'Gets most answers right with mixed working presentation, ~65% accuracy',
             'The steady-state path, false-accept guards'],
            ['CAPABLE', 'Right answers, pushes back on tutor errors, asks clarifications, ~85% accuracy',
             'Restraint when the student is moving fast, tutor honesty under challenge'],
            ['PROBE_RESISTANT', 'Bare answers, refuses to show working, "I just know"',
             'Working-request loop, banned-opener repetition, regen path'],
            ['NON_RESPONDER', 'Monosyllabic — "ok", "yes", "idk"',
             'Non-answer skip path, exit-ticket gating, premature-advance guard'],
        ])

    add_para(doc,
        'The current 23-scenario dataset distributes across these personas as '
        'follows:')

    add_table(doc,
        ['Persona', 'Scenarios'],
        [
            ['average', '8'],
            ['struggler', '7'],
            ['capable', '4'],
            ['non_responder', '2'],
            ['probe_resistant', '2'],
        ])

    # -----------------------------------------------------------------
    # 5. What This Achieves
    # -----------------------------------------------------------------
    doc.add_heading('5. What This Achieves', level=1)

    add_para(doc, 'Single-command regression detection.', bold=True)
    add_para(doc,
        'A developer changes a prompt, a judge, or a model. They run '
        'python manage.py run_eval, then python -m evals.report --diff, '
        'and immediately see which previously-passing scenarios now fail '
        'and which new ones pass. The before-and-after delta is a '
        'concrete artefact, not a vibe.')

    add_para(doc, 'Reproducible baseline across time.', bold=True)
    add_para(doc,
        'Because the dataset is committed to the repository, the same '
        'scenarios are exercised forever. Running today\'s harness against '
        'last week\'s git SHA produces a comparable score. This gives us '
        'an honest answer to "is the tutor improving over time" instead '
        'of relying on the impressions of whoever is reviewing the latest '
        'pilot sessions.')

    add_para(doc, 'Coverage of failure modes that production sampling misses.', bold=True)
    add_para(doc,
        'Production sampling can only catch behaviours that real students '
        'actually triggered. Some failure modes — banned-opener loops on '
        'a refusing student, premature advance through monosyllabic '
        'replies, false accept on a wrong MCQ option — are easier to '
        'manufacture deliberately than to find in the wild. The curated '
        'dataset puts these stress tests on a pre-defined schedule.')

    add_para(doc, 'Independent verdict from the judges themselves.', bold=True)
    add_para(doc,
        'The deterministic and rubric layers do not rely on the production '
        'judges for their judgement; they cross-check them. When a '
        'deterministic phrase-presence assertion catches a failure that '
        'the production judge missed, that gap is now visible. This means '
        'the harness can also evaluate the judges themselves over time, '
        'not just the tutor responses.')

    add_para(doc, 'Cheap, fast, and runnable any time.', bold=True)
    add_para(doc,
        'A full run (17 single-turn + 6 multi-turn scenarios) executes in '
        '~2-3 minutes wall-time and costs in the order of a few cents in '
        'LLM API usage. There is no setup beyond loading fixtures once. '
        'The harness can be triggered locally before a commit, in CI, or '
        'manually at any cadence.')

    # -----------------------------------------------------------------
    # 6. Phased Delivery
    # -----------------------------------------------------------------
    doc.add_heading('6. How It Was Built — Phased Delivery', level=1)
    add_para(doc,
        'The harness was built in six discrete phases, each shipping value '
        'standalone so progress could be inspected at every step:')

    add_table(doc,
        ['Phase', 'Deliverable', 'Status'],
        [
            ['1', 'Skeleton + fixture extractor + one smoke scenario', '✓ shipped'],
            ['2', 'Deterministic + judge-label assertion vocabulary; 10 single-turn scenarios', '✓ shipped'],
            ['3', 'LLM-as-judge rubric scorer (single-turn + trajectory variants)', '✓ shipped'],
            ['4', 'Multi-turn driver integration + trajectory scorer; 3 multi-turn scenarios', '✓ shipped'],
            ['5', 'report.py — overall summary + run-over-run diff', '✓ shipped'],
            ['6', '3 additional simulator personas + math content + 10 more scenarios (total: 23)', '✓ shipped'],
            ['(future)', 'Grow dataset to 60–80 scenarios; pre-merge CI integration', 'pending'],
        ])

    # -----------------------------------------------------------------
    # 7. Current State and Limitations
    # -----------------------------------------------------------------
    doc.add_heading('7. Current State and Limitations', level=1)

    add_para(doc, 'What works today:', bold=True)
    add_bullet(doc, 'All 23 scenarios load and execute end-to-end.')
    add_bullet(doc, 'All five personas are wired into the multi-turn driver.')
    add_bullet(doc, 'The three scoring layers compose correctly; per-scenario verdicts include the full breakdown.')
    add_bullet(doc, 'The report tool produces readable summaries and meaningful diffs between any two runs.')
    add_bullet(doc, 'Fixture extraction is reproducible from prod_content_dump.sql.')

    add_para(doc, 'Active constraints:', bold=True)
    add_bullet(doc,
        'API access. The harness depends on the configured LLM providers '
        '(currently Anthropic Haiku for the tutor, the judge, and the '
        'rubric). Without an active Anthropic subscription, calls fall '
        'back or fail. The provider can be swapped per the project\'s '
        'ModelConfig pattern.')
    add_bullet(doc,
        'Dataset breadth. The current 23 scenarios cover ~12 of the 19 '
        'failure categories defined in the broader benchmark vocabulary. '
        'Reaching 60–80 scenarios is incremental authoring work — the '
        'infrastructure is in place.')
    add_bullet(doc,
        'Cost estimation. Per-call token totals are tracked but not yet '
        'converted to USD. A shared cost_estimator module is planned per '
        'the simulator plan.')

    # -----------------------------------------------------------------
    # 8. Where to Look in the Code
    # -----------------------------------------------------------------
    doc.add_heading('8. Where the Code Lives', level=1)
    add_para(doc,
        'For readers who want to dig in:')

    add_table(doc,
        ['Path', 'What it contains'],
        [
            ['evals/runner.py', 'The orchestrator: scenario discovery, single-turn and multi-turn execution paths, run blob persistence.'],
            ['evals/scorers/deterministic.py', 'Layer-1 verbs: phrase / structural / label-set assertions.'],
            ['evals/scorers/trajectory.py', 'Multi-turn verbs: expected_reason, repetition window, label fan-out across the session.'],
            ['evals/scorers/llm_rubric.py', 'Layer-3 LLM-as-judge scorer for single-turn (score) and multi-turn (score_trajectory).'],
            ['evals/report.py', 'Summary + diff report tool. Pure JSON-in, text-out.'],
            ['evals/fixtures/extract.py', 'One-time SQL-to-fixture converter. Reads prod_content_dump.sql, emits Django fixtures.'],
            ['evals/dataset/', 'The scenario YAMLs themselves, organised by category (math, format, pedagogy, multi_turn, personas, crosscutting).'],
            ['apps/tutoring/management/commands/run_eval.py', 'The Django CLI entry point — invoked via manage.py run_eval.'],
            ['memory/eval_harness_plan.md', 'The original design document this implementation was built against.'],
        ])

    # -----------------------------------------------------------------
    # 9. Summary
    # -----------------------------------------------------------------
    doc.add_heading('9. Summary in One Paragraph', level=1)
    add_para(doc,
        'The AI Tutor now has an automated regression suite. It is curated, '
        'reproducible, fast, and quantitative. Every change to the engine, '
        'every prompt revision, every model swap can be evaluated against '
        'a stable benchmark in under three minutes with a single command. '
        'Where there was previously only "we think the tutor got better" '
        'there is now a number that goes up or down — and an artefact that '
        'tells you exactly which scenarios moved and why.')

    # -----------------------------------------------------------------
    # Save
    # -----------------------------------------------------------------
    doc.save(output_path)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    out = build(args.output)
    print(f"Wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
