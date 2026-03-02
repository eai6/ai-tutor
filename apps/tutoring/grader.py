"""
Grader - Evaluates student answers against expected answers.

Supports multiple grading strategies:
1. Exact match (MCQ, True/False)
2. Numeric tolerance (for math answers)
3. LLM-based rubric grading (for free-text)

Design principle: Be generous with correct answers (normalize spacing,
case, etc.) but accurate. When in doubt, use the LLM grader.
"""

import logging
import re
import json
from dataclasses import dataclass
from typing import Optional, Literal
from enum import Enum

from pydantic import BaseModel, Field

from apps.curriculum.models import LessonStep
from apps.llm.client import BaseLLMClient, LLMResponse

logger = logging.getLogger(__name__)


class GradingLLMResult(BaseModel):
    """Structured LLM grading output."""
    result: Literal["correct", "partial", "incorrect"] = Field(
        description="The grading result: correct, partial, or incorrect"
    )
    score: float = Field(description="Score from 0.0 to 1.0", ge=0.0, le=1.0)
    feedback: str = Field(description="Brief, encouraging feedback for the student")


class GradeResult(Enum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    PARTIAL = "partial"  # For rubric-based grading


@dataclass
class GradingOutcome:
    """Result of grading a student answer."""
    result: GradeResult
    feedback: str
    score: float  # 0.0 to 1.0
    details: Optional[dict] = None  # Extra info (LLM reasoning, etc.)


def normalize_answer(answer: str) -> str:
    """Normalize answer for comparison: lowercase, strip, collapse spaces."""
    return " ".join(answer.lower().strip().split())


def grade_exact_match(student_answer: str, expected_answer: str) -> GradingOutcome:
    """
    Grade by exact match (after normalization).
    Used for MCQ, True/False.
    """
    student_normalized = normalize_answer(student_answer)
    expected_normalized = normalize_answer(expected_answer)
    
    # Also check if student gave letter (A, B, C) for MCQ
    # Handle both "A" and "a" and "A)" etc.
    student_letter = re.sub(r'[^a-zA-Z]', '', student_answer).upper()
    expected_letter = re.sub(r'[^a-zA-Z]', '', expected_answer).upper()
    
    if student_normalized == expected_normalized or student_letter == expected_letter:
        return GradingOutcome(
            result=GradeResult.CORRECT,
            feedback="Correct!",
            score=1.0,
        )
    else:
        return GradingOutcome(
            result=GradeResult.INCORRECT,
            feedback="That's not quite right.",
            score=0.0,
        )


def grade_numeric(
    student_answer: str,
    expected_answer: str,
    tolerance: float = 0.01,
) -> GradingOutcome:
    """
    Grade numeric answers with tolerance.
    Handles various formats: "42", "42.0", "$42", "42%", etc.
    """
    def extract_number(s: str) -> Optional[float]:
        """Extract numeric value from string."""
        # Remove common prefixes/suffixes
        cleaned = re.sub(r'[$%,]', '', s.strip())
        try:
            return float(cleaned)
        except ValueError:
            return None
    
    student_num = extract_number(student_answer)
    expected_num = extract_number(expected_answer)
    
    if student_num is None:
        return GradingOutcome(
            result=GradeResult.INCORRECT,
            feedback="I couldn't understand that as a number. Please enter a numeric answer.",
            score=0.0,
        )
    
    if expected_num is None:
        # Fallback to exact match if expected isn't numeric
        return grade_exact_match(student_answer, expected_answer)
    
    # Check if within tolerance
    if abs(student_num - expected_num) <= tolerance * abs(expected_num) if expected_num != 0 else abs(student_num) <= tolerance:
        return GradingOutcome(
            result=GradeResult.CORRECT,
            feedback="Correct!",
            score=1.0,
        )
    else:
        return GradingOutcome(
            result=GradeResult.INCORRECT,
            feedback="That's not the right answer.",
            score=0.0,
        )


def grade_true_false(student_answer: str, expected_answer: str) -> GradingOutcome:
    """Grade True/False questions."""
    true_variants = {'true', 't', 'yes', 'y', '1', 'correct'}
    false_variants = {'false', 'f', 'no', 'n', '0', 'incorrect', 'wrong'}
    
    student_lower = student_answer.lower().strip()
    expected_lower = expected_answer.lower().strip()
    
    student_is_true = student_lower in true_variants
    student_is_false = student_lower in false_variants
    expected_is_true = expected_lower in true_variants
    
    if not (student_is_true or student_is_false):
        return GradingOutcome(
            result=GradeResult.INCORRECT,
            feedback="Please answer True or False.",
            score=0.0,
        )
    
    if student_is_true == expected_is_true:
        return GradingOutcome(
            result=GradeResult.CORRECT,
            feedback="Correct!",
            score=1.0,
        )
    else:
        return GradingOutcome(
            result=GradeResult.INCORRECT,
            feedback="That's not right.",
            score=0.0,
        )


def _get_instructor_client():
    """Get an instructor-wrapped client for structured grading output."""
    try:
        import instructor
        from apps.llm.models import ModelConfig

        config = ModelConfig.get_for('tutoring')
        if not config:
            return None
        PROVIDER_MAP = {
            'anthropic': 'anthropic',
            'openai': 'openai',
            'google': 'google',
            'local_ollama': 'ollama',
        }
        provider = PROVIDER_MAP.get(config.provider, config.provider)
        return instructor.from_provider(
            f"{provider}/{config.model_name}",
            api_key=config.get_api_key(),
        )
    except Exception as e:
        logger.warning(f"Could not create instructor client for grading: {e}")
        return None


def grade_with_llm(
    student_answer: str,
    expected_answer: str,
    rubric: str,
    question: str,
    llm_client: BaseLLMClient,
    institution_id: int = None,
) -> GradingOutcome:
    """
    Use LLM to grade free-text answers against a rubric.

    Uses instructor for structured output. Falls back to raw LLM + json.loads
    if instructor is unavailable.
    """
    grading_prompt = f"""You are grading a student's answer. Be encouraging but accurate.

QUESTION: {question}

EXPECTED ANSWER: {expected_answer}

GRADING RUBRIC:
{rubric if rubric else "The answer should match the expected answer in meaning, not necessarily exact wording."}

STUDENT'S ANSWER: {student_answer}

Be generous with partial credit if the student shows understanding.
Grade this answer."""

    result_map = {
        "correct": GradeResult.CORRECT,
        "partial": GradeResult.PARTIAL,
        "incorrect": GradeResult.INCORRECT,
    }

    try:
        from apps.llm.prompts import get_prompt_or_default
        grading_sys_prompt = get_prompt_or_default(
            institution_id, 'grading_prompt',
            "You are a fair, encouraging grader.",
        )

        client = _get_instructor_client()
        if client:
            result = client.chat.completions.create(
                response_model=GradingLLMResult,
                messages=[
                    {"role": "system", "content": grading_sys_prompt},
                    {"role": "user", "content": grading_prompt},
                ],
                max_retries=2,
                max_tokens=200,
            )
            return GradingOutcome(
                result=result_map.get(result.result, GradeResult.INCORRECT),
                feedback=result.feedback,
                score=result.score,
            )

        # Fallback: raw LLM call if instructor unavailable
        logger.warning("Instructor unavailable for grading, using raw LLM call")
        response = llm_client.generate(
            messages=[{"role": "user", "content": grading_prompt + '\n\nRespond ONLY with JSON: {"result": "correct"|"partial"|"incorrect", "score": 0.0-1.0, "feedback": "..."}'}],
            system_prompt=grading_sys_prompt,
        )
        result_data = json.loads(response.content)
        return GradingOutcome(
            result=result_map.get(result_data["result"], GradeResult.INCORRECT),
            feedback=result_data.get("feedback", ""),
            score=float(result_data.get("score", 0.0)),
        )

    except Exception as e:
        logger.warning(f"LLM grading failed: {e}")
        return GradingOutcome(
            result=GradeResult.PARTIAL,
            feedback="Let me take another look at your answer...",
            score=0.5,
            details={"error": str(e)},
        )


def grade_answer(
    step: LessonStep,
    student_answer: str,
    llm_client: Optional[BaseLLMClient] = None,
) -> GradingOutcome:
    """
    Main grading function - routes to appropriate grader based on answer type.
    
    Args:
        step: The LessonStep being graded
        student_answer: What the student submitted
        llm_client: Required for free-text grading
        
    Returns:
        GradingOutcome with result, feedback, and score
    """
    if not student_answer.strip():
        return GradingOutcome(
            result=GradeResult.INCORRECT,
            feedback="Please provide an answer.",
            score=0.0,
        )
    
    answer_type = step.answer_type
    expected = step.expected_answer
    
    if answer_type == LessonStep.AnswerType.MULTIPLE_CHOICE:
        # For MCQ, also accept the choice text, not just the letter
        if step.choices:
            # Check if student answered with the choice text
            for i, choice in enumerate(step.choices):
                if normalize_answer(student_answer) == normalize_answer(choice):
                    student_answer = choice  # Normalize to choice text
                    break
        return grade_exact_match(student_answer, expected)
    
    elif answer_type == LessonStep.AnswerType.TRUE_FALSE:
        return grade_true_false(student_answer, expected)
    
    elif answer_type == LessonStep.AnswerType.SHORT_NUMERIC:
        return grade_numeric(student_answer, expected)
    
    elif answer_type == LessonStep.AnswerType.FREE_TEXT:
        if llm_client is None:
            # Fallback to simple match if no LLM
            return grade_exact_match(student_answer, expected)
        return grade_with_llm(
            student_answer=student_answer,
            expected_answer=expected,
            rubric=step.rubric,
            question=step.question,
            llm_client=llm_client,
        )
    
    else:
        # NONE or unknown type - no grading needed
        return GradingOutcome(
            result=GradeResult.CORRECT,
            feedback="",
            score=1.0,
        )
