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
    contains_mcq_option_block,
    find_non_reflective_prose_questions,
    find_prose_stem_duplicates,
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

    Detection (asymmetric by tool-fired vs no-tool):

    1. Tool fired (``posed_via_tool == True``). Strip the tool-
       appended stem and scan the LLM-authored prose for ANY
       non-reflective '?'-ending sentence. Verifiable Qs and
       unclassified Qs both count as offenders — one question per
       turn is the rule, the bank stem is that question, and any
       additional prose Q (Socratic, rhetorical, or otherwise) is
       stacking. Only sentences matching an explicit reflective
       pattern are allowed through. The Map Scale L1425 live
       verification on 2026-05-28 surfaced a Socratic
       "what does that tell you about which …" construction that
       the explicit verifiable patterns did not catch — moving to
       non-reflective detection here closes that recall gap and
       aligns the gate with the SHARED_PREAMBLE "Mid-move pose
       dedup" rule, which already promised the LLM that prose ``?``
       alongside a tool call is rejected.

    2. No tool fired (``posed_via_tool == False``). Trailing-only
       precision-favoring detection. The trailing question is the
       LLM's authored assessment (Path A — the screenshot case).
       Verifiable trailing → fail. Reflective trailing → pass.
       Unclassified trailing → fail (precision-favoring fallback).
       Non-trailing prose Qs on no-tool turns are not flagged here —
       only the trailing Q drives student input on the next turn.

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

        if posed_via_tool:
            prose_only = strip_trailing_tool_stem(
                response_text, pose_tool_stem,
            )
            offending = find_non_reflective_prose_questions(prose_only)
            if not offending:
                return GateResult(passed=True, name="curriculum_fidelity")
            offending_clip = [s[:200] for s in offending]
            primary = offending[0]
            return _fail(
                "curriculum_fidelity",
                f"stacked prose question alongside tool-posed stem "
                f"({len(offending)} found): {primary[:120]!r}",
                span,
                payload={
                    "offending_questions": offending_clip,
                    "trailing_question": offending_clip[0],
                    "move": move,
                    "stacked_with_tool": True,
                    "match_count": len(offending),
                },
            )

        # No-tool path: trailing-only, precision-favoring.
        if not is_verifiable_prose_question(response_text):
            return GateResult(passed=True, name="curriculum_fidelity")
        trailing = _last_question_sentence(response_text)
        return _fail(
            "curriculum_fidelity",
            f"trailing prose question is verifiable (no tool pose): "
            f"{trailing[:120]!r}",
            span,
            payload={
                "offending_questions": [trailing[:200]],
                "trailing_question": trailing[:200],
                "move": move,
                "stacked_with_tool": False,
                "match_count": 1,
            },
        )


# ──────────────────────────────────────────────────────────────────────
# Stem duplication — LLM-authored prose copies the bank stem verbatim
# ──────────────────────────────────────────────────────────────────────


def run_stem_duplication_check(
    response_text: str,
    *,
    selected_move: str,
    posed_via_tool: bool,
    pose_tool_stem: str,
) -> GateResult:
    """Detect prose that duplicates the tool-posed bank stem.

    Failure mode surfaced on Map Scale L1425 session 123 T2
    (2026-05-28): the LLM authored the full bank stem as a STATEMENT
    in its prose AND also called the ``pose_question`` tool. The
    engine then appended the tool stem again — the student saw the
    stem twice (LLM-copy then engine-appended), with a degraded
    artifact paren ``)`` between them when the curriculum_fidelity
    gate's degrade pass had also trimmed a "(True or False?" tail
    from the LLM-copy.

    Detection lives in :func:`find_prose_stem_duplicates` — verbatim
    contiguous substring match between the LLM-authored prose and
    the tool-emitted stem text (with answer-shape suffixes like
    "(True or False?)" and ``A) ... B) ...`` stripped from the stem
    side first; those have their own detection via the
    curriculum_fidelity gate's question-pattern scan).

    Skip conditions:
      - ``posed_via_tool == False`` — no stem to duplicate.
      - ``pose_tool_stem`` is empty — same.
      - Terminal move (``close_topic``) — no assessment authored.
    """
    move = (selected_move or "").strip()
    with emit_span("audit", "gate.stem_duplication") as span:
        if move == "close_topic":
            return GateResult(
                passed=True,
                name="stem_duplication",
                skipped=True,
                reason="terminal_move",
            )
        if not posed_via_tool:
            return GateResult(
                passed=True,
                name="stem_duplication",
                skipped=True,
                reason="no_tool_pose",
            )
        if not (pose_tool_stem or "").strip():
            return GateResult(
                passed=True,
                name="stem_duplication",
                skipped=True,
                reason="empty_tool_stem",
            )
        prose_only = strip_trailing_tool_stem(
            response_text, pose_tool_stem,
        )
        dups = find_prose_stem_duplicates(prose_only, pose_tool_stem)
        if not dups:
            return GateResult(passed=True, name="stem_duplication")
        primary = dups[0]
        return _fail(
            "stem_duplication",
            f"prose duplicates tool-posed stem "
            f"({len(primary)} chars): {primary[:120]!r}",
            span,
            payload={
                "duplicated_substrings": [d[:300] for d in dups],
                "match_chars": len(primary),
                "move": move,
            },
        )


# ──────────────────────────────────────────────────────────────────────
# One-question-per-turn — deterministic floor + Haiku extractor ceiling
# ──────────────────────────────────────────────────────────────────────


def run_one_question_check(
    response_text: str,
    *,
    selected_move: str,
    posed_via_tool: bool,
    pose_tool_stem: str = "",
    llm_client=None,
) -> GateResult:
    """Enforce ONE action prompt per turn + the active-end rule.

    Belt-and-suspenders (open_question_authority_redesign.md §7 step 5),
    chosen over deterministic-only because a regex scan optimises for one
    bug class (stacked '?'/MCQ) and misses what an LLM generalises to:

      1. **Deterministic floor (runs first, no LLM):** scan the LLM-
         authored prose for ≥2 non-reflective '?'-sentences, or an MCQ
         option block (which ends on an option line, not '?', so the
         trailing-'?' scan misses it). Catches the session-100 T1560 /
         image-#2 buried-MCQ shapes the dormant Haiku extractor provably
         missed. On a tool-posed turn the bank stem is the ONE allowed
         prompt, so the prose must add zero; on a no-tool turn one prose
         prompt is the turn's single (perceived-and-graded) assessment.

      2. **Haiku extractor ceiling (runs when the floor passes):**
         generalises to action prompts the regex cannot see — imperatives
         ("now you try"), fill-ins, retrieval asks — via ``action_count``,
         and enforces the active-end rule (Active Learning Ch.10) via
         ``has_active_end``. Fail-soft: extractor unavailable → the
         deterministic floor's verdict stands (never blocks on LLM error).

    Principle #5 Minimising Cognitive Load (Ch.14) — one idea per turn.
    Skips the active-end requirement on ``close_topic`` (it legitimately
    ends the scope without a new ask); the stacking check still runs.
    """
    move = (selected_move or "").strip()
    with emit_span("audit", "gate.one_question_per_turn") as span:
        # The bank stem (when tool-posed) is the ONE allowed assessment;
        # scan only the LLM-authored prose for ADDITIONAL prompts.
        prose = (
            strip_trailing_tool_stem(response_text, pose_tool_stem)
            if posed_via_tool else response_text
        )
        non_reflective = find_non_reflective_prose_questions(prose)
        has_mcq_block = contains_mcq_option_block(prose)
        # Prompt count in the prose: each non-reflective '?'-sentence is
        # one; an MCQ block with no '?' stem counts as one on its own.
        prose_prompts = max(len(non_reflective), 1 if has_mcq_block else 0)
        # Allowance: tool-posed → 0 extra prose prompts (bank stem is the
        # one); no-tool → 1 prose prompt is the turn's single assessment.
        allowed = 0 if posed_via_tool else 1

        if prose_prompts > allowed:
            offenders = list(non_reflective)
            if has_mcq_block:
                offenders.append("<MCQ option block authored in prose>")
            offenders_clip = [s[:200] for s in offenders] or ["<extra prompt>"]
            return _fail(
                "one_question_per_turn",
                f"stacked action prompts ({prose_prompts} in prose, "
                f"{allowed} allowed): {offenders_clip[0][:120]!r}",
                span,
                payload={
                    "kind": "stacked",
                    "offending_questions": offenders_clip,
                    "trailing_question": offenders_clip[0],
                    "move": move,
                    "match_count": prose_prompts,
                },
            )

        # Deterministic floor passed — ask Haiku for the broader classes.
        from apps.tutoring.v2.services.question_extractor import (
            extract_action_prompts,
        )
        extracted = extract_action_prompts(
            tutor_text=response_text,
            selected_move=move,
            llm_client=llm_client,
        )
        if extracted is None:
            # Fail-soft: extractor unavailable → floor verdict stands.
            return GateResult(passed=True, name="one_question_per_turn")

        if extracted.action_count > 1:
            return _fail(
                "one_question_per_turn",
                f"extractor found {extracted.action_count} action prompts; "
                f"emit exactly one",
                span,
                payload={
                    "kind": "stacked",
                    "offending_questions": list(extracted.stacked_examples),
                    "trailing_question": (
                        extracted.stacked_examples[0]
                        if extracted.stacked_examples else extracted.primary_action
                    ),
                    "primary_action": extracted.primary_action,
                    "move": move,
                    "match_count": extracted.action_count,
                },
            )

        if move != "close_topic" and not extracted.has_active_end:
            return _fail(
                "one_question_per_turn",
                "turn does not end on an action the student takes "
                "(active-end rule)",
                span,
                payload={
                    "kind": "passive_end",
                    "move": move,
                    "primary_action": extracted.primary_action,
                },
            )

        return GateResult(passed=True, name="one_question_per_turn")


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
    if gate_name == "stem_duplication":
        return run_stem_duplication_check(
            response_text,
            selected_move=ctx.selected_move,
            posed_via_tool=ctx.posed_via_tool,
            pose_tool_stem=ctx.pose_tool_stem,
        )
    if gate_name == "one_question_per_turn":
        return run_one_question_check(
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
                "ALSO contained a question. Under the curriculum-"
                "fidelity contract there is ONE question per turn — "
                "the tool's bank stem IS that question. When the tool "
                "fires, the prose must contain ZERO additional "
                "questions of any kind: not a verifiable Q (named "
                "options, numeric values, yes/no, closed-set picks), "
                "not a Socratic Q ('what does that tell you about …', "
                "'given X, what …'), not a rhetorical Q ('what does "
                "that mean?'). The only allowance is explicitly-"
                "reflective shapes ('what's your intuition?', 'where "
                "have you seen this?') — but those usually belong on "
                "no-tool turns, not stacked alongside a bank pose."
                + flagged_clause +
                " Rewrite the reply with the SAME tool call, but "
                "convert each prose question into a statement, or "
                "remove it entirely. Keep the same teaching move."
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
    if gate_name == "stem_duplication":
        payload = gate_result.payload or {}
        dups = payload.get("duplicated_substrings") or []
        primary = (dups[0] if dups else "")[:160].rstrip()
        primary_clause = (
            f' The duplicated substring was: "{primary}".'
            if primary else ""
        )
        return (
            "Your previous reply called the pose_question tool, but "
            "the LLM-authored prose ALSO contained the bank stem "
            "verbatim. The tool emits the stem to the student — "
            "including it in your prose makes the stem appear twice. "
            "This is the 'Tool-vs-prose dedup' rule in the shared "
            "preamble: when you call the pose_question tool, your "
            "prose must NOT include the stem text — neither as a "
            "question nor as a statement."
            + primary_clause +
            " Rewrite the reply with the SAME tool call, but replace "
            "the duplicated text with a brief lead-in: 'Try this:', "
            "'Next:', 'Here's a quick check:'. The tool's emitted "
            "stem IS your turn's prompt to the student."
        )
    if gate_name == "one_question_per_turn":
        payload = gate_result.payload or {}
        if payload.get("kind") == "passive_end":
            return (
                "Your previous reply ended on a statement or explanation "
                "with nothing for the student to DO. Principle #1 Active "
                "Learning (Ch.10): every turn ends on one action the "
                "student takes. Rewrite so the turn ends with a single "
                "clear ask — a question, a 'now you try', or a step to "
                "attempt. Keep the same teaching move."
            )
        offending = payload.get("offending_questions") or []
        quoted = "; ".join(f'"{q[:120].rstrip()}"' for q in offending[:4])
        flagged = f" Flagged: {quoted}." if quoted else ""
        return (
            "Your previous reply contained more than one thing for the "
            "student to answer (stacked questions / a worked step PLUS a "
            "question / two questions / an embedded multiple-choice). "
            "Principle #5 Minimising Cognitive Load (Ch.14): exactly ONE "
            "action prompt per turn."
            + flagged +
            " Rewrite so the turn poses a SINGLE prompt — keep the most "
            "important one, convert the rest to statements or drop them. "
            "Keep the same teaching move."
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

    if gate_name == "one_question_per_turn":
        payload = gate_result.payload or {}
        if payload.get("kind") == "passive_end":
            # No safe way to synthesise an action deterministically; the
            # retry already had its chance. Ship as-is (a missing ask is
            # a pedagogy miss, not a P1 safety issue).
            return text
        # Stacked: keep the LAST verbatim '?'-prompt (typically the live
        # pose the student answers) and strip the earlier ones.
        offenders = [
            o for o in (payload.get("offending_questions") or [])
            if o and not o.startswith("<")
        ]
        verbatim = [o for o in offenders if o in text]
        if len(verbatim) >= 2:
            for o in verbatim[:-1]:
                text = text.replace(o, "", 1)
            return re.sub(r"\n{3,}", "\n\n", text).strip()
        return text

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

    if gate_name == "stem_duplication":
        payload = gate_result.payload or {}
        dups = payload.get("duplicated_substrings") or []
        stripped = text
        for dup in dups:
            dup = (dup or "").strip()
            if not dup:
                continue
            # Try exact match first.
            if dup in stripped:
                stripped = stripped.replace(dup, "", 1)
                continue
            # Whitespace-normalized fallback: walk through the response
            # word-by-word and excise the run that matches the
            # normalized substring. Handles line-wrap differences
            # between detection-time (normalized) and degrade-time.
            norm_dup = " ".join(dup.split())
            if not norm_dup:
                continue
            words = stripped.split()
            joined = " ".join(words)
            if norm_dup in joined:
                idx = joined.find(norm_dup)
                # Map back into the original text by counting words
                # in the prefix and using their original whitespace.
                prefix_words = joined[:idx].split()
                prefix_word_count = len(prefix_words)
                dup_word_count = len(norm_dup.split())
                # Find the slice in original text.
                cur = 0
                start_idx = 0
                end_idx = len(stripped)
                seen_words = 0
                in_word = False
                for i, ch in enumerate(stripped):
                    if ch.isspace():
                        if in_word:
                            seen_words += 1
                            in_word = False
                            if seen_words == prefix_word_count:
                                start_idx = i
                            elif seen_words == prefix_word_count + dup_word_count:
                                end_idx = i
                                break
                    else:
                        in_word = True
                if seen_words < prefix_word_count + dup_word_count and in_word:
                    seen_words += 1
                    if seen_words == prefix_word_count + dup_word_count:
                        end_idx = len(stripped)
                if start_idx < end_idx <= len(stripped):
                    stripped = stripped[:start_idx] + stripped[end_idx:]
        # Tidy: collapse extra blank lines + trim trailing whitespace.
        stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
        if not stripped:
            return "Let's keep going — try the next step."
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
    "stem_duplication",
    "one_question_per_turn",
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
