"""Replay real student inputs through Layer S and pretty-print the
analysis. Two modes:

  --transcript   Run a fixed list of student replies drawn from the
                 production "angles around a point" session that
                 motivated this work, with realistic expected
                 answers for each.

  --db           Walk every math SessionTurn in the local database
                 and report the state distribution + flagged samples.

Run with `python manage.py shell < scripts/replay_layer_s.py` or via
the management command shim below.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Add repo root to sys.path so `config.settings` resolves whether the
# script is run from the repo root or anywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ai_tutor.config.settings")
django.setup()

from ai_tutor.apps.tutoring.student_working_analyzer import (  # noqa: E402
    WorkingState,
    analyze_working,
    build_working_analysis_block,
)


# Production "angles around a point" transcript. Each entry is the
# student's verbatim input + the expected_answer for the step they
# were on. Drawn from the Belonie-Vaani session shared in the
# 2026-04-30 design discussion.
PRODUCTION_TRANSCRIPT = [
    # (student_input, expected_answer, note)
    ("180",       "360", "warm-up: 'how many degrees in a full rotation?'"),
    ("80",        "85",  "find x where 90+85+100+x=360"),
    ("275",       "275", "interim: '90+85+100=?'"),
    ("85",        "85",  "find x = 360-275"),
    ("96 + 70 + 110 = 275\n360 - 275 = 85", "85",
                          "showing full working (with a typo: 96 vs 95)"),
    ("275",       "275", "interim: 95+70+110=?"),
    ("80 + 4x + 2x = 360", None, "writing the equation setup"),
    ("6x",        None,  "combining like terms"),
    ("260",       "280", "subtraction error 360-80=260"),
    ("46.66",     "46.67", "division 280/6"),
    ("50 + 85 + 90 + x = 360", None, "equation setup"),
    ("225",       "225", "interim sum"),
    ("135",       "135", "find x = 360-225"),
    ("2x + 3x + 4x + x = 360", None, "equation setup"),
    # The bug case: student stops at intermediate, tutor used to
    # finish for them
    ("95 + 70 + 110 = 275", "85",
                          "★ THE PRODUCTION BUG: PARTIAL_CORRECT, tutor must NOT finish"),
    ("95 + 70 + 110 = 285", "85",
                          "★ PARTIAL_WRONG: addition error, must point at step 1"),
    ("95 + 70 + 110 = 275\n360 - 275 = 85", "85",
                          "★ COMPLETE_CORRECT: full working + right answer"),
    ("100 + 100 = 250\n250 - 165 = 85", "85",
                          "★ Edge: wrong intermediate, right final → PARTIAL_WRONG"),
]


_STATE_COLORS = {
    WorkingState.NO_WORKING:       "\033[90m",  # grey
    WorkingState.PARTIAL_CORRECT:  "\033[36m",  # cyan
    WorkingState.PARTIAL_WRONG:    "\033[31m",  # red
    WorkingState.COMPLETE_CORRECT: "\033[32m",  # green
    WorkingState.COMPLETE_WRONG:   "\033[35m",  # magenta
}
_RESET = "\033[0m"


def replay_transcript(verbose: bool = False) -> None:
    print(f"\n{'='*78}")
    print("LAYER S REPLAY — production transcript")
    print(f"{'='*78}\n")

    state_counts: dict = {s: 0 for s in WorkingState}
    for student_input, expected, note in PRODUCTION_TRANSCRIPT:
        analysis = analyze_working(student_input, expected_answer=expected)
        state_counts[analysis.state] += 1

        color = _STATE_COLORS.get(analysis.state, "")
        state_label = f"{color}{analysis.state.value:<18}{_RESET}"
        steps_label = f"{len(analysis.steps)} step(s)"

        first_err = ""
        if analysis.first_error_idx is not None:
            first_err = f" first_error=step{analysis.first_error_idx}"
        propagated = ""
        if analysis.propagated_idxs:
            ids = ",".join(str(i) for i in sorted(analysis.propagated_idxs))
            propagated = f" propagated={ids}"

        # Compact single-line form
        compact_input = student_input.replace("\n", " | ")[:60]
        if len(student_input) > 60:
            compact_input += "..."
        print(f"  [{state_label}] {steps_label:<10}{first_err}{propagated}")
        print(f"    input:    {compact_input!r}")
        print(f"    expected: {expected!r}")
        print(f"    final:    {analysis.final_claim}")
        print(f"    note:     {note}")
        if verbose:
            print()
            for line in build_working_analysis_block(analysis).splitlines():
                print(f"    | {line}")
        print()

    print(f"{'='*78}")
    print("STATE DISTRIBUTION")
    print(f"{'='*78}")
    total = sum(state_counts.values())
    for state, count in state_counts.items():
        pct = (count / total) * 100 if total else 0
        color = _STATE_COLORS.get(state, "")
        print(f"  {color}{state.value:<20}{_RESET} {count:>3}  ({pct:>5.1f}%)")
    print(f"  {'-'*20} {total:>3}")


def replay_db(limit: int = 50, verbose: bool = False) -> None:
    """Walk math SessionTurn rows in the local DB. Tiny DB → tiny
    sample, but the structure works against any DB if pointed at one
    with math data."""
    from ai_tutor.apps.curriculum.models import Course
    from ai_tutor.apps.tutoring.models import SessionTurn

    math_courses = [c for c in Course.objects.all() if c.is_math]
    if not math_courses:
        print("(no math courses in this database — try --transcript instead)")
        return

    turns = (
        SessionTurn.objects.filter(
            session__lesson__unit__course__in=math_courses,
            role="student",
        )
        .select_related("session", "step")
        .order_by("-created_at")[:limit]
    )
    print(f"\n{'='*78}")
    print(f"LAYER S REPLAY — {turns.count()} student turns from DB")
    print(f"{'='*78}\n")

    state_counts: dict = {s: 0 for s in WorkingState}
    for t in turns:
        expected = (
            getattr(t.step, "expected_answer", None) if t.step else None
        )
        analysis = analyze_working(t.content, expected_answer=expected)
        state_counts[analysis.state] += 1

        color = _STATE_COLORS.get(analysis.state, "")
        compact_input = (t.content or "").replace("\n", " | ")[:60]
        if len(t.content or "") > 60:
            compact_input += "..."
        first_err = ""
        if analysis.first_error_idx is not None:
            first_err = f" first_error=step{analysis.first_error_idx}"
        print(
            f"  [{color}{analysis.state.value:<18}{_RESET}] "
            f"{len(analysis.steps)} step(s){first_err}"
        )
        print(f"    input:    {compact_input!r}")
        print(f"    expected: {expected!r}")
        if verbose and analysis.steps:
            print()
            for line in build_working_analysis_block(analysis).splitlines():
                print(f"    | {line}")
        print()

    print(f"{'='*78}")
    print("STATE DISTRIBUTION")
    print(f"{'='*78}")
    total = sum(state_counts.values())
    for state, count in state_counts.items():
        pct = (count / total) * 100 if total else 0
        color = _STATE_COLORS.get(state, "")
        print(f"  {color}{state.value:<20}{_RESET} {count:>3}  ({pct:>5.1f}%)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcript", action="store_true",
                        help="Replay the production transcript")
    parser.add_argument("--db", action="store_true",
                        help="Replay from the local DB")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print the full prompt block per turn")
    parser.add_argument("--limit", type=int, default=50,
                        help="Max turns to pull from DB")
    args = parser.parse_args()

    if not args.transcript and not args.db:
        args.transcript = True  # default

    if args.transcript:
        replay_transcript(verbose=args.verbose)
    if args.db:
        replay_db(limit=args.limit, verbose=args.verbose)


if __name__ == "__main__":
    main()
