"""Deterministic grader for summative exam answers.

No LLM calls during submit — every question type is graded by simple
rules so a 30-question exam grades in <100ms. Short-answer and
data-interpretation use a keyword-count rule (the question's
`answer_data.keywords` + `min_keywords`).

Returns per-question results + total score.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple


def _norm(s) -> str:
    return ' '.join(str(s or '').split()).strip().lower()


# Math-symbol normalisation. Students typing on phone / laptop
# keyboards rarely produce degree symbols, true Unicode squareds,
# pi glyphs, etc. — they write "38" instead of "38°", "x^2" instead
# of "x²", "pi" or "3.14" instead of "π". Without normalisation,
# substring keyword matching gives false negatives for
# mathematically correct answers. See the 2026-04-29 exit-ticket
# false-negative bug (student wrote "38, 142, 38, 142", keywords
# were "38°, 142°, 38°, 142°").
_MATH_REPLACEMENTS = (
    ('°', ''),       # degree
    ('º', ''),       # masculine ordinal sometimes used as degree
    ('²', '^2'),
    ('³', '^3'),
    ('√', 'sqrt'),
    ('π', 'pi'),
    ('×', '*'),
    ('÷', '/'),
    ('−', '-'),      # Unicode minus → ASCII hyphen
    ('–', '-'),      # en dash
    ('—', '-'),      # em dash
    ('≤', '<='),
    ('≥', '>='),
    ('≠', '!='),
    ('±', '+-'),
    ('½', '1/2'),
    ('¼', '1/4'),
    ('¾', '3/4'),
)


def _math_norm(s) -> str:
    """Normalise math notation so '38°' and '38' are equivalent for
    keyword matching. Lowercase + whitespace-collapsed first, then
    swap symbol variants for ASCII equivalents.
    """
    text = _norm(s)
    for src, dst in _MATH_REPLACEMENTS:
        text = text.replace(src, dst)
    return text


def grade_one(question, student_answer) -> Tuple[bool, str]:
    """Return (is_correct, reason). `reason` is a short tag for logging."""
    q_type = (question.question_type or 'mcq').lower()
    data = question.answer_data or {}

    if q_type == 'mcq':
        ans = student_answer if isinstance(student_answer, str) else ''
        ok = ans.upper().strip() == (question.correct_answer or '').upper().strip()
        return ok, ('mcq.letter_match' if ok else 'mcq.miss')

    if q_type == 'fill_in_blank':
        blanks = list(data.get('blanks') or [])
        student_blanks = (
            student_answer if isinstance(student_answer, list)
            else [student_answer]
        )
        accept = list(data.get('accept_alternatives') or [])
        correct = 0
        for i, expected in enumerate(blanks):
            given = _norm(student_blanks[i] if i < len(student_blanks) else '')
            if given == _norm(expected):
                correct += 1
                continue
            alts = []
            if i < len(accept):
                alts = [_norm(a) for a in (accept[i] or [])]
            if given and given in alts:
                correct += 1
        # Pass if majority of blanks are correct.
        threshold = max(1, len(blanks) // 2 + (len(blanks) % 2))
        ok = correct >= threshold
        return ok, f'fill_in_blank.{correct}/{len(blanks)}'

    if q_type == 'matching':
        pairs = list(data.get('pairs') or [])
        student_map = student_answer if isinstance(student_answer, dict) else {}
        correct = 0
        for p in pairs:
            left = _norm(p.get('left'))
            expected_right = _norm(p.get('right'))
            given_right = _norm(student_map.get(p.get('left'), ''))
            if expected_right and given_right == expected_right:
                correct += 1
        threshold = max(1, len(pairs) // 2 + (len(pairs) % 2))
        ok = correct >= threshold
        return ok, f'matching.{correct}/{len(pairs)}'

    if q_type in ('short_answer', 'data_interpretation'):
        # Apply math-symbol normalisation to BOTH sides so e.g. "38°"
        # and "38" are equivalent. The numeric-keyword path then
        # extracts pure numbers separately for tolerance comparison.
        text = _math_norm(student_answer if isinstance(student_answer, str) else '')
        if not text:
            return False, 'short.empty'
        keywords = [_math_norm(k) for k in (data.get('keywords') or []) if k]
        if not keywords:
            # No rubric — accept any non-empty response (lenient default).
            return True, 'short.no_rubric'
        # Count distinct keyword hits — substring match against the
        # student's whole answer.
        hits = 0
        for kw in keywords:
            if not kw:
                continue
            # Numeric keywords: extract numbers and compare with tolerance.
            # After _math_norm the degree symbol is gone, so "38°" → "38"
            # which matches as a numeric keyword.
            num_match = re.fullmatch(r'-?\d+(?:\.\d+)?', kw)
            if num_match:
                student_numbers = re.findall(r'-?\d+(?:\.\d+)?', text)
                target = float(kw)
                for sn in student_numbers:
                    try:
                        if abs(float(sn) - target) < 0.01:
                            hits += 1
                            break
                    except ValueError:
                        continue
                continue
            if kw in text:
                hits += 1
        min_kw = int(data.get('min_keywords') or max(1, len(keywords) // 2))
        ok = hits >= min_kw
        return ok, f'short.{hits}/{len(keywords)}'

    # Unknown type → conservative: not correct.
    return False, f'unknown.{q_type}'


def grade_attempt(questions: List, answers: Dict[int, object]) -> dict:
    """Grade an entire attempt.

    `questions`: list of ExitTicketQuestion model instances in serve order.
    `answers`: dict keyed by question id → the student's raw answer.

    Returns:
        {
            'total': int,
            'correct': int,
            'percent': float,
            'per_question': [
                {'question_id', 'is_correct', 'reason', 'concept_tag',
                 'student_answer', 'correct_answer', 'explanation'}
            ],
            'by_concept': {concept_tag: {'correct': int, 'total': int}},
        }
    """
    per_q = []
    by_concept: Dict[str, Dict[str, int]] = {}
    correct = 0

    for q in questions:
        student_answer = answers.get(q.id, '')
        is_correct, reason = grade_one(q, student_answer)
        if is_correct:
            correct += 1
        tag = q.concept_tag or '(uncategorized)'
        bucket = by_concept.setdefault(tag, {'correct': 0, 'total': 0})
        bucket['total'] += 1
        if is_correct:
            bucket['correct'] += 1

        # Surface a "correct answer" string for the review page.
        ad = q.answer_data or {}
        if (q.question_type or 'mcq') == 'mcq':
            correct_answer_text = (
                f"{q.correct_answer}: " +
                getattr(q, f"option_{(q.correct_answer or 'a').lower()}", '')
            )
        elif q.question_type == 'fill_in_blank':
            correct_answer_text = ' / '.join(ad.get('blanks') or [])
        elif q.question_type == 'matching':
            correct_answer_text = '; '.join(
                f"{p.get('left')} → {p.get('right')}" for p in (ad.get('pairs') or [])
            )
        else:
            correct_answer_text = ad.get('model_answer') or ''

        per_q.append({
            'question_id': q.id,
            'is_correct': is_correct,
            'reason': reason,
            'concept_tag': tag,
            'student_answer': student_answer,
            'correct_answer': correct_answer_text,
            'explanation': q.explanation or '',
        })

    total = len(questions)
    return {
        'total': total,
        'correct': correct,
        'percent': (correct / total * 100.0) if total else 0.0,
        'per_question': per_q,
        'by_concept': by_concept,
    }
