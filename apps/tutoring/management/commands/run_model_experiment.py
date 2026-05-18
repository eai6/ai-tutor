"""Run the cross-provider tutor quality experiment for the DeepMind meeting.

Drives `apps.tutoring.student_sim.driver.simulate_session` once per
(model × lesson × persona) cell, switching the active TUTORING
ModelConfig in between via an in-memory monkey-patch on
`ModelConfig.get_for` — never touches the saved row, so the prod
default stays untouched whether the script crashes or completes.

Captures per-cell metrics from the resulting TutorSession + writes a
markdown report (with verbatim transcripts) to
memory/deepmind_model_experiment_results.md.

Usage:
    python manage.py run_model_experiment
    python manage.py run_model_experiment --models opus,sonnet --personas struggler
    python manage.py run_model_experiment --lessons 540
    python manage.py run_model_experiment --max-turns 30  # bound each session

The script is RESTARTABLE — it skips (model, lesson, persona) cells
whose result is already on disk so a partial run can be resumed.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Experiment matrix
# ---------------------------------------------------------------------------
# Keep this list curated and stable — the markdown report indexes off
# these keys. Adding a model is fine; renaming an existing one breaks
# resumption.
@dataclass(frozen=True)
class ModelSpec:
    key: str             # short slug for CLI + report headers
    label: str           # human-readable name
    provider: str        # ModelConfig.provider
    model_name: str      # ModelConfig.model_name
    tier: str            # 'best' / 'mid' / 'small'
    temperature: float = 0.0


MODELS: list[ModelSpec] = [
    # Anthropic — Opus baseline already established; re-run Sonnet +
    # add Haiku for the small-tier datapoint.
    ModelSpec('opus', 'Claude Opus 4.7', 'anthropic',
              'claude-opus-4-7', 'best'),
    ModelSpec('sonnet', 'Claude Sonnet 4', 'anthropic',
              'claude-sonnet-4-20250514', 'mid'),
    ModelSpec('haiku', 'Claude Haiku 4.5', 'anthropic',
              'claude-haiku-4-5-20251001', 'small'),
    # Google — Gemini 3 Pro/Flash + 2.5 Pro for older-generation context.
    # Local DB shows 'gemini-3.1-pro-preview' as the real ID; using the
    # naming the seeded rows have.
    ModelSpec('gemini-3-pro', 'Gemini 3.1 Pro', 'google',
              'gemini-3.1-pro-preview', 'best'),
    ModelSpec('gemini-3-flash', 'Gemini 3.1 Flash', 'google',
              'gemini-3.1-flash-preview', 'mid'),
    ModelSpec('gemini-25-flash', 'Gemini 2.5 Flash', 'google',
              'gemini-2.5-flash', 'small'),
    # OpenAI — GPT-5 if reachable; otherwise GPT-4o as best-tier.
    ModelSpec('gpt-5', 'GPT-5', 'openai',
              'gpt-5', 'best'),
    ModelSpec('gpt-4o', 'GPT-4o', 'openai',
              'gpt-4o', 'mid'),
    ModelSpec('gpt-4o-mini', 'GPT-4o mini', 'openai',
              'gpt-4o-mini', 'small'),
]


# (lesson_id, subject_label)
LESSONS: list[tuple[int, str]] = [
    (540, 'Geography — Understanding Maps'),
    (638, 'Math — Angles around a point'),
]


PERSONAS: list[str] = ['struggler', 'capable']


# ---------------------------------------------------------------------------
# Per-cell result
# ---------------------------------------------------------------------------
@dataclass
class CellResult:
    model_key: str
    model_label: str
    provider: str
    model_name: str
    lesson_id: int
    lesson_label: str
    persona: str

    # Session outcome
    session_id: Optional[int] = None
    reason: str = ''             # 'completed' / 'exit_ticket' / 'deadlock' / 'max_turns' / 'error'
    turns: int = 0

    # Tool-use compliance
    tutor_turns: int = 0
    tutor_turns_with_tool: int = 0
    tool_use_rate: float = 0.0

    # Validator + regen quality
    validator_issue_counts: dict = field(default_factory=dict)
    regen_triggered_turns: int = 0
    regen_clean_first_cycle: int = 0
    regen_clean_any_cycle: int = 0
    regen_cycles_exhausted: int = 0   # shipped dirty
    answer_leak_incidents: int = 0
    repeated_question_incidents: int = 0
    no_question_incidents: int = 0

    # Wall + cost
    wall_seconds: float = 0.0
    student_tokens_in: int = 0
    student_tokens_out: int = 0

    # Final outcome (best signal we have without doing a separate exit-
    # ticket grading pass — terminate-reason + total turns suffice for
    # this slide deck)
    error: str = ''


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------
REPORT_PATH = Path('memory/deepmind_model_experiment_results.md')
RESULTS_JSONL = Path('memory/.deepmind_model_experiment_results.jsonl')


class Command(BaseCommand):
    help = "Run cross-provider tutor quality experiment for the DeepMind meeting."

    def add_arguments(self, parser):
        parser.add_argument('--models', default='',
                            help='Comma-separated ModelSpec.key (default: all).')
        parser.add_argument('--lessons', default='',
                            help='Comma-separated lesson IDs (default: all).')
        parser.add_argument('--personas', default='',
                            help='Comma-separated persona keys (default: all).')
        parser.add_argument('--max-turns', type=int, default=40,
                            help='Hard cap on tutor-student exchanges per session.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Print the matrix without running anything.')
        parser.add_argument('--skip-existing', action='store_true', default=True,
                            help='Skip cells already present in the JSONL log.')
        parser.add_argument('--force', action='store_true',
                            help='Re-run cells already in the JSONL log (overrides --skip-existing).')

    def handle(self, *args, **opts):
        models = self._filter(MODELS, opts['models'], lambda m: m.key)
        lessons = self._filter(LESSONS, opts['lessons'], lambda l: str(l[0]))
        personas = (
            [p.strip() for p in opts['personas'].split(',') if p.strip()]
            if opts['personas'] else PERSONAS
        )

        cells = [
            (m, l, p) for m in models for l in lessons for p in personas
        ]
        self.stdout.write(self.style.SUCCESS(
            f"Experiment matrix: {len(models)} models × {len(lessons)} lessons "
            f"× {len(personas)} personas = {len(cells)} cells."
        ))
        for m in models:
            self.stdout.write(f"  model: {m.key:18}  → {m.provider}/{m.model_name}")
        for l in lessons:
            self.stdout.write(f"  lesson: {l[0]}  → {l[1]}")
        self.stdout.write(f"  personas: {personas}")
        self.stdout.write('')

        if opts['dry_run']:
            return

        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing_keys = self._existing_keys() if not opts['force'] else set()
        if existing_keys:
            self.stdout.write(self.style.WARNING(
                f"Skipping {len(existing_keys)} already-completed cell(s) "
                f"(use --force to re-run)."
            ))

        # Patch ModelConfig.get_for so the tutor picks up the override
        # without writing to the DB. The patch survives the duration of
        # this command and is reverted in `finally`.
        from apps.llm.models import ModelConfig
        original_get_for = ModelConfig.get_for
        # Cell-scoped per-thread override — set to the current ModelSpec
        # while a session runs. None = use real chain.
        override_holder: dict = {'spec': None}

        @classmethod
        def patched_get_for(cls, purpose: str):
            spec: Optional[ModelSpec] = override_holder['spec']
            if spec is None or purpose != cls.Purpose.TUTORING.value:
                return original_get_for(purpose)
            # Build the runtime ModelConfig for the override.
            cfg = ModelConfig.resolve_runtime(spec.provider, spec.model_name)
            if cfg is not None:
                cfg.temperature = spec.temperature
                cfg.max_tokens = 1024
            return cfg

        ModelConfig.get_for = patched_get_for

        completed = 0
        failed = 0
        try:
            for spec, (lesson_id, lesson_label), persona in cells:
                key = f"{spec.key}|{lesson_id}|{persona}"
                if key in existing_keys:
                    continue
                self.stdout.write(self.style.HTTP_INFO(
                    f"\n[{completed + failed + 1}/{len(cells) - len(existing_keys)}] "
                    f"{spec.key} × L{lesson_id} × {persona}"
                ))
                override_holder['spec'] = spec
                result = self._run_one(spec, lesson_id, lesson_label, persona,
                                       max_turns=opts['max_turns'])
                self._append_result(result)
                if result.error:
                    failed += 1
                    self.stdout.write(self.style.ERROR(
                        f"  ↳ {spec.key}/L{lesson_id}/{persona}: error → {result.error[:100]}"
                    ))
                else:
                    completed += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"  ↳ ok: {result.turns} turns, "
                        f"tool-use {result.tool_use_rate:.0%}, "
                        f"regen clean cycle-1 {result.regen_clean_first_cycle}/{result.regen_triggered_turns}, "
                        f"reason={result.reason}, {result.wall_seconds:.1f}s"
                    ))
                override_holder['spec'] = None
        finally:
            ModelConfig.get_for = original_get_for

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f"Experiment done: {completed} cells ok, {failed} failed."
        ))
        self._write_report()

    # ----- helpers -----
    @staticmethod
    def _filter(items, csv: str, keyfn):
        if not csv.strip():
            return list(items)
        keep = {k.strip() for k in csv.split(',') if k.strip()}
        return [it for it in items if keyfn(it) in keep]

    @staticmethod
    def _existing_keys() -> set:
        if not RESULTS_JSONL.exists():
            return set()
        seen = set()
        with RESULTS_JSONL.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                    seen.add(f"{r['model_key']}|{r['lesson_id']}|{r['persona']}")
                except Exception:
                    pass
        return seen

    @staticmethod
    def _append_result(result: CellResult):
        with RESULTS_JSONL.open('a') as f:
            f.write(json.dumps(asdict(result)) + '\n')

    def _run_one(
        self, spec: ModelSpec, lesson_id: int, lesson_label: str,
        persona: str, *, max_turns: int,
    ) -> CellResult:
        from apps.tutoring.student_sim.driver import simulate_session
        from apps.tutoring.models import TutorSession

        result = CellResult(
            model_key=spec.key, model_label=spec.label,
            provider=spec.provider, model_name=spec.model_name,
            lesson_id=lesson_id, lesson_label=lesson_label,
            persona=persona,
        )
        t0 = time.monotonic()
        try:
            sim = simulate_session(
                lesson_id=lesson_id,
                persona=persona,
                max_turns=max_turns,
            )
        except Exception as exc:
            logger.exception("run_one crashed")
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

        # Pull tutor-side metrics from the persisted SessionTurns.
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
        return result

    def _write_report(self):
        """Compile the JSONL log into a markdown comparison report."""
        if not RESULTS_JSONL.exists():
            return
        results: list[dict] = []
        with RESULTS_JSONL.open() as f:
            for line in f:
                try:
                    results.append(json.loads(line))
                except Exception:
                    pass
        if not results:
            return

        # Sort: provider order from MODELS, then lesson, then persona.
        model_order = {m.key: i for i, m in enumerate(MODELS)}
        persona_order = {p: i for i, p in enumerate(PERSONAS)}
        results.sort(key=lambda r: (
            model_order.get(r['model_key'], 999),
            r['lesson_id'],
            persona_order.get(r['persona'], 999),
        ))

        lines: list[str] = []
        lines.append(f"# Cross-provider tutor experiment — DeepMind meeting prep")
        lines.append('')
        lines.append(f"Generated by `apps/tutoring/management/commands/run_model_experiment.py`. "
                     f"Each row is ONE simulated session — synthetic student persona "
                     f"(see `apps/tutoring/student_sim/personas.py`) drives the same "
                     f"`ConversationalTutor.respond()` path real students hit. "
                     f"The active TUTORING `ModelConfig` is swapped per cell via an "
                     f"in-memory monkey-patch — no DB writes, prod default untouched.")
        lines.append('')

        # Summary table — one row per cell
        lines.append('## Summary')
        lines.append('')
        lines.append('| model | lesson | persona | turns | reason | tool-use | regen-triggered | clean cycle-1 | shipped dirty | leak | repeat-Q | no-Q | wall (s) |')
        lines.append('|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|')
        for r in results:
            if r.get('error'):
                lines.append(
                    f"| {r['model_label']} | L{r['lesson_id']} | {r['persona']} "
                    f"| {r['turns']} | **ERROR** | — | — | — | — | — | — | — | {r['wall_seconds']:.1f} |"
                )
                continue
            lines.append(
                f"| {r['model_label']} | L{r['lesson_id']} | {r['persona']} "
                f"| {r['turns']} | {r['reason']} "
                f"| {r['tool_use_rate']:.0%} ({r['tutor_turns_with_tool']}/{r['tutor_turns']}) "
                f"| {r['regen_triggered_turns']} "
                f"| {r['regen_clean_first_cycle']} "
                f"| {r['regen_cycles_exhausted']} "
                f"| {r['answer_leak_incidents']} "
                f"| {r['repeated_question_incidents']} "
                f"| {r['no_question_incidents']} "
                f"| {r['wall_seconds']:.1f} |"
            )
        lines.append('')

        # Per-cell details
        lines.append('## Per-cell details')
        lines.append('')
        for r in results:
            lines.append(f"### {r['model_label']} × L{r['lesson_id']} × {r['persona']}")
            lines.append('')
            lines.append(f"- session_id: {r.get('session_id') or '—'}")
            lines.append(f"- provider/model: `{r['provider']}/{r['model_name']}`")
            lines.append(f"- reason: `{r['reason']}` after {r['turns']} student turns "
                         f"(wall {r['wall_seconds']:.1f}s)")
            if r.get('error'):
                lines.append(f"- ⚠️ error: `{r['error']}`")
                lines.append('')
                continue
            lines.append(f"- tool-use rate: **{r['tool_use_rate']:.0%}** "
                         f"({r['tutor_turns_with_tool']}/{r['tutor_turns']} tutor turns)")
            lines.append(f"- regen: triggered on {r['regen_triggered_turns']} turn(s); "
                         f"cycle-1 clean on {r['regen_clean_first_cycle']}; "
                         f"any-cycle clean on {r['regen_clean_any_cycle']}; "
                         f"cycles exhausted (shipped dirty) on {r['regen_cycles_exhausted']}")
            lines.append(f"- leak incidents: {r['answer_leak_incidents']}")
            lines.append(f"- repeated_question incidents: {r['repeated_question_incidents']}")
            lines.append(f"- no_question incidents: {r['no_question_incidents']}")
            if r['validator_issue_counts']:
                top_flags = sorted(
                    r['validator_issue_counts'].items(),
                    key=lambda kv: -kv[1],
                )[:8]
                joined = ', '.join(f"`{k}`={v}" for k, v in top_flags)
                lines.append(f"- validator-issue distribution: {joined}")
            lines.append(f"- student tokens: in {r['student_tokens_in']} / out {r['student_tokens_out']}")
            lines.append('')

        REPORT_PATH.write_text('\n'.join(lines))
        self.stdout.write(self.style.SUCCESS(
            f"Wrote report → {REPORT_PATH}"
        ))
