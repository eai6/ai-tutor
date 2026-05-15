"""Derive ContentEditTag suggestions from a teacher edit (Q5.2).

Two signals are combined:
  1. The DIFF between before_payload + after_payload — what changed?
  2. The judge_outputs that were already on the row — what did the AI
     judges flag at gen time? Those flags often predict the edit.

Output: a list of ContentEditTag.value strings that the teacher can
confirm or override. Conservative — better to under-suggest and let
the teacher add tags than to over-tag and bias the benchmark.

Mirrors `apps/benchmark/autopopulate.py::derive_suggested_labels` but
adapted for static content edits rather than tutor-turn annotations.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from apps.curriculum.quality_models import ContentEditTag


# ─── Judge violation code → tag mapping ────────────────────────────────
# Each judge code that we can detect on judge_outputs maps to one or
# more tags. Many-to-many: STEP_FACT_CONTRADICTED → FACTUAL_INCORRECT;
# PEDAGOGY_OFF_OBJECTIVE → OFF_TOPIC + POOR_PEDAGOGY.
_JUDGE_CODE_TO_TAGS: Dict[str, List[str]] = {
    # factual_step
    'STEP_FACT_CONTRADICTED': [ContentEditTag.FACTUAL_INCORRECT.value],
    'STEP_FACT_UNSUPPORTED':  [ContentEditTag.FACTUAL_INCORRECT.value],

    # figure_alignment
    'FIGURE_OFF_OBJECTIVE':       [ContentEditTag.OFF_TOPIC.value, ContentEditTag.MISLEADING_IMAGE.value],
    'FIGURE_FACTUAL_ERROR':       [ContentEditTag.FACTUAL_INCORRECT.value, ContentEditTag.MISLEADING_IMAGE.value],
    'FIGURE_LABEL_INACCURATE':    [ContentEditTag.MISLEADING_IMAGE.value, ContentEditTag.FACTUAL_INCORRECT.value],
    'FIGURE_PEDAGOGICALLY_WEAK':  [ContentEditTag.POOR_PEDAGOGY.value, ContentEditTag.MISLEADING_IMAGE.value],
    'FIGURE_VISUAL_QUALITY':      [ContentEditTag.FORMAT_ISSUE.value, ContentEditTag.MISLEADING_IMAGE.value],

    # exit_question
    'EXITQ_WRONG_ANSWER_KEY':     [ContentEditTag.WRONG_ANSWER_KEY.value, ContentEditTag.FACTUAL_INCORRECT.value],
    'EXITQ_MULTIPLE_CORRECT':     [ContentEditTag.AMBIGUOUS_QUESTION.value, ContentEditTag.WRONG_ANSWER_KEY.value],
    'EXITQ_AMBIGUOUS_DISTRACTOR': [ContentEditTag.AMBIGUOUS_QUESTION.value],
    'EXITQ_OFF_OBJECTIVE':        [ContentEditTag.OFF_TOPIC.value],
    'EXITQ_TRICK_WORDING':        [ContentEditTag.AMBIGUOUS_QUESTION.value, ContentEditTag.POOR_PEDAGOGY.value],

    # pedagogy_step
    'PEDAGOGY_GRADE_MISMATCH':     [ContentEditTag.WRONG_GRADE_LEVEL.value],
    'PEDAGOGY_NO_LEARNING_PROMPT': [ContentEditTag.POOR_PEDAGOGY.value],
    'PEDAGOGY_DOK_MISMATCH':       [ContentEditTag.POOR_PEDAGOGY.value],
    'PEDAGOGY_OFF_OBJECTIVE':      [ContentEditTag.OFF_TOPIC.value, ContentEditTag.POOR_PEDAGOGY.value],
    'PEDAGOGY_OVERLOAD':           [ContentEditTag.POOR_PEDAGOGY.value],

    # safety_content
    'SAFETY_HARMFUL_CONTENT':   [ContentEditTag.OTHER.value],   # admin attention needed
    'SAFETY_AGE_INAPPROPRIATE': [ContentEditTag.WRONG_GRADE_LEVEL.value],
    'SAFETY_CULTURAL_MISFIT':   [ContentEditTag.CULTURAL_MISFIT.value],
    'SAFETY_BIASED_FRAMING':    [ContentEditTag.CULTURAL_MISFIT.value, ContentEditTag.OTHER.value],

    # image_prompt (PRE-gen)
    'PROMPT_VAGUE':                  [ContentEditTag.MISLEADING_IMAGE.value],
    'PROMPT_HALLUCINATION_TRIGGER':  [ContentEditTag.FACTUAL_INCORRECT.value, ContentEditTag.MISLEADING_IMAGE.value],
    'PROMPT_OFF_TOPIC':              [ContentEditTag.OFF_TOPIC.value, ContentEditTag.MISLEADING_IMAGE.value],
    'PROMPT_WRONG_VISUAL_TYPE':      [ContentEditTag.MISLEADING_IMAGE.value, ContentEditTag.FORMAT_ISSUE.value],
    'PROMPT_GRADE_MISMATCH':         [ContentEditTag.WRONG_GRADE_LEVEL.value],
    'PROMPT_RELIES_ON_TEXT_IN_IMAGE': [ContentEditTag.FORMAT_ISSUE.value, ContentEditTag.MISLEADING_IMAGE.value],
}


# ─── Diff signals ──────────────────────────────────────────────────────
# Patterns we look for in the before/after diff that imply specific tags.
_NUMERIC_RE = re.compile(r"\b\d+(?:[\.,]\d+)?\b")
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-zA-Z'-]{3,}\b")


def _changed_numerics_or_proper_nouns(before_str: str, after_str: str) -> bool:
    """True when the edit replaced a number or proper noun with a
    different one — strong factual-edit signal.

    Compared as multiset: any number / proper-noun present in BEFORE
    that's absent in AFTER (or vice versa) counts as a swap.
    """
    if not before_str or not after_str:
        return False
    b_nums = set(_NUMERIC_RE.findall(before_str))
    a_nums = set(_NUMERIC_RE.findall(after_str))
    if (b_nums - a_nums) or (a_nums - b_nums):
        return True
    b_props = set(_PROPER_NOUN_RE.findall(before_str))
    a_props = set(_PROPER_NOUN_RE.findall(after_str))
    if (b_props - a_props) or (a_props - b_props):
        return True
    return False


def _length_changed_substantially(before_str: str, after_str: str) -> bool:
    """True when the post-edit text is substantially shorter/longer
    (>30% length change). Often indicates a readability / format fix
    or a scope cut."""
    if not before_str:
        return bool(after_str)
    if not after_str:
        return True
    ratio = len(after_str) / len(before_str)
    return ratio < 0.7 or ratio > 1.3


# ─── Public entry point ────────────────────────────────────────────────
def derive_suggested_tags(
    *,
    content_type: str,
    before_payload: Dict[str, Any],
    after_payload: Dict[str, Any],
    judge_outputs: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Return a deduplicated, ordered list of suggested ContentEditTag
    values for one teacher edit.

    Args:
        content_type: 'step' | 'exit_question' | 'image'
        before_payload: frozen pre-edit field snapshot
        after_payload: frozen post-edit field snapshot
        judge_outputs: snapshot of the row's judge_outputs at edit
            time. Optional — when None or empty, only diff-based
            signals fire.

    Returns:
        Ordered list of tag values (strings). May be empty when no
        signal fires — the teacher can still add tags manually in
        the admin detail view.
    """
    suggested: List[str] = []
    seen: Set[str] = set()

    def _add(tag: str):
        if tag in seen:
            return
        seen.add(tag)
        suggested.append(tag)

    # 1. Judge-flagged codes carry forward — most reliable signal.
    if judge_outputs:
        for judge_name, verdict in (judge_outputs or {}).items():
            if not isinstance(verdict, dict):
                continue
            for code in verdict.get('violations') or []:
                for tag in _JUDGE_CODE_TO_TAGS.get(str(code).upper(), []):
                    _add(tag)

    # 2. Diff signals per content type.
    bp = before_payload or {}
    ap = after_payload or {}

    if content_type == ContentEditEventConstants.STEP:
        before_text = bp.get('teacher_script') or ''
        after_text = ap.get('teacher_script') or ''
        if _changed_numerics_or_proper_nouns(before_text, after_text):
            _add(ContentEditTag.FACTUAL_INCORRECT.value)
        if _length_changed_substantially(before_text, after_text):
            _add(ContentEditTag.FORMAT_ISSUE.value)

    elif content_type == ContentEditEventConstants.EXIT_QUESTION:
        # WRONG_ANSWER_KEY signal: correct_answer letter changed.
        if (
            bp.get('correct_answer')
            and ap.get('correct_answer')
            and bp['correct_answer'] != ap['correct_answer']
        ):
            _add(ContentEditTag.WRONG_ANSWER_KEY.value)

        # FACTUAL_INCORRECT signal: any option text or stem changed
        # numerics/proper-nouns.
        for key in ('question_text', 'option_a', 'option_b',
                    'option_c', 'option_d'):
            b = (bp.get(key) or '').strip()
            a = (ap.get(key) or '').strip()
            if b != a and _changed_numerics_or_proper_nouns(b, a):
                _add(ContentEditTag.FACTUAL_INCORRECT.value)

        # AMBIGUOUS_QUESTION signal: ALL distractors changed (likely
        # rewriting because the original distractors were poor).
        changed_distractors = sum(
            1 for k in ('option_a', 'option_b', 'option_c', 'option_d')
            if (bp.get(k) or '') != (ap.get(k) or '')
            and (bp.get(k) or '').strip() != ap.get('correct_answer', '')
        )
        if changed_distractors >= 3:
            _add(ContentEditTag.AMBIGUOUS_QUESTION.value)

    elif content_type == ContentEditEventConstants.IMAGE:
        if (bp.get('url') or '') != (ap.get('url') or ''):
            _add(ContentEditTag.MISLEADING_IMAGE.value)

    return suggested


# Small helper to keep the content-type strings centralised. We use
# the model's TextChoices values but referencing the model would
# create a circular import (autopopulate is imported by capture
# helpers that the model code references). Plain string constants
# matching ContentEditEvent.ContentType keep the import graph clean.
class ContentEditEventConstants:
    STEP = 'step'
    EXIT_QUESTION = 'exit_question'
    IMAGE = 'image'


__all__ = ["derive_suggested_tags"]
