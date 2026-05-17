"""Answer-leak detector — structural guard for the tutor's hint-vs-reveal rule.

When the student answers wrong, the tutor should give a HINT (concept-level
clue) and let them try again. Sonnet 4 (current tutoring model) sometimes
soft-reveals by paraphrasing the canonical answer:

    Bank correct option B: "Use it to determine which direction you need to travel"
    Tutor response       : "A compass rose helps you figure out which direction to travel"
                           (different words, same answer — leak)

The rule "DO NOT REVEAL" in the system prompt doesn't bind tightly enough.
This module is the structural belt to that prompt brace.

Design (per memory/hint_vs_reveal_guards_plan.md W1, pilot directive
2026-05-17):

  ┌─ deterministic ─→ leak?  YES/NO ─┐
  ┤                                  ├─ both agree → use verdict
  └─ LLM judge ──────→ leak?  YES/NO ─┘  disagree → arbiter call → final

Both detectors run in PARALLEL for maximum safety. On agreement we trust
the consensus. On disagreement we call the LLM arbiter which sees both
detectors' reasoning and resolves.

Skip cases:
  - wrong_attempts >= 3 → reveal allowed; don't fire.
  - empty response → nothing to scan.

Returns None when no leak; otherwise a LeakVerdict with leaked=True +
diagnostic fields the regen layer uses to suppress the canonical answer
from its context (the leak-aware regen path — see W3).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Set

logger = logging.getLogger(__name__)


# Common English stopwords. Stripped before signature comparison so
# concept words drive the match. Conservative list — keep small to
# avoid over-stripping.
_STOPWORDS: Set[str] = {
    'a', 'an', 'the', 'of', 'to', 'in', 'on', 'for', 'is', 'are',
    'was', 'were', 'be', 'been', 'being', 'that', 'this', 'these',
    'those', 'it', 'its', 'as', 'at', 'by', 'with', 'from', 'or',
    'and', 'but', 'if', 'then', 'so', 'do', 'does', 'did', 'has',
    'have', 'had', 'will', 'would', 'should', 'could', 'can', 'may',
    'might', 'must', 'i', 'you', 'we', 'they', 'he', 'she', 'them',
    'us', 'his', 'her', 'their', 'our', 'your', 'my', 'me', 'about',
    'into', 'than', 'over', 'also', 'just', 'now', 'only', 'one',
    'two', 'three', 'all', 'any', 'each', 'when', 'where', 'while',
    'such', 'no', 'not', 'more', 'less', 'very', 'really',
}

_NGRAM_LEN = 4
_JACCARD_LEAK_THRESHOLD = 0.6


@dataclass
class LeakVerdict:
    leaked: bool
    reason: str
    sources: List[str] = field(default_factory=list)
    deterministic_said: Optional[bool] = None
    llm_said: Optional[bool] = None
    arbiter_said: Optional[bool] = None
    elapsed_ms: int = 0


# ---------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------

_NON_WORD_RE = re.compile(r"[^a-z0-9\s]+")
_MULTI_WS_RE = re.compile(r"\s+")


def _tokenize(text: str) -> List[str]:
    """Lowercase, strip punctuation, drop stopwords, return token list."""
    if not text:
        return []
    s = text.lower()
    s = _NON_WORD_RE.sub(' ', s)
    s = _MULTI_WS_RE.sub(' ', s).strip()
    return [t for t in s.split() if t and t not in _STOPWORDS]


def _ngrams(tokens: List[str], n: int) -> Set[tuple]:
    """All contiguous n-token n-grams from `tokens`."""
    if len(tokens) < n:
        return set()
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def _jaccard(a: Set, b: Set) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union) if union else 0.0


# ---------------------------------------------------------------------
# Deterministic check
# ---------------------------------------------------------------------

# "the answer is B" / "the correct option is C" — strong reveal signal.
_LETTER_STATEMENT_RE = re.compile(
    r"\bthe\s+(?:answer|correct\s+(?:option|choice|answer))\s+(?:is|would\s+be)\s+\(?([A-D])\b",
    re.IGNORECASE,
)


def _deterministic_check_mcq(
    response: str,
    correct_letter: str,
    options: dict,
    question_stem: str,
) -> Optional[str]:
    """Return a leak-reason string when the response leaks the MCQ answer,
    else None.

    Steps:
      1. Letter-statement regex match → leak.
      2. Build the correct option's signature (tokens + 4-grams), then
         SUBTRACT tokens/n-grams that also appear in the question stem
         OR in 2+ other options — those are generic/topical and OK to
         reuse in a hint.
      3. If any remaining 4-gram appears in the normalised response →
         leak.
      4. Else if Jaccard(remaining_tokens, response_tokens) >= 0.6 →
         leak.
    """
    if not response or not correct_letter:
        return None

    # Letter statement
    m = _LETTER_STATEMENT_RE.search(response)
    if m and m.group(1).upper() == correct_letter.upper():
        return f"letter_statement: response stated 'the answer is {correct_letter}'"

    correct_text = (options or {}).get(correct_letter.upper()) or ''
    if not correct_text:
        return None

    correct_tokens = _tokenize(correct_text)
    if not correct_tokens:
        return None

    response_tokens = _tokenize(response)
    response_ngrams = _ngrams(response_tokens, _NGRAM_LEN)

    # False-positive subtraction: tokens/n-grams that appear in the
    # question stem OR in 2+ other options are generic/topical.
    stem_tokens = set(_tokenize(question_stem))
    other_token_counts: dict = {}
    for k, v in (options or {}).items():
        if k.upper() == correct_letter.upper():
            continue
        for tok in _tokenize(v or ''):
            other_token_counts[tok] = other_token_counts.get(tok, 0) + 1
    generic_tokens = stem_tokens | {
        t for t, n in other_token_counts.items() if n >= 2
    }

    distinctive_tokens = [t for t in correct_tokens if t not in generic_tokens]
    distinctive_ngrams = _ngrams(distinctive_tokens, _NGRAM_LEN)

    # N-gram match on distinctive content
    leak_ngram = response_ngrams & distinctive_ngrams
    if leak_ngram:
        sample = ' '.join(next(iter(leak_ngram)))
        return f"ngram: response contains distinctive {_NGRAM_LEN}-gram from correct option ({sample!r})"

    # Jaccard on distinctive tokens
    if distinctive_tokens:
        jacc = _jaccard(set(distinctive_tokens), set(response_tokens))
        if jacc >= _JACCARD_LEAK_THRESHOLD:
            return f"jaccard: {jacc:.2f} overlap on distinctive tokens of correct option"

    return None


def _deterministic_check_text(
    response: str,
    expected_answer: str,
    explanation: str,
    question_stem: str,
) -> Optional[str]:
    """Same shape as the MCQ check but for short_answer / numeric / FIB.
    Builds distinctive content from expected_answer + explanation; n-gram
    + Jaccard against the response.
    """
    if not response:
        return None
    canonical = ((expected_answer or '') + ' ' + (explanation or '')).strip()
    if not canonical:
        return None

    canonical_tokens = _tokenize(canonical)
    if not canonical_tokens:
        return None

    response_tokens = _tokenize(response)
    response_ngrams = _ngrams(response_tokens, _NGRAM_LEN)

    stem_tokens = set(_tokenize(question_stem))
    distinctive_tokens = [t for t in canonical_tokens if t not in stem_tokens]
    distinctive_ngrams = _ngrams(distinctive_tokens, _NGRAM_LEN)

    leak_ngram = response_ngrams & distinctive_ngrams
    if leak_ngram:
        sample = ' '.join(next(iter(leak_ngram)))
        return f"ngram: response contains distinctive {_NGRAM_LEN}-gram from canonical ({sample!r})"

    if distinctive_tokens:
        jacc = _jaccard(set(distinctive_tokens), set(response_tokens))
        if jacc >= _JACCARD_LEAK_THRESHOLD:
            return f"jaccard: {jacc:.2f} overlap on distinctive canonical tokens"

    return None


def _deterministic_check(
    response: str,
    bank_question,
) -> Optional[str]:
    """Dispatch on question type. Returns a reason string when a leak is
    detected, else None.

    For chat-authored questions (bank_question is None) the deterministic
    check has nothing to compare against — returns None and lets the LLM
    judge handle it solo.
    """
    if bank_question is None:
        return None

    q_type = (getattr(bank_question, 'question_type', None) or '').lower()
    stem = (getattr(bank_question, 'question_text', None)
            or getattr(bank_question, 'question', None)
            or getattr(bank_question, 'teacher_script', '')
            or '')

    if q_type == 'mcq':
        correct_letter = (getattr(bank_question, 'correct_answer', '') or '').strip().upper()
        options = {
            'A': getattr(bank_question, 'option_a', '') or '',
            'B': getattr(bank_question, 'option_b', '') or '',
            'C': getattr(bank_question, 'option_c', '') or '',
            'D': getattr(bank_question, 'option_d', '') or '',
        }
        return _deterministic_check_mcq(response, correct_letter, options, stem)

    # short_answer / numeric / FIB / data_interpretation — text-canonical
    expected = (
        getattr(bank_question, 'expected_answer', None)
        or getattr(bank_question, 'correct_answer', None)
        or ''
    )
    answer_data = getattr(bank_question, 'answer_data', None) or {}
    if isinstance(answer_data, dict):
        expected = expected or (answer_data.get('model_answer') or '')
    explanation = getattr(bank_question, 'explanation', '') or ''
    return _deterministic_check_text(response, expected, explanation, stem)


# ---------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------

def detect_answer_leak(
    response: str,
    bank_question,                       # ExitTicketQuestion | LessonStep | None
    chat_authored_q: Optional[str],      # last tutor question text when chat-authored
    wrong_attempts: int,
    llm_client,                          # required — LLM judge always runs (when not skipped)
    reveal_threshold: int = 3,           # difficulty-tiered; caller passes from _reveal_threshold()
) -> Optional[LeakVerdict]:
    """Detect whether the tutor response leaked the canonical answer.

    Parallel detection — deterministic + LLM judge always run together.
    Agreement: trust the consensus. Disagreement: arbiter call resolves.
    Returns None when no leak detected; LeakVerdict when leak detected.

    `reveal_threshold` is the difficulty-tiered wrong-attempt count
    at which reveal becomes legitimate; the detector skips once that
    threshold is met so the canonical walkthrough isn't flagged.
    """
    import time
    t0 = time.monotonic()

    # Skip cases
    if not response or not response.strip():
        return None
    if wrong_attempts >= reveal_threshold:
        return None
    if bank_question is None and not chat_authored_q:
        return None

    # ---- deterministic ----
    det_reason = _deterministic_check(response, bank_question) if bank_question else None
    det_leaked = det_reason is not None

    # ---- LLM judge (always runs, even on chat-authored) ----
    llm_leaked, llm_reason = _llm_check(
        response=response,
        bank_question=bank_question,
        chat_authored_q=chat_authored_q,
        llm_client=llm_client,
    )

    sources = []
    if det_leaked:
        sources.append('deterministic')
    if llm_leaked:
        sources.append('llm')

    # ---- agreement / disagreement ----
    if det_leaked == llm_leaked:
        # Agreement (both leak OR both clean)
        if not det_leaked:
            return None
        elapsed = int((time.monotonic() - t0) * 1000)
        reason = det_reason or llm_reason or 'both detectors flagged leak'
        logger.info(
            "[LeakDetect] AGREE leak — det=%s, llm=%s — %d ms",
            det_reason, llm_reason, elapsed,
        )
        return LeakVerdict(
            leaked=True, reason=reason, sources=sources,
            deterministic_said=det_leaked, llm_said=llm_leaked,
            elapsed_ms=elapsed,
        )

    # Disagreement → arbiter
    logger.warning(
        "[LeakDetect] DISAGREE det=%s llm=%s — calling arbiter",
        det_leaked, llm_leaked,
    )
    arb_leaked, arb_reason = _arbiter_call(
        response=response,
        bank_question=bank_question,
        chat_authored_q=chat_authored_q,
        det_verdict=det_leaked, det_reason=det_reason or '',
        llm_verdict=llm_leaked, llm_reason=llm_reason or '',
        llm_client=llm_client,
    )
    elapsed = int((time.monotonic() - t0) * 1000)
    sources.append('arbiter')
    logger.info(
        "[LeakDetect] ARBITER said leak=%s reason=%r — %d ms",
        arb_leaked, arb_reason[:120], elapsed,
    )
    if not arb_leaked:
        return None
    return LeakVerdict(
        leaked=True, reason=arb_reason, sources=sources,
        deterministic_said=det_leaked, llm_said=llm_leaked,
        arbiter_said=arb_leaked, elapsed_ms=elapsed,
    )


# ---------------------------------------------------------------------
# LLM judge + arbiter (route through W6 unified grader)
# ---------------------------------------------------------------------

def _llm_check(
    response: str,
    bank_question,
    chat_authored_q: Optional[str],
    llm_client,
) -> (bool, str):
    """Call the unified grader's JUDGE_LEAK path. Returns (leaked, reason)."""
    if llm_client is None:
        return False, "no_llm_client"

    from apps.tutoring.exit_ticket_grader import (
        BatchLeakItem, JudgmentType, run_grading_batch,
    )

    # Build the item — bank Q if present, else chat-authored fallback
    if bank_question is not None:
        q_type = (getattr(bank_question, 'question_type', None) or '').lower()
        stem = (getattr(bank_question, 'question_text', None)
                or getattr(bank_question, 'question', None)
                or getattr(bank_question, 'teacher_script', '')
                or '')
        if q_type == 'mcq':
            correct_letter = (getattr(bank_question, 'correct_answer', '') or '').strip().upper()
            options = {
                'A': getattr(bank_question, 'option_a', '') or '',
                'B': getattr(bank_question, 'option_b', '') or '',
                'C': getattr(bank_question, 'option_c', '') or '',
                'D': getattr(bank_question, 'option_d', '') or '',
            }
            canonical = options.get(correct_letter, '')
        else:
            options = None
            answer_data = getattr(bank_question, 'answer_data', None) or {}
            canonical = (
                getattr(bank_question, 'expected_answer', None)
                or getattr(bank_question, 'correct_answer', None)
                or (answer_data.get('model_answer') if isinstance(answer_data, dict) else None)
                or ''
            )
    else:
        # Chat-authored: question came from prior tutor turn; no canonical
        # so we pass the chat-authored question as both stem and canonical
        # — the judge's job becomes "did the response give away the
        # expected answer to its own question?"
        stem = chat_authored_q or ''
        canonical = chat_authored_q or ''
        options = None

    item = BatchLeakItem(
        index=0,
        question_text=stem,
        canonical_answer=canonical,
        response=response,
        options=options,
    )
    try:
        results = run_grading_batch(
            [item], judgment_type=JudgmentType.JUDGE_LEAK, llm_client=llm_client,
        )
    except Exception as exc:
        logger.warning("[LeakDetect] LLM judge crash: %s", exc)
        return False, f"llm_crash: {exc}"

    if not results:
        return False, "llm_no_result"
    r = results[0]
    return bool(r.leaked), r.reason or ''


def _arbiter_call(
    response: str,
    bank_question,
    chat_authored_q: Optional[str],
    det_verdict: bool,
    det_reason: str,
    llm_verdict: bool,
    llm_reason: str,
    llm_client,
) -> (bool, str):
    """Arbiter resolves det/llm disagreement. Returns (leaked, reason)."""
    if llm_client is None:
        # Without an arbiter we fall back to LLM (more semantic).
        return llm_verdict, f"no_arbiter_client; defaulted to llm verdict ({llm_reason})"

    from apps.tutoring.exit_ticket_grader import (
        BatchLeakItem, JudgmentType, run_grading_batch,
    )

    # Reuse the same canonical / stem we'd give the LLM judge.
    if bank_question is not None:
        q_type = (getattr(bank_question, 'question_type', None) or '').lower()
        stem = (getattr(bank_question, 'question_text', None)
                or getattr(bank_question, 'question', None) or '')
        if q_type == 'mcq':
            correct_letter = (getattr(bank_question, 'correct_answer', '') or '').strip().upper()
            options = {
                'A': getattr(bank_question, 'option_a', '') or '',
                'B': getattr(bank_question, 'option_b', '') or '',
                'C': getattr(bank_question, 'option_c', '') or '',
                'D': getattr(bank_question, 'option_d', '') or '',
            }
            canonical = options.get(correct_letter, '')
        else:
            options = None
            answer_data = getattr(bank_question, 'answer_data', None) or {}
            canonical = (
                getattr(bank_question, 'expected_answer', None)
                or getattr(bank_question, 'correct_answer', None)
                or (answer_data.get('model_answer') if isinstance(answer_data, dict) else None)
                or ''
            )
    else:
        stem = chat_authored_q or ''
        canonical = chat_authored_q or ''
        options = None

    item = BatchLeakItem(
        index=0,
        question_text=stem,
        canonical_answer=canonical,
        response=response,
        options=options,
        arbiter=True,
        det_verdict=det_verdict, det_reason=det_reason,
        llm_verdict=llm_verdict, llm_reason=llm_reason,
    )
    try:
        results = run_grading_batch(
            [item],
            judgment_type=JudgmentType.JUDGE_LEAK,
            llm_client=llm_client,
            arbiter=True,
        )
    except Exception as exc:
        logger.warning("[LeakDetect] arbiter crash: %s — defaulting to LLM verdict", exc)
        return llm_verdict, f"arbiter_crash; defaulted to llm verdict ({llm_reason})"

    if not results:
        return llm_verdict, f"arbiter_empty; defaulted to llm verdict ({llm_reason})"
    r = results[0]
    return bool(r.leaked), r.reason or 'arbiter_no_reason'
