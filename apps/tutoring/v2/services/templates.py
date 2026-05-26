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
"""

from __future__ import annotations

from typing import Optional

from apps.tutoring.tracing import emit_span
from apps.tutoring.v2.contracts import GradingResult, Verdict


def render_safe_template(
    *,
    verdict: Optional[GradingResult],
    student_claim_present: bool = False,
    next_action_text: str = "",
) -> str:
    """Render a safe terminal template for the given verdict.

    The five templates per analysis §3:

      - correct       → "Yes — [affirmation]. [next action]"
      - partial       → "You've got part of it: [what_right]. What's
                        still missing: [what_missing]. [next action]"
      - wrong         → "Not quite. [first_misconception_redacted].
                        [next action]"
      - unverified    → "I want to check that with you before I'm sure
                        either way. [next action]"
      - no-verdict + student_claim_present →
                        "Let's check that together rather than guess.
                        [next action]"

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
                rendered = _render(
                    "Let's check that together rather than guess.",
                    next_action,
                )
            else:
                # Truly no-verdict and no claim → fall back to a
                # neutral hand-the-floor-back template. Conformance
                # should not have triggered here, but the safety floor
                # must cover every code path.
                template_key = "no_verdict_neutral"
                rendered = _render(
                    "Let's pick this back up together.",
                    next_action,
                )
            _annotate(span, template_key)
            return rendered

        safe = verdict.student_safe_feedback
        kind = verdict.verdict

        if kind == Verdict.CORRECT:
            template_key = "correct"
            affirmation = (safe.what_right or "you got it").strip()
            rendered = _render(f"Yes — {affirmation}.", next_action)

        elif kind == Verdict.PARTIAL:
            template_key = "partial"
            what_right = (safe.what_right or "you've got part of the idea").strip()
            what_missing = (safe.what_missing or "let's look at what's still missing").strip()
            rendered = _render(
                f"You've got part of it: {what_right}. What's still missing: {what_missing}.",
                next_action,
            )

        elif kind == Verdict.WRONG:
            template_key = "wrong"
            misc = (safe.first_misconception_redacted or "let's look again together").strip()
            rendered = _render(f"Not quite. {misc}.", next_action)

        elif kind == Verdict.UNVERIFIED:
            template_key = "unverified"
            rendered = _render(
                "I want to check that with you before I'm sure either way.",
                next_action,
            )

        else:
            # Shouldn't happen — Verdict enum is exhaustive — but cover
            # the case defensively.
            template_key = "unknown_verdict"
            rendered = _render("Let's pick this back up together.", next_action)

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
