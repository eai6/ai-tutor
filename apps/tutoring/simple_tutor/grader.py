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
    # Other question types (matching) route to verifier LLM directly in M5.
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

    if len(given) != len(blanks):
        return GradeResult(
            verdict=Verdict.INCORRECT,
            confidence=1.0,
            tier='fill_blank',
            justification=(
                f'expected {len(blanks)} blank(s), student provided '
                f'{len(given)}'
            ),
        )

    correct = 0
    per_blank: dict[str, float] = {}
    for i, (expected, supplied) in enumerate(zip(blanks, given)):
        alt_list = alternatives[i] if i < len(alternatives) else []
        ok = _blank_matches(supplied, expected, alt_list)
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
# Tier 2 — Verifier LLM (M5, stub for now)
# ============================================================================


def _grade_verifier_llm(question, student_answer: str) -> GradeResult:
    """M5 stub — implemented next milestone."""
    raise NotImplementedError(
        "Tier 2 verifier LLM lands in M5. Until then, short_answer / "
        "data_interpretation / matching questions in the middle "
        "embedding-similarity band fall through to here."
    )
