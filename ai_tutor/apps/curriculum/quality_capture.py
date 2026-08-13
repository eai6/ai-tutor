"""Capture helpers for ContentEditEvent (Q5.3).

One public function `record_content_edit_event()` called from the
edit/save callsites in apps/dashboard/views.py:
  - step_edit (when teacher_script changes)
  - lesson_step_save_regen (Q3.1 manual regen save)
  - exit_question_edit (when fields change via inline edit)
  - exit_question_save_regen (Q3.2 manual regen save)
  - regenerate_media action in step_edit (image swapped)

Fail-soft — capture errors are logged but never raise, so an analytics
write doesn't block the teacher's primary action.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ai_tutor.apps.curriculum.quality_autopopulate import derive_suggested_tags
from ai_tutor.apps.curriculum.quality_models import ContentEditEvent

logger = logging.getLogger(__name__)


def _step_payload(step) -> Dict[str, Any]:
    return {
        'teacher_script': step.teacher_script or '',
        'question': step.question or '',
        'step_type': step.step_type,
        'expected_answer': step.expected_answer or '',
    }


def _question_payload(question) -> Dict[str, Any]:
    return {
        'question_text': question.question_text or '',
        'option_a': question.option_a or '',
        'option_b': question.option_b or '',
        'option_c': question.option_c or '',
        'option_d': question.option_d or '',
        'correct_answer': question.correct_answer or '',
        'explanation': question.explanation or '',
        'question_type': question.question_type,
    }


def _image_payload(media: Dict[str, Any]) -> Dict[str, Any]:
    """media is the dict shape stored in step.media['images'][i]."""
    return {
        'url': media.get('url') or '',
        'description': media.get('description') or '',
        'caption': media.get('caption') or '',
        'alt': media.get('alt') or '',
    }


def record_content_edit_event(
    *,
    content_type: str,
    content_id: int,
    lesson,
    before_payload: Dict[str, Any],
    after_payload: Dict[str, Any],
    judge_outputs: Optional[Dict[str, Any]] = None,
    source: str = ContentEditEvent.Source.MANUAL_EDIT,
    teacher_notes: str = '',
    edited_by=None,
) -> Optional[ContentEditEvent]:
    """Persist one ContentEditEvent for the benchmark.

    Returns the created event, or None on failure (failure is logged
    + swallowed so the caller's primary action isn't blocked).

    Skips creating the event when before_payload == after_payload —
    no actual change happened, no benchmark signal.
    """
    # No-op when nothing actually changed (e.g. teacher hit save
    # without editing anything).
    if before_payload == after_payload:
        return None

    try:
        suggested = derive_suggested_tags(
            content_type=content_type,
            before_payload=before_payload,
            after_payload=after_payload,
            judge_outputs=judge_outputs,
        )
        evt = ContentEditEvent.objects.create(
            content_type=content_type,
            content_id=content_id,
            lesson=lesson,
            before_payload=before_payload or {},
            after_payload=after_payload or {},
            suggested_tags=suggested,
            # Default error_tags to the suggestions so a teacher who
            # doesn't visit the admin still leaves usable benchmark
            # labels. They can override later.
            error_tags=suggested,
            judge_outputs_at_edit=(judge_outputs or {}),
            source=source,
            teacher_notes=teacher_notes or '',
            edited_by=edited_by if edited_by and edited_by.is_authenticated else None,
        )
        logger.info(
            f"[ContentEditEvent] {content_type} #{content_id} "
            f"src={source} suggested={suggested}"
        )
        return evt
    except Exception as exc:
        logger.warning(
            f"[ContentEditEvent] capture failed for {content_type} "
            f"#{content_id}: {type(exc).__name__}: {exc}"
        )
        return None


__all__ = [
    "record_content_edit_event",
    "_step_payload",
    "_question_payload",
    "_image_payload",
]
