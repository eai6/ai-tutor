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

    def handle(self, *args, smoke, scenario, single_turn, multi_turn, **kwargs) -> None:
        if single_turn and multi_turn:
            raise CommandError("Pass at most one of --single-turn / --multi-turn.")
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

        if not scenarios:
            self.stdout.write(self.style.WARNING("No scenarios matched."))
            return

        self.stdout.write(
            self.style.SUCCESS(f"\n=== Running {len(scenarios)} scenario(s) ===\n")
        )

        result = run(scenarios)
        out_path = write_run(result)

        # Per-scenario one-liner.
        for sr in result.results:
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
                    detail = f"trajectory ok"
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
                f"  {label} {sr.scenario_id:<40} {detail}{mode_tag}{rubric_tag}"
            )

        # Summary line.
        summary = (
            f"\nResult: passed={result.passed} failed={result.failed} "
            f"errored={result.errored} total={result.total_scenarios}"
        )
        style = self.style.SUCCESS if result.failed == 0 and result.errored == 0 else self.style.WARNING
        self.stdout.write(style(summary))
        self.stdout.write(f"Output: {out_path}\n")
