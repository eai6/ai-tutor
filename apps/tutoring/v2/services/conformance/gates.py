"""Deterministic conformance gates — Phase 2 §2.4.

Each gate is a pure-ish function: takes the candidate response + context,
returns a ``GateResult``. No LLM calls live here — the gates that need an
LLM (tutor-claim adjudication, conformance classifier) live in
``classifier.py`` and ``check.py``.

The gates lift from existing legacy modules so behaviour stays identical
where it's already correct:

- ``safety_check``       wraps ``apps/tutoring/judges/safety.py``
- ``answer_leak_check``  wraps ``apps/tutoring/answer_leak.py`` (scoped
  to verdict=wrong/partial + unanswered-open-question turns)
- ``figure_ref_check``   wraps ``apps/tutoring/judges/figure_ref.py``,
  extended for figure_facts quantitative-claim guard
- ``praise_filter``      wraps ``apps/tutoring/praise_filter.py`` (the
  module is now a backward-compat no-op; we keep the rule slot)
- ``state_coherence``    new code — runtime invariant checks
- ``rule_check``         new deterministic code — numeric mutation +
  authored-example provenance (the thin surface §3 deletion table
  keeps from the legacy LLM-based rule judge)

Span emission (per Phase 3 §3.3, owned by Phase 2): each gate emits a
``conformance.<name>`` span via ``apps.tutoring.tracing.emit_span``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from apps.tutoring.tracing import emit_span
from apps.tutoring.v2.contracts import (
    GradingResult,
    SessionRuntimeState,
    Verdict,
)


@dataclass
class GateResult:
    """Result of a single deterministic gate."""

    passed: bool
    name: str
    reason: str = ""
    skipped: bool = False
    payload: dict = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────
# State-coherence gate (new code — replaces the deprecated `history` judge)
# ──────────────────────────────────────────────────────────────────────


def run_state_coherence_check(
    *,
    runtime_state: SessionRuntimeState,
    selected_move: str,
    verdict: Optional[GradingResult],
    allowed_moves: List[str],
) -> GateResult:
    """Validate engine-set values agree with the response context.

    Per Phase 2 §2.4 (replaces the legacy ``history`` judge):
      - ``current_move`` must be one the engine selected (a known move).
      - If a verdict was produced this turn, it must correspond to the
        open question that was being graded.
      - If no ``open_question`` is set, the engine must not be in a
        verdict-bearing state.

    Cheap; runs every turn.
    """
    with emit_span("audit", "conformance.state_coherence") as span:
        # 1. Selected move is in the allowed set.
        if selected_move not in allowed_moves:
            return _fail(
                "state_coherence",
                f"selected_move={selected_move!r} not in allowed_moves",
                span,
            )

        # 2. Verdict / open_question consistency.
        has_verdict = verdict is not None
        has_open = runtime_state.open_question is not None

        if has_verdict and not has_open:
            # We graded something but state has no open question — bug.
            return _fail(
                "state_coherence",
                "verdict produced without open_question in runtime_state",
                span,
            )

        # 3. unverified_run_length must be non-negative and bounded.
        if runtime_state.unverified_run_length < 0:
            return _fail(
                "state_coherence",
                "unverified_run_length is negative",
                span,
            )

        return GateResult(passed=True, name="state_coherence")


# ──────────────────────────────────────────────────────────────────────
# Extended figure-ref gate (lifts legacy + adds figure_facts claim check)
# ──────────────────────────────────────────────────────────────────────


# Numeric / quantitative claim pattern — e.g. "40°", "12 cm", "3.5", "1/2".
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

    Two layers per analysis §3 / Phase 2 §2.4:

    1. **Deictic phrases**: "looking at the diagram", "in the figure"
       etc. with NO attached figure → reject. Lifts the legacy
       ``figure_ref`` judge unchanged.

    2. **Quantitative / spatial claims about an attached figure**:
       when a figure IS attached and the tutor makes a quantitative
       claim ("the angle is 40°", "12 cm to the right"), that claim
       must appear in the ``figure_facts`` for the asset — otherwise
       reject. Extends the legacy judge per §3 deletion table.
    """
    from apps.tutoring.judges.figure_ref import run_figure_ref_judge

    with emit_span("audit", "conformance.figure_ref") as span:
        # Layer 1 — deictic check.
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

        # Layer 2 — only runs when a figure IS attached AND we have
        # facts to compare against. If no facts were extracted at
        # authoring time we conservatively skip (don't fabricate a
        # mismatch).
        if attached_media_count > 0 and figure_facts:
            facts_blob = " ".join(figure_facts).lower()
            claims = [m.group(0) for m in _QUANT_RE.finditer(response_text or "")]
            unmatched: List[str] = []
            for claim in claims[:10]:  # cap to bound the loop
                norm = claim.strip().lower()
                if norm in facts_blob:
                    continue
                # tolerate punctuation / spacing
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
# Numeric mutation + authored-example rule check
# (new deterministic code — §3 deletion table thin surface)
# ──────────────────────────────────────────────────────────────────────


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _extract_numbers(text: str) -> List[str]:
    """Extract numeric tokens. Returns canonical string forms."""
    if not text:
        return []
    out: List[str] = []
    for m in _NUM_RE.finditer(text):
        tok = m.group(0)
        # Normalize trailing ".0".
        if tok.endswith(".0"):
            tok = tok[:-2]
        out.append(tok)
    return out


def run_rule_check(
    response_text: str,
    *,
    open_question_stem: str = "",
    bank_stems: Optional[List[str]] = None,
    recent_student_turns: Optional[List[str]] = None,
) -> GateResult:
    """Numeric mutation + authored-example provenance check.

    Per Phase 2 §2.4 / §3 deletion table — keeps the thin deterministic
    surface from the deprecated LLM-based ``rule`` judge:

    - **Numeric mutation**: a number that appears in ``open_question_stem``
      must not appear in the response in a *different* form (e.g. the
      stem says "40°" but the tutor says "42°"). We cannot detect this
      precisely without alignment, so we use a weaker check: every
      number in the response that *looks* problem-derived (within
      ±20% of any stem number) must equal a stem number exactly.
      Conservative — false negatives are acceptable (the math grader
      catches arithmetic errors anyway).

    - **Authored-example**: any concrete number in the response that
      doesn't appear in (a) the open-question stem, (b) recent student
      turns, or (c) the supplied ``bank_stems`` is flagged as
      potentially authored. Cap at 10 candidates to bound the loop.

    Skip when ``response_text`` is empty or contains no numbers.
    """
    bank_stems = bank_stems or []
    recent_student_turns = recent_student_turns or []

    with emit_span("audit", "conformance.rule_check") as span:
        response_nums = _extract_numbers(response_text)
        if not response_nums:
            return GateResult(
                passed=True, name="rule_check", skipped=True
            )

        # Build the set of allowed numbers from every visible source.
        allowed: set[str] = set()
        for src in [open_question_stem, *bank_stems, *recent_student_turns]:
            for n in _extract_numbers(src):
                allowed.add(n)

        # Tolerate small whole-number constants (0..9) — these are
        # universally allowed as conversational artifacts.
        for i in range(10):
            allowed.add(str(i))

        # Authored-example detection.
        unauthored: List[str] = []
        for n in response_nums[:20]:
            if n in allowed:
                continue
            if n.lstrip("-") in allowed:
                continue
            unauthored.append(n)
            if len(unauthored) >= 3:
                break

        if unauthored:
            return _fail(
                "rule_check",
                f"authored numbers not in any visible source: {unauthored}",
                span,
                payload={"unauthored": unauthored},
            )

        return GateResult(passed=True, name="rule_check")


# ──────────────────────────────────────────────────────────────────────
# Safety pre-screen (wraps legacy judge — kept verbatim in role)
# ──────────────────────────────────────────────────────────────────────


def run_safety_check(response_text: str, *, llm_client=None) -> GateResult:
    """Lift ``apps/tutoring/judges/safety.py`` as the safety floor.

    Per §3 deletion table the safety judge is **kept verbatim**.
    """
    from apps.tutoring.judges.safety import run_safety_judge

    with emit_span("audit", "conformance.safety") as span:
        try:
            result = run_safety_judge(
                response_text or "",
                llm_client=llm_client,
            )
        except Exception as exc:  # belt-and-braces; never block a turn
            return GateResult(
                passed=True,
                name="safety",
                skipped=True,
                reason=f"safety judge raised: {type(exc).__name__}",
            )
        # The legacy safety judge marks `flagged=True` on violations
        # and surfaces a reason string. If the result lacks those
        # fields (older shape) we conservatively pass.
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
# Answer-leak (scoped to wrong/partial + unanswered-open-question turns)
# ──────────────────────────────────────────────────────────────────────


def run_answer_leak_check(
    response_text: str,
    *,
    verdict: Optional[GradingResult],
    open_question_stem: str,
    private_canonical: str,
    llm_client=None,
) -> GateResult:
    """Lifts ``apps/tutoring/answer_leak.py`` as a scoped conformance check.

    Scope per analysis §3:
      - Runs under verdict=``wrong``, verdict=``partial``, and any turn
        with an unanswered open question.
      - Under verdict=``correct``, affirmative restatement of the
        canonical is allowed — skip.
      - Under verdict=``unverified``, the canonical is unknown by
        definition — skip.

    The legacy detector takes a ``bank_question`` row; here we adapt by
    constructing a minimal duck-typed object carrying the canonical so
    the detector's LLM check sees the right surface.
    """
    from apps.tutoring.answer_leak import detect_answer_leak

    with emit_span("audit", "conformance.answer_leak") as span:
        if not response_text or not response_text.strip():
            return GateResult(passed=True, name="answer_leak", skipped=True)

        # Scope: skip under correct / unverified.
        if verdict is not None and verdict.verdict == Verdict.CORRECT:
            return GateResult(
                passed=True, name="answer_leak", skipped=True,
                reason="verdict=correct (affirmative restatement allowed)",
            )
        if verdict is not None and verdict.verdict == Verdict.UNVERIFIED:
            return GateResult(
                passed=True, name="answer_leak", skipped=True,
                reason="verdict=unverified (no canonical to leak)",
            )

        if not (private_canonical or "").strip():
            return GateResult(
                passed=True, name="answer_leak", skipped=True,
                reason="no canonical available",
            )

        # Build a minimal bank-question-like object the detector accepts.
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
# Praise filter (kept as a conformance rule — module is now a no-op
# shim but the slot is preserved per the analysis §3 deletion table)
# ──────────────────────────────────────────────────────────────────────


_BARE_PRAISE_OPENERS = (
    "correct!", "right!", "yes!", "exactly!", "perfect!",
    "you nailed it", "spot on", "well done!", "amazing!",
    "you got it!", "brilliant!", "exactly.", "right.",
)


def run_praise_filter(
    response_text: str,
    *,
    verdict: Optional[GradingResult],
) -> GateResult:
    """Strip bare-praise openers under every non-``correct`` verdict.

    Lifts the *role* of ``apps/tutoring/praise_filter.py`` (which is now
    a backward-compat no-op shim per its module docstring) — we keep a
    deterministic praise-opener detector here so conformance can reject
    the response and force a rewrite rather than depending on the
    stripper.

    Per analysis §3: runs under every non-``correct`` verdict.
    """
    with emit_span("audit", "conformance.praise_filter") as span:
        if verdict is None or verdict.verdict == Verdict.CORRECT:
            return GateResult(
                passed=True, name="praise_filter", skipped=True,
                reason="verdict=correct (praise allowed) or no verdict",
            )
        text = (response_text or "").strip().lower()
        if not text:
            return GateResult(passed=True, name="praise_filter", skipped=True)
        for opener in _BARE_PRAISE_OPENERS:
            if text.startswith(opener):
                return _fail(
                    "praise_filter",
                    f"bare praise opener under verdict={verdict.verdict.value}: {opener!r}",
                    span,
                    payload={"opener": opener},
                )
        return GateResult(passed=True, name="praise_filter")


# ──────────────────────────────────────────────────────────────────────
# Open-question stickiness — safety floor for the scaffold/probe moves
# ──────────────────────────────────────────────────────────────────────


def run_open_question_stickiness_check(
    *,
    selected_move: str,
    runtime_state: SessionRuntimeState,
    pending_pose,  # PendingPose | None — imported lazily to avoid cycles
) -> GateResult:
    """Safety floor: a *stay-on-item* move must keep the open question live.

    Background. Across both MATHS-S1 and GEO-S5 evaluations, the
    dominant remaining drift after the run-3 prompt tightening was:
    the LLM commits a NEW pose (different ``bank_id``) while the
    student's open question is still live and below the ``pivot``
    attempt threshold. The strengthened SCAFFOLD_HINT prompt reduced
    but did not eliminate this.

    This gate is the safety floor that catches the drift after the
    fact. It does not change move selection (the engine still picked
    ``scaffold_hint`` / ``name_misconception`` / etc.) and it does not
    pre-route. On rejection, the existing retry / safe-terminal path
    handles the response — the per-move terminal restates the open
    question, which is exactly the recovery the principle asks for
    (Science of learning principle: Targeted Remediation — stay on
    the same item, scaffold the path, do not change the question).

    Scope (when the gate is active):
      * selected_move is one of the *stay-on-item* moves whose contract
        explicitly keeps the open question live: ``scaffold_hint``,
        ``name_misconception``, ``pose_question`` (when the engine
        intends to re-pose the same item).
      * runtime_state.open_question is set.
      * pending_pose is set (the LLM used the tool channel this turn).

    Out of scope (explicitly NOT a stay-on-item move):
      * ``confirm_and_extend`` — by contract this move ADVANCES after a
        correct answer (Deliberate Practice — keep the next
        problem at the edge of ability, not the middle). The "twist" is
        a new bank slot on the same concept; treating it as
        stay-on-item produced the GEO-S5 P1 cascade where every correct
        rich answer fell back to a "let's slow down" terminal.
      * ``pivot`` is meant to introduce a new item.
      * ``close_topic`` / ``worked_example`` / ``explain`` advance or
        teach; they are not probe-shaped.

    When all three "active scope" conditions hold AND the pending_pose's
    ``(source, id)`` differs from the open question's ``(source, id)``,
    the gate fails. Other cases are skipped (turn with no tool call has
    nothing to check; the opening turn of a session has no open
    question).
    """
    with emit_span("audit", "conformance.open_question_stickiness") as span:
        # IMPORTANT: ``confirm_and_extend`` is NOT in this list. Its
        # contract is to ADVANCE on a correct answer (Deliberate
        # Practice — keep the next problem at the edge of ability).
        # Including it forced every correct-rich-answer turn into the
        # "let's slow down" terminal (run-5 GEO-S5 P1 finding).
        probe_moves = (
            "scaffold_hint",
            "name_misconception",
            "pose_question",
        )
        if selected_move not in probe_moves:
            return GateResult(
                passed=True,
                name="open_question_stickiness",
                skipped=True,
                reason=f"move={selected_move} out of scope",
            )
        open_q = runtime_state.open_question
        if open_q is None:
            return GateResult(
                passed=True,
                name="open_question_stickiness",
                skipped=True,
                reason="no open question",
            )
        if pending_pose is None:
            return GateResult(
                passed=True,
                name="open_question_stickiness",
                skipped=True,
                reason="no tool-call pose this turn",
            )
        pose_ref = pending_pose.question_ref
        if pose_ref.source == open_q.source and pose_ref.id == open_q.id:
            return GateResult(
                passed=True,
                name="open_question_stickiness",
            )
        # Drift detected. Reject so the retry / terminal path can run.
        return _fail(
            "open_question_stickiness",
            (
                f"{selected_move} posed a new item "
                f"({pose_ref.source}:{pose_ref.id}) while the open "
                f"question ({open_q.source}:{open_q.id}) is still live; "
                "stay on the same item or use the pivot move"
            ),
            span,
            payload={
                "selected_move": selected_move,
                "open_source": open_q.source,
                "open_id": open_q.id,
                "posed_source": pose_ref.source,
                "posed_id": pose_ref.id,
            },
        )


# ──────────────────────────────────────────────────────────────────────
# Internal helper
# ──────────────────────────────────────────────────────────────────────


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
