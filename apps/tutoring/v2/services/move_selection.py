"""Pure-function move selection — Phase 2 §2.3.

Move selection is NOT an LLM call. Inputs per analysis §4 + Phase 2 §2.3:
  - verdict.kind (correct | wrong | partial | unverified)
  - attempts_on_open_question
  - objective_progress
  - unverified_run_length
  - current_move
  - move_history
  - profile_summary  (free-text only — NOT skills_snapshot, NOT
                      StudentSkillMastery rows)

verdict.bare_answer is **explicitly NOT** a move-selection input — it
biases the selected move's prompt content (Phase 2 §2.1.1). Move
selection sees only verdict.kind.

The 9 moves per analysis §4:
  pose_question, confirm_and_advance, confirm_and_extend,
  scaffold_hint, name_misconception, worked_example, explain,
  pivot, close_topic.

Safety valves (§7 item 3) fire OUTSIDE this module — they live in
``TutorEngine``. Per the analysis: "pivot / close_topic should fire
first under normal conditions".
"""

from __future__ import annotations

from typing import Optional

from apps.tutoring.v2.contracts import (
    GradingResult,
    ObjectiveProgress,
    SessionRuntimeState,
    Verdict,
)
from apps.tutoring.v2.services.intent_classifier import (
    classify_student_intent,
    intent_to_move,
)


ALLOWED_MOVES = (
    "pose_question",
    "confirm_and_advance",
    "confirm_and_extend",
    "scaffold_hint",
    "name_misconception",
    "worked_example",
    "explain",
    "pivot",
    "close_topic",
)


# Help-request detection (Science of Learning — Direct
# Instruction: when the student signals they don't have the
# concept yet, teach it explicitly before asking for more
# retrieval). Implementation lives in ``intent_classifier`` and uses
# a fast Haiku-backed LLM call rather than regex patterns. The regex
# version that shipped in Phase 2 missed the dominant help-request
# phrasings observed in the run-5 MATHS-S1 evaluation ("i dont know
# how to do percentages", "can you teach me") because they don't
# match the narrow ``don't get/understand`` / ``show me how``
# templates the regex enumerates. The LLM classifier generalises
# across subjects, dialects, and misspellings.


def detect_help_request(
    student_input: str,
    *,
    open_question_stem: str = "",
) -> Optional[str]:
    """Return ``"worked_example"``, ``"explain"``, or ``None``.

    Delegates to the LLM-based intent classifier. Fail-soft:
    returns ``None`` on classifier outage so move selection proceeds
    on the verdict-driven path.

    ``open_question_stem`` is optional context used by the classifier
    to disambiguate "I don't understand" (the question vs. the
    concept). Callers that don't have it can omit it.
    """
    if not student_input or not student_input.strip():
        return None
    intent = classify_student_intent(
        student_input=student_input,
        open_question_stem=open_question_stem,
    )
    return intent_to_move(intent)


# Objective-evidence threshold — when to fire ``close_topic`` from the
# router based on correctness signal alone.
#
# Design role (CLAUDE.md guidance — "deterministic gates as safety
# floors, not flow controllers"): this is the "definitely-enough"
# floor. The ``close_topic`` move prompt carries the real pedagogical
# judgement about when to wrap an objective. The router only fires
# close_topic from here when the evidence is unambiguous mastery
# (Principle #4 Mastery Learning Ch.13 — hold the same bar; vary the
# path).
#
# Tightened 2026-05-27 — the prior 2 correct / ≥50% ratio threshold
# closed objectives after a single passing answer on items the bank
# treated as separate (MATHS-S1 2026-05-27 T1440 — close after one
# practice item) and let majority-wrong sessions close (GEO-S5
# 2026-05-27 T1479 — close on a help-request when the verdictless
# safety valve fired). The new threshold requires:
#   - ≥ 2 correct verdicts on this objective AND
#   - ≥ 3 total attempts (so 2 correct is at least 2-of-3, not 2-of-2)
#     OR ratio ≥ 0.66 when attempts < 3 (a clean 2/2 still counts).
# Pivot / close_topic from the LLM-generated move prompts are
# expected to fire first in normal sessions; this is the upper bound.
_OBJECTIVE_MIN_CORRECT = 2
_OBJECTIVE_MIN_RATIO = 0.66
_OBJECTIVE_MIN_ATTEMPTS_FOR_CLOSE = 2


# ──────────────────────────────────────────────────────────────────────
# Active Learning — doing-rate window (Phase 4)
# ──────────────────────────────────────────────────────────────────────
#
# Principle #1 *Active Learning* (Ch.10): "Student is *doing* on ≥60%
# of turns". When the doing-rate over the last 5 student turns drops
# below 60% (i.e. 3 or fewer attempts out of 5), the next move is
# biased toward LIGHTER cognitive lift — a smaller, easier ask the
# student can succeed on, restoring momentum on successful retrievals
# rather than piling on more listening.
#
# This is a soft bias, not a hard override: the wrong-branch
# escalation (scaffold_hint → name_misconception → pivot) stays intact
# (those moves react to correctness, not effort). The bias only
# changes:
#   - PARTIAL + attempts≥3: worked_example  → scaffold_hint
#   - CORRECT + attempts≤1: confirm_and_extend → confirm_and_advance

_DOING_RATE_WINDOW = 5
_DOING_RATE_FLOOR = 0.6  # 60% — Ch.10's testable imperative


def _compute_doing_rate(window: list[bool]) -> float:
    """Fraction of recent student turns where they actually attempted.

    Empty window returns 1.0 (no signal yet → no bias).
    """
    if not window:
        return 1.0
    truthy = sum(1 for b in window if bool(b))
    return truthy / max(1, len(window))


def update_doing_rate_window(
    runtime_state: SessionRuntimeState,
    *,
    attempting: bool,
) -> None:
    """Append the current turn's attempting/hedging flag to the window.

    Caller passes ``True`` when the intent classifier returned
    ``attempting``, ``False`` when it returned a help-request / meta
    intent. The window holds the last ``_DOING_RATE_WINDOW`` flags;
    older entries drop off.

    Mutates runtime_state in place.
    """
    window = list(runtime_state.student_doing_rate_window or [])
    window.append(bool(attempting))
    if len(window) > _DOING_RATE_WINDOW:
        window = window[-_DOING_RATE_WINDOW:]
    runtime_state.student_doing_rate_window = window


def select_move(
    *,
    verdict: Optional[GradingResult],
    runtime_state: SessionRuntimeState,
    profile_summary: str = "",
    objective_just_opened: bool = False,
    current_objective: str = "",
    student_input: str = "",
    help_request_move: Optional[str] = None,
) -> str:
    """Deterministic move pick. Returns one of ``ALLOWED_MOVES``.

    Args:
      verdict: grader output for the just-graded student input, or
        ``None`` on transitional / opening turns.
      runtime_state: the loaded ``SessionRuntimeState``.
      profile_summary: free-text qualitative recall. Read but NOT used
        as a primary discriminator in MVP — present so future tuning
        can lean on it without changing the contract.
      objective_just_opened: True on the very first turn of a new
        objective (TutorEngine bookkeeping).
      current_objective: enabling-objective slug for ``objective_progress``
        lookup.
      student_input: the student's latest message. Used only to detect
        explicit help-requests (when ``help_request_move`` is not
        pre-computed); never matched against curriculum content.
      help_request_move: optional pre-computed help-request override
        (``"worked_example"`` / ``"explain"`` / ``None``). Callers that
        already ran the intent classifier upstream — TutorEngine does
        this once per turn before grading — pass it here to avoid a
        second LLM call. When ``None`` the function falls through to
        the verdict-driven path; when explicitly ``""`` / sentinel the
        on-demand classifier still runs (back-compat for legacy tests).
    """
    attempts = runtime_state.attempts_on_open_question
    counters = runtime_state.safety_valve_counters
    remediation = runtime_state.remediation_state
    move_history = list(runtime_state.move_history or [])

    # ── Safety-valve-adjacent close conditions (the engine's safety
    # valves are checked separately, but `close_topic` is also the
    # right move when objective evidence has saturated — fire it
    # eagerly rather than wait for the outer cap.)
    obj_progress = runtime_state.objective_progress.get(current_objective)
    if _objective_evidence_sufficient(obj_progress):
        return "close_topic"

    # ── Explicit help-request override (Direct Instruction +
    # Cognitive Load). When the student explicitly asks for an
    # explanation or worked example, that beats every verdict-driven
    # branch below — answering "show me how" with another retrieval
    # scaffold is the wrong move regardless of what the grader said.
    #
    # Prefer the caller-supplied override (TutorEngine pre-classifies
    # once per turn). When absent, classify on demand for back-compat
    # with direct callers (tests, template-renderer paths).
    if help_request_move is not None:
        help_kind = help_request_move
    else:
        open_q_stem = (
            runtime_state.open_question.rendered_stem
            if runtime_state.open_question is not None
            else ""
        )
        help_kind = detect_help_request(
            student_input, open_question_stem=open_q_stem,
        )
    if help_kind in ("worked_example", "explain"):
        return help_kind

    # ── No verdict this turn (opening / transitional / free-chat) ──
    if verdict is None:
        # First turn on a new objective → frame the concept first.
        if objective_just_opened:
            # If the student profile suggests prior struggle, lead with
            # a worked example instead of a bare explanation.
            if profile_summary and "struggl" in profile_summary.lower():
                return "worked_example"
            return "explain"
        # No open question and nothing graded → invite engagement.
        if runtime_state.open_question is None:
            return "pose_question"
        # Still awaiting an answer on the open question and we have no
        # verdict to react to — keep the floor with the student.
        return "pose_question"

    kind = verdict.verdict

    # Phase 4 — Active Learning doing-rate bias. Computed once per
    # call and used by the verdict-branch picks below to favour
    # lighter cognitive lift when the student has been hedging /
    # asking for help. Principle #1 Active Learning Ch.10 — momentum
    # builds on successful retrievals; a struggling student needs a
    # smaller ask they can succeed on, not a heavier worked example.
    doing_rate_low = (
        _compute_doing_rate(runtime_state.student_doing_rate_window)
        < _DOING_RATE_FLOOR
    )

    # ── verdict=correct branch ──
    if kind == Verdict.CORRECT:
        # If this correct closes out the objective, the
        # `_objective_evidence_sufficient` check above will have
        # returned `close_topic` already. So here we are still inside
        # the objective. Default to confirm_and_advance; switch to
        # confirm_and_extend on early mastery (first correct after
        # zero/one attempt — student likely had the idea already).
        if attempts <= 1 and obj_progress and obj_progress.correct >= 1:
            # When the student has been hedging, keep the celebration
            # but DON'T extend into harder territory — a fresh easy
            # ask via confirm_and_advance is the right next step.
            if doing_rate_low:
                return "confirm_and_advance"
            return "confirm_and_extend"
        return "confirm_and_advance"

    # ── verdict=wrong branch ──
    if kind == Verdict.WRONG:
        # `pivot` fires when:
        #  (a) 4+ wrong attempts on the same item, or
        #  (b) name_misconception fired previously on this item AND the
        #      very next attempt is still wrong.
        if attempts >= 4:
            return "pivot"
        if (
            remediation is not None
            and not remediation.resolved
            and _just_after(move_history, "name_misconception")
        ):
            return "pivot"
        # `name_misconception` on the 3rd wrong attempt.
        if attempts >= 3:
            return "name_misconception"
        # Default scaffolding on attempts 1–2.
        return "scaffold_hint"

    # ── verdict=partial branch ──
    if kind == Verdict.PARTIAL:
        # Scaffold the missing piece; if the student has been stuck a
        # while on this item, escalate to worked_example.
        # Active Learning bias: when doing-rate is low, prefer
        # scaffold_hint even at attempts≥3 — a worked example here
        # piles on listening when the student needs a small win.
        if attempts >= 3 and not doing_rate_low:
            return "worked_example"
        return "scaffold_hint"

    # ── verdict=unverified branch ──
    if kind == Verdict.UNVERIFIED:
        # Three consecutive unverified turns → explain and reset.
        if runtime_state.unverified_run_length >= 3:
            return "explain"
        # When an open_question is still in flight, scaffold back to
        # it rather than pose a fresh one — the open Q stays the
        # focus, and ``scaffold_hint``'s prompt surfaces uncertainty
        # by design (it cites ``what_right`` / ``what_missing`` from
        # student_safe_feedback, which the conformance classifier
        # reads as a hedged stance).
        if runtime_state.open_question is not None:
            return "scaffold_hint"
        # No open question (free-chat / topic-divergence) → probe
        # via a fresh tool-posed question. The tutor's pose_question
        # move prompt is verdict-aware and surfaces uncertainty in
        # the lead_in on unverified turns.
        return "pose_question"

    # Defensive default.
    return "pose_question"


def _objective_evidence_sufficient(
    progress: Optional[ObjectiveProgress],
) -> bool:
    """Decide whether the current objective has enough evidence to close.

    This is the safety-floor close trigger, not the primary one. The
    LLM-generated ``close_topic`` move prompt is expected to surface
    organic closes before this fires; this catches the case where the
    LLM has been ambivalent for several correct attempts in a row.
    (Principle #4 Mastery Learning Ch.13 — close on demonstrated
    mastery; never lower the bar to raise the close rate.)
    """
    if progress is None:
        return False
    if progress.closed:
        # Already closed — don't double-fire close_topic.
        return False
    if progress.attempts < _OBJECTIVE_MIN_ATTEMPTS_FOR_CLOSE:
        return False
    if progress.correct < _OBJECTIVE_MIN_CORRECT:
        return False
    ratio = progress.correct / max(1, progress.attempts)
    return ratio >= _OBJECTIVE_MIN_RATIO


def _just_after(move_history: list[str], target_move: str) -> bool:
    """True when the immediately-previous move equals ``target_move``."""
    if not move_history:
        return False
    return move_history[-1] == target_move
