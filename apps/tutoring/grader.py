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


# --- Numeric parsing for math answers (reusable across grader + tutor engine) ---

# Common unit suffixes we strip before parsing. Extend as needed.
_UNIT_SUFFIX_RE = re.compile(
    r"\s*(kg|g|mg|m|cm|mm|km|l|ml|s|h|hr|hrs|min|mins|seconds?|minutes?|hours?|"
    r"years?|yrs?|days?|meters?|grams?|litres?|liters?|degrees?|°|°c|°f)\b",
    re.IGNORECASE,
)

_MIXED_NUMBER_RE = re.compile(r"^(-?)(\d+)[\s\-_]+(\d+)/(\d+)$")
_FRACTION_RE = re.compile(r"^(-?\d+)/(\d+)$")


def parse_math_answer(text: str) -> Optional[float]:
    """Parse a single student or expected answer string as a float.

    Handles:
      - Integers / decimals: "42", "5.25"
      - Improper fractions: "21/4" -> 5.25
      - Mixed numbers: "3 3/4" -> 3.75, "-3 3/4" -> -3.75
      - Percentages: "75%" -> 0.75
      - Currency: "$42" -> 42.0
      - Thousands commas: "1,234" -> 1234.0
      - Trailing units stripped: "5 1/4 kg" -> 5.25, "90 degrees" -> 90.0

    Returns None if the string cannot be parsed as a single number.
    """
    if text is None:
        return None

    s = str(text).strip()
    if not s:
        return None

    # Percentage handling: strip trailing % and divide by 100 at the end.
    is_percent = s.endswith("%")
    if is_percent:
        s = s[:-1].strip()

    # Strip currency prefix.
    if s.startswith("$"):
        s = s[1:].strip()

    # Strip trailing unit words ("5 1/4 kg" -> "5 1/4", "90 degrees" -> "90").
    # Only strip if a unit suffix is present at the end of the string.
    s_no_unit = _UNIT_SUFFIX_RE.sub("", s).strip()
    if s_no_unit:
        s = s_no_unit

    # Remove thousands commas only when they sit between digits (avoid breaking
    # pathological "1,2,3" inputs, which we treat as None).
    s = re.sub(r"(?<=\d),(?=\d{3}(\D|$))", "", s)

    # Mixed number: "3 3/4" or "-3 3/4". Group 1 is the optional sign.
    m = _MIXED_NUMBER_RE.match(s)
    if m:
        sign, whole, num, den = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            whole_f = float(whole)
            num_f = float(num)
            den_f = float(den)
            if den_f == 0:
                return None
            value = whole_f + num_f / den_f
            if sign == "-":
                value = -value
            return _apply_percent(value, is_percent)
        except ValueError:
            return None

    # Improper fraction: "21/4" or "-21/4".
    m = _FRACTION_RE.match(s)
    if m:
        num, den = m.group(1), m.group(2)
        try:
            den_f = float(den)
            if den_f == 0:
                return None
            return _apply_percent(float(num) / den_f, is_percent)
        except ValueError:
            return None

    # Plain int/float, or fallback fails cleanly.
    try:
        return _apply_percent(float(s), is_percent)
    except ValueError:
        return None


def _apply_percent(value: float, is_percent: bool) -> float:
    return value / 100.0 if is_percent else value


def numeric_equals(a: float, b: float, tolerance: float = 1e-6) -> bool:
    """Relative-tolerance equality for floats.

    Uses absolute tolerance when the reference is near zero.
    """
    if a is None or b is None:
        return False
    if a == b:
        return True
    denom = max(abs(a), abs(b))
    if denom < tolerance:
        return abs(a - b) <= tolerance
    return abs(a - b) / denom <= tolerance


@dataclass
class MathCheckResult:
    """Outcome of a deterministic math answer check.

    Returned by check_math_answer() when both sides parse as numbers.
    None is returned when parsing fails; caller should fall through to
    LLM evaluation.
    """
    is_correct: bool
    student_parsed: float
    expected_parsed: float
    reasoning: str


def check_math_answer(
    student_answer: str,
    expected_answer: str,
    tolerance: float = None,
) -> Optional[MathCheckResult]:
    """Deterministic math answer comparison.

    Parses both sides via parse_math_answer() and compares numerically.
    Returns None if either side fails to parse -- caller falls through to
    the LLM evaluator for free-text explanations.

    Tolerance default: 1e-3 when either side had a decimal point (real-world
    measurement), otherwise 1e-6 (exact for rationals).
    """
    student_parsed = parse_math_answer(student_answer)
    expected_parsed = parse_math_answer(expected_answer)

    if student_parsed is None or expected_parsed is None:
        return None

    if tolerance is None:
        has_decimal = "." in (student_answer or "") or "." in (expected_answer or "")
        tolerance = 1e-3 if has_decimal else 1e-6

    is_correct = numeric_equals(student_parsed, expected_parsed, tolerance=tolerance)
    reasoning = (
        f"numeric match: {student_parsed} == {expected_parsed}"
        if is_correct
        else f"numeric mismatch: student={student_parsed} vs expected={expected_parsed}"
    )
    return MathCheckResult(
        is_correct=is_correct,
        student_parsed=student_parsed,
        expected_parsed=expected_parsed,
        reasoning=reasoning,
    )


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
    Handles: integers, decimals, improper fractions ("21/4"), mixed numbers
    ("3 3/4"), percentages ("75%"), currency ("$42"), and trailing units
    ("5 1/4 kg").
    """
    student_num = parse_math_answer(student_answer)
    expected_num = parse_math_answer(expected_answer)

    if student_num is None:
        return GradingOutcome(
            result=GradeResult.INCORRECT,
            feedback="I couldn't understand that as a number. Please enter a numeric answer.",
            score=0.0,
        )

    if expected_num is None:
        # Fallback to exact match if expected isn't numeric
        return grade_exact_match(student_answer, expected_answer)

    if numeric_equals(student_num, expected_num, tolerance=tolerance):
        return GradingOutcome(
            result=GradeResult.CORRECT,
            feedback="Correct!",
            score=1.0,
        )
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
