"""pose_question — server-side topic/difficulty slot selection.

Post-v2-prune step 4 (plan §4.3). The LLM ASKS for an assessment
question by passing a topic / subskill + difficulty hint + reason.
The backend reads the session's ``delivered_lesson_step_ids`` ledger,
picks an undelivered ``LessonStep`` matching the topic, and returns
the stem + canonical answer.

Gone (deleted in this commit):
  - Two-phase commit + ``pre_pose_token`` cache (the LLM never picks
    a specific slot now, so there is nothing to sign).
  - Phase A derivability + repeat guards (the tool owns dedup; the
    LLM cannot propose an invalid slot because it never names one).
  - ``ToolRejection`` + per-turn rejection feedback loop.

The tool still returns a typed ``PendingPose`` so the existing Phase
B commit path in ``ContextManager.commit_pending_pose(...)`` stays
unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from apps.tutoring.v2.contracts import (
    PendingPose,
    QuestionRef,
    QuestionSource,
    VisibleContextSnapshot,
)

logger = logging.getLogger(__name__)


POSE_QUESTION_LLM_TOOL_NAME = "pose_question"


# ----------------------------------------------------------------------
# Tool definition for the LLM
# ----------------------------------------------------------------------


def build_pose_question_tool() -> dict:
    """Build the Anthropic-shape tool dict for the new contract.

    The LLM provides:
      - ``topic_or_subskill``: short description of what to assess.
      - ``difficulty_hint``: ``easier`` | ``same`` | ``harder``.
      - ``reason``: one-line justification (logged on the span).

    The backend returns the chosen slot's stem + canonical + step id,
    or ``exhausted=True`` when no eligible undelivered slot exists.
    """
    return {
        "name": POSE_QUESTION_LLM_TOOL_NAME,
        "description": (
            "Ask the backend for ONE assessment question to pose to the "
            "student. Provide the topic or subskill, a difficulty hint, "
            "and a one-line reason. The backend selects an undelivered "
            "question from the lesson bank that matches and returns its "
            "stem + canonical answer. NEVER type the question stem in "
            "your text response — call this tool and the backend will "
            "append the stem verbatim."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_or_subskill": {
                    "type": "string",
                    "description": (
                        "Short description of the concept or subskill "
                        "the question should assess "
                        "(e.g. 'convert compass direction to three-figure "
                        "bearing')."
                    ),
                },
                "difficulty_hint": {
                    "type": "string",
                    "enum": ["easier", "same", "harder"],
                    "description": (
                        "Relative difficulty vs. the last delivered "
                        "question on this objective."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One-line justification for this pose "
                        "(logged for observability)."
                    ),
                },
            },
            "required": ["topic_or_subskill", "difficulty_hint", "reason"],
        },
    }


# ----------------------------------------------------------------------
# Slot selection
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PoseSelection:
    """Outcome of a pose_question tool call.

    Either a concrete slot (``exhausted=False``, all fields populated)
    or ``exhausted=True`` (no undelivered match; lesson is done for
    this topic).
    """

    stem: str
    canonical_answer: str
    lesson_step_id: int
    exhausted: bool = False


_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def select_pose_slot(
    *,
    lesson_id: int,
    delivered_step_ids: list[int],
    topic_or_subskill: str,
    difficulty_hint: str,
) -> PoseSelection:
    """Pick the next un-delivered LessonStep matching topic + difficulty.

    Returns ``PoseSelection(exhausted=True)`` when no eligible slot
    exists. Slot ranking:

      1. Topic match — token overlap on ``enabling_objective`` + ``question``.
      2. Difficulty — ``harder`` prefers higher ``order_index``,
         ``easier`` prefers lower, ``same`` is neutral.
      3. Ascending ``order_index`` as the final tiebreak.
    """
    from apps.curriculum.models import LessonStep

    candidates = list(
        LessonStep.objects
        .filter(lesson_id=lesson_id)
        .exclude(id__in=delivered_step_ids or [])
        .exclude(question__isnull=True)
        .exclude(question__exact="")
        .order_by("order_index")
    )
    if not candidates:
        return PoseSelection(
            stem="", canonical_answer="", lesson_step_id=0, exhausted=True,
        )

    topic_terms = _tokenize(topic_or_subskill)

    def topic_score(step) -> int:
        if not topic_terms:
            return 0
        obj_terms = _tokenize(getattr(step, "enabling_objective", ""))
        q_terms = _tokenize(getattr(step, "question", ""))
        return len(topic_terms & (obj_terms | q_terms))

    if difficulty_hint == "harder":
        def diff_key(step) -> int:
            return -int(getattr(step, "order_index", 0))
    elif difficulty_hint == "easier":
        def diff_key(step) -> int:
            return int(getattr(step, "order_index", 0))
    else:
        def diff_key(step) -> int:
            return 0

    candidates.sort(
        key=lambda s: (
            -topic_score(s),
            diff_key(s),
            int(getattr(s, "order_index", 0)),
        )
    )

    pick = candidates[0]
    stem = _render_bank_stem_with_options(pick)
    canonical = (getattr(pick, "expected_answer", "") or "").strip()
    return PoseSelection(
        stem=stem,
        canonical_answer=canonical,
        lesson_step_id=int(pick.id),
        exhausted=False,
    )


# ----------------------------------------------------------------------
# Build a PendingPose from a PoseSelection — used by student_tutor
# ----------------------------------------------------------------------


def build_pending_pose(
    selection: PoseSelection,
    *,
    recent_transcript: list[str],
    mcq_option_order: Optional[list[str]] = None,
) -> PendingPose:
    """Wrap a successful ``PoseSelection`` into a ``PendingPose``.

    Phase B commit (``ContextManager.commit_pending_pose``) reads
    ``question_ref`` + ``canonical`` + ``rendered_stem`` to update the
    runtime state.
    """
    return PendingPose(
        question_ref=QuestionRef(
            source=QuestionSource.LESSON_STEP,
            id=selection.lesson_step_id,
        ),
        canonical=selection.canonical_answer,
        rendered_stem=selection.stem,
        jaccard_signature="",  # no signature index any more
        visible_context=VisibleContextSnapshot(
            visible_prompt=selection.stem,
            recent_transcript=list(recent_transcript or [])[-6:],
            mcq_option_order=list(mcq_option_order or []),
        ),
        token=None,
    )


# ----------------------------------------------------------------------
# Bank-stem rendering (formerly in student_tutor.py)
# ----------------------------------------------------------------------


_MCQ_LETTER_RE = re.compile(r"^\s*([A-Da-d])\s*[).:\-]")


def _render_bank_stem_with_options(step) -> str:
    """Render a LessonStep's question with an answer-shape suffix.

    Subject-agnostic. The student must always be able to tell from the
    rendered stem *what shape* of answer is expected.

    Resolved cases:
      - ``multiple_choice``: append the ``choices`` list verbatim.
      - ``true_false``: append "(True or False?)".
      - Other answer types: return the stem as authored.
    """
    stem = (getattr(step, "question", "") or "").strip()
    if not stem:
        return ""
    answer_type = (getattr(step, "answer_type", "") or "").strip().lower()

    if answer_type == "multiple_choice":
        choices = getattr(step, "choices", None) or []
        if not isinstance(choices, list) or not choices:
            return stem
        rendered_choices: list[str] = []
        synth_idx = 0
        for c in choices:
            if not isinstance(c, str):
                continue
            cs = c.strip()
            if not cs:
                continue
            if _MCQ_LETTER_RE.match(cs):
                rendered_choices.append(cs)
            else:
                letter = chr(ord("A") + synth_idx)
                rendered_choices.append(f"{letter}) {cs}")
            synth_idx += 1
        if not rendered_choices:
            return stem
        return f"{stem}\n\n" + "\n".join(rendered_choices)

    if answer_type == "true_false":
        lower = stem.lower()
        if "true or false" in lower or "true/false" in lower:
            return stem
        return f"{stem}\n\n(True or False?)"

    return stem


def extract_mcq_letters(step) -> list[str]:
    """Return the ordered list of MCQ option letters for a step."""
    answer_type = (getattr(step, "answer_type", "") or "").strip().lower()
    if answer_type != "multiple_choice":
        return []
    choices = getattr(step, "choices", None) or []
    if not isinstance(choices, list):
        return []
    letters: list[str] = []
    synth_idx = 0
    for choice in choices:
        if not isinstance(choice, str):
            continue
        cs = choice.strip()
        if not cs:
            continue
        m = _MCQ_LETTER_RE.match(cs)
        if m:
            letters.append(m.group(1).upper())
        else:
            letters.append(chr(ord("A") + synth_idx))
        synth_idx += 1
    return letters


def lookup_lesson_step(lesson_step_id: int) -> Optional[Any]:
    """Resolve a LessonStep instance by id — small helper for tests."""
    from apps.curriculum.models import LessonStep
    return LessonStep.objects.filter(pk=lesson_step_id).first()
