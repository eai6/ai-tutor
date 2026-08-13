"""Export-time hydration helpers for benchmark JSONL.

Functions here are read-only — they pull additional context from the live
DB at export time so the snapshot JSON can stay frozen. The tradeoff is
that step content for deleted lessons returns null, which is acceptable.

Speaker labels: per user spec we use capitalized "Tutor" / "Student" rather
than BEA's lowercase "teacher" / "student". Matches our internal
terminology and the inline labels in the conversation_history string.
"""

from typing import Dict, List, Optional


def hydrate_step_context(snapshot_item: dict) -> Optional[dict]:
    """Look up the LessonStep referenced by snapshot_item['current_step_id'].

    Returns None when:
      - snapshot has no current_step_id
      - step has been deleted (course regen wipes LessonStep rows)

    Returned dict carries the step's structured material — what makes our
    benchmark items distinguishable from MathDial / TutorBench items
    that lack lesson-level grounding:
      step_id, step_type, teacher_script, question, answer_type,
      expected_answer, choices, rubric, priority
    """
    step_id = snapshot_item.get('current_step_id')
    if not step_id:
        return None

    from ai_tutor.apps.curriculum.models import LessonStep
    step = LessonStep.objects.filter(id=step_id).first()
    if not step:
        return None

    return {
        'step_id': step.id,
        'step_type': step.step_type,
        'teacher_script': step.teacher_script or '',
        'question': step.question or '',
        'answer_type': step.answer_type,
        'expected_answer': step.expected_answer or '',
        'choices': step.choices,
        'rubric': step.rubric or '',
        'priority': step.priority,
    }


def normalise_speaker(role: str) -> str:
    """Internal SessionTurn.role → BEA-style speaker label.

    'tutor' → 'Tutor', 'student' → 'Student'. Capitalized per user spec
    (memory/benchmark_jsonl_export_plan.md). External BEA-2023 evaluators
    will need a one-line lowercase mapping if they're case-sensitive; we
    chose consistency with our internal terminology over strict 2023 spec.
    """
    if role == 'tutor':
        return 'Tutor'
    if role == 'student':
        return 'Student'
    return role.title() if role else 'Unknown'


def utterances_from_history(history: List[dict]) -> List[Dict[str, str]]:
    """Snapshot conversation_history → BEA-2023-shaped utterances list.

    Input:  [{turn_id, role, text}, ...]
    Output: [{text, speaker}, ...] with capitalized speaker labels.
    """
    return [
        {'text': h.get('text', ''), 'speaker': normalise_speaker(h.get('role', ''))}
        for h in (history or [])
    ]


def conversation_history_string(utterances: List[Dict[str, str]]) -> str:
    """BEA 2025 shape — single string with inline speaker labels.

    "Tutor: ...\\nStudent: ..." — matches the BEA 2025 dev-set example
    exactly (capitalised inline labels, newline-separated turns).
    """
    return '\n'.join(
        f"{u.get('speaker', 'Unknown')}: {u.get('text', '')}"
        for u in utterances
    )
