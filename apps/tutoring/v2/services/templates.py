"""Safe terminal templates — Phase 2 §2.5 + 2026-05-26 per-move terminal patch.

When conformance retry still fails, the response is replaced by a
deterministic verdict-keyed + move-keyed template — never released
free-form.

Templates are the **safety floor**, not the default path; they only
fire when (a) ``tutor.respond`` raises, or (b) two consecutive
conformance failures land. They never re-route move selection — the
move was already picked upstream.

What this module guarantees:

  1. Every template emits a sentence the student can act on
     (a posed question, a worked-example shape, a restated open
     question + concrete next step, an explicit close — never a
     dangling connective like "Here's one for you to try.").
  2. Voice rules mirror ``move_prompts.SHARED_PREAMBLE`` — no system
     vocabulary ("transcript", "verdict", "grader") leaks.
  3. Lesson-step content (``teacher_script`` / ``worked_example``)
     surfaced via ``MoveAnchor`` is used verbatim when the move
     contractually needs content (``explain`` / ``worked_example``).

The ``template.fallback`` span + ``SessionTurn.metadata.fallback_used
= true`` rollup make the trigger rate a tunable quality signal.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Optional

from apps.tutoring.tracing import emit_span
from apps.tutoring.v2.contracts import GradingResult, Verdict


# Rotating opener pools per branch. The pool size is small (3 each)
# because templates are the safety floor — variety here is a polish,
# not the main quality lever. Each line is content-bearing on its own
# (it would still parse as a complete tutor turn even without the
# next-action suffix).

_UNVERIFIED_OPENERS = (
    "Let me check that with you before we go further.",
    "I want to make sure we're on the same page here.",
    "Quick check on that — let's pin it down together.",
)

_NO_VERDICT_STUDENT_CLAIM_OPENERS = (
    "Let me check that one with you.",
    "Worth confirming before we move on.",
    "Let's make sure that's right together.",
)

_NO_VERDICT_NEUTRAL_OPENERS = (
    "Let's keep going.",
    "Right, let's stay with it.",
    "Carrying on then.",
)

_CORRECT_AFFIRMATIONS = (
    "Yes — {affirmation}.",
    "Right — {affirmation}.",
    "Got it — {affirmation}.",
)

_WRONG_OPENERS = (
    "Not quite — {misc}.",
    "Not there yet — {misc}.",
    "Almost — {misc}.",
)

_PARTIAL_TEMPLATE = (
    "You've got part of it: {what_right}. What's still missing: "
    "{what_missing}."
)


# Rotation pool for the open-question action floor. Three subject-
# agnostic phrasings so consecutive fallbacks don't read as identical
# scripted lines (run-4 MATHS-S1 §4 noted T8/T9/T10 all emitting the
# same wording). Each line ends in a request to attempt one specific
# part of the open question.
_OPEN_Q_ACTION_FLOORS = (
    "Looking at the question one more time: {oq} Try just one step of it "
    "and I'll guide you from there.",
    "Here's the question again, in plain words: {oq} Pick one piece of "
    "it that feels closest to something you can answer, and try just "
    "that part.",
    "Let's slow down on the same question: {oq} Tell me what you'd do "
    "first, even if you're not sure about the rest.",
)

# Same shape, used when there's no open question but an objective is
# available — keeps the student moving without inventing a problem.
_OBJECTIVE_ACTION_FLOORS = (
    "Let's stay with the idea — {obj} — and try one small piece of it "
    "together. Tell me where you'd start.",
    "Same idea — {obj} — narrowed down: what's one thing you remember "
    "about it that you can put into words?",
    "Holding on the same idea: {obj}. Pick the part you feel most "
    "confident about and start there.",
)


# ──────────────────────────────────────────────────────────────────────
# Move-aware safety-floor anchor
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MoveAnchor:
    """Pedagogy anchor passed from TutorEngine to render_safe_template.

    Subject-agnostic: every field defaults empty. When a field is
    populated, the per-move template uses it; when empty, the template
    falls back to a generic "restate the open question" shape that
    still ends with a concrete action for the student.
    """

    selected_move: str = ""
    open_question_stem: str = ""
    objective: str = ""
    teacher_script: str = ""
    worked_example: str = ""


def _pick(pool: tuple[str, ...]) -> str:
    """Pick one opener from a rotation pool.

    Random rather than round-robin because the template module is
    stateless across turns — a deterministic rotation would need
    threading per-session state, which isn't worth the wiring for a
    safety-floor cosmetic improvement.
    """
    return random.choice(pool)


def render_safe_template(
    *,
    verdict: Optional[GradingResult],
    student_claim_present: bool = False,
    next_action_text: str = "",
    move_anchor: Optional[MoveAnchor] = None,
) -> str:
    """Render a safe terminal template for the given verdict + move.

    Emits a ``template.fallback`` span so the trigger rate can be
    monitored (Phase 3 dashboards).

    ``move_anchor`` is optional for backwards compatibility with call
    sites that pre-date the per-move terminal patch. When absent, the
    behaviour reduces to the original verdict-keyed shape + the
    ``next_action_text`` connective.
    """
    with emit_span("audit", "template.fallback") as span:
        anchor = move_anchor or MoveAnchor()
        move = (anchor.selected_move or "").strip()
        next_action = (next_action_text or "").strip()

        # Move-specific bodies that deliver pedagogy minimum content.
        # These run BEFORE the verdict-keyed path because for these
        # moves the move itself, not the verdict, dominates what the
        # student should see this turn.
        if move == "worked_example":
            template_key = "move:worked_example"
            rendered = _render_worked_example_terminal(anchor)
            _annotate(span, template_key)
            return rendered

        if move == "explain":
            template_key = "move:explain"
            rendered = _render_explain_terminal(anchor)
            _annotate(span, template_key)
            return rendered

        if move == "close_topic":
            template_key = "move:close_topic"
            rendered = _render_close_terminal(anchor)
            _annotate(span, template_key)
            return rendered

        # Verdict-keyed prefix path. For moves whose contract is "pose
        # one assessment or one scaffold step on the open question",
        # the safe terminal now restates the open question (or
        # objective) so the turn ends with something concrete the
        # student can act on — instead of a dangling "here's one for
        # you to try" connective.
        action_floor = _action_floor_for_move(move, anchor, next_action)

        if verdict is None:
            if student_claim_present:
                template_key = "no_verdict_student_claim"
                rendered = _render(
                    _pick(_NO_VERDICT_STUDENT_CLAIM_OPENERS), action_floor,
                )
            else:
                # Truly no-verdict and no claim → fall back to a
                # neutral hand-the-floor-back template. Conformance
                # should not have triggered here, but the safety floor
                # must cover every code path.
                template_key = "no_verdict_neutral"
                rendered = _render(
                    _pick(_NO_VERDICT_NEUTRAL_OPENERS), action_floor,
                )
            _annotate(span, template_key)
            return rendered

        safe = verdict.student_safe_feedback
        kind = verdict.verdict

        if kind == Verdict.CORRECT:
            template_key = "correct"
            affirmation = (safe.what_right or "you got it").strip()
            rendered = _render(
                _pick(_CORRECT_AFFIRMATIONS).format(affirmation=affirmation),
                action_floor,
            )

        elif kind == Verdict.PARTIAL:
            template_key = "partial"
            what_right = (safe.what_right or "you've got part of the idea").strip()
            what_missing = (safe.what_missing or "let's look at what's still missing").strip()
            rendered = _render(
                _PARTIAL_TEMPLATE.format(
                    what_right=what_right, what_missing=what_missing,
                ),
                action_floor,
            )

        elif kind == Verdict.WRONG:
            template_key = "wrong"
            misc = (safe.first_misconception_redacted or "let's look again together").strip()
            rendered = _render(_pick(_WRONG_OPENERS).format(misc=misc), action_floor)

        elif kind == Verdict.UNVERIFIED:
            template_key = "unverified"
            rendered = _render(_pick(_UNVERIFIED_OPENERS), action_floor)

        else:
            # Shouldn't happen — Verdict enum is exhaustive — but cover
            # the case defensively.
            template_key = "unknown_verdict"
            rendered = _render(_pick(_NO_VERDICT_NEUTRAL_OPENERS), action_floor)

        _annotate(span, template_key)
        return rendered


# ──────────────────────────────────────────────────────────────────────
# Per-move terminal bodies
# ──────────────────────────────────────────────────────────────────────


def _render_worked_example_terminal(anchor: MoveAnchor) -> str:
    """Worked-example move floor.

    Lookup order (subject-agnostic):
      1. ``LessonStep.educational_content.worked_example`` (the
         lesson-authored anchor) — used verbatim with a subgoal
         wrapper.
      2. ``LessonStep.teacher_script`` (the lesson-authored direct-
         instruction text) — used as the body when no worked-example
         JSON exists. Better than dropping the worked-example
         obligation when authored content is partially missing.
      3. Restate the open question (or objective) and ask the student
         to attempt the very first step — models the *method shape*
         (one step at a time) when no authored content is available.

    Never silently drops the worked-example obligation.
    """
    we = (anchor.worked_example or "").strip()
    if we:
        body = (
            "Let me walk through a worked example so the steps are "
            "concrete:\n\n"
            f"{we}\n\n"
            "Now you try it"
        )
        oq = (anchor.open_question_stem or "").strip()
        if oq:
            body += f" — apply the same steps to: {oq}"
        else:
            body += " — what's the first step you'd take?"
        return body

    # No authored worked-example JSON, but the lesson's teacher_script
    # may carry the same content in narrative form. Use it as the
    # worked-example body. This is generic across subjects — every
    # ``LessonStep`` has the field; the LLM uses whatever's there.
    ts = (anchor.teacher_script or "").strip()
    if ts:
        body = (
            "Let me walk you through the idea one step at a time:\n\n"
            f"{ts}\n\nNow you try it"
        )
        oq = (anchor.open_question_stem or "").strip()
        if oq:
            body += f" — apply the same thinking to: {oq}"
        else:
            body += " — what's the first thing you'd do?"
        return body

    # Final degradation: decompose the open question (or objective)
    # into a "try just the first step" ask. Still meets the worked-
    # example obligation's spirit: model the *method shape*.
    target = _question_or_objective(anchor)
    if target:
        return (
            "Let's slow this down and work through it one step at a "
            "time. Here's the question again, in plain words:\n\n"
            f"{target}\n\n"
            "Pick just the very first step you'd take, and tell me what "
            "you'd do — no need to finish the whole thing yet."
        )
    return (
        "Let's slow this down. Walk me through what you'd do first if "
        "you were starting this from scratch — just the first step."
    )


def _render_explain_terminal(anchor: MoveAnchor) -> str:
    """Explain move floor.

    Uses ``LessonStep.teacher_script`` when available (lesson-authored
    direct-instruction text). Otherwise restates the objective + open
    question and asks for one concrete reaction.
    """
    ts = _trim_to_sentences(anchor.teacher_script, max_sentences=4)
    if ts:
        body = ts
        oq = (anchor.open_question_stem or "").strip()
        if oq:
            body += (
                f"\n\nWith that in mind, here's the question we're on: "
                f"{oq}\n\nTry one part of it and I'll guide you from there."
            )
        else:
            body += "\n\nTell me which part of that you'd like to start with."
        return body

    target = _question_or_objective(anchor)
    if target:
        return (
            "Here's the idea in plain words: "
            f"{target}\n\nWhich part of this would you like me to break "
            "down further?"
        )
    return (
        "Let's reset on the idea. Tell me one thing you remember about "
        "what we've been working on, and I'll build from there."
    )


def _render_close_terminal(anchor: MoveAnchor) -> str:
    """Close-topic move floor.

    Names what's done and explicitly signals the exit-ticket
    transition. The frontend listens for the "exit ticket" / "set it
    up" cue, and the v2 routing close_topic envelope also surfaces
    the exit-ticket payload now (apps/tutoring/v2/routing.py).
    """
    return (
        "Nice work on this one. You're ready for the exit ticket — "
        "I'll set it up."
    )


# ──────────────────────────────────────────────────────────────────────
# Action floor for moves that don't have their own terminal body
# ──────────────────────────────────────────────────────────────────────


def _action_floor_for_move(
    move: str, anchor: MoveAnchor, next_action_hint: str,
) -> str:
    """Return the concrete next-action sentence after a verdict opener.

    For moves whose contract is "pose one assessment / one scaffold
    step / one extension on the open question", the floor restates
    the open question and asks for a small specific step. For moves
    without an open-question dependency, falls back to the engine's
    ``next_action_hint`` connective (the original behaviour).

    Phase 4 (memory/v2_unverified_trap_redesign.md Fix 3): the raw
    learning-objective string is NEVER substituted into student-facing
    prose. The objective is internal curriculum metadata; verbatim
    substitution leaked system vocabulary ("Students will be able to
    …") into the GEO run-6 T4 fallback. When no open_question is
    available, the action floor uses a generic decompose-the-idea
    phrasing that the active move's prompt is responsible for
    paraphrasing the objective into.
    """
    open_q_moves = (
        "pose_question",
        "scaffold_hint",
        "name_misconception",
        "pivot",
        "confirm_and_extend",
    )
    if move not in open_q_moves:
        # confirm_and_advance + any unrecognised move retain the
        # original connective. They don't carry an open-question
        # restatement obligation.
        return next_action_hint

    oq = (anchor.open_question_stem or "").strip()
    if oq:
        return _pick(_OPEN_Q_ACTION_FLOORS).format(oq=oq)
    # No open question — DO NOT substitute the raw objective. Hand
    # back a subject-agnostic decompose prompt instead.
    return (
        "Let's take one small step on what we're working on. Tell me "
        "the first thing that comes to mind, and we'll build from there."
    )


def _question_or_objective(anchor: MoveAnchor) -> str:
    """Pick the best available "thing to point the student at".

    Phase 4: returns the open-question stem only. The raw objective is
    deliberately NOT used as a fallback — see ``_action_floor_for_move``
    for the rationale (the GEO run-6 T4 LO leak). Callers handle an
    empty return with a generic decompose prompt.
    """
    return (anchor.open_question_stem or "").strip()


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _trim_to_sentences(text: str, *, max_sentences: int) -> str:
    """Keep at most the first ``max_sentences`` sentences of ``text``."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    parts = _SENTENCE_END.split(cleaned)
    kept = parts[: max(1, max_sentences)]
    return " ".join(p.strip() for p in kept if p.strip()).strip()


def _render(prefix: str, next_action: str) -> str:
    """Join the verdict prefix with the next-action slot."""
    prefix = prefix.strip()
    if not next_action:
        return prefix
    return f"{prefix} {next_action.strip()}"


def _annotate(span, template_key: str) -> None:
    """Attach the chosen template key to the span payload."""
    if span is None:
        return
    payload = span.get("payload") or {}
    payload["template_key"] = template_key
    span["payload"] = payload
