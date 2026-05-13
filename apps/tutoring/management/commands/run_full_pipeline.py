"""End-to-end local pipeline: simulator → sampler → annotator → metrics.

One command that drives the full feedback loop on your laptop:

  1. Pick a lesson from the rotation (math + geography constants).
  2. Run the synthetic-student simulator against the live tutor — fresh
     SessionTurns reflecting the *current* tutor prompt + judge stack.
  3. Sample N items from the new turns into BenchmarkItem rows.
  4. Spawn the annotator orchestrator (subprocess; uses chrome-devtools-
     mcp + Anthropic Sonnet) to label each item via the dashboard UI.
  5. Read back the resulting BenchmarkRun + append a JSONL line to
     ``ops/annotator_agent/metrics_history.jsonl`` for trend tracking.

Usage::

    # Default rotation, 1 lesson, fresh struggler session, 3 items:
    python manage.py run_full_pipeline

    # Pin a specific lesson:
    python manage.py run_full_pipeline --lesson 638

    # Cheaper run (smaller session, fewer items):
    python manage.py run_full_pipeline --max-turns 6 --max-items 1

    # Skip the annotator (just exercise the simulator + sampler):
    python manage.py run_full_pipeline --skip-annotator

This is the LOCAL counterpart to .github/workflows/annotator_agent.yml.
The CI workflow uses a frozen fixture for cost predictability; this
command runs everything fresh so changes to tutor prompt / judges /
persona are reflected immediately.

Estimated cost per full run (1 lesson, 10 turns, 3 items):
  - Tutor (Opus 4.7): ~$0.50–1
  - Student-bot (Gemini 2.5 Flash): ~$0.002 — negligible
  - Annotator (Sonnet 4.6): ~$0.70–0.90 per item × 3 = ~$2–3

See memory/automated_annotator_agent_plan.md.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import subprocess
import sys
from datetime import datetime, timezone

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from apps.benchmark.models import BenchmarkItem, BenchmarkRun
from apps.benchmark.sampling import build_item_snapshot
from apps.tutoring.models import SessionTurn, TutorSession
from apps.tutoring.student_sim import PERSONAS, simulate_session


# Lesson constants — IDs of READY lessons in the local DB the pipeline
# rotates through. Update when you generate / pull more lessons.
LESSON_ROTATION = [
    638,  # Math — Angles around a point (Layer S Demo)
    543,  # Geography — Working with Graphs (4 step images)
    546,  # Geography — Comparing Earth's Layers (8 ready steps)
]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
METRICS_FILE = REPO_ROOT / 'ops' / 'annotator_agent' / 'metrics_history.jsonl'


class Command(BaseCommand):
    help = "Local end-to-end: simulator → sampler → annotator → metrics."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument('--lesson', type=int, default=None,
                            help='Specific lesson ID. Default: pick from LESSON_ROTATION.')
        parser.add_argument('--persona', default='struggler',
                            choices=sorted(PERSONAS),
                            help='Student persona for the simulator (default: struggler).')
        parser.add_argument('--max-turns', type=int, default=10,
                            help='Tutor↔student exchanges in the simulated session (default: 10).')
        parser.add_argument('--max-items', type=int, default=3,
                            help='How many BenchmarkItems the annotator labels (default: 3).')
        parser.add_argument('--max-steps', type=int, default=80,
                            help='Hard cap on annotator-agent loop iterations (default: 80).')
        parser.add_argument('--base-url', default='http://127.0.0.1:8000',
                            help='Where the dev server is running. Default: http://127.0.0.1:8000.')
        parser.add_argument('--skip-annotator', action='store_true',
                            help='Stop after sampling — useful for cheap simulator-only iterations.')
        parser.add_argument('--seed', type=int, default=None,
                            help='Random seed for lesson selection.')

    def handle(self, *args, lesson, persona, max_turns, max_items,
               max_steps, base_url, skip_annotator, seed, **kwargs) -> None:
        rng = random.Random(seed)
        lesson_id = lesson or rng.choice(LESSON_ROTATION)

        self.stdout.write(self.style.SUCCESS(
            f"\n=== Full pipeline: lesson={lesson_id} persona={persona} "
            f"max_turns={max_turns} max_items={max_items} ===\n"
        ))

        # ---- Phase 1: simulator ----------------------------------------
        self.stdout.write(self.style.HTTP_INFO("[1/4] Running simulator..."))
        sim_result = simulate_session(
            lesson_id=lesson_id,
            persona=persona,
            max_turns=max_turns,
        )
        self.stdout.write(
            f"  Simulator: reason={sim_result.reason} turns={sim_result.turns} "
            f"session_id={sim_result.session_id} "
            f"(student tokens in={sim_result.student_tokens_in} "
            f"out={sim_result.student_tokens_out})"
        )

        # ---- Phase 2: sample ------------------------------------------
        self.stdout.write(self.style.HTTP_INFO(
            f"[2/4] Sampling {max_items} item(s) from synthetic_struggler..."
        ))
        # We sample directly from the just-created session's tutor turns
        # to guarantee we get items from THIS run rather than from older
        # synthetic data that may already be in the pool.
        new_tutor_turns = list(
            SessionTurn.objects
            .filter(session_id=sim_result.session_id, role='tutor')
            .exclude(judge_outputs={})
            .order_by('id')
        )
        if not new_tutor_turns:
            raise CommandError(
                "Simulator produced no tutor turns with judge_outputs — "
                "session may have errored out before the tutor responded."
            )
        # Spread the sample across the session: take evenly-spaced turns.
        if len(new_tutor_turns) <= max_items:
            picks = new_tutor_turns
        else:
            stride = max(1, len(new_tutor_turns) // max_items)
            picks = new_tutor_turns[::stride][:max_items]

        item_ids = []
        for turn in picks:
            snapshot = build_item_snapshot(turn)
            item, _ = BenchmarkItem.objects.update_or_create(
                item_id=snapshot['item']['item_id'],
                defaults={
                    'source_turn': turn,
                    'subject': snapshot['item']['subject'],
                    'lesson_id': snapshot['item']['lesson_id'],
                    'snapshot': snapshot,
                    'stratum': f'synthetic_{persona}',
                },
            )
            item_ids.append(item.item_id)
        self.stdout.write(f"  Sampled: {item_ids}")

        if skip_annotator:
            self.stdout.write(self.style.WARNING(
                "Skipping annotator (--skip-annotator). Done."
            ))
            return

        # ---- Phase 3: annotator agent ---------------------------------
        self.stdout.write(self.style.HTTP_INFO(
            f"[3/4] Running annotator agent (max_steps={max_steps})..."
        ))
        ann_cmd = [
            sys.executable, '-m', 'ops.annotator_agent.orchestrator',
            '--base-url', base_url,
            '--persona', persona,
            '--max-items', str(len(item_ids)),
            '--max-steps', str(max_steps),
        ]
        ann_proc = subprocess.run(
            ann_cmd, cwd=str(REPO_ROOT),
            env={**os.environ},
        )
        if ann_proc.returncode != 0:
            raise CommandError(
                f"Annotator orchestrator exited non-zero ({ann_proc.returncode})"
            )

        # ---- Phase 4: read run + append metrics line ------------------
        self.stdout.write(self.style.HTTP_INFO(
            "[4/4] Reading BenchmarkRun + appending metrics line..."
        ))
        run = BenchmarkRun.objects.order_by('-id').first()
        if run is None:
            raise CommandError(
                "No BenchmarkRun row exists — agent never clicked Score now"
            )

        metrics = run.metrics or {}
        line = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'sha': _git_short_sha(),
            'mode': 'local-pipeline',
            'lesson_id': lesson_id,
            'persona': persona,
            'sim_session_id': sim_result.session_id,
            'sim_reason': sim_result.reason,
            'sim_turns': sim_result.turns,
            'item_ids': item_ids,
            'run_id': run.id,
            'annotator_role': run.annotator_role,
            'total': run.total_items,
            'passed': run.passed,
            'failed': run.failed,
            'pass_rate': (metrics.get('overall') or {}).get('pass_rate'),
            'failure_categories': metrics.get('failure_categories'),
            'by_stratum': (metrics.get('slices') or {}).get('by_stratum'),
            'by_subject': (metrics.get('slices') or {}).get('by_subject'),
        }
        METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with METRICS_FILE.open('a') as f:
            f.write(json.dumps(line, default=str) + '\n')

        overall = metrics.get('overall') or {}
        self.stdout.write(self.style.SUCCESS(
            f"\n=== Done. Run #{run.id} — pass rate "
            f"{(overall.get('pass_rate') or 0)*100:.1f}% "
            f"({run.passed}/{run.total_items}). "
            f"Appended to {METRICS_FILE.relative_to(REPO_ROOT)}.\n"
        ))


def _git_short_sha() -> str:
    """Best-effort current commit short SHA. Empty string if not in a git repo."""
    try:
        r = subprocess.run(
            ['git', 'rev-parse', '--short=12', 'HEAD'],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip() if r.returncode == 0 else ''
    except Exception:
        return ''
