"""Run the AI Tutor eval harness.

Phase 1 — smoke only. Drives ConversationalTutor.respond() against one or
more scenarios under evals/dataset/ and writes a result JSON to evals/runs/.

Usage:
    python manage.py run_eval --smoke              # run evals/dataset/smoke/*.yaml
    python manage.py run_eval --scenario smoke_001 # run one scenario by id
    python manage.py run_eval                      # run everything except smoke/

See memory/eval_harness_plan.md.
"""
from __future__ import annotations

import argparse
import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Run the eval harness against one or more scenarios."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--smoke', action='store_true',
                            help='Only run scenarios under evals/dataset/smoke/.')
        parser.add_argument('--scenario', default=None,
                            help='Run a single scenario by id (filename stem).')
        parser.add_argument('--single-turn', action='store_true',
                            help='Run only single_turn scenarios (skip multi_turn).')
        parser.add_argument('--multi-turn', action='store_true',
                            help='Run only multi_turn scenarios.')
        parser.add_argument('--subset', default=None,
                            help='Only run scenarios carrying this tag. '
                                 'Combines with --single-turn / --multi-turn.')
        parser.add_argument('--sample', type=int, default=None,
                            help='Run a random sample of N scenarios instead of '
                                 'the full set, drawn AFTER the other filters. '
                                 'Use with --seed for a reproducible draw. This '
                                 'replaces the old hardcoded "core15" subset: a '
                                 'fixed blessed subset silently becomes the '
                                 'benchmark, and its 15 scenarios could not '
                                 'resolve a model difference below ~36pp. Sample '
                                 'to cut wall-clock; run the full set to publish.')
        parser.add_argument('--seed', type=int, default=0,
                            help='Seed for --sample (default: 0). Same seed + '
                                 'same dataset = same draw.')
        parser.add_argument('--resume', nargs='?', const='latest', default=None,
                            metavar='PARTIAL_JSON',
                            help='Resume a killed run from its checkpoint: '
                                 'skip scenarios already completed there and '
                                 'fold their results into this run. With no '
                                 'value, uses the newest '
                                 'evals/runs/partial_*.json. Pass the SAME '
                                 'filters/--sample/--seed as the original '
                                 'run so the scenario set matches.')

    def handle(self, *args, smoke, scenario, single_turn, multi_turn, subset,
               sample, seed, resume, **kwargs) -> None:
        if single_turn and multi_turn:
            raise CommandError("Pass at most one of --single-turn / --multi-turn.")
        if sample is not None and sample < 1:
            raise CommandError("--sample must be >= 1.")

        # Benchmark question-type policy. Production defaults to mcq-only
        # (tools.py `_allowed_tutoring_types`), which FORCES the tutor to
        # fabricate multiple-choice options for math's authored short_numeric
        # questions and drops those questions from the pool — the root cause of
        # the math underperformance + timeout bottleneck (see
        # memory/math_mcq_fabrication_diagnosis.md). Enable `short_numeric` so
        # the eval measures real math tutoring: it routes to the deterministic
        # math grader (grader.py `_grade_math`), so step-advance still fires
        # cleanly. `short_answer` stays OFF — its partial verdicts were the
        # original reason mcq-only exists. setdefault so an explicit env override
        # still wins; scoped to run_eval (production never invokes this command).
        import os as _os
        _os.environ.setdefault('TUTORING_QUESTION_TYPES', 'mcq,short_numeric')

        # Import inside handle so Django app loading completes first.
        from evals.runner import discover_scenarios, run, write_run

        try:
            scenarios = discover_scenarios(smoke=smoke, scenario_id=scenario)
        except FileNotFoundError as exc:
            raise CommandError(str(exc)) from exc

        if single_turn:
            scenarios = [s for s in scenarios if s.mode == 'single_turn']
        elif multi_turn:
            scenarios = [s for s in scenarios if s.mode == 'multi_turn']

        if subset:
            scenarios = [s for s in scenarios if subset in s.tags]

        if not scenarios:
            hint = f" for subset={subset!r}" if subset else ""
            self.stdout.write(self.style.WARNING(f"No scenarios matched{hint}."))
            return

        # Sample AFTER filtering, and sort first so the draw does not depend on
        # filesystem ordering. A sampled run is a sampled run — say so loudly, or
        # a partial score gets read as a full one.
        if sample is not None and sample < len(scenarios):
            import random
            population = sorted(scenarios, key=lambda s: s.id)
            scenarios = random.Random(seed).sample(population, sample)
            scenarios.sort(key=lambda s: s.id)
            self.stdout.write(self.style.WARNING(
                f">> SAMPLED RUN: {sample} of {len(population)} scenarios "
                f"(seed={seed}). Scores are NOT comparable to a full run."
            ))

        # Make the tutor engine explicit + loud. This eval targets the
        # production simple_tutor engine; if the legacy ConversationalTutor is
        # active (SIMPLE_TUTOR_ENGINE set to a falsy value) the scores are for
        # the wrong engine — warn rather than silently mislead.
        import os as _os
        from apps.tutoring import simple_tutor as _st
        engine_name = 'simple_tutor' if _st.is_enabled() else 'conversational_tutor'
        flag = _os.environ.get('SIMPLE_TUTOR_ENGINE', '<unset → default on>')
        banner = f">> Tutor engine: {engine_name}  (SIMPLE_TUTOR_ENGINE={flag})"
        from apps.tutoring.simple_tutor.tools import _allowed_tutoring_types
        banner += f"  question_types={','.join(_allowed_tutoring_types())}"
        if engine_name == 'simple_tutor':
            self.stdout.write(self.style.SUCCESS(banner))
        else:
            self.stdout.write(self.style.ERROR(
                banner + "  ← WARNING: LEGACY engine, NOT simple_tutor. Unset "
                "SIMPLE_TUTOR_ENGINE (or set it to 1) to evaluate the production engine."
            ))

        self.stdout.write(
            self.style.SUCCESS(f"\n=== Running {len(scenarios)} scenario(s) ===\n")
        )
        self.stdout.write(
            "Checkpoint: evals/runs/partial_*.json is rewritten after every "
            "scenario — if this run is killed, salvage completed results "
            "from there.\n"
        )
        self.stdout.flush()

        # Resume: load the checkpoint's completed results, drop any that no
        # longer match the current scenario set (different filters than the
        # original run — a stale result folded into a differently-scoped
        # board would misreport it), and let run() skip the rest.
        prior_results = None
        consumed_partial = None
        if resume is not None:
            from pathlib import Path as _Path
            from evals.runner import latest_partial, load_partial, _git_sha
            if resume == 'latest':
                consumed_partial = latest_partial()
                if consumed_partial is None:
                    raise CommandError(
                        "No evals/runs/partial_*.json found to resume from.")
            else:
                consumed_partial = _Path(resume)
                if not consumed_partial.exists():
                    raise CommandError(f"{consumed_partial} does not exist.")
            try:
                prior_results, prior_sha = load_partial(consumed_partial)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise CommandError(f"Cannot resume from {consumed_partial}: {exc}")
            current_ids = {s.id for s in scenarios}
            stale = [r for r in prior_results
                     if r.scenario_id not in current_ids]
            if stale:
                self.stdout.write(self.style.WARNING(
                    f">> RESUME: dropping {len(stale)} checkpointed result(s) "
                    f"not in the current scenario set (different filters or "
                    f"seed than the original run?): "
                    f"{', '.join(r.scenario_id for r in stale[:5])}…"))
                prior_results = [r for r in prior_results
                                 if r.scenario_id in current_ids]
            if prior_sha and prior_sha != _git_sha():
                self.stdout.write(self.style.WARNING(
                    f">> RESUME: checkpoint was written at {prior_sha}, "
                    f"current code is {_git_sha()} — the resumed scenarios "
                    f"were scored by DIFFERENT code. Fine for salvage, "
                    f"misleading for an A/B."))
            self.stdout.write(self.style.SUCCESS(
                f">> RESUME: {len(prior_results)} completed scenario(s) "
                f"loaded from {consumed_partial.name}; "
                f"{len(scenarios) - len(prior_results)} to run."))
            self.stdout.flush()

        # Per-scenario one-liner — STREAMED as each scenario completes, and
        # flushed. Printing these only after run() returned meant a mid-run
        # SIGKILL (2026-08-04 Colab OOM) showed nothing per-scenario: the
        # lines sat in Python's block buffer behind a file redirect. The
        # runner also checkpoints a partial_*.json after every scenario now,
        # so a killed sweep salvages everything completed.
        def _print_result(sr, index, total):
            rubric_tag = ''
            if sr.rubric_result:
                rr = sr.rubric_result
                mean = rr.get('mean_score', 0.0)
                thresh = rr.get('pass_threshold', 0.0)
                if rr.get('error'):
                    rubric_tag = f" [rubric ERR: {rr['error'][:40]}]"
                else:
                    rubric_tag = f" [rubric {mean:.2f}/{thresh:.2f}]"

            mode_tag = ''
            if sr.mode == 'multi_turn':
                mode_tag = f" [{sr.sim_reason or '?'} @ {sr.sim_turns} turns]"

            if sr.error:
                label = self.style.ERROR('ERR  ')
                detail = sr.error
            elif sr.passed:
                label = self.style.SUCCESS('PASS ')
                if sr.mode == 'multi_turn':
                    detail = "trajectory ok"
                else:
                    detail = (sr.tutor_response[:60] + '...') if len(sr.tutor_response) > 60 else sr.tutor_response
                    detail = detail.replace('\n', ' ')
            else:
                label = self.style.WARNING('FAIL ')
                fails = [r.name for r in sr.assertion_results if not r.passed]
                if sr.rubric_result and not sr.rubric_result.get('passed'):
                    fails.append('rubric')
                detail = f"failed: {', '.join(fails)}"
            self.stdout.write(
                f"  [{index}/{total}] {label} {sr.scenario_id:<40} "
                f"{detail}{mode_tag}{rubric_tag}"
            )
            self.stdout.flush()

        result = run(scenarios, on_result=_print_result,
                     prior_results=prior_results)
        out_path = write_run(result)
        # The consumed checkpoint is superseded by the final JSON — leaving
        # it would make the next bare --resume pick up an already-finished
        # run's checkpoint.
        if consumed_partial is not None:
            consumed_partial.unlink(missing_ok=True)

        # Summary line.
        summary = (
            f"\nResult: passed={result.passed} failed={result.failed} "
            f"errored={result.errored} total={result.total_scenarios}"
        )
        style = self.style.SUCCESS if result.failed == 0 and result.errored == 0 else self.style.WARNING
        self.stdout.write(style(summary))
        self.stdout.write(f"Output: {out_path}\n")
