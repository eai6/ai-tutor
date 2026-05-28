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
from dataclasses import dataclass
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


# Closed move set the router may emit. Mirrors ``tutor_engine.ALLOWED_MOVES``
# — duplicated here so the router can validate its own output without
# importing from the engine (would create a circular import).
_ALLOWED_MOVES: frozenset[str] = frozenset({
    "confirm_and_advance",
    "confirm_and_extend",
    "scaffold_hint",
    "name_misconception",
    "worked_example",
    "explain",
    "pivot",
    "close_topic",
})


class RouterMalformedError(RuntimeError):
    """The LLM router produced an unusable decision after one retry.

    Raised by ``MoveRouter.route`` when the LLM emits invalid JSON, a
    payload that fails ``RouterDecision`` validation, or a move name not
    in the closed set — and the single retry with an explicit reminder
    also fails the same way. The dispatch layer
    (``v2_respond_dispatch``) catches this and ships the standard
    graceful-failure envelope; the engine never silently coerces to a
    default move.
    """


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

        Recovery contract:

        - **Infrastructure failures** (no client configured, LLM call
          raises): return a conservative fail-soft ``RouterDecision``.
          A network blip or unconfigured model is not a quality issue
          we can retry our way out of.

        - **Content failures** (non-dict JSON, ``RouterDecision``
          validation error, move name not in the closed set): one
          retry with an explicit reminder appended to the user prompt
          naming the failure mode and the closed move set. If the
          retry also fails, raise ``RouterMalformedError`` — the
          dispatch layer ships the standard graceful envelope.
          The engine never silently coerces to a default move.

        The retry budget is one. We log a ``router.retry_recovered``
        span on a recovered retry (visible on the v2 observability
        dashboard) so persistent prompt drift is observable.
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

            base_prompt = render_router_user_prompt(request)
            attempt = self._call_router_once(
                client=client, user_prompt=base_prompt,
            )
            if attempt.infrastructure_failure:
                fallback = _fallback_decision(
                    request, reason=attempt.failure_reason,
                )
                _stamp_span(
                    span, request, fallback, fail_soft=True,
                    reason=attempt.failure_reason,
                )
                return fallback

            if attempt.decision is not None and self._is_valid_decision(
                attempt.decision
            ):
                # Happy path — first call produced a usable decision.
                if span is not None and attempt.tokens_in is not None:
                    span["tokens_in"] = attempt.tokens_in
                    span["tokens_out"] = attempt.tokens_out
                _stamp_span(
                    span, request, attempt.decision,
                    fail_soft=False, reason="",
                )
                return attempt.decision

            # Determine the failure code + payload echo for the retry
            # reminder. ``move_not_in_set`` is a post-validation check —
            # the decision parsed and schema-validated, but contains a
            # move name outside the closed set.
            first_failure = (
                attempt.content_failure
                or ("move_not_in_set" if attempt.decision is not None else "unknown")
            )
            first_payload_echo = (
                attempt.last_payload
                or _decision_move_repr(attempt.decision)
            )
            reminder = _build_retry_reminder(
                failure=first_failure,
                last_payload=first_payload_echo,
            )
            retry_prompt = f"{base_prompt}\n\n{reminder}"
            retry = self._call_router_once(
                client=client, user_prompt=retry_prompt,
            )

            if retry.infrastructure_failure:
                # Retry hit an infra failure (network blip on the
                # second call). Fail-soft to the same conservative
                # default we'd use for a first-call infra failure —
                # retry transport errors don't change the answer.
                fallback = _fallback_decision(
                    request,
                    reason=f"retry_{retry.failure_reason}",
                )
                _stamp_span(
                    span, request, fallback, fail_soft=True,
                    reason=f"retry_{retry.failure_reason}",
                )
                return fallback

            if retry.decision is not None and self._is_valid_decision(
                retry.decision
            ):
                with emit_span(
                    "audit", "router.retry_recovered",
                    payload={
                        "first_failure": first_failure,
                        "first_move": _decision_move_repr(attempt.decision),
                    },
                ):
                    pass
                if span is not None and retry.tokens_in is not None:
                    # Surface both calls' token cost on the parent span.
                    span["tokens_in"] = (
                        (attempt.tokens_in or 0) + retry.tokens_in
                    )
                    span["tokens_out"] = (
                        (attempt.tokens_out or 0) + retry.tokens_out
                    )
                _stamp_span(
                    span, request, retry.decision,
                    fail_soft=False, reason="retry_recovered",
                )
                return retry.decision

            # Retry also failed — hard fail. The engine's dispatch
            # layer catches ``RouterMalformedError`` and surfaces the
            # standard graceful-failure envelope.
            retry_failure = (
                retry.content_failure
                or ("move_not_in_set" if retry.decision is not None else "unknown")
            )
            with emit_span(
                "audit", "router.malformed_after_retry",
                payload={
                    "first_failure": first_failure,
                    "retry_failure": retry_failure,
                    "first_move": _decision_move_repr(attempt.decision),
                    "retry_move": _decision_move_repr(retry.decision),
                },
            ):
                pass
            _stamp_span(
                span, request, None, fail_soft=False,
                reason="malformed_after_retry",
            )
            raise RouterMalformedError(
                "router output invalid after one retry; "
                f"first={first_failure!r} retry={retry_failure!r}"
            )

    # ==================================================================
    # Validation + single-call helper
    # ==================================================================

    @staticmethod
    def _is_valid_decision(decision: RouterDecision) -> bool:
        """Check every move name the decision contains is in the closed set."""
        if decision.verdict_needed:
            mbv = decision.moves_by_verdict or {}
            if not mbv:
                return False
            return all(
                isinstance(v, str) and v in _ALLOWED_MOVES
                for v in mbv.values()
            )
        return bool(decision.move) and decision.move in _ALLOWED_MOVES

    def _call_router_once(
        self, *, client, user_prompt: str,
    ) -> "_RouterAttempt":
        """One LLM call → parse → validate. Never raises.

        Returns an ``_RouterAttempt`` capturing one of:
          - infrastructure_failure=True with ``failure_reason``
          - content_failure="non_dict_payload" | "validation_error"
            with ``last_payload`` (raw text) for the retry reminder
          - decision (RouterDecision) — caller must still check
            ``_is_valid_decision`` (move-in-closed-set).
        """
        try:
            response = client.generate(
                messages=[{"role": "user", "content": user_prompt}],
                system_prompt=SHARED_ROUTER_SYSTEM,
                max_tokens=600,
            )
            raw_text = (response.content or "").strip()
        except Exception as exc:
            logger.warning(
                "[MoveRouter] LLM call raised %s",
                type(exc).__name__,
            )
            return _RouterAttempt(
                infrastructure_failure=True,
                failure_reason=type(exc).__name__,
            )

        payload = _safe_json_loads(raw_text)
        if not isinstance(payload, dict):
            return _RouterAttempt(
                content_failure="non_dict_payload",
                last_payload=raw_text,
                tokens_in=getattr(response, "tokens_in", 0),
                tokens_out=getattr(response, "tokens_out", 0),
            )

        try:
            decision = RouterDecision.model_validate(payload)
        except ValidationError as exc:
            logger.warning(
                "[MoveRouter] RouterDecision validation failed: %s",
                str(exc)[:200],
            )
            return _RouterAttempt(
                content_failure="validation_error",
                last_payload=raw_text,
                tokens_in=getattr(response, "tokens_in", 0),
                tokens_out=getattr(response, "tokens_out", 0),
            )

        return _RouterAttempt(
            decision=decision,
            tokens_in=getattr(response, "tokens_in", 0),
            tokens_out=getattr(response, "tokens_out", 0),
        )

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
# Per-attempt result + retry reminder
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _RouterAttempt:
    """One LLM-call attempt at producing a ``RouterDecision``.

    Exactly one of ``decision`` / ``content_failure`` /
    ``infrastructure_failure`` is meaningful per attempt. The retry
    loop in ``MoveRouter.route`` inspects these to decide whether to
    return, retry once, or hard-fail.
    """

    decision: Optional[RouterDecision] = None
    # Content-level failure: the LLM responded but the response was
    # unusable. These are retry-eligible.
    #   "non_dict_payload" — JSON didn't parse to a dict.
    #   "validation_error" — RouterDecision schema validation failed.
    #   "move_not_in_set" — Decision validated but move name is not in
    #                       the closed set (caller sets this AFTER the
    #                       call by re-checking validity).
    content_failure: Optional[str] = None
    last_payload: str = ""  # raw text, for the retry reminder
    # Infrastructure-level failure: no client, network exception, etc.
    # NOT retry-eligible — fail-soft to the conservative default.
    infrastructure_failure: bool = False
    failure_reason: str = ""
    # Token accounting for span observability.
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None


def _build_retry_reminder(*, failure: str, last_payload: str) -> str:
    """Construct the reminder appended to the user prompt on retry.

    Names the specific failure mode and the closed move set so the
    LLM can self-correct on the second pass. Kept short — the cache
    benefit of the unchanged system prompt is preserved; only the user
    prompt grows by ~10 lines on retry turns.
    """
    moves_list = ", ".join(sorted(_ALLOWED_MOVES))
    if failure == "non_dict_payload":
        body = (
            "Your previous response did not parse as JSON. Emit a single "
            "JSON object only — no prose, no markdown fences, no leading "
            "or trailing commentary."
        )
    elif failure == "validation_error":
        body = (
            "Your previous response did not match the required schema. "
            "Re-read the OUTPUT SCHEMA section in the system prompt; "
            "emit only the keys it specifies for the case you classified."
        )
    elif failure == "move_not_in_set":
        body = (
            "Your previous response contained a move name that is not in "
            f"the closed set: {{{moves_list}}}. Every ``move`` and every "
            "value in ``moves_by_verdict`` must be one of these exact "
            "strings — no synonyms, no new moves, no underscored variants."
        )
    else:
        body = (
            "Your previous response was rejected. Re-read the OUTPUT "
            f"SCHEMA and the closed move set: {{{moves_list}}}. Emit "
            "valid JSON only."
        )
    snippet = (last_payload or "").strip()
    if len(snippet) > 240:
        snippet = snippet[:240] + "…"
    return (
        "=== RETRY — your previous output was rejected ===\n"
        f"{body}\n"
        f"Previous output (rejected): {snippet!r}\n"
        "Emit the corrected JSON now."
    )


def _decision_move_repr(decision: Optional[RouterDecision]) -> str:
    """Compact string of the move(s) a decision contains, for telemetry."""
    if decision is None:
        return ""
    if decision.verdict_needed:
        mbv = decision.moves_by_verdict or {}
        return ",".join(f"{k}={v}" for k, v in sorted(mbv.items()))
    return decision.move or ""


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
    decision: Optional[RouterDecision],
    *,
    fail_soft: bool,
    reason: str,
    raw_chars: int = 0,
) -> None:
    if span is None:
        return
    if decision is None:
        # malformed_after_retry — record the failure shape without
        # pretending a decision exists.
        payload: dict[str, Any] = {
            "case": "",
            "verdict_needed": None,
            "move": "",
            "moves_by_verdict": None,
            "reason": "",
            "fail_soft": fail_soft,
            "pose_tool_available": request.pose_tool_available,
        }
    else:
        payload = {
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
