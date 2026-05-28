"""MoveRouter — LLM move-selection for the v2 engine.

Post-prune Commit D §4.2: the router runs FIRST on every turn,
unconditionally, BEFORE the grader. Its output encodes both the case
classification AND the move(s):

- Non-answer-attempt turns (help_request / opening_turn / forced_close):
  ``{case, move, verdict_needed: false, reason}``. The engine skips
  the grader and calls the tutor directly with ``move``.

- Answer-attempt turns: ``{case: "answer_attempt", verdict_needed:
  true, moves_by_verdict: {correct, partial, wrong}, reason}``. The
  engine grades, then picks the matching row — no engine-side mapping
  table, no override.

Mirrors ``StudentGrader``'s shape: stateless, constructor-injectable
LLM client factory, single public method, Pydantic-typed output, span
instrumentation, fail-soft contract.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from pydantic import ValidationError

from apps.tutoring.tracing import emit_span
from apps.tutoring.v2.contracts import (
    ObjectiveProgress,
    RouterDecision,
    RouterRequest,
    SessionRuntimeState,
    TutoringContext,
)
from apps.tutoring.v2.services.router_prompts import (
    SHARED_ROUTER_SYSTEM,
    render_router_user_prompt,
)

logger = logging.getLogger(__name__)


# How many transcript turns to surface to the router. Plan §2.1: 10
# turns is the design choice; tunable via the per-turn span if the
# prompt-cache miss rate climbs (plan §7 risk 3).
ROUTER_TRANSCRIPT_WINDOW = 10


class MoveRouter:
    """Stateless LLM move-selection service."""

    def __init__(
        self,
        *,
        router_client_factory=None,
    ) -> None:
        """``router_client_factory`` lets tests inject a fake LLM client.

        When ``None`` the router resolves the MOVE_ROUTER ``ModelConfig``
        at call time (mirrors ``StudentGrader``'s client-resolution
        path).
        """
        self._router_client_factory = router_client_factory

    # ==================================================================
    # Public entry point
    # ==================================================================

    def route(self, request: RouterRequest) -> RouterDecision:
        """Pick the move + principle emphasis + focus note for one turn.

        Fail-soft contract: every LLM-side failure (no client, raise on
        generate, unparseable JSON, ValidationError) returns a
        conservative ``RouterDecision`` based on the grader verdict and
        emits a ``router.decision`` span with ``fail_soft=true``. The
        turn never breaks on a router outage — the engine still
        produces a tutor response.
        """
        with emit_span("audit", "router.decision") as span:
            client = self._resolve_router_client()
            if client is None:
                fallback = _fallback_decision(
                    request, reason="no_client",
                )
                _stamp_span(span, request, fallback, fail_soft=True,
                            reason="no_client")
                return fallback

            user_prompt = render_router_user_prompt(request)
            try:
                response = client.generate(
                    messages=[{"role": "user", "content": user_prompt}],
                    system_prompt=SHARED_ROUTER_SYSTEM,
                    max_tokens=600,
                )
                raw_text = (response.content or "").strip()
            except Exception as exc:
                logger.warning(
                    "[MoveRouter] LLM call raised %s — fail-soft default",
                    type(exc).__name__,
                )
                fallback = _fallback_decision(
                    request, reason=f"raise:{type(exc).__name__}",
                )
                _stamp_span(span, request, fallback, fail_soft=True,
                            reason=type(exc).__name__)
                return fallback

            payload = _safe_json_loads(raw_text)
            if not isinstance(payload, dict):
                logger.warning(
                    "[MoveRouter] non-dict JSON from router LLM — fail-soft default",
                )
                fallback = _fallback_decision(
                    request, reason="non_dict_payload",
                )
                _stamp_span(span, request, fallback, fail_soft=True,
                            reason="non_dict_payload",
                            raw_chars=len(raw_text))
                return fallback

            try:
                decision = RouterDecision.model_validate(payload)
            except ValidationError as exc:
                logger.warning(
                    "[MoveRouter] RouterDecision validation failed: %s — fail-soft",
                    str(exc)[:200],
                )
                fallback = _fallback_decision(
                    request, reason="validation_error",
                )
                _stamp_span(span, request, fallback, fail_soft=True,
                            reason="validation_error",
                            raw_chars=len(raw_text))
                return fallback

            if span is not None:
                span["tokens_in"] = getattr(response, "tokens_in", 0)
                span["tokens_out"] = getattr(response, "tokens_out", 0)
            _stamp_span(span, request, decision, fail_soft=False,
                        reason="")
            return decision

    # ==================================================================
    # Client resolution
    # ==================================================================

    def _resolve_router_client(self):
        if self._router_client_factory is not None:
            return self._router_client_factory()
        from apps.tutoring.v2.services.student_grader import (
            _build_client_for_purpose,
        )
        return _build_client_for_purpose("move_router")


# ──────────────────────────────────────────────────────────────────────
# RouterRequest builder — single site for context → request snapshot
# ──────────────────────────────────────────────────────────────────────


def build_router_request(
    *,
    context: TutoringContext,
    student_input: str,
    pose_tool_available: bool,
    media_catalog: Optional[list[dict]] = None,
) -> RouterRequest:
    """Snapshot a ``RouterRequest`` from the current context.

    Single site so the engine doesn't re-derive the snapshot at each
    call site. Window size = ``ROUTER_TRANSCRIPT_WINDOW``.

    Note (Commit D): the router runs BEFORE the grader, so the
    request no longer carries a verdict. The new per-open-question +
    per-objective counter fields are read from runtime_state — the
    engine writes them after each turn in
    ``TutorEngine.update_counters_post_turn``.
    """
    runtime_state: SessionRuntimeState = context.runtime_state
    obj_key = (context.current_objective or "_").strip() or "_"
    progress: Optional[ObjectiveProgress] = (
        runtime_state.objective_progress.get(obj_key)
    )
    last_n = list((context.full_transcript or [])[-ROUTER_TRANSCRIPT_WINDOW:])
    media_summary = _summarise_media_catalog(media_catalog or [])

    open_q = runtime_state.open_question
    counters = runtime_state.safety_valve_counters

    # Derive the two counters that come straight from existing state.
    prior_attempts = (progress.attempts if progress else 0)
    correct_on_obj = (progress.correct if progress else 0)

    return RouterRequest(
        last_n_turns=last_n,
        student_input=student_input,
        profile_summary=context.profile_summary or "",
        objective=context.current_objective or "",
        lesson_title=context.lesson_title or "",
        lesson_subject=context.lesson_subject or "",
        lesson_step_teacher_script=(
            context.current_step_teacher_script or ""
        ),
        lesson_step_worked_example=(
            context.current_step_worked_example or ""
        ),
        media_catalog_summary=media_summary,
        is_final_step=context.is_final_step,
        move_history=list(runtime_state.move_history or []),
        objective_correct=(progress.correct if progress else 0),
        objective_wrong=(progress.wrong if progress else 0),
        objective_partial=(progress.partial if progress else 0),
        objective_attempts=(progress.attempts if progress else 0),
        turns_in_session=counters.turns_in_session,
        turns_on_current_objective=counters.turns_on_current_objective,
        verdictless_turns=counters.verdictless_turns,
        attempts_on_open_question=runtime_state.attempts_on_open_question,
        open_question_stem=(open_q.rendered_stem if open_q else ""),
        open_question_has_pending=open_q is not None,
        # ── NEW counter fields (Commit D) ──
        wrong_attempts_on_open_question=(
            runtime_state.wrong_attempts_on_open_question
        ),
        partial_attempts_on_open_question=(
            runtime_state.partial_attempts_on_open_question
        ),
        consecutive_wrong_on_open_question=(
            runtime_state.consecutive_wrong_on_open_question
        ),
        objective_turn_count=counters.turns_on_current_objective,
        prior_answer_attempts_on_objective=prior_attempts,
        correct_on_objective=correct_on_obj,
        unscaffolded_correct_on_objective=(
            runtime_state.unscaffolded_correct_on_open_question_objective
        ),
        recent_verdicts=list(runtime_state.recent_verdicts or []),
        pose_tool_available=pose_tool_available,
    )


def _summarise_media_catalog(catalog: list[dict]) -> str:
    if not catalog:
        return "(none)"
    parts: list[str] = []
    for idx, entry in enumerate(catalog[:5], start=1):
        title = (entry.get("title") or "").strip() or "(untitled)"
        parts.append(f"[{idx}] {title}")
    if len(catalog) > 5:
        parts.append(f"... and {len(catalog) - 5} more")
    return "; ".join(parts)


# ──────────────────────────────────────────────────────────────────────
# Fail-soft default
# ──────────────────────────────────────────────────────────────────────


def _fallback_decision(
    request: RouterRequest, *, reason: str,
) -> RouterDecision:
    """Conservative ``RouterDecision`` when the router LLM is unavailable.

    Post-Commit D shape — the router runs before the grader, so the
    fallback's shape depends on whether an open question is in flight:

    - Open question pending → answer-attempt fallback. ``verdict_needed
      = True``, ``moves_by_verdict`` = {correct: confirm_and_advance,
      partial: scaffold_hint, wrong: scaffold_hint}.

    - Otherwise → help-request / opening-turn shape. ``verdict_needed =
      False``, ``move = "explain"``.

    ``reason`` carries ``router_unavailable_fallback`` + the underlying
    cause so the observability dashboard can spot a fallback storm.
    """
    if request.open_question_has_pending:
        return RouterDecision(
            case="answer_attempt",
            verdict_needed=True,
            moves_by_verdict={
                "correct": "confirm_and_advance",
                "partial": "scaffold_hint",
                "wrong": "scaffold_hint",
            },
            reason=f"router_unavailable_fallback:{reason}",
        )
    return RouterDecision(
        case="opening_turn",
        verdict_needed=False,
        move="explain",
        reason=f"router_unavailable_fallback:{reason}",
    )


# ──────────────────────────────────────────────────────────────────────
# Observability helpers
# ──────────────────────────────────────────────────────────────────────


def _stamp_span(
    span: Optional[dict],
    request: RouterRequest,
    decision: RouterDecision,
    *,
    fail_soft: bool,
    reason: str,
    raw_chars: int = 0,
) -> None:
    if span is None:
        return
    payload: dict[str, Any] = {
        "case": decision.case,
        "verdict_needed": decision.verdict_needed,
        "move": decision.move,
        "moves_by_verdict": (
            dict(decision.moves_by_verdict)
            if decision.moves_by_verdict else None
        ),
        "reason": _truncate(decision.reason, 200),
        "fail_soft": fail_soft,
        "pose_tool_available": request.pose_tool_available,
    }
    if reason:
        payload["fail_soft_reason"] = reason
    if raw_chars:
        payload["raw_chars"] = raw_chars
    span["payload"] = payload


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ──────────────────────────────────────────────────────────────────────
# Module-private JSON helpers
# ──────────────────────────────────────────────────────────────────────


def _safe_json_loads(text: str) -> Optional[Any]:
    """Best-effort JSON parse — strips fences / extracts the first object."""
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None
