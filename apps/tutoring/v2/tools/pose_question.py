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
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

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

    # 1b. Phase 4 Fix 4b — deterministic safety floor. An MCQ-shaped
    # stem ("which of the following…", "the following options…",
    # "select one of…") with no rendered options is an unanswerable
    # question and must not reach the student. The renderer for bank
    # MCQ slots is supposed to populate ``mcq_option_order`` AND
    # include the choices in ``rendered_stem`` (see
    # ``_render_bank_stem_with_options`` in student_tutor.py). When
    # both are missing, refuse here so the engine can pick a different
    # slot. The Haiku question-extractor (Fix 2c) is the higher-level
    # catch; this is the belt-and-braces.
    if _looks_like_mcq_stem_without_options(args.rendered_stem) and not args.mcq_option_order:
        return ToolRejection(
            reason="mcq_options_missing",
            detail=(
                "rendered stem references multiple options (e.g. "
                "'which of the following…') but mcq_option_order is "
                "empty; pick a different slot or fix the bank row"
            ),
        )

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


# ----------------------------------------------------------------------
# LLM-facing tool builder — slot-based bank surface
# ----------------------------------------------------------------------

POSE_QUESTION_LLM_TOOL_NAME = "pose_question"


def build_anthropic_pose_question_tool(
    *,
    lesson_id: int,
    posed_step_ids: set[int] | None = None,
    max_stem_chars: int = 160,
) -> tuple[Optional[dict], dict[int, Any]]:
    """Build the slot-indexed Anthropic tool dict for the LLM.

    Returns ``(tool_dict, slot_to_step_map)``. ``tool_dict`` is None
    when the lesson has no posable steps (no step with a populated
    ``question`` field after filtering out steps already in
    ``posed_step_ids``); the caller falls back to the plain
    ``generate()`` text path.

    The LLM only ever sees an integer ``slot`` — the canonical answer
    stays on the backend. This mirrors the legacy
    ``ConversationalTutor._build_pose_question_tool`` surface so the
    Anthropic / Gemini / OpenAI tool-use APIs all behave the same.

    ``posed_step_ids`` is the set of LessonStep ids already in
    ``runtime_state.posed_question_ledger`` for this session — the
    builder drops those slots up front so the in-session repeat guard
    has nothing to reject during Phase A.
    """
    from apps.curriculum.models import LessonStep

    posed_step_ids = posed_step_ids or set()
    steps = (
        LessonStep.objects
        .filter(lesson_id=lesson_id)
        .exclude(id__in=posed_step_ids)
        .exclude(question__isnull=True)
        .exclude(question__exact="")
        .order_by("order_index")
    )

    slot_map: dict[int, Any] = {}
    menu_lines: list[str] = []
    for slot_idx, step in enumerate(steps):
        stem = (step.question or "").strip()
        if not stem:
            continue
        slot_map[slot_idx] = step
        # Phase 4 Fix 4a — surface the answer_type hint in the menu so
        # the LLM can pick an MCQ slot with eyes-open (e.g. when
        # building a confirmation question, an MCQ may be easier for
        # the student to answer than a free-text recall).
        answer_type = (
            getattr(step, "answer_type", "") or ""
        ).strip().lower()
        type_tag = f" [{answer_type}]" if answer_type else ""
        menu_lines.append(
            f"  {slot_idx}{type_tag}: {stem[:max_stem_chars]}"
        )

    if not slot_map:
        return None, {}

    max_slot = max(slot_map.keys())
    tool = {
        "name": POSE_QUESTION_LLM_TOOL_NAME,
        "description": (
            "Pose ONE verified question from the lesson bank to the "
            "student. This is the ONLY legal way to ask a question "
            "with a checkable answer (numerical, MCQ, fill-in, short "
            "answer). The slot index maps to a question in the bank — "
            "the backend renders the canonical stem verbatim. "
            "Optionally supply a SHORT one-sentence ``lead_in`` "
            "(≤80 chars, no '?'). NEVER type the question stem in "
            "your text response — emit a real tool_use block.\n\n"
            "Available slots:\n" + "\n".join(menu_lines)
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slot": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": max_slot,
                    "description": (
                        f"Bank slot to pose. Must be one of: "
                        f"{sorted(slot_map.keys())}"
                    ),
                },
                "lead_in": {
                    "type": "string",
                    "description": (
                        "Optional short transition shown before the "
                        "bank question. At most one sentence, no '?'. "
                        "Leave empty if the bank stem stands alone."
                    ),
                },
            },
            "required": ["slot"],
        },
    }
    return tool, slot_map


def make_resolve_canonical_for_lesson(slot_map: dict[int, Any]):
    """Build a ``resolve_canonical`` callback bound to a slot map.

    Used by ``validate_pose`` to resolve a ``QuestionRef`` (always
    ``LESSON_STEP`` source on this path) to the step's
    ``expected_answer``. Raises ``LookupError`` when the step id is
    unknown — ``validate_pose`` converts that to a ``ToolRejection``.
    """
    step_by_id = {step.id: step for step in slot_map.values()}

    def _resolve(ref: QuestionRef) -> str:
        step = step_by_id.get(ref.id)
        if step is None:
            raise LookupError(
                f"lesson step id={ref.id} not in current slot map"
            )
        return (step.expected_answer or "").strip()

    return _resolve


def _noop_pre_pose_check(**_kwargs) -> None:
    """Placeholder pre-pose check used when ``BANK_PREPOSE_RECHECK=off``.

    ``validate_pose`` only calls this when the env flag is on; we
    still supply a callable so the parameter shape stays uniform.
    Phase 2+ supplies the real grounded-derivability check.
    """
    return None


# ----------------------------------------------------------------------
# Phase 4 Fix 4b — MCQ-without-options safety floor
# ----------------------------------------------------------------------

# Subject-agnostic phrases that imply "an option list follows".
# Matched on the rendered stem to refuse a pose where the bank row
# stores the stem in choice-list shape but the renderer dropped the
# choices. (GEO run-6 T16/T18/T20 P1.) Case-insensitive whole-phrase
# match; very tight so a non-MCQ stem mentioning "the following" in
# passing doesn't get refused.
_MCQ_STEM_REQUIRES_OPTIONS_RE = re.compile(
    r"\b("
    r"which (?:of |one of )?the following"
    r"|the following options"
    r"|select one of the following"
    r"|choose one of the following"
    r"|pick one of the following"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_mcq_stem_without_options(rendered_stem: str) -> bool:
    """True when the rendered stem reads like an MCQ but options are
    not part of the visible text either."""
    text = (rendered_stem or "").strip()
    if not text:
        return False
    if not _MCQ_STEM_REQUIRES_OPTIONS_RE.search(text):
        return False
    # If the renderer has already inlined A)/B)/C)/D) into the visible
    # stem, the student CAN answer — don't refuse.
    inlined_options = re.search(
        r"(?m)^\s*[A-Da-d]\s*[).:\-]\s+\S", text,
    )
    return inlined_options is None
