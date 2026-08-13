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
    # 2026-05-19: was 'gemini-3.1-flash-preview' (404s — doesn't exist).
    # The real Gemini 3 Flash preview lives at this id.
    ModelSpec('gemini-3-flash', 'Gemini 3 Flash Preview', 'google',
              'gemini-3-flash-preview', 'mid'),
    ModelSpec('gemini-25-flash', 'Gemini 2.5 Flash', 'google',
              'gemini-2.5-flash', 'small'),
    # Added 2026-05-19 — focused Gemini Flash re-evaluation after the
    # original 'gemini-3.1-flash-preview' returned 404 on the matrix
    # run. These 4 ids are all confirmed available + tool-calling on
    # smoke tests; pick winner via this experiment.
    ModelSpec('gemini-35-flash', 'Gemini 3.5 Flash', 'google',
              'gemini-3.5-flash', 'mid'),
    ModelSpec('gemini-31-flash-lite', 'Gemini 3.1 Flash Lite', 'google',
              'gemini-3.1-flash-lite', 'small'),
    ModelSpec('gemini-31-flash-lite-preview', 'Gemini 3.1 Flash Lite Preview',
              'google', 'gemini-3.1-flash-lite-preview', 'small'),
    # Custom-tools-tuned Pro variant — hypothesis is this fixes the
    # 3-pro answer-leak regression (3 leaks vs 0 for Opus/Sonnet on
    # the same matrix).
    ModelSpec('gemini-31-pro-customtools', 'Gemini 3.1 Pro Custom Tools',
              'google', 'gemini-3.1-pro-preview-customtools', 'best'),
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

    # Regen-sweep mode only: which regen model this cell used (the
    # tutor model is held fixed in that mode). Empty in the standard
    # tutor-model matrix.
    regen_model_key: str = ''
    regen_model_label: str = ''
    regen_model_name: str = ''

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

# Regen-sweep mode writes here instead — kept separate so it never
# collides with the standard tutor-model matrix above.
REGEN_SWEEP_REPORT_PATH = Path('memory/deepmind_regen_sweep_results.md')
REGEN_SWEEP_JSONL = Path('memory/.deepmind_regen_sweep_results.jsonl')


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
        parser.add_argument('--regen-model', default='',
                            help='REGEN-SWEEP MODE: comma-separated ModelSpec.key(s) '
                                 'to use as the regen ensemble model. When set, the '
                                 'tutor model is held fixed (--models, single key; '
                                 'default gemini-31-flash-lite-preview) and the regen '
                                 'model becomes the varied axis. Isolates regen-model '
                                 'quality. Writes a separate report '
                                 '(deepmind_regen_sweep_results.md).')

    def handle(self, *args, **opts):
        if opts.get('regen_model'):
            return self._handle_regen_sweep(opts)
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
        from ai_tutor.apps.llm.models import ModelConfig
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
    def _existing_keys(path: Path = RESULTS_JSONL, *, regen: bool = False) -> set:
        if not path.exists():
            return set()
        seen = set()
        with path.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                    axis = r['regen_model_key'] if regen else r['model_key']
                    seen.add(f"{axis}|{r['lesson_id']}|{r['persona']}")
                except Exception:
                    pass
        return seen

    @staticmethod
    def _append_result(result: CellResult, path: Path = RESULTS_JSONL):
        with path.open('a') as f:
            f.write(json.dumps(asdict(result)) + '\n')

    def _run_one(
        self, spec: ModelSpec, lesson_id: int, lesson_label: str,
        persona: str, *, max_turns: int, regen_spec: Optional[ModelSpec] = None,
    ) -> CellResult:
        from ai_tutor.apps.tutoring.student_sim.driver import simulate_session
        from ai_tutor.apps.tutoring.models import TutorSession

        result = CellResult(
            model_key=spec.key, model_label=spec.label,
            provider=spec.provider, model_name=spec.model_name,
            lesson_id=lesson_id, lesson_label=lesson_label,
            persona=persona,
        )
        if regen_spec is not None:
            result.regen_model_key = regen_spec.key
            result.regen_model_label = regen_spec.label
            result.regen_model_name = regen_spec.model_name
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

    # ------------------------------------------------------------------
    # Regen-sweep mode
    # ------------------------------------------------------------------
    def _handle_regen_sweep(self, opts):
        """Sweep the regen ensemble model with the tutor held fixed.

        The standard matrix varies the TUTORING ModelConfig and reads
        regen-clean rates as a side metric — but the regen model is a
        constant there, so those numbers say nothing about regen-model
        quality. This mode inverts it: tutor fixed, regen varied.

        Two patch points are needed. The tutor model resolves via
        `ModelConfig.get_for('tutoring')`, but the regen ensemble
        resolves via the `ConversationalTutor.regen_clients` property
        (a direct `ModelConfig.objects.filter(purpose='regen')` query),
        so `get_for` alone does not reach it.
        """
        regen_specs = self._filter(MODELS, opts['regen_model'], lambda m: m.key)
        if not regen_specs:
            self.stderr.write(self.style.ERROR(
                f"--regen-model matched no known ModelSpec keys. "
                f"Known: {', '.join(m.key for m in MODELS)}"
            ))
            return

        # Tutor model held fixed. Default to the production tutor.
        tutor_candidates = self._filter(MODELS, opts['models'], lambda m: m.key)
        if opts['models'] and len(tutor_candidates) != 1:
            self.stderr.write(self.style.ERROR(
                "In regen-sweep mode --models must resolve to exactly one "
                "key (it is the fixed tutor model)."
            ))
            return
        if opts['models'] and tutor_candidates:
            tutor_spec = tutor_candidates[0]
        else:
            tutor_spec = next(
                (m for m in MODELS if m.key == 'gemini-31-flash-lite-preview'),
                MODELS[0],
            )

        lessons = self._filter(LESSONS, opts['lessons'], lambda l: str(l[0]))
        personas = (
            [p.strip() for p in opts['personas'].split(',') if p.strip()]
            if opts['personas'] else PERSONAS
        )
        cells = [
            (rs, l, p) for rs in regen_specs for l in lessons for p in personas
        ]
        self.stdout.write(self.style.SUCCESS(
            f"REGEN SWEEP — tutor fixed at {tutor_spec.key} "
            f"({tutor_spec.provider}/{tutor_spec.model_name}); "
            f"{len(regen_specs)} regen model(s) × {len(lessons)} lessons "
            f"× {len(personas)} personas = {len(cells)} cells."
        ))
        for rs in regen_specs:
            self.stdout.write(f"  regen: {rs.key:24} → {rs.provider}/{rs.model_name}")
        self.stdout.write('')

        if opts['dry_run']:
            return

        REGEN_SWEEP_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = (
            self._existing_keys(REGEN_SWEEP_JSONL, regen=True)
            if not opts['force'] else set()
        )
        if existing:
            self.stdout.write(self.style.WARNING(
                f"Skipping {len(existing)} already-completed cell(s) "
                f"(use --force to re-run)."
            ))

        from django.conf import settings as dj_settings
        from ai_tutor.apps.llm.models import ModelConfig
        from ai_tutor.apps.tutoring.conversational_tutor import ConversationalTutor

        # --- Patch 0: force the ensemble regen path ---
        # When TUTOR_SELF_RETRY_ENABLED is on (the dev default, since
        # DEBUG=True), regen re-invokes the *tutor's own* llm_client and
        # never touches `regen_clients` — so a regen-model swap would be
        # a no-op. Production runs with it OFF (DEBUG=False), using the
        # `run_regen_ensemble` path that DOES resolve `regen_clients`.
        # Force it off here so the sweep measures the production path.
        original_self_retry = getattr(
            dj_settings, 'TUTOR_SELF_RETRY_ENABLED', False)
        dj_settings.TUTOR_SELF_RETRY_ENABLED = False

        # --- Patch 1: hold the tutor model fixed via get_for ---
        original_get_for = ModelConfig.get_for

        @classmethod
        def patched_get_for(cls, purpose: str):
            if purpose != cls.Purpose.TUTORING.value:
                return original_get_for(purpose)
            cfg = ModelConfig.resolve_runtime(
                tutor_spec.provider, tutor_spec.model_name)
            if cfg is not None:
                cfg.temperature = tutor_spec.temperature
                cfg.max_tokens = 1024
            return cfg

        # --- Patch 2: swap the regen ensemble model ---
        # `regen_clients` is a @property; replace it with one that
        # returns a single-model ensemble built from the override spec.
        regen_holder: dict = {'spec': None}
        original_regen_prop = ConversationalTutor.regen_clients

        def patched_regen_clients(inst):
            spec: Optional[ModelSpec] = regen_holder['spec']
            if spec is None:
                return original_regen_prop.fget(inst)
            cached = getattr(inst, '_regen_sweep_clients', None)
            if cached is not None:
                return cached
            from ai_tutor.apps.llm.client import get_llm_client
            cfg = ModelConfig.resolve_runtime(spec.provider, spec.model_name)
            if cfg is None:
                raise RuntimeError(
                    f"resolve_runtime returned None for regen model "
                    f"{spec.provider}/{spec.model_name} — missing API key "
                    f"env var?"
                )
            cfg.temperature = 0.2
            cfg.max_tokens = 900
            inst._regen_sweep_clients = [get_llm_client(cfg)]
            return inst._regen_sweep_clients

        ModelConfig.get_for = patched_get_for
        ConversationalTutor.regen_clients = property(patched_regen_clients)

        completed = failed = 0
        try:
            for regen_spec, (lesson_id, lesson_label), persona in cells:
                key = f"{regen_spec.key}|{lesson_id}|{persona}"
                if key in existing:
                    continue
                self.stdout.write(self.style.HTTP_INFO(
                    f"\n[{completed + failed + 1}/{len(cells) - len(existing)}] "
                    f"regen={regen_spec.key} × L{lesson_id} × {persona} "
                    f"(tutor={tutor_spec.key})"
                ))
                regen_holder['spec'] = regen_spec
                result = self._run_one(
                    tutor_spec, lesson_id, lesson_label, persona,
                    max_turns=opts['max_turns'], regen_spec=regen_spec,
                )
                self._append_result(result, REGEN_SWEEP_JSONL)
                if result.error:
                    failed += 1
                    self.stdout.write(self.style.ERROR(
                        f"  ↳ error → {result.error[:100]}"
                    ))
                else:
                    completed += 1
                    self.stdout.write(self.style.SUCCESS(
                        f"  ↳ ok: {result.turns} turns, regen clean cycle-1 "
                        f"{result.regen_clean_first_cycle}/{result.regen_triggered_turns}, "
                        f"any-cycle {result.regen_clean_any_cycle}, "
                        f"shipped-dirty {result.regen_cycles_exhausted}, "
                        f"{result.wall_seconds:.1f}s"
                    ))
                regen_holder['spec'] = None
        finally:
            ModelConfig.get_for = original_get_for
            ConversationalTutor.regen_clients = original_regen_prop
            dj_settings.TUTOR_SELF_RETRY_ENABLED = original_self_retry

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f"Regen sweep done: {completed} cells ok, {failed} failed."
        ))
        self._write_regen_report()

    def _write_regen_report(self):
        """Compile the regen-sweep JSONL into a markdown report."""
        if not REGEN_SWEEP_JSONL.exists():
            return
        results: list[dict] = []
        with REGEN_SWEEP_JSONL.open() as f:
            for line in f:
                try:
                    results.append(json.loads(line))
                except Exception:
                    pass
        if not results:
            return

        model_order = {m.key: i for i, m in enumerate(MODELS)}
        persona_order = {p: i for i, p in enumerate(PERSONAS)}
        results.sort(key=lambda r: (
            model_order.get(r.get('regen_model_key', ''), 999),
            r['lesson_id'],
            persona_order.get(r['persona'], 999),
        ))

        lines: list[str] = []
        lines.append('# Regen-model sweep — tutor held fixed')
        lines.append('')
        lines.append('Generated by `run_model_experiment --regen-model ...`. '
                     'The tutor `ModelConfig` is held fixed; the **regen '
                     'ensemble** model is the varied axis (single-model regen '
                     'per cell). This isolates regen-model quality — the '
                     'standard matrix in `deepmind_model_experiment_results.md` '
                     'instead varies the tutor and holds regen constant.')
        lines.append('')
        tutors = sorted({f"`{r['provider']}/{r['model_name']}`" for r in results})
        lines.append(f"Fixed tutor model: {', '.join(tutors)}")
        lines.append('')

        # Aggregate per regen model — the headline.
        agg: dict = {}
        for r in results:
            if r.get('error'):
                continue
            k = r.get('regen_model_label') or r.get('regen_model_key') or '—'
            a = agg.setdefault(k, {'cells': 0, 'trig': 0, 'c1': 0,
                                   'any': 0, 'dirty': 0, 'wall': 0.0})
            a['cells'] += 1
            a['trig'] += r['regen_triggered_turns']
            a['c1'] += r['regen_clean_first_cycle']
            a['any'] += r['regen_clean_any_cycle']
            a['dirty'] += r['regen_cycles_exhausted']
            a['wall'] += r['wall_seconds']
        lines.append('## Aggregate per regen model')
        lines.append('')
        lines.append('| regen model | cells | regen-triggered | cycle-1 clean '
                     '| any-cycle clean | shipped dirty | any-cycle rate '
                     '| mean wall (s) |')
        lines.append('|---|---:|---:|---:|---:|---:|---:|---:|')
        for k, a in agg.items():
            rate = (a['any'] / a['trig']) if a['trig'] else 0.0
            c1rate = (a['c1'] / a['trig']) if a['trig'] else 0.0
            mwall = (a['wall'] / a['cells']) if a['cells'] else 0.0
            lines.append(
                f"| {k} | {a['cells']} | {a['trig']} "
                f"| {a['c1']} ({c1rate:.0%}) | {a['any']} ({rate:.0%}) "
                f"| {a['dirty']} | {rate:.0%} | {mwall:.1f} |"
            )
        lines.append('')

        # Per-cell summary table.
        lines.append('## Per-cell summary')
        lines.append('')
        lines.append('| regen model | lesson | persona | turns | reason '
                     '| regen-triggered | clean cycle-1 | any-cycle clean '
                     '| shipped dirty | leak | wall (s) |')
        lines.append('|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|')
        for r in results:
            rl = r.get('regen_model_label') or r.get('regen_model_key') or '—'
            if r.get('error'):
                lines.append(
                    f"| {rl} | L{r['lesson_id']} | {r['persona']} "
                    f"| {r['turns']} | **ERROR** | — | — | — | — | — "
                    f"| {r['wall_seconds']:.1f} |"
                )
                continue
            lines.append(
                f"| {rl} | L{r['lesson_id']} | {r['persona']} "
                f"| {r['turns']} | {r['reason']} "
                f"| {r['regen_triggered_turns']} "
                f"| {r['regen_clean_first_cycle']} "
                f"| {r['regen_clean_any_cycle']} "
                f"| {r['regen_cycles_exhausted']} "
                f"| {r['answer_leak_incidents']} "
                f"| {r['wall_seconds']:.1f} |"
            )
        lines.append('')

        # Per-cell details — validator-issue distribution is where the
        # "which issues regen could not clean" insight lives.
        lines.append('## Per-cell details')
        lines.append('')
        for r in results:
            rl = r.get('regen_model_label') or r.get('regen_model_key') or '—'
            lines.append(f"### {rl} (regen) × L{r['lesson_id']} × {r['persona']}")
            lines.append('')
            lines.append(f"- session_id: {r.get('session_id') or '—'}")
            lines.append(f"- tutor (fixed): `{r['provider']}/{r['model_name']}`")
            lines.append(f"- regen model: `{r.get('regen_model_name') or '—'}`")
            lines.append(f"- reason: `{r['reason']}` after {r['turns']} student "
                         f"turns (wall {r['wall_seconds']:.1f}s)")
            if r.get('error'):
                lines.append(f"- ⚠️ error: `{r['error']}`")
                lines.append('')
                continue
            lines.append(f"- regen: triggered on {r['regen_triggered_turns']} "
                         f"turn(s); cycle-1 clean on {r['regen_clean_first_cycle']}; "
                         f"any-cycle clean on {r['regen_clean_any_cycle']}; "
                         f"shipped dirty on {r['regen_cycles_exhausted']}")
            lines.append(f"- tool-use rate: {r['tool_use_rate']:.0%} "
                         f"({r['tutor_turns_with_tool']}/{r['tutor_turns']})")
            if r['validator_issue_counts']:
                top = sorted(r['validator_issue_counts'].items(),
                             key=lambda kv: -kv[1])[:8]
                joined = ', '.join(f"`{k}`={v}" for k, v in top)
                lines.append(f"- validator-issue distribution: {joined}")
            lines.append('')

        REGEN_SWEEP_REPORT_PATH.write_text('\n'.join(lines))
        self.stdout.write(self.style.SUCCESS(
            f"Wrote regen-sweep report → {REGEN_SWEEP_REPORT_PATH}"
        ))
