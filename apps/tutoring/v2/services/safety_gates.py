"""Safety gates + per-gate recovery — v2 prune plan §4.4.

Four deterministic gates — ``curriculum_fidelity``, ``safety``,
``figure_ref``, ``answer_leak``. Each gate gets ONE retry with a
gate-specific reminder; if the retry still fails, the response is
degraded (offending span stripped or redacted) and shipped. The
response ALWAYS ships; the frontend never sees a gate-failed error.

This module replaces the deleted ``services/conformance/`` package
+ ``services/templates.py`` + ``services/move_escalation.py``.

The gate functions ``run_safety_check``, ``run_figure_ref_check``,
``run_answer_leak_check`` lift verbatim from the legacy conformance
gates — they were the three keepers per plan §3. The
``run_curriculum_fidelity_check`` gate was added 2026-05-28 to
enforce the curriculum-fidelity contract
(``memory/curriculum_fidelity_principle.md``): all assessable
questions must go through the ``pose_question`` tool against
bank-authored ``LessonStep`` rows; the tutor must never author
verifiable questions in prose.

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
from apps.tutoring.v2.services.conformance_check import (
    _last_question_sentence,
    find_verifiable_prose_questions,
    is_verifiable_prose_question,
    strip_trailing_tool_stem,
)

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
# Curriculum-fidelity — block verifiable prose Qs that bypass the tool
# ──────────────────────────────────────────────────────────────────────


def run_curriculum_fidelity_check(
    response_text: str,
    *,
    selected_move: str,
    posed_via_tool: bool,
    pose_tool_stem: str = "",
) -> GateResult:
    """Enforce one-question-per-turn with bank-authored provenance.

    Per ``memory/curriculum_fidelity_principle.md`` — all assessable
    questions must go through the ``pose_question`` tool against
    bank-authored ``LessonStep`` rows. The tutor's LLM-authored prose
    must contain ZERO verifiable questions, regardless of whether
    the tool also fired.

    Unified two-stage detection:

    1. Strip the tool-appended stem (no-op when no tool fired) and
       scan EVERY '?'-ending sentence in the LLM-authored prose. Any
       sentence matching an explicit verifiable pattern is a
       violation. Multi-violation reporting: all offending sentences
       are surfaced in the payload so the retry reminder names them
       all and the degrade pass can strip them all in one shot.

    2. (No-tool path only) If stage 1 found nothing but the response
       ends with '?' and the trailing sentence is unclassified, fall
       back to precision-favoring trailing-only detection. The tool-
       fired path skips this fallback — the bank stem is already a
       valid assessment, so unclassified prose Qs there are more
       likely conversational rhetoric than missed assessments.

    Skip on terminal move (``close_topic``) — no assessment authored.
    """
    move = (selected_move or "").strip()
    with emit_span("audit", "gate.curriculum_fidelity") as span:
        if move == "close_topic":
            return GateResult(
                passed=True,
                name="curriculum_fidelity",
                skipped=True,
                reason="terminal_move",
            )

        prose_only = strip_trailing_tool_stem(
            response_text, pose_tool_stem,
        )
        offending = find_verifiable_prose_questions(prose_only)
        if offending:
            offending_clip = [s[:200] for s in offending]
            reason_label = (
                "stacked prose question alongside tool-posed stem"
                if posed_via_tool
                else "verifiable prose question authored by tutor"
            )
            primary = offending[0]
            return _fail(
                "curriculum_fidelity",
                f"{reason_label} "
                f"({len(offending)} found): {primary[:120]!r}",
                span,
                payload={
                    "offending_questions": offending_clip,
                    # ``trailing_question`` retained for backward-compat
                    # with the existing observability schema; equals the
                    # first offending sentence.
                    "trailing_question": offending_clip[0],
                    "move": move,
                    "stacked_with_tool": posed_via_tool,
                    "match_count": len(offending),
                },
            )

        # No explicit verifiable patterns matched. In the no-tool path
        # only, fall back to precision-favoring trailing-only detection
        # — an unclassified trailing Q in a no-tool turn is the silent-
        # skip risk (Path A) and we'd rather false-positive than miss.
        if not posed_via_tool and is_verifiable_prose_question(response_text):
            trailing = _last_question_sentence(response_text)
            return _fail(
                "curriculum_fidelity",
                f"unclassified trailing question on no-tool turn: "
                f"{trailing[:120]!r}",
                span,
                payload={
                    "offending_questions": [trailing[:200]],
                    "trailing_question": trailing[:200],
                    "move": move,
                    "stacked_with_tool": False,
                    "match_count": 1,
                    "unclassified_fallback": True,
                },
            )

        return GateResult(passed=True, name="curriculum_fidelity")


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
    # Selected move name (e.g. ``explain``, ``confirm_and_advance``).
    # Threaded so the curriculum_fidelity gate can skip on
    # ``close_topic`` (the only terminal move) and the dashboard can
    # attribute gate failures to the originating move.
    selected_move: str = ""
    # The tool-appended bank stem, when ``posed_via_tool`` is True.
    # Used by the curriculum_fidelity gate to strip the stem from the
    # response before scanning the LLM-authored prose for stacked
    # verifiable questions (Path C — MATHS run-11 §3 R1).
    pose_tool_stem: str = ""


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
    if gate_name == "curriculum_fidelity":
        return run_curriculum_fidelity_check(
            response_text,
            selected_move=ctx.selected_move,
            posed_via_tool=ctx.posed_via_tool,
            pose_tool_stem=ctx.pose_tool_stem,
        )
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
    if gate_name == "curriculum_fidelity":
        payload = gate_result.payload or {}
        stacked = bool(payload.get("stacked_with_tool", False))
        offending: list[str] = payload.get("offending_questions") or []
        if not offending:
            # Backward-compat: older payload shape used only
            # ``trailing_question``. Wrap it into a single-element list.
            trailing = (payload.get("trailing_question") or "").strip()
            if trailing:
                offending = [trailing]
        if len(offending) == 1:
            flagged_clause = (
                f' The flagged prose question was: "{offending[0][:160].rstrip()}".'
            )
        elif len(offending) > 1:
            quoted = "; ".join(
                f'"{q[:120].rstrip()}"' for q in offending[:5]
            )
            extra = (
                f" (and {len(offending) - 5} more)"
                if len(offending) > 5 else ""
            )
            flagged_clause = (
                f" The flagged prose questions ({len(offending)} "
                f"found) were: {quoted}{extra}. Remove ALL of them."
            )
        else:
            flagged_clause = ""
        if stacked:
            return (
                "Your previous reply already posed the assessment via "
                "the pose_question tool, but the LLM-authored prose "
                "ALSO asked a question with a single canonical answer. "
                "Under the curriculum-fidelity contract there is ONE "
                "assessment per turn — the tool's bank stem IS that "
                "assessment. The prose lead-in must contain ZERO "
                "additional verifiable questions (no named options, "
                "no numeric values, no yes/no, no closed-set picks)."
                + flagged_clause +
                " Rewrite the reply with the SAME tool call, but "
                "remove the verifiable question from the prose. "
                "Reflective or scene-setting prose is fine; "
                "verifiable prose questions are not. Keep the same "
                "teaching move."
            )
        return (
            "Your previous reply ended with a question that has a "
            "single canonical answer (a named option, a numeric "
            "value, a yes/no fact, or a closed-set pick). Under the "
            "curriculum-fidelity contract, all assessable questions "
            "go through the pose_question tool against bank-authored "
            "lesson questions — the tutor must NEVER author a "
            "verifiable question in prose."
            + flagged_clause +
            " Rewrite the reply so it ends in ONE of the following "
            "three ways: (a) close the explanation with no trailing "
            "question — the next turn will pose via the tool; OR "
            "(b) call pose_question now to pose the assessment via "
            "the tool; OR (c) end with a genuinely reflective prompt "
            "that has NO single correct answer "
            "(e.g. 'what do you already know about X?', "
            "'which of these matches your intuition?', "
            "'have you seen this near you?'). Keep the same teaching "
            "move; only the trailing question changes."
        )
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
      - curriculum_fidelity: strip the trailing verifiable prose Q.
        The framing remains; the next turn's router picks Rule 6
        (no open_question pending) and poses via the tool.
      - safety:     redact the offending sentence; ship the rest.
      - figure_ref: strip the sentence containing the deictic/claim.
      - answer_leak: replace the canonical span with ``___``; if that
        fails, strip the leaking sentence.
    """
    text = response_text or ""
    if not text.strip():
        return "Let's keep going — try the next step."

    if gate_name == "curriculum_fidelity":
        payload = gate_result.payload or {}
        offending: list[str] = payload.get("offending_questions") or []
        if not offending:
            trailing_payload = (payload.get("trailing_question") or "").strip()
            if trailing_payload:
                offending = [trailing_payload]
        # Re-derive against the current degrade-pass text in case the
        # response on a retry-failed attempt has slightly different
        # sentence boundaries.
        if not offending:
            trailing = _last_question_sentence(text)
            if trailing:
                offending = [trailing]
        if not offending:
            return text
        stripped = text
        for offender in offending:
            offender = (offender or "").strip()
            if not offender:
                continue
            if offender in stripped:
                stripped = stripped.replace(offender, "", 1)
                continue
            # Best-effort: drop the sentence containing the offender's
            # first 40 characters (handles minor whitespace drift
            # between detection-time text and degrade-time text).
            needle = offender[:40].strip()
            if needle and needle in stripped:
                start = stripped.find(needle)
                # Walk forward to the next sentence boundary.
                end = start + len(needle)
                while end < len(stripped) and stripped[end] not in ".!?\n":
                    end += 1
                if end < len(stripped):
                    end += 1
                stripped = (stripped[:start] + stripped[end:])
        # Tidy up: collapse multiple blank lines and trim trailing
        # whitespace. Do not touch the bank stem at the end (when
        # ``posed_via_tool=True``) — only the LLM-authored prose was
        # mutated.
        stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
        if not stripped:
            return "Let's keep going — we'll work through the next step together."
        return stripped

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


_GATE_ORDER: tuple[str, ...] = (
    "curriculum_fidelity",
    "safety",
    "figure_ref",
    "answer_leak",
)


def run_gates_with_recovery(
    response_text: str,
    *,
    ctx: GateContext,
    retry_fn: RetryFn,
    gates: Optional[tuple[str, ...]] = None,
) -> RecoveryResult:
    """Run the gates with per-gate one-retry-then-degrade recovery.

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
    fixed order; a retry triggered by gate N is checked against gate
    N only — gates 1..N-1 are not re-checked on the retry (they
    passed on the prior text). Gate N+1 runs against whichever text
    we kept (retried-and-passed, retried-and-degraded, or
    original-and-degraded).

    ``gates`` overrides the module-level ``_GATE_ORDER`` when caller
    wants to run a subset (e.g. ``start_session`` runs only the
    curriculum_fidelity gate because no canonical / figure / verdict
    exists at opener time).
    """
    text = response_text or ""
    failures: list[GateFailure] = []

    gate_order = gates if gates is not None else _GATE_ORDER

    for gate_name in gate_order:
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
