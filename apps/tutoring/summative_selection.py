"""Per-attempt stratified selection from a summative question bank.

The summative bank holds ~90 questions tagged with `concept_tag` (= a
teaching objective). Each student attempt sees ~30 questions, picked
so every objective has at least one question and the difficulty mix
mirrors the bank.

`select_questions_for_attempt(summative_exam, *, count=None, seed=None)`
returns a list of `ExitTicketQuestion` instances in the order they
should be served. Pass `seed` to make the pick deterministic for
testing or for re-attempts you want to be identical.
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Iterable, List, Optional


def _normalize_tag(tag: str) -> str:
    return ' '.join((tag or '').split()).lower()


def _difficulty_quota(target_count: int) -> dict:
    """Difficulty mix mirrors the DOK target for summatives."""
    easy = round(target_count * 0.30)
    hard = round(target_count * 0.20)
    medium = target_count - easy - hard
    return {'easy': easy, 'medium': medium, 'hard': hard}


def select_questions_for_attempt(
    summative_exam,
    *,
    count: Optional[int] = None,
    seed: Optional[int] = None,
) -> List:
    """Pick `count` questions from the summative's bank, stratified to
    cover every concept_tag (teaching objective) and matching the
    difficulty quota.

    Returns a list of `ExitTicketQuestion` model instances in serve order.
    """
    rng = random.Random(seed)

    # data_interpretation is disabled platform-wide (figures unreliable);
    # filter out any legacy questions of that type so existing banks
    # still serve cleanly.
    questions = list(
        summative_exam.questions.exclude(question_type='data_interpretation')
    )
    if not questions:
        return []

    target = count if count is not None else (
        summative_exam.questions_per_attempt or 30
    )
    target = min(target, len(questions))

    # Group by normalized concept_tag so case/whitespace differences don't fragment.
    by_tag: dict[str, list] = defaultdict(list)
    for q in questions:
        by_tag[_normalize_tag(q.concept_tag)].append(q)

    # Pass 1: seed one question from each tag, picking the most balanced
    # difficulty available within that tag.
    selected: list = []
    selected_ids = set()
    diff_used = {'easy': 0, 'medium': 0, 'hard': 0}
    diff_quota = _difficulty_quota(target)
    tags_in_random_order = list(by_tag.keys())
    rng.shuffle(tags_in_random_order)

    for tag in tags_in_random_order:
        if len(selected) >= target:
            break
        bucket = list(by_tag[tag])
        rng.shuffle(bucket)
        # Prefer a difficulty whose quota isn't yet met.
        bucket.sort(
            key=lambda q: (
                diff_used.get(q.difficulty, 0) >= diff_quota.get(q.difficulty, 0),
                rng.random(),
            )
        )
        for q in bucket:
            if q.id in selected_ids:
                continue
            selected.append(q)
            selected_ids.add(q.id)
            diff_used[q.difficulty] = diff_used.get(q.difficulty, 0) + 1
            break

    # Pass 2: fill remaining slots with a difficulty-aware random draw,
    # respecting (but not blocking on) the quota.
    if len(selected) < target:
        remaining = [q for q in questions if q.id not in selected_ids]
        rng.shuffle(remaining)
        # Prefer questions whose difficulty isn't yet maxed.
        remaining.sort(
            key=lambda q: (
                diff_used.get(q.difficulty, 0) >= diff_quota.get(q.difficulty, 0),
                rng.random(),
            )
        )
        for q in remaining:
            if len(selected) >= target:
                break
            selected.append(q)
            selected_ids.add(q.id)
            diff_used[q.difficulty] = diff_used.get(q.difficulty, 0) + 1

    # Final order: shuffle so students don't see a predictable easy→hard ramp.
    rng.shuffle(selected)
    return selected


def coverage_report(summative_exam) -> dict:
    """Diagnostic — what's in the bank, by tag and difficulty?"""
    questions = list(summative_exam.questions.all())
    by_tag: dict[str, int] = defaultdict(int)
    by_difficulty: dict[str, int] = defaultdict(int)
    for q in questions:
        by_tag[q.concept_tag or '(uncategorized)'] += 1
        by_difficulty[q.difficulty] += 1
    return {
        'total': len(questions),
        'unique_tags': len(by_tag),
        'by_tag': dict(sorted(by_tag.items(), key=lambda kv: -kv[1])),
        'by_difficulty': dict(by_difficulty),
        'target_count': summative_exam.question_bank_size,
        'per_attempt': summative_exam.questions_per_attempt,
    }
