"""
Grader - Evaluates student answers against expected answers.

Supports multiple grading strategies:
1. Exact match (MCQ, True/False)
2. Numeric tolerance (for math answers)
3. LLM-based rubric grading (for free-text)

Design principle: Be generous with correct answers (normalize spacing,
case, etc.) but accurate. When in doubt, use the LLM grader.
"""

import re
import json
from dataclasses import dataclass
from typing import Optional
from enum import Enum

from apps.curriculum.models import LessonStep
from apps.llm.client import BaseLLMClient, LLMResponse


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


def grade_with_llm(
    student_answer: str,
    expected_answer: str,
    rubric: str,
    question: str,
    llm_client: BaseLLMClient,
) -> GradingOutcome:
    """
    Use LLM to grade free-text answers against a rubric.
    
    Returns structured grading with score and feedback.
    """
    grading_prompt = f"""You are grading a student's answer. Be encouraging but accurate.

QUESTION: {question}

EXPECTED ANSWER: {expected_answer}

GRADING RUBRIC:
{rubric if rubric else "The answer should match the expected answer in meaning, not necessarily exact wording."}

STUDENT'S ANSWER: {student_answer}

Grade this answer and respond in this exact JSON format:
{{
    "result": "correct" or "partial" or "incorrect",
    "score": 0.0 to 1.0,
    "feedback": "Brief, encouraging feedback for the student"
}}

Be generous with partial credit if the student shows understanding.
Respond ONLY with the JSON, no other text.
"""
    
    try:
        response = llm_client.generate(
            messages=[{"role": "user", "content": grading_prompt}],
            system_prompt="You are a fair, encouraging grader. Respond only with valid JSON.",
        )
        
        # Parse LLM response
        result_data = json.loads(response.content)
        
        result_map = {
            "correct": GradeResult.CORRECT,
            "partial": GradeResult.PARTIAL,
            "incorrect": GradeResult.INCORRECT,
        }
        
        return GradingOutcome(
            result=result_map.get(result_data["result"], GradeResult.INCORRECT),
            feedback=result_data.get("feedback", ""),
            score=float(result_data.get("score", 0.0)),
            details={"llm_response": response.content, "tokens_used": response.tokens_in + response.tokens_out},
        )
        
    except (json.JSONDecodeError, KeyError) as e:
        # Fallback if LLM doesn't return valid JSON
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
