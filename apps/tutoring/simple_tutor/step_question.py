"""LessonStep → grader/prompt adapter.

The simple-tutor engine treats ``LessonStep.question`` + ``.expected_answer``
as the PRIMARY question for a step (most production lessons author content
this way). The grader + prompt renderer were originally written against
``ExitTicketQuestion``; this thin adapter exposes the same surface so the
rest of the engine doesn't branch by source.

Keep it dumb — no DB writes, no caching, just attribute mapping. The
session stores ``current_question_id = step.pk`` and
``current_question_source = 'lesson_step'``; rehydrate by calling
``StepQuestion.from_step_id(pk)``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# Numeric pattern used to decide whether a step's expected_answer looks
# like a number (→ short_numeric / math grader) vs free text. Matches the
# same shapes the math grader's _NUMBER_RX accepts (int, decimal,
# scientific, with optional degree / unit suffix stripped).
_NUMERIC_VALUE_RX = re.compile(
    r'^\s*-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\s*[°%]?\s*$'
)


@dataclass(frozen=True)
class StepQuestion:
    """Read-only adapter wrapping a ``LessonStep`` into the
    ExitTicketQuestion-shaped surface the grader + prompt renderer
    expect.

    Exposes:
        pk / id              — LessonStep.pk
        question_type        — 'short_numeric' when expected_answer
                               parses as a number, else 'short_answer'
        question_text        — LessonStep.question (the prompt text)
        correct_answer       — LessonStep.expected_answer (verbatim)
        answer_data          — {'computed': float, 'model_answer': str}
                               for numeric answers; {'model_answer': str}
                               for text answers
        enabling_objective   — LessonStep.enabling_objective
        option_a..d          — '' (LessonSteps don't author MCQ options;
                               those live on ExitTicketQuestion)
        source               — 'lesson_step' constant, for engine logging
    """
    pk: int
    question_text: str
    expected_answer: str
    enabling_objective: str
    step_type: str
    phase: str

    # Source discriminator — mirrors TutorSession.QuestionSource.LESSON_STEP
    source: str = 'lesson_step'

    # MCQ surface — always empty for step-backed questions. Kept so the
    # grader can ``getattr(question, 'option_a', '')`` uniformly.
    option_a: str = ''
    option_b: str = ''
    option_c: str = ''
    option_d: str = ''

    @property
    def id(self) -> int:
        return self.pk

    @property
    def question_type(self) -> str:
        """Infer the grader tier from expected_answer shape.

        - Pure numeric → 'short_numeric' (routes to math grader)
        - Anything else → 'short_answer' (routes to embedding gate + LLM
          verifier)

        We don't infer 'mcq' here — LessonSteps don't carry options. If
        a lesson author wants MCQ grading, they should add an
        ExitTicketQuestion with options.
        """
        ans = (self.expected_answer or '').strip()
        if ans and _NUMERIC_VALUE_RX.match(ans):
            return 'short_numeric'
        return 'short_answer'

    @property
    def correct_answer(self) -> str:
        """Mirror of expected_answer — the grader reads ``correct_answer``
        for MCQ and as a fallback for math when ``answer_data`` is empty.
        """
        return (self.expected_answer or '').strip()

    @property
    def answer_data(self) -> dict[str, Any]:
        """Provide ``computed`` for numeric answers, ``model_answer`` for
        all answers. The math grader consults both.
        """
        ans = (self.expected_answer or '').strip()
        if not ans:
            return {}
        out: dict[str, Any] = {'model_answer': ans}
        if _NUMERIC_VALUE_RX.match(ans):
            # Strip the optional ° / % suffix; parse the numeric body.
            numeric_body = re.sub(r'[°%\s]', '', ans)
            try:
                out['computed'] = float(numeric_body)
            except ValueError:
                pass
        return out

    @classmethod
    def from_step(cls, step) -> 'StepQuestion':
        """Build from a ``LessonStep`` instance."""
        return cls(
            pk=step.pk,
            question_text=(step.question or '').strip(),
            expected_answer=(step.expected_answer or '').strip(),
            enabling_objective=(step.enabling_objective or '').strip(),
            step_type=(step.step_type or '').strip(),
            phase=(step.phase or '').strip(),
        )

    @classmethod
    def from_step_id(cls, step_id: int):
        """Rehydrate by LessonStep pk. Returns None if the step no
        longer exists (e.g. curriculum was edited mid-session).
        """
        from apps.curriculum.models import LessonStep
        step = LessonStep.objects.filter(pk=step_id).first()
        if step is None:
            return None
        return cls.from_step(step)


def has_question(step) -> bool:
    """True when a LessonStep has authored question content that the
    engine should anchor to.

    Both ``question`` and ``expected_answer`` should be present — a
    question with no expected answer can't be graded, so we treat it as
    not-a-question and let the engine fall back to ExitTicketQuestion
    (or None).
    """
    if step is None:
        return False
    q = (getattr(step, 'question', '') or '').strip()
    a = (getattr(step, 'expected_answer', '') or '').strip()
    return bool(q) and bool(a)
