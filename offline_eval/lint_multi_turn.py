#!/usr/bin/env python3
"""Lint the multi-turn eval scenarios.

Catches the traps that silently produce wrong verdicts:
  - missing mode / trajectory verb / rubric / pass_threshold
  - lesson_id not in the eval fixtures
  - the max_turns trap: a completion-expecting scenario on a 10-step lesson
    with too small a max_turns, where a thorough tutor hits max_turns
    mid-lesson and fails expected_reason spuriously
  - the dead label assertion: no_label_anywhere with TOOL_LEAK/BANNED_OPENER/
    ASK_WORKING (derive_suggested_labels never emits these for simple_tutor)

Exit non-zero if any problem is found. Run:
    venv/bin/python offline_eval/lint_multi_turn.py
"""
from __future__ import annotations

import glob
import sys

import yaml

# LessonStep counts in the eval fixtures (verified 2026-07-06).
STEPS = {1137: 10, 1138: 10, 1463: 5, 1464: 5}
TRAJ_VERBS = {
    "expected_reason", "max_turn_count",
    "no_repeated_tutor_phrase_within_window", "no_label_anywhere",
    "no_tool_syntax_in_any_turn",
}
DEAD_LABELS = {"TOOL_LEAK", "BANNED_OPENER", "ASK_WORKING"}
# A 10-step lesson needs ~2 turns/step + exit ticket to complete; below this a
# completion-only scenario risks a spurious max_turns failure.
MIN_TURNS_FOR_COMPLETION_10STEP = 20


def lint() -> list[str]:
    problems: list[str] = []
    files = sorted(glob.glob("evals/dataset/multi_turn/*.yaml"))
    for f in files:
        d = yaml.safe_load(open(f))
        name = f.split("/")[-1]
        if d.get("mode") != "multi_turn":
            problems.append(f"{name}: mode != multi_turn")
        a = d.get("assertions", {}) or {}
        if not (set(a) & TRAJ_VERBS):
            problems.append(f"{name}: no trajectory verb in assertions")
        if not d.get("rubric"):
            problems.append(f"{name}: no rubric")
        if d.get("pass_threshold") is None:
            problems.append(f"{name}: no pass_threshold")
        lid = d.get("lesson_id")
        if lid not in STEPS:
            problems.append(f"{name}: lesson_id {lid} not in fixtures {sorted(STEPS)}")
        # max_turns trap
        reasons = set(a.get("expected_reason", []) or [])
        mt = int(d.get("max_turns", 0))
        if (lid in STEPS and STEPS[lid] == 10 and reasons
                and "max_turns" not in reasons
                and mt < MIN_TURNS_FOR_COMPLETION_10STEP):
            problems.append(
                f"{name}: max_turns trap — 10-step lesson {lid}, max_turns={mt} "
                f"< {MIN_TURNS_FOR_COMPLETION_10STEP}, completion-only "
                f"expected_reason={sorted(reasons)}"
            )
        # dead label assertion
        labels = {str(x).upper() for x in (a.get("no_label_anywhere", []) or [])}
        dead = labels & DEAD_LABELS
        if dead:
            problems.append(
                f"{name}: dead label assertion no_label_anywhere={sorted(dead)} "
                f"(never emitted for simple_tutor turns; use "
                f"no_tool_syntax_in_any_turn instead)"
            )
    return problems


def main() -> int:
    problems = lint()
    n_files = len(glob.glob("evals/dataset/multi_turn/*.yaml"))
    for p in problems:
        print("FAIL", p)
    print(f"\n{len(problems)} problem(s) across {n_files} multi_turn scenarios")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
