"""pose_question / pose_inline_question tool schemas with backend
provenance enforcement.

Per Phase 1 §4:
  - Accept ONLY ``question_ref`` (typed cross-table) or
    ``pre_pose_token`` (opaque). Reject ``correct_answer`` outright.
  - Two-phase commit: Phase A returns a ``PendingPose`` after running
    derivability (under ``BANK_PREPOSE_RECHECK=on``) + the always-on
    repeat guards. Phase B commits via
    ``ContextManager.commit_pending_pose(...)`` after conformance
    approves the candidate response.

The grader's ``pre_pose_check`` is a Phase 2 NotImplementedError stub
in Phase 1; this module routes to it but tests assert only that
routing happens, not the outcome.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from apps.tutoring.v2.contracts import (
    PendingPose,
    QuestionRef,
    QuestionSource,
    VisibleContextSnapshot,
)
from apps.tutoring.v2.tools.repeat_guards import (
    GuardResult,
    canonicalize_stem,
    cross_session_repeat_guard,
    in_session_repeat_guard,
)
from apps.tutoring.v2.tools.token_cache import (
    TokenAlreadyConsumed,
    TokenInvalid,
    token_cache,
)


# ----------------------------------------------------------------------
# Tool argument schema (strict — Pydantic v2)
# ----------------------------------------------------------------------


class PoseQuestionToolArgs(BaseModel):
    """Strict tool-call schema for pose_question / pose_inline_question.

    Allowed inputs:
      - ``question_ref`` (one of LESSON_STEP / EXIT_TICKET_QUESTION /
        INLINE_GENERATED) — backend resolves the canonical from the
        table indicated by ``source``.
      - ``pre_pose_token`` (opaque, HMAC-signed) — backend retrieves
        the canonical the token stamped.

    Disallowed inputs:
      - ``correct_answer`` (any form) — REFUSED to remove the
        LLM-supplied-canonical surface.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_ref: Optional[QuestionRef] = None
    pre_pose_token: Optional[str] = None
    rendered_stem: str = Field(default="", description="Stem text shown to student")
    attached_media_ids: list[int] = Field(default_factory=list)
    recent_transcript: list[str] = Field(default_factory=list)
    mcq_option_order: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one_provenance(self) -> "PoseQuestionToolArgs":
        if (self.question_ref is None) == (self.pre_pose_token is None):
            raise ValueError(
                "exactly one of question_ref or pre_pose_token must be set"
            )
        return self


# ----------------------------------------------------------------------
# Refusal / rejection
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ToolRejection:
    """Returned when validation refuses the tool call.

    Tool calls REFUSED at validation time leave SessionRuntimeState
    untouched (Phase A is read-only). TutorEngine selects an
    alternate (Phase 2 wiring).
    """

    reason: str
    detail: str = ""

    @property
    def refused(self) -> bool:
        return True


# ----------------------------------------------------------------------
# Phase A validation entry point
# ----------------------------------------------------------------------


def validate_pose(
    *,
    session_id: int,
    student_id: int,
    raw_args: dict[str, Any],
    runtime_state,
    asked_questions: Optional[dict],
    resolve_canonical,
    pre_pose_check,
) -> PendingPose | ToolRejection:
    """Phase A validation of a pose_question / pose_inline_question call.

    Steps (in order):
      1. Schema validation — refuses ``correct_answer`` and any
         malformed shape via Pydantic ``extra="forbid"``.
      2. Provenance resolution — token path peeks the token cache
         (read-only); bank path resolves canonical via
         ``resolve_canonical(question_ref)`` (caller-supplied so this
         module stays Django-import-free for tests).
      3. Visible-context snapshot capture from tool inputs.
      4. ``in_session_repeat_guard`` — always-on.
      5. ``cross_session_repeat_guard`` — always-on.
      6. Derivability ``pre_pose_check`` — only when bank path AND
         ``BANK_PREPOSE_RECHECK=on``. Token path skips this (already
         validated at issue time).

    Returns ``PendingPose`` on pass, ``ToolRejection`` on any
    failure. NEVER mutates ``runtime_state`` or the token cache.
    """
    from apps.tutoring.v2.config.flags import bank_prepose_recheck_enabled

    # 1. Schema validation.
    try:
        args = PoseQuestionToolArgs(**raw_args)
    except ValidationError as exc:
        return ToolRejection(reason="schema_invalid", detail=str(exc))

    visible_context = VisibleContextSnapshot(
        visible_prompt=args.rendered_stem,
        attached_media_ids=list(args.attached_media_ids),
        recent_transcript=list(args.recent_transcript),
        mcq_option_order=list(args.mcq_option_order),
    )

    # 2a. Provenance — pre_pose_token path.
    canonical: str = ""
    question_ref: Optional[QuestionRef] = None
    token_value: Optional[str] = None
    is_token_path = args.pre_pose_token is not None

    if is_token_path:
        token_value = args.pre_pose_token
        try:
            cached = token_cache.peek(session_id, token_value)
        except (TokenInvalid, TokenAlreadyConsumed) as exc:
            return ToolRejection(reason="token_invalid", detail=str(exc))
        canonical = cached.canonical
        # Token-issued questions are stamped INLINE_GENERATED on the
        # pre_pose path; the visible context was checked at issue time.
        question_ref = QuestionRef(
            source=QuestionSource.PRE_POSE_TOKEN,
            id=0,
        )
    else:
        # 2b. Provenance — bank path.
        question_ref = args.question_ref
        try:
            canonical = resolve_canonical(question_ref)
        except LookupError as exc:
            return ToolRejection(reason="ref_unresolved", detail=str(exc))
        if not canonical:
            return ToolRejection(
                reason="ref_unresolved",
                detail=f"no canonical for {question_ref.composite_key()}",
            )

    # 3. Canonical signature for repeat detection.
    signature = canonicalize_stem(args.rendered_stem)

    # 4. In-session repeat guard.
    in_sess = in_session_repeat_guard(
        candidate_signature=signature,
        ledger=runtime_state.posed_question_ledger,
        candidate_ref=question_ref,
    )
    if in_sess.refused:
        return ToolRejection(reason="in_session_repeat", detail=in_sess.reason)

    # 5. Cross-session repeat guard.
    cross_sess = cross_session_repeat_guard(
        candidate_ref=question_ref,
        asked_questions=asked_questions,
    )
    if cross_sess.refused:
        return ToolRejection(reason="cross_session_repeat", detail=cross_sess.reason)

    # 6. Derivability check — bank path under BANK_PREPOSE_RECHECK=on.
    # Token path is already validated at issue time; inline-generated
    # path goes through the grader at issue time too.
    if not is_token_path and bank_prepose_recheck_enabled():
        try:
            pre_pose_check(
                question_ref=question_ref,
                canonical=canonical,
                visible_prompt=args.rendered_stem,
                attached_media_ids=list(args.attached_media_ids),
                recent_transcript=list(args.recent_transcript),
            )
        except NotImplementedError:
            # Phase 1: grader stub. Tests assert routing happens;
            # Phase 2 supplies the real implementation.
            pass
        except Exception as exc:  # noqa: BLE001 — pre_pose_check is a
            # caller-supplied callable; we don't know its raise shape.
            return ToolRejection(reason="not_derivable", detail=str(exc))

    return PendingPose(
        question_ref=question_ref,
        canonical=canonical,
        rendered_stem=args.rendered_stem,
        jaccard_signature=signature,
        visible_context=visible_context,
        token=token_value,
    )


# ----------------------------------------------------------------------
# Thin tool wrappers — Phase 2 wires these into the LLM tool-use
# call sites; Phase 1 ships them so the imports + schema are stable.
# ----------------------------------------------------------------------


class PoseQuestionTool:
    """Phase 2 wires this into the LLM tool-use surface."""

    name = "pose_question"
    args_schema = PoseQuestionToolArgs


class PoseInlineQuestionTool:
    """Phase 2 wires this into the LLM tool-use surface."""

    name = "pose_inline_question"
    args_schema = PoseQuestionToolArgs
