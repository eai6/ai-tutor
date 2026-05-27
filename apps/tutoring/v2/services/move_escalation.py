"""Move-escalation ladder — Phase 4, memory/v2_unverified_trap_redesign.md Fix 3.

When conformance rejects a move twice, the engine ESCALATES to a
pedagogically meaningful neighbour move rather than degenerating to a
generic verdict-keyed prose template. The ladder reads:

  test  →  teach the method  →  re-frame the concept  →  hand off

Every step on the ladder still ends with an action the student takes
(Principle #1 Active Learning Ch.10), so escalation never sacrifices
"student is doing" momentum for "tutor is telling".

Subject-agnostic. The ladder operates on move names; the per-move
prompt + per-move terminal template already handles subject-specific
phrasing.

Design rationale
================

Each entry on the ladder targets one *more direct* move below it on
the Active+Direct teaching ladder:

  * ``pose_question``     → ``explain``          (teach before re-posing —
                                                  the student isn't ready
                                                  to retrieve yet; build
                                                  the framing first)
  * ``scaffold_hint``     → ``worked_example``   (give them the method;
                                                  hints aren't enough)
  * ``name_misconception``→ ``worked_example``   (the misconception isn't
                                                  named-able from this
                                                  turn's signal; deliver
                                                  the method instead)
  * ``worked_example``    → ``pivot``            (the same subskill is
                                                  stuck; try a different
                                                  angle on the same idea)
  * ``confirm_and_advance``→ ``pose_question``   (the confirmation didn't
                                                  land; pose the next
                                                  slot directly)
  * ``confirm_and_extend`` → ``pose_question``   (extension didn't take;
                                                  pose the slot directly)
  * ``explain``           → ``pose_question``    (talk got us nowhere;
                                                  let the student DO
                                                  something to surface
                                                  what they know)
  * ``pivot``             → ``explain``          (the new angle didn't
                                                  click; reset framing
                                                  before pivoting again)
  * ``close_topic``       → ``close_topic``      (terminal — hands off to
                                                  the exit-ticket
                                                  retrieval, which is
                                                  itself active)

Science of Learning citations:
  - Principle #1 Active Learning (Ch.10) — every escalation node ends
    with an action the student takes.
  - Principle #2 Direct Instruction (Ch.11) — escalations move toward
    the Direct end of the ladder when active retrieval is failing.
  - Principle #5 Minimising Cognitive Load (Ch.14) — labelled worked
    example replaces a hint when the hint produced a wrong answer.
  - Principle #12 Targeted Remediation (Ch.21) — the bar stays;
    additional scaffolding is added.
"""

from __future__ import annotations


# Authoritative ladder. Mapping is one-step; the engine only does one
# escalation per turn. If that also fails, the safe-template floor
# fires keyed to the escalated move (so the student still gets the
# escalated move's pedagogy, not a generic apology).
_ESCALATION_LADDER: dict[str, str] = {
    "pose_question":         "explain",
    "scaffold_hint":         "worked_example",
    "name_misconception":    "worked_example",
    "worked_example":        "pivot",
    "confirm_and_advance":   "pose_question",
    "confirm_and_extend":    "pose_question",
    "explain":               "pose_question",
    "pivot":                 "explain",
    "close_topic":           "close_topic",
}


def escalation_target(failed_move: str) -> str:
    """Pure function — failed move → escalated move.

    Returns ``failed_move`` unchanged when the move is unrecognised
    (defensive — the engine has a fixed 9-move table, but a future
    addition should not silently break the escalation pass).
    """
    move = (failed_move or "").strip()
    return _ESCALATION_LADDER.get(move, move)


def is_terminal(move: str) -> bool:
    """True when the move escalates to itself (no further ladder step)."""
    return escalation_target(move) == (move or "").strip()
