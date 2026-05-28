"""Safety gates + per-gate recovery — v2 prune plan §4.4.

Three deterministic gates only — ``safety``, ``figure_ref``,
``answer_leak``. Each gate gets ONE retry with a gate-specific
reminder; if the retry still fails, the response is degraded
(offending span stripped or redacted) and shipped. The response
ALWAYS ships; the frontend never sees a gate-failed error.

This module replaces the deleted ``services/conformance/`` package
+ ``services/templates.py`` + ``services/move_escalation.py``.

The gate functions themselves (``run_safety_check``,
``run_figure_ref_check``, ``run_answer_leak_check``) lift verbatim
from the legacy conformance gates — they were the three keepers per
plan §3. The classifier-driven gates (state_coherence, rule_check,
praise_filter, open_question_stickiness) are gone.

Observability: every gate trigger emits a ``gate.failure`` span with
the gate name + reason + the degradation action taken. The dashboard
surfaces both first-attempt failure rate (gates doing their job) and
second-attempt failure rate (degrade-and-ship — the real quality
alarm; target ≤2% per plan §8).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from apps.tutoring.tracing import emit_span
from apps.tutoring.v2.contracts import GradingResult, Verdict

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# GateResult — small shared shape across the three gates
# ──────────────────────────────────────────────────────────────────────


@dataclass
class GateResult:
    """Result of a single deterministic gate."""

    passed: bool
    name: str
    reason: str = ""
    skipped: bool = False
    payload: dict = field(default_factory=dict)


def _fail(name: str, reason: str, span, payload: Optional[dict] = None) -> GateResult:
    """Mark a span as failed and return a failing GateResult."""
    if span is not None:
        span_payload = span.get("payload") or {}
        span_payload["passed"] = False
        if payload:
            span_payload.update(payload)
        span["payload"] = span_payload
    return GateResult(
        passed=False, name=name, reason=reason, payload=payload or {}
    )


# ──────────────────────────────────────────────────────────────────────
# Safety — wraps the legacy safety judge
# ──────────────────────────────────────────────────────────────────────


def run_safety_check(response_text: str, *, llm_client=None) -> GateResult:
    """Run the child-safety judge on the candidate response."""
    from apps.tutoring.judges.safety import run_safety_judge

    with emit_span("audit", "gate.safety") as span:
        try:
            result = run_safety_judge(
                response_text or "",
                llm_client=llm_client,
            )
        except Exception as exc:
            return GateResult(
                passed=True,
                name="safety",
                skipped=True,
                reason=f"safety judge raised: {type(exc).__name__}",
            )
        flagged = bool(getattr(result, "flagged", False))
        if flagged:
            reason = (
                getattr(result, "reason", None)
                or getattr(result, "category", None)
                or "safety violation"
            )
            return _fail(
                "safety",
                f"safety judge flagged: {reason}",
                span,
                payload={"flagged": True, "reason": str(reason)[:200]},
            )
        return GateResult(passed=True, name="safety")


# ──────────────────────────────────────────────────────────────────────
# Figure-ref — deictic guard + figure_facts quantitative-claim check
# ──────────────────────────────────────────────────────────────────────


_QUANT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:°|degrees|cm|mm|m|km|kg|g|°c|°f|%|/\d+)?\b",
    re.IGNORECASE,
)


def run_figure_ref_check(
    response_text: str,
    *,
    attached_media_count: int = 0,
    figure_facts: Optional[List[str]] = None,
) -> GateResult:
    """Deictic-phrase guard + figure_facts quantitative-claim check.

    1. Deictic phrases ("looking at the diagram", "in the figure")
       with NO attached figure → reject.
    2. Quantitative claims about an attached figure must appear in
       the ``figure_facts`` for that asset.
    """
    from apps.tutoring.judges.figure_ref import run_figure_ref_judge

    with emit_span("audit", "gate.figure_ref") as span:
        leg = run_figure_ref_judge(
            response_text or "",
            attached_media_count=attached_media_count,
        )
        if leg.issues:
            return _fail(
                "figure_ref",
                f"deictic without attached figure: {leg.issues[0]}",
                span,
                payload={"issues": leg.issues, "in_question": leg.in_question},
            )

        if attached_media_count > 0 and figure_facts:
            facts_blob = " ".join(figure_facts).lower()
            claims = [m.group(0) for m in _QUANT_RE.finditer(response_text or "")]
            unmatched: List[str] = []
            for claim in claims[:10]:
                norm = claim.strip().lower()
                if norm in facts_blob:
                    continue
                if re.sub(r"\s+", "", norm) in re.sub(r"\s+", "", facts_blob):
                    continue
                unmatched.append(claim)
            if unmatched:
                return _fail(
                    "figure_ref",
                    f"quantitative claim not in figure_facts: {unmatched[0]!r}",
                    span,
                    payload={"unmatched_claims": unmatched},
                )

        return GateResult(passed=True, name="figure_ref")


# ──────────────────────────────────────────────────────────────────────
# Answer-leak — scoped to wrong/partial + unanswered-open-question turns
# ──────────────────────────────────────────────────────────────────────


def run_answer_leak_check(
    response_text: str,
    *,
    verdict: Optional[GradingResult],
    open_question_stem: str,
    private_canonical: str,
    llm_client=None,
) -> GateResult:
    """Detect tutor revealing the canonical answer to the open question.

    Scope:
      - Skip under verdict=correct (affirmative restatement is fine).
      - Skip when no canonical is available.
      - Otherwise run the legacy detector.
    """
    from apps.tutoring.answer_leak import detect_answer_leak

    with emit_span("audit", "gate.answer_leak") as span:
        if not response_text or not response_text.strip():
            return GateResult(passed=True, name="answer_leak", skipped=True)

        if verdict is not None and verdict.verdict == Verdict.CORRECT:
            return GateResult(
                passed=True, name="answer_leak", skipped=True,
                reason="verdict=correct (affirmative restatement allowed)",
            )

        if not (private_canonical or "").strip():
            return GateResult(
                passed=True, name="answer_leak", skipped=True,
                reason="no canonical available",
            )

        class _BankShim:
            question_text = open_question_stem
            answer = private_canonical
            question_type = "short_answer"
            choices = None
            correct_choice = None
        shim = _BankShim()

        wrong_attempts = 0
        if verdict is not None and verdict.verdict == Verdict.WRONG:
            wrong_attempts = 1

        try:
            res = detect_answer_leak(
                response=response_text,
                bank_question=shim,
                chat_authored_q=None,
                wrong_attempts=wrong_attempts,
                llm_client=llm_client,
            )
        except Exception as exc:
            return GateResult(
                passed=True, name="answer_leak", skipped=True,
                reason=f"detector raised: {type(exc).__name__}",
            )

        if res is None or not getattr(res, "leaked", False):
            return GateResult(passed=True, name="answer_leak")

        return _fail(
            "answer_leak",
            f"answer leak detected: {getattr(res, 'reason', '')[:120]}",
            span,
            payload={"leaked": True, "reason": getattr(res, "reason", "")[:200]},
        )


# ──────────────────────────────────────────────────────────────────────
# run_gates_with_recovery — per-gate one-retry-then-degrade loop
# ──────────────────────────────────────────────────────────────────────


@dataclass
class GateContext:
    """Inputs the gates and recovery loop need beyond the response text."""

    verdict: Optional[GradingResult] = None
    open_question_stem: str = ""
    private_canonical: str = ""
    attached_media_count: int = 0
    figure_facts: List[str] = field(default_factory=list)
    available_figure_descriptions: List[str] = field(default_factory=list)
    posed_via_tool: bool = False
    lesson_has_media: bool = True


@dataclass
class GateFailure:
    """One gate-failure record for the trace span."""

    gate: str
    attempt: int  # 1 = first run, 2 = retry
    reason: str
    degraded: bool  # True when the response was redacted/stripped after retry


@dataclass
class RecoveryResult:
    """Outcome of ``run_gates_with_recovery`` — response always ships."""

    text: str
    failures: List[GateFailure] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return any(f.degraded for f in self.failures)

    @property
    def first_attempt_failure_gates(self) -> List[str]:
        return [f.gate for f in self.failures if f.attempt == 1]

    @property
    def degraded_gates(self) -> List[str]:
        return [f.gate for f in self.failures if f.degraded]


RetryFn = Callable[[str], str]


def _run_gate(gate_name: str, response_text: str, ctx: GateContext) -> GateResult:
    """Run one named gate against the response."""
    if gate_name == "safety":
        # ``figure_ref`` short-circuit per legacy: bank-stem deictic
        # on a lesson with no media is curriculum content, not LLM
        # authorship — handled in the figure_ref branch below.
        return run_safety_check(response_text)
    if gate_name == "figure_ref":
        if ctx.posed_via_tool and not ctx.lesson_has_media:
            return GateResult(
                passed=True,
                name="figure_ref",
                skipped=True,
                reason="bank_stem_deictic_no_media",
            )
        return run_figure_ref_check(
            response_text,
            attached_media_count=ctx.attached_media_count,
            figure_facts=ctx.figure_facts,
        )
    if gate_name == "answer_leak":
        return run_answer_leak_check(
            response_text,
            verdict=ctx.verdict,
            open_question_stem=ctx.open_question_stem,
            private_canonical=ctx.private_canonical,
        )
    raise ValueError(f"unknown gate: {gate_name!r}")


def _reminder_for(gate_name: str, gate_result: GateResult, ctx: GateContext) -> str:
    """Build the gate-specific reminder appended to the retry prompt."""
    if gate_name == "safety":
        return (
            "Your previous reply was flagged by the safety check. "
            "Rewrite the reply without any unsafe, off-topic, or harmful content. "
            "Stay strictly on the lesson's topic. Keep the same teaching move."
        )
    if gate_name == "figure_ref":
        if ctx.available_figure_descriptions:
            figures = "; ".join(
                f"[{i + 1}] {desc[:80]}"
                for i, desc in enumerate(ctx.available_figure_descriptions[:5])
            )
            tail = f" Available figures: {figures}."
        else:
            tail = " No figures are attached to this turn — do not refer to one."
        return (
            "Your previous reply referred to a figure that isn't attached, "
            "or stated a quantitative claim that isn't supported by the "
            "attached figure's facts. Rewrite without that reference."
            + tail
        )
    if gate_name == "answer_leak":
        return (
            "Your previous reply revealed the canonical answer to the open "
            "question. The student needs to derive it themselves. Rewrite "
            "the reply WITHOUT stating, restating, or paraphrasing the "
            "answer; you may ask a smaller-step question or scaffold the "
            "next move."
        )
    return ""


# Sentences/clauses for the degrade pass — basic prose splitter.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\d])")


def _degrade_for(
    gate_name: str,
    response_text: str,
    gate_result: GateResult,
    ctx: GateContext,
) -> str:
    """Strip / redact the offending span from the response.

    Per plan §4.4 table:
      - safety:     redact the offending sentence; ship the rest.
      - figure_ref: strip the sentence containing the deictic/claim.
      - answer_leak: replace the canonical span with ``___``; if that
        fails, strip the leaking sentence.
    """
    text = response_text or ""
    if not text.strip():
        return "Let's keep going — try the next step."

    if gate_name == "safety":
        sentences = _SENTENCE_SPLIT_RE.split(text)
        flagged_terms = []
        reason = (gate_result.payload or {}).get("reason", "") or gate_result.reason
        if isinstance(reason, str) and reason:
            flagged_terms.append(reason.split(":")[-1].strip().lower())
        kept = [
            s for s in sentences
            if not any(t and t in s.lower() for t in flagged_terms)
        ]
        kept_text = " ".join(s for s in kept if s.strip())
        if not kept_text.strip():
            kept_text = "Let's stay on the lesson — try the next step."
        return kept_text

    if gate_name == "figure_ref":
        issues: list[str] = []
        issues.extend((gate_result.payload or {}).get("issues", []) or [])
        issues.extend(
            (gate_result.payload or {}).get("unmatched_claims", []) or []
        )
        if not issues:
            return text
        sentences = _SENTENCE_SPLIT_RE.split(text)
        kept = [
            s for s in sentences
            if not any(issue and issue.lower() in s.lower() for issue in issues)
        ]
        kept_text = " ".join(s for s in kept if s.strip())
        return kept_text or text

    if gate_name == "answer_leak":
        canonical = (ctx.private_canonical or "").strip()
        if canonical and canonical in text:
            return text.replace(canonical, "___")
        # Best-effort: strip any sentence containing the canonical (case
        # insensitive). If we can't isolate, fall through to a short
        # safe line.
        if canonical:
            sentences = _SENTENCE_SPLIT_RE.split(text)
            kept = [
                s for s in sentences
                if canonical.lower() not in s.lower()
            ]
            kept_text = " ".join(s for s in kept if s.strip())
            if kept_text.strip():
                return kept_text
        return "Let's stay with the same question — try the next step yourself."

    return text


_GATE_ORDER: tuple[str, ...] = ("safety", "figure_ref", "answer_leak")


def run_gates_with_recovery(
    response_text: str,
    *,
    ctx: GateContext,
    retry_fn: RetryFn,
) -> RecoveryResult:
    """Run the 3 gates with per-gate one-retry-then-degrade recovery.

    Contract:
      - Returns a ``RecoveryResult`` whose ``text`` ALWAYS ships.
      - For each gate that fails on the candidate, calls
        ``retry_fn(reminder)`` once with a gate-specific reminder. If
        the retried response passes the same gate, the retried text
        becomes the working text and the loop continues with the next
        gate. If the retried response still fails, the offending span
        is stripped/redacted from the working text and the loop
        continues.
      - ``retry_fn`` may raise — exceptions are caught and treated as
        a retry failure, falling through to degradation.

    The retry budget is ONE per gate per turn. Gates are checked in a
    fixed order (safety → figure_ref → answer_leak); a retry triggered
    by gate N is checked against gate N only — gates 1..N-1 are not
    re-checked on the retry (they passed on the prior text). Gate N+1
    runs against whichever text we kept (retried-and-passed,
    retried-and-degraded, or original-and-degraded).
    """
    text = response_text or ""
    failures: list[GateFailure] = []

    for gate_name in _GATE_ORDER:
        result = _run_gate(gate_name, text, ctx)
        if result.passed or result.skipped:
            continue

        with emit_span("audit", "gate.failure") as span:
            if span is not None:
                span["payload"] = {
                    "gate": gate_name,
                    "attempt": 1,
                    "reason": result.reason[:200],
                }
            failures.append(
                GateFailure(
                    gate=gate_name, attempt=1, reason=result.reason, degraded=False,
                )
            )

        reminder = _reminder_for(gate_name, result, ctx)
        try:
            retried = retry_fn(reminder)
        except Exception as exc:
            logger.warning(
                "[safety_gates] retry_fn raised for gate=%s: %s",
                gate_name, type(exc).__name__,
            )
            retried = ""

        retried = (retried or "").strip()
        if not retried:
            # No retry text available — degrade the current text in place.
            text = _degrade_for(gate_name, text, result, ctx)
            failures.append(
                GateFailure(
                    gate=gate_name,
                    attempt=2,
                    reason="retry produced no text",
                    degraded=True,
                )
            )
            with emit_span("audit", "gate.failure") as span:
                if span is not None:
                    span["payload"] = {
                        "gate": gate_name,
                        "attempt": 2,
                        "degraded": True,
                        "reason": "retry produced no text",
                    }
            continue

        retry_result = _run_gate(gate_name, retried, ctx)
        if retry_result.passed or retry_result.skipped:
            # Retry fixed the violation — adopt the retried text.
            text = retried
            continue

        # Retry also failed → degrade and ship.
        text = _degrade_for(gate_name, retried, retry_result, ctx)
        failures.append(
            GateFailure(
                gate=gate_name,
                attempt=2,
                reason=retry_result.reason,
                degraded=True,
            )
        )
        with emit_span("audit", "gate.failure") as span:
            if span is not None:
                span["payload"] = {
                    "gate": gate_name,
                    "attempt": 2,
                    "degraded": True,
                    "reason": retry_result.reason[:200],
                }

    return RecoveryResult(text=text, failures=failures)
