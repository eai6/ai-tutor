"""A/B test runner — drives the AB_TESTING_PLAN matrix.

Purpose: generate transcripts the judge can mine for *recommendations to
improve the tutoring system prompt*. Models are a robustness axis, not
the unit of evaluation — see `design/AB_TESTING_PLAN.md`.

Matrix: 2 supported models (Anthropic Claude, Google Gemini) × 2 lessons
× 2 personas = 8 cells per prompt variant. OpenAI/GPT is out of scope.

Saves transcripts + metrics under ab-test-reports/.

Run with:  venv/bin/python scripts/run_ab_test.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import django
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()


# ---------------------------------------------------------------------------
# Matrix
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelSpec:
    key: str
    label: str
    provider: str
    model_name: str
    temperature: float = 0.2


_MATRIX_MODE = os.environ.get('EVAL_MATRIX_MODE', 'deploy').lower()

# Two matrix modes (2026-05-23 cost-optimised redesign):
#
# `deploy` (default; runs on every push to main via CI):
#   2 models × 2 lessons × 1 persona = **4 cells**
#   {Sonnet 4, Gemini 3 Flash} × {L1137 math, L1425 geo} × error_prone
#   Wall: ~25 min. Cost: ~$15. BEA in-scope: ~50-70 turns / run.
#   Both prod-tier models so we catch cross-model regressions on every
#   deploy. error_prone consistently produces 12-17 BEA in-scope turns
#   per cell; struggler/capable produced only 0-3 in-scope turns each
#   in prior runs and are excluded from the lean matrix.
#
# `full` (manual workflow_dispatch with eval_matrix_mode=full):
#   2 models × 4 lessons × 3 personas = **24 cells**
#   Adds Gemini for robustness + struggler/capable for persona variance
#   + 2 more lessons for content coverage.
#   Wall: ~100 min. Cost: ~$60. Use for ad-hoc comprehensive sweeps.

if _MATRIX_MODE == 'full':
    MODELS = [
        ModelSpec('sonnet-4', 'Claude Sonnet 4', 'anthropic', 'claude-sonnet-4-20250514', 0.2),
        ModelSpec('gemini-3-flash', 'Gemini 3 Flash', 'google', 'gemini-3-flash-preview', 0.2),
    ]
    LESSONS = [
        (1137, 'Math — Angles around a point'),
        (1138, 'Math — Angles on a straight line'),
        (1425, 'Geography — Map Scale and Map Types'),
        (540,  'Geography — Understanding Maps'),
    ]
    PERSONAS = ['struggler', 'capable', 'error_prone']
else:  # deploy (lean)
    MODELS = [
        ModelSpec('sonnet-4', 'Claude Sonnet 4', 'anthropic', 'claude-sonnet-4-20250514', 0.2),
        ModelSpec('gemini-3-flash', 'Gemini 3 Flash', 'google', 'gemini-3-flash-preview', 0.2),
    ]
    LESSONS = [
        (1137, 'Math — Angles around a point'),
        (1425, 'Geography — Map Scale and Map Types'),
    ]
    PERSONAS = ['error_prone']

OUT_DIR = Path(os.environ.get('AB_REPORT_DIR', 'ab-test-reports'))
RESULTS_JSONL = OUT_DIR / 'cell_results.jsonl'
TRANSCRIPTS_DIR = OUT_DIR / 'raw_transcripts'


@dataclass
class CellResult:
    model_key: str
    model_label: str
    provider: str
    model_name: str
    lesson_id: int
    lesson_label: str
    persona: str
    session_id: Optional[int] = None
    reason: str = ''
    turns: int = 0
    tutor_turns: int = 0
    tutor_turns_with_tool: int = 0
    tool_use_rate: float = 0.0
    validator_issue_counts: dict = field(default_factory=dict)
    regen_triggered_turns: int = 0
    regen_clean_first_cycle: int = 0
    regen_clean_any_cycle: int = 0
    regen_cycles_exhausted: int = 0
    answer_leak_incidents: int = 0
    repeated_question_incidents: int = 0
    no_question_incidents: int = 0
    wall_seconds: float = 0.0
    student_tokens_in: int = 0
    student_tokens_out: int = 0
    transcript_chars: int = 0
    transcript_path: str = ''
    error: str = ''


def _existing_keys() -> set:
    if not RESULTS_JSONL.exists():
        return set()
    keys = set()
    with RESULTS_JSONL.open() as f:
        for line in f:
            try:
                r = json.loads(line)
                keys.add(f"{r['model_key']}|{r['lesson_id']}|{r['persona']}")
            except Exception:
                pass
    return keys


def _save_transcript(session_id: int, spec: ModelSpec, lesson_id: int, persona: str) -> tuple[str, int]:
    from apps.tutoring.models import TutorSession
    sess = TutorSession.objects.get(id=session_id)
    lines = [
        f"# Transcript — model={spec.label}  lesson={lesson_id}  persona={persona}",
        f"session_id={session_id}  status={sess.status}",
        "",
    ]
    for t in sess.turns.order_by('id'):
        md = t.metadata or {}
        flags = md.get('validator_issues') or []
        flag_str = f"  [flags: {','.join(flags)}]" if flags else ''
        lines.append(f"--- {t.role.upper()} (id={t.id}, tools={md.get('tool_use_count', 0)}){flag_str}")
        lines.append(t.content)
        lines.append('')
    content = '\n'.join(lines)
    out = TRANSCRIPTS_DIR / f"{spec.key}_L{lesson_id}_{persona}.md"
    out.write_text(content)
    return str(out), len(content)


def run_cell(spec: ModelSpec, lesson_id: int, lesson_label: str, persona: str, max_turns: int = 20) -> CellResult:
    from apps.tutoring.student_sim.driver import simulate_session
    from apps.tutoring.models import TutorSession

    result = CellResult(
        model_key=spec.key, model_label=spec.label,
        provider=spec.provider, model_name=spec.model_name,
        lesson_id=lesson_id, lesson_label=lesson_label, persona=persona,
    )
    t0 = time.monotonic()
    try:
        sim = simulate_session(lesson_id=lesson_id, persona=persona, max_turns=max_turns)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        result.error = f"{type(exc).__name__}: {exc}"
        result.wall_seconds = time.monotonic() - t0
        return result

    result.wall_seconds = time.monotonic() - t0
    result.session_id = sim.session_id
    result.reason = sim.reason
    result.turns = sim.turns
    result.student_tokens_in = sim.student_tokens_in
    result.student_tokens_out = sim.student_tokens_out
    if sim.reason == 'error':
        result.error = sim.error
        return result

    sess = TutorSession.objects.get(id=sim.session_id)
    flag_counter: Counter = Counter()
    for t in sess.turns.filter(role='tutor'):
        md = t.metadata or {}
        result.tutor_turns += 1
        if (md.get('tool_use_count') or 0) > 0:
            result.tutor_turns_with_tool += 1
        for issue in (md.get('validator_issues') or []):
            flag_counter[issue] += 1
        audit = md.get('regen_audit') or {}
        if audit:
            result.regen_triggered_turns += 1
            cycles = audit.get('cycles') or []
            if cycles:
                if cycles[0].get('clean'):
                    result.regen_clean_first_cycle += 1
                if audit.get('clean'):
                    result.regen_clean_any_cycle += 1
                else:
                    result.regen_cycles_exhausted += 1
    if result.tutor_turns:
        result.tool_use_rate = result.tutor_turns_with_tool / result.tutor_turns
    result.validator_issue_counts = dict(flag_counter)
    result.answer_leak_incidents = flag_counter.get('answer_leak', 0)
    result.repeated_question_incidents = flag_counter.get('repeated_question', 0)
    result.no_question_incidents = flag_counter.get('no_question', 0)

    result.transcript_path, result.transcript_chars = _save_transcript(
        sim.session_id, spec, lesson_id, persona,
    )
    return result


def main():
    from apps.llm.models import ModelConfig

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    cells = [(m, l, p) for m in MODELS for l in LESSONS for p in PERSONAS]
    existing = _existing_keys()
    print(f"Matrix: {len(MODELS)} models × {len(LESSONS)} lessons × {len(PERSONAS)} personas = {len(cells)} cells")
    if existing:
        print(f"Already done: {len(existing)} — will skip")

    original_get_for = ModelConfig.get_for
    holder: dict = {'spec': None}

    @classmethod
    def patched(cls, purpose: str):
        spec = holder['spec']
        if spec is None or purpose != cls.Purpose.TUTORING.value:
            return original_get_for(purpose)
        cfg = ModelConfig.resolve_runtime(spec.provider, spec.model_name)
        if cfg is not None:
            cfg.temperature = spec.temperature
            cfg.max_tokens = 1024
        return cfg

    ModelConfig.get_for = patched
    completed = failed = 0
    try:
        for i, (spec, (lesson_id, lesson_label), persona) in enumerate(cells, 1):
            key = f"{spec.key}|{lesson_id}|{persona}"
            if key in existing:
                continue
            print(f"\n[{i}/{len(cells)}] {spec.key} × L{lesson_id} × {persona}")
            holder['spec'] = spec
            result = run_cell(spec, lesson_id, lesson_label, persona)
            holder['spec'] = None

            with RESULTS_JSONL.open('a') as f:
                f.write(json.dumps(asdict(result)) + '\n')

            if result.error:
                failed += 1
                print(f"  ↳ ERROR: {result.error[:200]}")
            else:
                completed += 1
                print(f"  ↳ ok: turns={result.turns} tool-use={result.tool_use_rate:.0%} "
                      f"leak={result.answer_leak_incidents} "
                      f"reason={result.reason} {result.wall_seconds:.1f}s")
    finally:
        ModelConfig.get_for = original_get_for

    print(f"\nDone: {completed} ok, {failed} failed")


if __name__ == '__main__':
    main()
