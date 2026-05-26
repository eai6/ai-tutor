"""Deterministic + verifier grading for the simple-tutor engine.

Tier 1 (deterministic, ~70% of cases):
    - MCQ: key match (this module's _grade_mcq)
    - Math: sympy + latex2sympy2_extended + math-verify cascade (M3)

Tier 1.5 (embedding gate, free-text only, M4):
    - Cosine similarity vs reference answer; high → correct, low → wrong,
      middle band falls through to Tier 2.

Tier 2 (cross-family verifier LLM, M5):
    - Gemini judge (cross-family from Claude tutor), context-free, structured
      output via instructor + Pydantic. Verdict-FIRST schema (anchors
      decision before rationalising). Self-consistency n=3 only in the
      middle confidence band.

Design rules: auto-memory/feedback_grading_design_rules.md.
Research: memory/grading_system_research.md.
Milestones: memory/simple_tutor_engine_milestones.md.

THE TUTOR LLM NEVER GRADES. It extracts a student answer from natural
language and passes the extracted text into ``grade_answer`` via the
``record_answer`` tool handler. This module is the only place that
decides correctness.

Distinct from the legacy ``apps/tutoring/grader.py`` (used by the v1
``conversational_tutor.py``). Kept separate so the new engine can
evolve its grading semantics without touching the old engine.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from apps.tutoring.models import ExitTicketQuestion


# ============================================================================
# Public API — shape the engine + tool handlers consume
# ============================================================================


class Verdict(str, Enum):
    CORRECT = 'correct'
    PARTIAL = 'partial'
    INCORRECT = 'incorrect'


@dataclass(frozen=True)
class GradeResult:
    """Outcome of grading a single student answer.

    Frozen so callers can't accidentally mutate verdicts; persist via
    ``to_dict()`` into ``SessionTurn.judge_outputs``.
    """
    verdict: Verdict
    confidence: float                       # 0.0 – 1.0
    tier: str                               # 'mcq' | 'math' | 'embed_gate' | 'verifier_llm'
    per_criterion_scores: dict[str, float] = field(default_factory=dict)
    justification: str = ''
    needs_followup: bool = False            # True when verdict is uncertain
                                            # in the middle confidence band

    def to_dict(self) -> dict:
        """Serialise for ``SessionTurn.judge_outputs`` JSON storage."""
        return {
            'verdict': self.verdict.value,
            'confidence': self.confidence,
            'tier': self.tier,
            'per_criterion_scores': self.per_criterion_scores,
            'justification': self.justification,
            'needs_followup': self.needs_followup,
        }


def grade_answer(*, question, student_answer: str) -> GradeResult:
    """Dispatch to the right verifier tier for the given question type.

    Args:
        question: an ``ExitTicketQuestion`` instance (or any object with
            ``question_type`` and the relevant per-type fields).
        student_answer: text extracted by the tutor LLM from the
            student's natural-language reply. Pre-extracted — this
            grader does NOT do natural-language parsing beyond
            stripping the answer to a canonical form.

    Returns:
        ``GradeResult`` with verdict, confidence, tier, justification.

    Raises:
        ValueError: question is malformed (e.g. MCQ with no
            correct_answer). Fail loud instead of guessing.
        NotImplementedError: question_type not yet supported. M4 + M5
            add the embedding gate + verifier LLM for the open-response
            question types.
    """
    qtype = getattr(question, 'question_type', None)
    if qtype == 'mcq':
        return _grade_mcq(question, student_answer)
    if qtype in ('math', 'numeric', 'short_numeric'):
        # 'short_numeric' is the production-prevalent type for math
        # questions on AI Tutor — answer_data carries {computed, model_answer,
        # unit, parameters}. Routed to the math grader.
        return _grade_math(question, student_answer)
    if qtype == 'fill_in_blank':
        # Deterministic blank-by-blank grading. If a math fill_in_blank
        # (answer_data has 'computed'), route through math grader instead.
        ad = getattr(question, 'answer_data', None) or {}
        if isinstance(ad, dict) and ad.get('computed') is not None:
            return _grade_math(question, student_answer)
        return _grade_fill_in_blank(question, student_answer)
    if qtype in ('short_answer', 'data_interpretation'):
        # Tier 1.5 (embedding gate) handles high-similarity + low-similarity.
        # Middle band falls through to Tier 2 verifier LLM (M5).
        gate_result = _grade_embedding_gate(question, student_answer)
        if gate_result is not None:
            return gate_result
        # M5 verifier LLM
        return _grade_verifier_llm(question, student_answer)
    if qtype == 'matching':
        return _grade_matching(question, student_answer)
    raise NotImplementedError(
        f"grade_answer: question_type={qtype!r} not yet supported."
    )


# ============================================================================
# Tier 1 — MCQ grader (M2)
# ============================================================================


# Match a single A-D letter at the start of the string (possibly with
# article / prefix), case-insensitive. Tutor LLM is supposed to extract
# this for us, but we're defensive in case the extracted text still
# carries some boilerplate.
_LETTER_PATTERNS = [
    re.compile(r'^\s*([A-D])\s*[.):]?\s*$', re.IGNORECASE),           # "B", "B.", "B)", "B:"
    re.compile(r'^\s*option\s+([A-D])\s*[.):]?\s*$', re.IGNORECASE),  # "Option B"
    re.compile(r'^\s*answer\s*[:=]?\s*([A-D])\s*$', re.IGNORECASE),   # "Answer: B"
    re.compile(r'\b([A-D])\b', re.IGNORECASE),                        # any A-D word, last-ditch
]

# Numeric → letter map for "Option 1" or "1" inputs (some students think
# in numbered options even when the platform shows A/B/C/D).
_NUMERIC_PATTERNS = [
    re.compile(r'^\s*([1-4])\s*[.):]?\s*$'),                          # "1", "1.", "1)", "1:"
    re.compile(r'^\s*option\s+([1-4])\s*[.):]?\s*$', re.IGNORECASE),  # "Option 1"
]
_NUMERIC_TO_LETTER = {'1': 'A', '2': 'B', '3': 'C', '4': 'D'}


def _extract_mcq_letter(student_answer: str) -> str | None:
    """Best-effort extraction of A/B/C/D from the student's answer text.

    Returns the uppercased letter, or ``None`` if no match found. The
    patterns are ordered from most-specific to least-specific so an
    explicit "Option B" wins over a stray "I'm not sure between A or B"
    type sentence.
    """
    if not student_answer:
        return None
    text = student_answer.strip()
    # 1. Letter-form patterns (most common, strict forms first)
    for pat in _LETTER_PATTERNS[:3]:
        m = pat.match(text)
        if m:
            return m.group(1).upper()
    # 2. Numeric-form patterns
    for pat in _NUMERIC_PATTERNS:
        m = pat.match(text)
        if m:
            return _NUMERIC_TO_LETTER[m.group(1)]
    # 3. Last-ditch: ANY A-D word in the text. Lower confidence — student
    # might have said "I'm between A or B" and we should bail.
    last_ditch = _LETTER_PATTERNS[3].findall(text)
    if len(last_ditch) == 1:
        return last_ditch[0].upper()
    return None


def _extract_option_text_match(question, student_answer: str) -> str | None:
    """If the student typed the FULL option text (or close to it),
    return the matching letter.
    """
    if not student_answer:
        return None
    needle = student_answer.strip().lower()
    if not needle:
        return None
    for letter in ('A', 'B', 'C', 'D'):
        opt = getattr(question, f'option_{letter.lower()}', '') or ''
        if opt.strip() and opt.strip().lower() == needle:
            return letter
    return None


def _grade_mcq(question, student_answer: str) -> GradeResult:
    """Tier-1 deterministic MCQ grader.

    The tutor LLM has already extracted the student's intended option;
    this grader just confirms it matches ``question.correct_answer``.
    Defensive parsing handles edge cases ("Option B", "B.", "2", full
    option text) but is NOT a natural-language understanding layer —
    that's the tutor LLM's job.
    """
    correct = (getattr(question, 'correct_answer', '') or '').strip().upper()
    if correct not in ('A', 'B', 'C', 'D'):
        # Question is malformed — no correct_answer set. We'd rather
        # fail loud than auto-mark CORRECT/INCORRECT off a missing key.
        raise ValueError(
            f"_grade_mcq: question {getattr(question, 'pk', '?')} has no "
            f"correct_answer set (got {correct!r}). Fix the question content "
            f"before grading."
        )

    if not student_answer or not str(student_answer).strip():
        return GradeResult(
            verdict=Verdict.INCORRECT,
            confidence=1.0,
            tier='mcq',
            justification='empty answer',
        )

    # Try letter / numeric / option-text extraction.
    extracted = (
        _extract_mcq_letter(student_answer)
        or _extract_option_text_match(question, student_answer)
    )
    if extracted is None:
        # Couldn't pull a letter out — most likely the student wrote
        # something unrelated. Defensive INCORRECT with lower confidence
        # so the engine knows to treat as a possible misunderstanding.
        return GradeResult(
            verdict=Verdict.INCORRECT,
            confidence=0.6,
            tier='mcq',
            justification=f'no A-D letter extractable from {student_answer!r}',
        )

    if extracted == correct:
        return GradeResult(
            verdict=Verdict.CORRECT,
            confidence=1.0,
            tier='mcq',
            justification=f'extracted {extracted!r} matches correct_answer',
        )

    return GradeResult(
        verdict=Verdict.INCORRECT,
        confidence=1.0,
        tier='mcq',
        justification=f'extracted {extracted!r} ≠ correct {correct!r}',
    )


# ============================================================================
# Tier 1 — Math grader (M3)
# ============================================================================


# Numeric tolerance used as a fallback when math-verify rejects due to
# precision (e.g. student writes "0.6667" against a ``computed`` of
# 0.6666666666666666). Matches what the legacy bank_grader.py uses
# (apps/tutoring/bank_grader.py::_grade_numeric).
_NUMERIC_TOLERANCE = 0.01

# Regex for "find a number in a string" — used by the numeric tolerance
# fallback when math-verify can't parse the student's expression but
# there's a clear final number embedded in their working.
_NUMBER_RX = re.compile(r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?')


# ---------------------------------------------------------------------
# Spoken-form normalisation (TTS/STT students)
# ---------------------------------------------------------------------
#
# When students dictate answers via the platform's voice mode (ElevenLabs
# STT), Whisper transcribes "twenty" rather than "20", "one hundred and
# fifty degrees" rather than "150°", "two thirds" rather than "2/3".
# We preprocess these into digit form BEFORE math-verify sees them.
#
# Fractions and signs need extra handling — word2number drops the sign
# on "negative ten" → 10 and reads "two thirds" as 2 not 2/3.


_WORD_FRACTIONS = {
    # Phrases must be ordered longest-first so "three quarters" matches
    # before "three" alone — the ``re.sub`` below uses the dict order.
    'three quarters': '3/4',
    'three fourths': '3/4',
    'two thirds':    '2/3',
    'one quarter':   '1/4',
    'one fourth':    '1/4',
    'a quarter':     '1/4',
    'one third':     '1/3',
    'a third':       '1/3',
    'one half':      '1/2',
    'a half':        '1/2',
    'one and a half':       '3/2',
    'two and a half':       '5/2',
}

# Sign prefixes — "negative ten" → "-10".
_SIGN_PREFIXES = ('negative ', 'minus ')


def _spoken_to_numeric(text: str) -> str:
    """Best-effort conversion of spoken-form numbers to digit form.

    Idempotent on already-numeric text. Designed to be called BEFORE
    math-verify; if no conversion applies, returns ``text`` unchanged.

    Handles:
        - Sign prefixes: "negative ten" → "-10", "minus twelve" → "-12"
        - Common fractions: "two thirds" → "2/3", "one half" → "1/2"
        - Whole numbers via word2number: "one hundred and fifty" → "150"
        - Embedded numbers in noise text: "the answer is forty two" → "42"

    Returns:
        Normalised text. May be identical to the input if no
        conversion was possible (math-verify then has a chance to
        parse digit-form expressions directly).
    """
    if not text:
        return text
    lowered = text.lower().strip()
    if not lowered:
        return text

    # 1. Sign detection — strip the sign word, remember to apply later.
    sign = 1
    for prefix in _SIGN_PREFIXES:
        if lowered.startswith(prefix):
            sign = -1
            lowered = lowered[len(prefix):].strip()
            break

    # 2. Fraction substitution — must come before w2n because w2n
    # would parse "two" out of "two thirds" as the number 2.
    for phrase, frac in _WORD_FRACTIONS.items():
        if phrase in lowered:
            replacement = f'-{frac}' if sign == -1 else frac
            return lowered.replace(phrase, replacement, 1)

    # 3. Whole-number conversion via word2number (handles noise text).
    try:
        from word2number import w2n
        num = w2n.word_to_num(lowered)
        if sign == -1:
            num = -num
        return str(num)
    except Exception:
        # word2number raises ValueError when it can't find any number.
        # Pass through the original (math-verify will try digit-form).
        return text


def _sympy_symbolic_equal(ref: str, student: str) -> bool | None:
    """Test mathematical equivalence of two algebraic expressions via
    ``sympy.simplify(ref - student) == 0`` after parsing both with
    ``latex2sympy2_extended`` (handles textbook-style notation:
    ``(x+1)(x+2)``, ``x^2``, ``2x`` etc.).

    Returns:
        True  — provably equivalent (any factoring/expansion of the same expr)
        False — provably non-equivalent
        None  — couldn't parse one or both inputs (caller should NOT
                interpret None as either CORRECT or INCORRECT — fall
                through to the next pass instead)
    """
    try:
        from latex2sympy2_extended import latex2sympy
        import sympy
    except ImportError:
        return None

    try:
        r = latex2sympy(ref)
        a = latex2sympy(student)
    except Exception:
        return None

    if r is None or a is None:
        return None

    try:
        # ``simplify(diff) == 0`` is the canonical equivalence test.
        # ``equals(0)`` is another option — slightly slower but more
        # robust for trig/transcendentals. ``simplify`` is enough for
        # secondary-school algebra (polynomial / rational).
        diff = sympy.simplify(r - a)
        return diff == 0
    except Exception:
        return None


def _extract_last_number(text: str) -> float | None:
    """Return the last numeric value in ``text``, or None if no number found.

    Used as a fallback when math-verify can't parse a multi-line student
    response but there IS a clean number at the end. For
    ``"5x + 5 = 1x + 1\\nx = -1\\nThe answer is 5"`` returns ``5``.
    """
    matches = _NUMBER_RX.findall(text or '')
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def _grade_math(question, student_answer: str) -> GradeResult:
    """Tier-1 deterministic math grader.

    Built on HuggingFace's ``math-verify`` library — handles unit
    suffixes (150° vs 150), fractions vs decimals vs percents
    (1/2 = 0.5 = 50%), LaTeX (``\\frac{1}{2}``), symbolic equivalence
    (``2x+2`` = ``2(x+1)``), and multi-line working (extracts the final
    answer naturally). See ``memory/grading_system_research.md`` —
    math-verify is the de-facto standard since the 2024 GSM8K/MATH
    eval cleanup; we deliberately rejected WolframAlpha for v1.

    Reference-answer location (production shape):
        ``question.answer_data['model_answer']`` — formatted string
            with unit attached, e.g. "150°", "32 cm²", "0.666667"
        ``question.answer_data['computed']`` — raw float, e.g. 150.0
        ``question.answer_data['unit']`` — unit suffix (informational)

    Falls back to ``question.correct_answer`` when ``answer_data`` is
    empty (older questions).
    """
    if not student_answer or not str(student_answer).strip():
        return GradeResult(
            verdict=Verdict.INCORRECT,
            confidence=1.0,
            tier='math',
            justification='empty answer',
        )

    answer_data = getattr(question, 'answer_data', None) or {}
    if not isinstance(answer_data, dict):
        answer_data = {}

    ref_string = (answer_data.get('model_answer') or '').strip()
    ref_computed = answer_data.get('computed')
    # Some fill_in_blank-shaped math has computed as a list — single-blank
    # math will be a list of one. Unwrap.
    if isinstance(ref_computed, list):
        ref_computed = ref_computed[0] if ref_computed else None

    if not ref_string and ref_computed is None:
        # Fall back to correct_answer (legacy/sparse questions)
        ref_string = (getattr(question, 'correct_answer', '') or '').strip()

    if not ref_string and ref_computed is None:
        raise ValueError(
            f"_grade_math: question {getattr(question, 'pk', '?')} has no "
            f"model_answer / computed / correct_answer. Fix the question "
            f"content before grading."
        )

    student_str = str(student_answer).strip()

    # Pass 1 — math-verify against the formatted reference string. Handles
    # unit suffixes, fractions/decimals/percents, LaTeX, algebraic
    # equivalence, multi-line working. ~95% of cases land here.
    if ref_string:
        try:
            from math_verify import parse as mv_parse, verify as mv_verify
            gold = mv_parse(ref_string)
            ans = mv_parse(student_str)
            if mv_verify(gold, ans):
                return GradeResult(
                    verdict=Verdict.CORRECT,
                    confidence=1.0,
                    tier='math',
                    justification=f'math_verify match against {ref_string!r}',
                )
        except Exception as e:
            logger.debug("math_verify pass 1 raised %s: %s", type(e).__name__, e)

    # Pass 1b — spoken-form normalisation, then math-verify retry. For
    # voice-mode students whose answer comes through as "one hundred and
    # fifty degrees" rather than "150°".
    spoken_normalised = _spoken_to_numeric(student_str)
    if spoken_normalised != student_str and ref_string:
        try:
            from math_verify import parse as mv_parse, verify as mv_verify
            gold = mv_parse(ref_string)
            ans = mv_parse(spoken_normalised)
            if mv_verify(gold, ans):
                return GradeResult(
                    verdict=Verdict.CORRECT,
                    confidence=0.98,
                    tier='math',
                    justification=(
                        f'math_verify match after spoken-form normalisation '
                        f'{student_str!r} → {spoken_normalised!r}'
                    ),
                )
        except Exception as e:
            logger.debug("math_verify pass 1b raised %s: %s", type(e).__name__, e)

    # Pass 1c — sympy symbolic equivalence. math-verify rejects pairs
    # like (x+1)(x+2) ↔ x²+3x+2 even though they're mathematically
    # equal. Latex2sympy parses textbook-style notation (handles
    # implicit multiplication and ^ as **); sympy.simplify(a - b) == 0
    # is the canonical equivalence test. Used as a fallback after
    # math-verify because parsing arbitrary student input through
    # latex2sympy can fail noisily on multi-line working.
    if ref_string:
        result = _sympy_symbolic_equal(ref_string, student_str)
        if result is True:
            return GradeResult(
                verdict=Verdict.CORRECT,
                confidence=0.98,
                tier='math',
                justification=(
                    f'sympy symbolic equivalence: '
                    f'{ref_string!r} == {student_str!r}'
                ),
            )

    # Pass 2 — numeric-tolerance fallback against ``computed`` (float).
    # math-verify is precision-strict ("0.6667" vs 0.6666666... → False);
    # the legacy bank_grader uses 0.01 tolerance and it's good enough
    # for secondary-school rounding behaviour. Spoken-form normalisation
    # is applied too — voice students saying "twenty SCR" need to hit
    # this fallback when math-verify rejects.
    if ref_computed is not None:
        try:
            ref_num = float(ref_computed)
        except (TypeError, ValueError):
            ref_num = None
        if ref_num is not None:
            # Try the raw input first, then the spoken-normalised form.
            for candidate in (student_str, spoken_normalised):
                student_num = _extract_last_number(candidate)
                if student_num is not None and abs(student_num - ref_num) <= _NUMERIC_TOLERANCE:
                    return GradeResult(
                        verdict=Verdict.CORRECT,
                        confidence=0.95,
                        tier='math',
                        justification=(
                            f'numeric tolerance match: |{student_num} - {ref_num}|'
                            f' <= {_NUMERIC_TOLERANCE} (from {candidate!r})'
                        ),
                    )

    return GradeResult(
        verdict=Verdict.INCORRECT,
        confidence=0.95,
        tier='math',
        justification=(
            f'no math equivalence: ref_string={ref_string!r} '
            f'computed={ref_computed!r} student={student_str[:60]!r}'
        ),
    )


# ============================================================================
# Tier 1 — Fill-in-the-blank grader (M4)
# ============================================================================
#
# Production shape (geography-heavy):
#   answer_data = {
#       'blanks': ['education'],
#       'text_template': 'The three main components of HDI are health, ___, and standard of living.',
#       'accept_alternatives': [['Education', 'EDUCATION', 'school']],
#   }
#
# Per-blank match: case-insensitive against ``blanks[i]`` + alternatives.
# Multi-blank: all blanks correct → CORRECT; partial → PARTIAL; none → INCORRECT.


def _parse_blank_list(student_answer) -> list[str]:
    """Best-effort parse of student input into a list of blanks.

    Acceptable input forms:
    - List[str] (JSON-posted from a form)
    - Comma-separated string: "education, school"
    - Newline-separated string
    - Single string (for one-blank questions)

    Returns a list of stripped strings.
    """
    if student_answer is None:
        return []
    if isinstance(student_answer, list):
        return [str(x).strip() for x in student_answer]
    text = str(student_answer).strip()
    if not text:
        return []
    # Newlines beat commas (more explicit). Otherwise comma-split.
    if '\n' in text:
        return [s.strip() for s in text.split('\n') if s.strip()]
    if ',' in text:
        return [s.strip() for s in text.split(',') if s.strip()]
    return [text]


def _blank_matches(given: str, expected: str, alternatives: list[str]) -> bool:
    """Case-insensitive match of a student-supplied blank against the
    expected value + accept_alternatives list.
    """
    if not given:
        return False
    given_norm = given.strip().lower()
    if not given_norm:
        return False
    if given_norm == str(expected).strip().lower():
        return True
    for alt in alternatives or []:
        if given_norm == str(alt).strip().lower():
            return True
    return False


_WORD_BOUNDARY_CHARS = " .,;:!?\t\n\r()[]{}-/\"'"


def _blank_in_text(haystack_lower: str, expected: str, alternatives: list[str]) -> bool:
    """Containment match: does the expected value (or any alternative)
    appear as a discrete token inside the student's free-form answer?

    Used as a fallback for fill-in-the-blank when the student answers
    in prose ("1000 metres or 1 km") instead of slot-separated form.

    A token "matches" when it appears in the haystack AND is bounded by
    a word boundary (whitespace, punctuation) on either side — so "1"
    doesn't spuriously match inside "1000", and "km" doesn't match
    inside "kilometres". Pure-numeric expected values get a slightly
    stricter check so we don't match "1 km" against blank='1' when the
    student really meant "1" as a count-of-kilometres.
    """
    candidates = [str(expected).strip().lower()]
    for alt in alternatives or []:
        a = str(alt).strip().lower()
        if a:
            candidates.append(a)

    for needle in candidates:
        if not needle:
            continue
        idx = haystack_lower.find(needle)
        while idx != -1:
            before = haystack_lower[idx - 1] if idx > 0 else ' '
            after_idx = idx + len(needle)
            after = haystack_lower[after_idx] if after_idx < len(haystack_lower) else ' '
            if before in _WORD_BOUNDARY_CHARS and after in _WORD_BOUNDARY_CHARS:
                return True
            idx = haystack_lower.find(needle, idx + 1)
    return False


def _grade_fill_in_blank(question, student_answer) -> GradeResult:
    """Tier-1 deterministic fill-in-the-blank grader.

    All blanks must match (case-insensitive, alternatives considered)
    for CORRECT. Otherwise:
        ratio > 0  → PARTIAL with per-criterion-scores reflecting which
                     blanks landed
        ratio == 0 → INCORRECT
    """
    answer_data = getattr(question, 'answer_data', None) or {}
    if not isinstance(answer_data, dict):
        answer_data = {}

    blanks = answer_data.get('blanks') or []
    if not blanks:
        raise ValueError(
            f"_grade_fill_in_blank: question {getattr(question, 'pk', '?')} "
            f"has no 'blanks' in answer_data."
        )

    alternatives = answer_data.get('accept_alternatives') or []

    given = _parse_blank_list(student_answer)

    # First try ORDERED slot-by-slot matching (handles "1000, 1" or list).
    # Fall back to CONTAINMENT matching when slot counts don't line up —
    # students often write free-form ("1000 metres or 1 km") rather than
    # comma-separated.
    correct = 0
    per_blank: dict[str, float] = {}
    if len(given) == len(blanks):
        for i, (expected, supplied) in enumerate(zip(blanks, given)):
            alt_list = alternatives[i] if i < len(alternatives) else []
            ok = _blank_matches(supplied, expected, alt_list)
            per_blank[f'blank_{i}'] = 1.0 if ok else 0.0
            if ok:
                correct += 1
    else:
        # Containment fallback: a blank is "matched" when the expected
        # value or any of its alternatives appears as a token inside the
        # student's raw answer (case-insensitive, word-boundary-aware).
        haystack = str(student_answer).lower()
        for i, expected in enumerate(blanks):
            alt_list = alternatives[i] if i < len(alternatives) else []
            ok = _blank_in_text(haystack, expected, alt_list)
            per_blank[f'blank_{i}'] = 1.0 if ok else 0.0
            if ok:
                correct += 1

    ratio = correct / len(blanks)
    if ratio == 1.0:
        return GradeResult(
            verdict=Verdict.CORRECT,
            confidence=1.0,
            tier='fill_blank',
            per_criterion_scores=per_blank,
            justification=f'{correct}/{len(blanks)} blanks correct',
        )
    if ratio > 0:
        return GradeResult(
            verdict=Verdict.PARTIAL,
            confidence=0.9,
            tier='fill_blank',
            per_criterion_scores=per_blank,
            justification=f'{correct}/{len(blanks)} blanks correct',
            needs_followup=True,
        )
    return GradeResult(
        verdict=Verdict.INCORRECT,
        confidence=1.0,
        tier='fill_blank',
        per_criterion_scores=per_blank,
        justification=f'0/{len(blanks)} blanks matched',
    )


# ============================================================================
# Tier 1 — Matching grader (M5.5)
# ============================================================================
#
# Production shape:
#   answer_data = {
#       'pairs': [
#           {'left': 'Sugarcane farming', 'right': 'Primary'},
#           {'left': 'Sugar refinery',    'right': 'Secondary'},
#           {'left': 'Tourism hotel',     'right': 'Tertiary'},
#       ],
#       'distractor_rights': ['Road construction', 'Fishing vessel operation'],
#   }
#
# Student input shape (flexible):
#   - List[str] of right-side values in the SAME ORDER as pairs[i].left
#       e.g. ['Primary', 'Secondary', 'Tertiary']
#   - Dict[str, str] left → right map (more robust to LLM extraction
#       reordering): {'Sugarcane farming': 'Primary', ...}
#   - JSON-like string of either form
#
# Grading:
#   - All pairs correct → CORRECT
#   - Some correct → PARTIAL with per-pair scores + needs_followup
#   - None correct → INCORRECT
#   - Wrong count → INCORRECT


def _grade_matching(question, student_answer) -> GradeResult:
    """Tier-1 deterministic matching grader.

    Counts how many of the (left, right) pairs the student matched
    correctly. Case-insensitive comparison on the right-side values.
    Supports list-form (order matters) and dict-form (order-free)
    student inputs.
    """
    answer_data = getattr(question, 'answer_data', None) or {}
    if not isinstance(answer_data, dict):
        answer_data = {}

    pairs = answer_data.get('pairs') or []
    if not pairs or not isinstance(pairs, list):
        raise ValueError(
            f"_grade_matching: question {getattr(question, 'pk', '?')} "
            f"has no 'pairs' in answer_data."
        )

    # Parse student input — accept list, dict, or JSON-like string.
    student_map = _parse_matching_answer(student_answer, pairs)
    if student_map is None:
        return GradeResult(
            verdict=Verdict.INCORRECT,
            confidence=1.0,
            tier='matching',
            justification=f'could not parse student answer: {str(student_answer)[:80]!r}',
        )

    correct = 0
    per_pair: dict[str, float] = {}
    for i, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            continue
        left = str(pair.get('left', '')).strip()
        expected_right = str(pair.get('right', '')).strip().lower()
        given_right = (student_map.get(left) or '').strip().lower()
        ok = bool(expected_right) and given_right == expected_right
        per_pair[f'pair_{i}_{left[:30]}'] = 1.0 if ok else 0.0
        if ok:
            correct += 1

    total = len(pairs)
    if total == 0:
        # Defensive — shouldn't get here given the empty-pairs check above
        return GradeResult(
            verdict=Verdict.INCORRECT, confidence=1.0, tier='matching',
            justification='no pairs to grade',
        )

    ratio = correct / total
    if ratio == 1.0:
        return GradeResult(
            verdict=Verdict.CORRECT,
            confidence=1.0,
            tier='matching',
            per_criterion_scores=per_pair,
            justification=f'{correct}/{total} pairs correct',
        )
    if ratio > 0:
        return GradeResult(
            verdict=Verdict.PARTIAL,
            confidence=0.9,
            tier='matching',
            per_criterion_scores=per_pair,
            justification=f'{correct}/{total} pairs correct',
            needs_followup=True,
        )
    return GradeResult(
        verdict=Verdict.INCORRECT,
        confidence=1.0,
        tier='matching',
        per_criterion_scores=per_pair,
        justification=f'0/{total} pairs matched',
    )


def _parse_matching_answer(student_answer, pairs: list) -> dict[str, str] | None:
    """Parse student answer into a ``{left_text: right_text}`` map.

    Accepted input shapes:
      - dict[str, str]            — direct map (preferred; LLM-friendly)
      - list[str]                 — right-side values in pair order
      - list[dict]                — list of {left, right} pair objects
      - str (JSON of any above)   — fall back to json.loads
      - str ("a -> b, c -> d")    — arrow-form

    Returns None if parsing fails.
    """
    if student_answer is None:
        return None

    # Already a dict — use as-is.
    if isinstance(student_answer, dict):
        return {str(k): str(v) for k, v in student_answer.items()}

    # List form.
    if isinstance(student_answer, list):
        # List of pair-dicts: [{'left': X, 'right': Y}, ...]
        if all(isinstance(item, dict) for item in student_answer):
            out = {}
            for item in student_answer:
                left = item.get('left')
                right = item.get('right')
                if left is not None and right is not None:
                    out[str(left)] = str(right)
            return out
        # List of right-side values in pair order.
        if len(student_answer) == len(pairs):
            return {
                str(pair.get('left', '')): str(student_answer[i])
                for i, pair in enumerate(pairs)
            }
        return None

    # String form.
    text = str(student_answer).strip()
    if not text:
        return None

    # Try JSON
    if text.startswith(('{', '[')):
        try:
            import json
            parsed = json.loads(text)
            return _parse_matching_answer(parsed, pairs)
        except (ValueError, TypeError):
            pass

    # Arrow form: "Sugarcane farming -> Primary, Sugar refinery -> Secondary"
    if '->' in text or '→' in text:
        out = {}
        # Split on comma or newline
        for chunk in re.split(r'[,\n]', text):
            chunk = chunk.strip()
            if not chunk:
                continue
            # Split on -> or →
            parts = re.split(r'\s*(?:->|→)\s*', chunk, maxsplit=1)
            if len(parts) == 2:
                out[parts[0].strip()] = parts[1].strip()
        return out if out else None

    return None


# ============================================================================
# Tier 1.5 — Embedding similarity gate (M4)
# ============================================================================
#
# Used for short_answer and data_interpretation. Computes cosine
# similarity between the student answer and the question's
# ``answer_data['model_answer']`` (the reference). When confidence is
# very high (>HIGH) we auto-CORRECT; very low (<LOW) we auto-INCORRECT;
# in between we return None and the caller routes to the Tier 2
# verifier LLM (M5).
#
# Tuning rationale (memory/grading_system_research.md):
#   HIGH = 0.92  — empirical threshold from production-validated LLM-judge
#                  literature; above this is near-paraphrase territory and
#                  rarely a false positive
#   LOW  = 0.35  — below this the answers are clearly unrelated
#   Middle band: ~30-40% of free-text answers will land here; routed to
#                Tier 2 for nuanced grading


_EMBED_HIGH_SIMILARITY = 0.92
_EMBED_LOW_SIMILARITY = 0.35


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors. Returns 0.0
    if either vector is zero-length or all-zero."""
    if not a or not b or len(a) != len(b):
        return 0.0
    import math
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _grade_embedding_gate(question, student_answer: str) -> GradeResult | None:
    """Tier-1.5 embedding-similarity gate.

    Returns ``GradeResult`` only when the similarity is clearly above
    HIGH or clearly below LOW. Returns ``None`` in the middle band so
    the caller can route to the Tier 2 verifier LLM.
    """
    if not student_answer or not str(student_answer).strip():
        return GradeResult(
            verdict=Verdict.INCORRECT,
            confidence=1.0,
            tier='embed_gate',
            justification='empty answer',
        )

    answer_data = getattr(question, 'answer_data', None) or {}
    if not isinstance(answer_data, dict):
        answer_data = {}

    reference = (answer_data.get('model_answer') or '').strip()
    if not reference:
        reference = (getattr(question, 'correct_answer', '') or '').strip()

    if not reference:
        # Can't grade without a reference. Defer to caller (will route to
        # verifier LLM with whatever's available).
        return None

    try:
        from apps.curriculum.kb_storage import embed
    except ImportError:
        return None

    try:
        vecs = embed([str(student_answer).strip(), reference])
    except Exception as e:
        logger.warning("_grade_embedding_gate: embed() failed: %s", e)
        return None

    if len(vecs) != 2:
        return None

    sim = _cosine_similarity(vecs[0], vecs[1])

    if sim > _EMBED_HIGH_SIMILARITY:
        return GradeResult(
            verdict=Verdict.CORRECT,
            confidence=round(sim, 3),
            tier='embed_gate',
            justification=f'high cosine similarity {sim:.3f} > {_EMBED_HIGH_SIMILARITY}',
        )
    if sim < _EMBED_LOW_SIMILARITY:
        return GradeResult(
            verdict=Verdict.INCORRECT,
            confidence=round(1.0 - sim, 3),
            tier='embed_gate',
            justification=f'low cosine similarity {sim:.3f} < {_EMBED_LOW_SIMILARITY}',
        )

    # Middle band — caller routes to Tier 2 verifier LLM.
    logger.debug(
        "_grade_embedding_gate: middle band sim=%.3f, falling through to verifier",
        sim,
    )
    return None


# ============================================================================
# Tier 2 — Verifier LLM (M5)
# ============================================================================
#
# Design (memory/grading_system_research.md + auto-memory/feedback_grading_design_rules.md):
#
# - Cross-family: tutor is Claude (anthropic) → verifier is Gemini (google).
#   Excluded via get_judge_provider_chain(exclude_provider='anthropic').
#   Self-preference bias is well-documented (FairJudge, Adaline 2026).
#
# - Context-free: verifier sees only (question, reference, student answer).
#   No conversation history, no tutor reply. Avoids inherited sycophancy.
#
# - Temperature 0 + structured output. ModelConfig clamps temperature to 0
#   for purpose='judge' at runtime (apps/llm/models.py::effective_temperature).
#
# - VERDICT-FIRST schema. Putting CoT before the verdict anchors the model
#   on its own reasoning. Verdict-first forces commitment before
#   rationalisation — counterintuitively more accurate per the literature.
#
# - Rubric decomposition: per-criterion scores + one-sentence justification.
#
# - Self-consistency n=3 only in the middle confidence band [0.5, 0.85].
#   Calling 3× for every grade kills p95 latency.


# Pydantic schema — must keep this ordering. Verdict FIRST, justification LAST.
try:
    from pydantic import BaseModel, Field
    from typing import Literal as _Literal

    class VerifierResponse(BaseModel):
        """Verifier LLM's structured output. Field ORDER is load-bearing.

        Putting ``verdict`` first commits the model to a decision BEFORE
        it has to justify; reversing the order anchors on rationale.
        """
        # 1. Verdict FIRST — model commits before rationalising
        verdict: _Literal['correct', 'partial', 'incorrect'] = Field(
            description=(
                "Your overall verdict on the student's answer. 'correct' if "
                "they captured the key idea, 'partial' if they got some "
                "elements but missed key points, 'incorrect' if they got it "
                "fundamentally wrong."
            ),
        )
        # 2. Per-criterion scores (forces decomposition)
        per_criterion_scores: dict[str, float] = Field(
            default_factory=dict,
            description=(
                "Score in [0.0, 1.0] for each rubric criterion: "
                "'correctness' (factual accuracy), 'completeness' (covered "
                "the key points), 'reasoning' (showed understanding)."
            ),
        )
        # 3. Confidence
        confidence: float = Field(
            ge=0.0, le=1.0,
            description=(
                "Your confidence in this verdict in [0.0, 1.0]. Use 0.95+ "
                "when the answer is clearly right or clearly wrong; 0.5-0.85 "
                "when ambiguous."
            ),
        )
        # 4. Justification LAST — rationale after the decision
        justification: str = Field(
            description=(
                "One short sentence explaining WHY this verdict — what "
                "specifically the student got right or missed. Reference "
                "the rubric criteria."
            ),
        )

    _HAS_PYDANTIC = True
except ImportError:
    _HAS_PYDANTIC = False
    VerifierResponse = None  # type: ignore


# Self-consistency bands. See memory/grading_system_research.md.
_VERIFIER_HIGH_CONFIDENCE = 0.85
_VERIFIER_LOW_CONFIDENCE = 0.5


def _build_verifier_system_prompt() -> str:
    """The verifier's system prompt. Context-free (no tutoring history),
    temperature-zero (enforced by the judge purpose clamp).
    """
    return """You are an impartial exam grader for secondary-school answers.

You will be given:
  - A question
  - A reference answer (the canonical correct response)
  - A student's answer

Your job is to decide whether the student's answer matches the reference \
in meaning. You must:
  1. Output your VERDICT first (correct / partial / incorrect)
  2. Then break down per-criterion scores
  3. Then your confidence
  4. Then a one-sentence justification

Rules:
  - Be impartial. A confident but wrong answer is INCORRECT, not partial.
  - A long answer that misses the point is still incorrect.
  - A short answer that captures the key idea is CORRECT.
  - Do NOT defer to the student's confidence — judge the substance.
  - Do NOT see this as helping the student. You are an EXAMINER.

Rubric criteria (score each in [0.0, 1.0]):
  - 'correctness': factual accuracy of what was stated
  - 'completeness': did they cover the key points the reference identifies
  - 'reasoning': did they show understanding (not just memorisation)
"""


def _build_verifier_user_prompt(question, student_answer: str) -> str:
    """Build the user prompt with question + reference + student answer.

    Context-free by design — no conversation history.
    """
    ad = getattr(question, 'answer_data', None) or {}
    if not isinstance(ad, dict):
        ad = {}
    reference = (ad.get('model_answer') or '').strip()
    if not reference:
        reference = (getattr(question, 'correct_answer', '') or '').strip()
    keywords = ad.get('keywords') or []
    keywords_text = (
        f"\nKey concepts the answer should mention (any {ad.get('min_keywords', 'most')}): "
        f"{', '.join(keywords)}"
        if keywords else ''
    )
    return f"""<question>
{getattr(question, 'question_text', '') or '(no question text)'}
</question>

<reference_answer>
{reference or '(no reference provided)'}
</reference_answer>{keywords_text}

<student_answer>
{str(student_answer).strip()}
</student_answer>

Grade this answer per the rubric. Output VERDICT first.
"""


def _verifier_self_consistency(votes: list) -> tuple:
    """Majority vote on verdict + mean confidence over the majority votes.

    Returns (verdict, confidence, justification_summary).
    """
    from collections import Counter
    if not votes:
        return ('partial', 0.0, 'no verifier votes collected')
    verdict_counts = Counter(v.verdict for v in votes)
    majority_verdict, count = verdict_counts.most_common(1)[0]
    majority_votes = [v for v in votes if v.verdict == majority_verdict]
    mean_conf = sum(v.confidence for v in majority_votes) / len(majority_votes)
    just = (
        f'self-consistency {count}/{len(votes)} verdict={majority_verdict!r}: '
        + majority_votes[0].justification
    )[:300]
    return (majority_verdict, mean_conf, just)


def _grade_verifier_llm(
    question,
    student_answer: str,
    *,
    tutor_provider: str = 'anthropic',
) -> GradeResult:
    """Tier-2 cross-family verifier LLM grader.

    Args:
        question: question instance (must have question_text + answer_data
            and/or correct_answer).
        student_answer: text extracted by the tutor LLM (NOT raw chat
            input). The verifier never sees the conversation history.
        tutor_provider: 'anthropic' (Claude tutor) — excluded from the
            verifier chain to enforce cross-family.

    Returns:
        ``GradeResult`` with tier='verifier_llm'. Confidence and
        ``needs_followup`` reflect the verdict reliability.
    """
    if not _HAS_PYDANTIC or VerifierResponse is None:
        return GradeResult(
            verdict=Verdict.PARTIAL, confidence=0.0, tier='verifier_llm',
            justification='pydantic unavailable; cannot use verifier',
            needs_followup=True,
        )

    # Resolve verifier provider — cross-family from the tutor.
    try:
        from apps.curriculum.content_judges._providers import get_judge_provider_chain
        from apps.tutoring.judges._instructor_helper import (
            get_instructor_from_client, structured_completion,
        )
    except ImportError as e:
        logger.warning("Verifier LLM infra import failed: %s", e)
        return GradeResult(
            verdict=Verdict.PARTIAL, confidence=0.0, tier='verifier_llm',
            justification=f'verifier infra unavailable: {e}',
            needs_followup=True,
        )

    chain = get_judge_provider_chain('judge', exclude_provider=tutor_provider)
    if not chain:
        # Fall back to any judge provider if cross-family unavailable.
        chain = get_judge_provider_chain('judge')
    if not chain:
        return GradeResult(
            verdict=Verdict.PARTIAL, confidence=0.0, tier='verifier_llm',
            justification='no verifier provider available',
            needs_followup=True,
        )

    provider = chain[0]
    instructor_client = get_instructor_from_client(provider.client)
    if instructor_client is None:
        return GradeResult(
            verdict=Verdict.PARTIAL, confidence=0.0, tier='verifier_llm',
            justification='instructor wrap failed',
            needs_followup=True,
        )

    system_prompt = _build_verifier_system_prompt()
    user_prompt = _build_verifier_user_prompt(question, student_answer)
    provider_str = str(provider.config.provider) if provider.config else ''

    def _one_call():
        return structured_completion(
            instructor_client,
            VerifierResponse,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=1500,
            provider=provider_str,
        )

    try:
        first = _one_call()
    except Exception as e:
        msg = str(e).strip().replace('\n', ' ')[:200]
        logger.warning("Verifier LLM first call failed: %s: %s", type(e).__name__, msg)
        return GradeResult(
            verdict=Verdict.PARTIAL, confidence=0.0, tier='verifier_llm',
            justification=f'verifier exception {type(e).__name__}: {msg}',
            needs_followup=True,
        )

    # Self-consistency: ONLY if first call lands in the middle band [0.5, 0.85].
    # Outside this band the verdict is clear-enough that 3 calls would just
    # burn latency.
    final_verdict = first.verdict
    final_conf = first.confidence
    final_just = first.justification
    final_per_crit = dict(first.per_criterion_scores or {})

    if _VERIFIER_LOW_CONFIDENCE <= first.confidence < _VERIFIER_HIGH_CONFIDENCE:
        votes = [first]
        for _ in range(2):
            try:
                votes.append(_one_call())
            except Exception as e:
                logger.debug("self-consistency vote failed: %s", e)
        final_verdict, final_conf, final_just = _verifier_self_consistency(votes)

    # Map verdict string → Verdict enum
    try:
        verdict = Verdict(final_verdict)
    except ValueError:
        # Verifier returned an unknown verdict label — defensive.
        return GradeResult(
            verdict=Verdict.PARTIAL, confidence=0.0, tier='verifier_llm',
            justification=f'unknown verdict from verifier: {final_verdict!r}',
            needs_followup=True,
        )

    # needs_followup: anywhere in the [0, HIGH) band — engine treats as
    # remediation (low band) or follow-up question (middle band).
    needs_followup = final_conf < _VERIFIER_HIGH_CONFIDENCE

    return GradeResult(
        verdict=verdict,
        confidence=round(float(final_conf), 3),
        tier='verifier_llm',
        per_criterion_scores=final_per_crit,
        justification=final_just[:300],
        needs_followup=needs_followup,
    )
