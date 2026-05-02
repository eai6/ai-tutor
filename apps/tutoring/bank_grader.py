"""Deterministic grader for bank-pulled questions (P3 of
memory/curriculum_tutor_v2_plan.md).

The platform-wide architectural rule: the LLM never calculates correct
answers. It only compares a student's answer to a verified, approved
schema answer that already exists on the question record. This module
implements the comparison.

For every question type the bank can pull, this module knows how to
grade it from data alone — no LLM call:

  question_type    | grade signal source
  ─────────────────|─────────────────────────────────────────────
  mcq              | ExitTicketQuestion.correct_answer (letter)
                   | OR option text match
  short_numeric    | ExitTicketQuestion.answer_data['computed']
                   | OR LessonStep.expected_answer
  fill_in_blank    | answer_data['blanks'] (per-blank list)
  matching         | answer_data['pairs']  (left → right map)
  short_answer     | answer_data['model_answer'] (final-answer field)

Returns a `BankGradeResult` with `is_correct` (True/False/None),
`expected` (what the bank says), `student_parsed` (normalised
student input), and `detail` (per-blank or per-pair breakdown).

`is_correct=None` means the comparison could not be performed
confidently — caller falls through to the existing LLM evaluator
for short_answer working review or for unknown question shapes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from apps.tutoring.student_working_analyzer import safe_eval_arithmetic

logger = logging.getLogger(__name__)


@dataclass
class BankGradeResult:
    is_correct: Optional[bool]
    expected: Any = None
    student_parsed: Any = None
    detail: Dict = field(default_factory=dict)
    skip_reason: str = ""

    def to_metadata(self) -> Dict:
        return {
            "is_correct": self.is_correct,
            "expected": self.expected,
            "student_parsed": self.student_parsed,
            "detail": self.detail,
            "skip_reason": self.skip_reason,
        }


def _norm(s: str) -> str:
    """Normalise a student-typed string for comparison: lowercase,
    collapse whitespace, strip punctuation, drop unit symbols."""
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\.,;:!?]+$", "", s)
    # Drop common unit suffixes — the schema may store "180°" while
    # the student typed "180".
    s = s.replace("°", "").replace(" deg", "").replace(" degrees", "")
    s = s.replace(" m²", "").replace(" m2", "").replace(" sq m", "")
    s = s.replace(" kg", "").replace(" %", "").replace("%", "")
    return s.strip()


def _numeric_equal(a: str, b: str, eps: float = 0.01) -> bool:
    """Compare two strings as numbers when possible; fall back to
    normalised string equality when either side isn't numeric."""
    av = safe_eval_arithmetic(a)
    bv = safe_eval_arithmetic(b)
    if av is not None and bv is not None:
        return abs(av - bv) <= eps
    return _norm(a) == _norm(b)


def grade_bank_response(question, student_input) -> BankGradeResult:
    """Grade a student's input against the bank's stored answer.

    `question` is an ExitTicketQuestion model instance OR a
    duck-typed object with the same attributes (question_type,
    correct_answer, option_a/b/c/d, answer_data).

    `student_input` accepts:
      - str  — typed answer (any question type)
      - list — for fill_in_blank (per-blank list) or matching
               (list of {left, right} dicts)
      - dict — for short_answer (with `final_answer` + `working`)

    Returns BankGradeResult. Never raises; on unrecoverable
    inputs returns is_correct=None with skip_reason set.
    """
    if question is None:
        return BankGradeResult(is_correct=None, skip_reason="no_question")
    if student_input is None:
        return BankGradeResult(is_correct=None, skip_reason="no_student_input")

    qt = (getattr(question, "question_type", "") or "mcq").lower()
    # Strict empty check: empty string only (lists / dicts pass through).
    if isinstance(student_input, str):
        raw = student_input.strip()
        if not raw:
            return BankGradeResult(is_correct=None, skip_reason="empty_student_input")
    else:
        raw = student_input

    if qt == "mcq":
        return _grade_mcq(question, raw)
    if qt == "short_numeric":
        return _grade_numeric(question, raw)
    if qt == "fill_in_blank":
        return _grade_fill_blank(question, raw)
    if qt == "matching":
        return _grade_matching(question, raw)
    if qt == "short_answer":
        return _grade_short_answer(question, raw)
    return BankGradeResult(is_correct=None, skip_reason=f"unknown_type:{qt}")


# ─── MCQ ───────────────────────────────────────────────────────────


def _grade_mcq(question, raw: str) -> BankGradeResult:
    correct_letter = (getattr(question, "correct_answer", "") or "").strip().upper()
    if not correct_letter:
        return BankGradeResult(
            is_correct=None, skip_reason="mcq_no_correct_answer",
        )

    norm = _norm(raw)
    norm_upper = norm.upper().strip()

    # Direct letter answer ("A" / "a" / "A." / "(A)" / "(B).")
    letter_match = re.match(r"^[\(\[]?\s*([A-D])\s*[\)\]\.]*$", str(raw).strip(), re.IGNORECASE)
    if letter_match:
        student_letter = letter_match.group(1).upper()
        return BankGradeResult(
            is_correct=(student_letter == correct_letter),
            expected=correct_letter,
            student_parsed=student_letter,
        )

    # Full-text match: compare against each option, find which
    # option the student picked, then compare to correct_letter.
    options = {
        "A": getattr(question, "option_a", "") or "",
        "B": getattr(question, "option_b", "") or "",
        "C": getattr(question, "option_c", "") or "",
        "D": getattr(question, "option_d", "") or "",
    }
    for letter, text in options.items():
        if not text:
            continue
        if _norm(text) == norm or _numeric_equal(text, raw):
            return BankGradeResult(
                is_correct=(letter == correct_letter),
                expected=correct_letter,
                student_parsed=letter,
                detail={"matched_option_text": text},
            )

    return BankGradeResult(
        is_correct=False,
        expected=correct_letter,
        student_parsed=raw[:60],
        detail={"reason": "no_option_matched"},
    )


# ─── short_numeric ─────────────────────────────────────────────────


def _grade_numeric(question, raw: str) -> BankGradeResult:
    ad = getattr(question, "answer_data", None) or {}
    if not isinstance(ad, dict):
        ad = {}

    # Preferred: computed numeric value from the renderer
    computed = ad.get("computed")
    expected_str = ad.get("model_answer") or ""

    student_v = safe_eval_arithmetic(raw)
    if computed is None and not expected_str:
        # Fall back to LessonStep.expected_answer if attribute exists
        ea = getattr(question, "expected_answer", None) or ""
        if ea:
            return BankGradeResult(
                is_correct=_numeric_equal(raw, ea),
                expected=ea,
                student_parsed=raw[:60],
            )
        return BankGradeResult(is_correct=None, skip_reason="numeric_no_expected")

    if computed is not None and student_v is not None:
        ok = abs(student_v - float(computed)) <= 0.01
        return BankGradeResult(
            is_correct=ok,
            expected=expected_str or computed,
            student_parsed=student_v,
        )
    if expected_str:
        return BankGradeResult(
            is_correct=_numeric_equal(raw, expected_str),
            expected=expected_str,
            student_parsed=raw[:60],
        )
    return BankGradeResult(is_correct=None, skip_reason="numeric_unparseable")


# ─── fill_in_blank ─────────────────────────────────────────────────


def _grade_fill_blank(question, raw: str) -> BankGradeResult:
    """Student input expected as a list-like value: either a Python
    list (when posted as JSON), a comma/newline-separated string, or
    a single value when there's only one blank.
    """
    ad = getattr(question, "answer_data", None) or {}
    blanks = ad.get("blanks") or []
    if not blanks:
        return BankGradeResult(is_correct=None, skip_reason="fill_no_blanks")

    # Tolerant input parsing: list passes through; string splits on
    # ',' or newline.
    if isinstance(raw, list):
        student_blanks = [str(x).strip() for x in raw]
    else:
        student_blanks = [s.strip() for s in re.split(r"[,;\n]", str(raw)) if s.strip()]

    # If the student gave fewer blanks than expected, count missing
    # as wrong rather than skipping.
    per_blank = []
    all_correct = True
    for i, expected in enumerate(blanks):
        student = student_blanks[i] if i < len(student_blanks) else ""
        ok = bool(student) and _numeric_equal(student, str(expected))
        per_blank.append({"expected": str(expected), "student": student, "is_correct": ok})
        if not ok:
            all_correct = False

    return BankGradeResult(
        is_correct=all_correct,
        expected=list(blanks),
        student_parsed=student_blanks,
        detail={"per_blank": per_blank},
    )


# ─── matching ──────────────────────────────────────────────────────


def _grade_matching(question, raw) -> BankGradeResult:
    """Student input expected as either a list of {left, right}
    dicts (preferred — the UI submits structured) or a string with
    'left → right' lines.
    """
    ad = getattr(question, "answer_data", None) or {}
    pairs = ad.get("pairs") or []
    if not pairs:
        return BankGradeResult(is_correct=None, skip_reason="matching_no_pairs")

    expected_map = {_norm(p["left"]): _norm(str(p["right"])) for p in pairs if "left" in p and "right" in p}

    # Parse student answer
    student_pairs: List[Dict] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and "left" in item and "right" in item:
                student_pairs.append({"left": str(item["left"]), "right": str(item["right"])})
    else:
        # Parse "left → right" / "left -> right" / "left = right" lines
        for line in re.split(r"\n+", str(raw).strip()):
            m = re.split(r"\s*(?:→|->|=>|=|:)\s*", line, maxsplit=1)
            if len(m) == 2 and m[0].strip() and m[1].strip():
                student_pairs.append({"left": m[0].strip(), "right": m[1].strip()})

    if not student_pairs:
        return BankGradeResult(is_correct=False, expected=expected_map, student_parsed=raw[:80] if isinstance(raw, str) else raw, detail={"reason": "unparseable_pairs"})

    per_pair = []
    all_correct = True
    for sp in student_pairs:
        left_n = _norm(sp["left"])
        expected_right = expected_map.get(left_n)
        ok = expected_right is not None and _numeric_equal(sp["right"], expected_right)
        per_pair.append({"left": sp["left"], "student_right": sp["right"], "expected_right": expected_right, "is_correct": ok})
        if not ok:
            all_correct = False

    # Any expected-left the student didn't address → wrong
    student_lefts = {_norm(sp["left"]) for sp in student_pairs}
    for el in expected_map:
        if el not in student_lefts:
            all_correct = False
            per_pair.append({"left": el, "student_right": None, "expected_right": expected_map[el], "is_correct": False})

    return BankGradeResult(
        is_correct=all_correct,
        expected=expected_map,
        student_parsed=student_pairs,
        detail={"per_pair": per_pair},
    )


# ─── short_answer (two-field design) ───────────────────────────────


def _grade_short_answer(question, raw) -> BankGradeResult:
    """Two-field design (per the v2 plan):
      - final_answer field is graded deterministically
      - working field is reviewed by an LLM later (this grader does
        NOT touch working). The runtime tutor passes working through
        a separate review step.

    Student input shapes accepted:
      - dict {final_answer, working} — fully structured
      - str — treated as final_answer only (working blank)
    """
    ad = getattr(question, "answer_data", None) or {}
    expected_final = ad.get("model_answer") or ""
    if not expected_final:
        return BankGradeResult(
            is_correct=None, skip_reason="short_answer_no_model_answer",
        )

    if isinstance(raw, dict):
        student_final = (raw.get("final_answer") or raw.get("answer") or "").strip()
    else:
        student_final = str(raw).strip()

    if not student_final:
        return BankGradeResult(
            is_correct=False,
            expected=expected_final,
            student_parsed="",
            detail={"reason": "no_final_answer"},
        )

    return BankGradeResult(
        is_correct=_numeric_equal(student_final, expected_final),
        expected=expected_final,
        student_parsed=student_final,
        detail={"working_grading": "deferred_to_llm_review"},
    )
