"""Safe terminal templates — Phase 2 §2.5.

Five verdict-keyed templates per refactor-analysis §3. Drawn from
``GradingResult.student_safe_feedback`` (never ``private_canonical``).
The "next action" slot is filled from the next action ``TutorEngine``
would have selected — passed in by the caller.

When conformance retry still fails, the response is replaced by a
deterministic verdict-keyed template — never released free-form.
Templates are the **safety floor**, not the default path; the
``template.fallback`` span + ``SessionTurn.metadata.fallback_used =
true`` rollup make the trigger rate a tunable quality signal.

Voice rules (mirror move_prompts.py SHARED_PREAMBLE):
- Student-facing language only — no system vocabulary ("transcript",
  "verdict", "grader", "I couldn't verify from the transcript").
- Each verdict branch picks from a small rotation so the templates
  don't sound like the same scripted line every time.
"""

from __future__ import annotations

import random
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
) -> str:
    """Render a safe terminal template for the given verdict.

    Emits a ``template.fallback`` span so the trigger rate can be
    monitored (Phase 3 dashboards).
    """
    with emit_span("audit", "template.fallback") as span:
        next_action = (next_action_text or "").strip()

        # No-verdict + student_claim_present branch (covers the case
        # where the grader produced nothing this turn but the student
        # made a factual assertion the tutor would otherwise jump on).
        if verdict is None:
            if student_claim_present:
                template_key = "no_verdict_student_claim"
                rendered = _render(_pick(_NO_VERDICT_STUDENT_CLAIM_OPENERS), next_action)
            else:
                # Truly no-verdict and no claim → fall back to a
                # neutral hand-the-floor-back template. Conformance
                # should not have triggered here, but the safety floor
                # must cover every code path.
                template_key = "no_verdict_neutral"
                rendered = _render(_pick(_NO_VERDICT_NEUTRAL_OPENERS), next_action)
            _annotate(span, template_key)
            return rendered

        safe = verdict.student_safe_feedback
        kind = verdict.verdict

        if kind == Verdict.CORRECT:
            template_key = "correct"
            affirmation = (safe.what_right or "you got it").strip()
            rendered = _render(
                _pick(_CORRECT_AFFIRMATIONS).format(affirmation=affirmation),
                next_action,
            )

        elif kind == Verdict.PARTIAL:
            template_key = "partial"
            what_right = (safe.what_right or "you've got part of the idea").strip()
            what_missing = (safe.what_missing or "let's look at what's still missing").strip()
            rendered = _render(
                _PARTIAL_TEMPLATE.format(what_right=what_right, what_missing=what_missing),
                next_action,
            )

        elif kind == Verdict.WRONG:
            template_key = "wrong"
            misc = (safe.first_misconception_redacted or "let's look again together").strip()
            rendered = _render(_pick(_WRONG_OPENERS).format(misc=misc), next_action)

        elif kind == Verdict.UNVERIFIED:
            template_key = "unverified"
            rendered = _render(_pick(_UNVERIFIED_OPENERS), next_action)

        else:
            # Shouldn't happen — Verdict enum is exhaustive — but cover
            # the case defensively.
            template_key = "unknown_verdict"
            rendered = _render(_pick(_NO_VERDICT_NEUTRAL_OPENERS), next_action)

        _annotate(span, template_key)
        return rendered


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
