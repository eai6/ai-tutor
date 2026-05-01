"""Course-level summative exam generator — aggregates per-lesson banks.

The summative for a course is built by sampling questions from each
lesson's existing exit-ticket bank. If a lesson doesn't have an exit
ticket yet, we kick the existing per-lesson generator
(`apps.curriculum.content_generator.generate_exit_ticket_for_lesson`)
to make one. Lessons in a course → ~3 questions each → ~90 in the bank.

Why aggregate instead of one big LLM call:
  - Each per-lesson generator call is small (~35 questions, ~10k tokens),
    parallelizable, and already battle-tested.
  - One bad lesson doesn't poison the whole bank.
  - Zero net new LLM cost when exit tickets already exist (the common
    case after content generation has run).
  - Coverage is automatic: every lesson contributes, and each lesson's
    teaching objective ends up represented.

See `memory/summative_assessments_plan.md`.
"""

from __future__ import annotations

import logging
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

logger = logging.getLogger(__name__)


# Summative bank size policy (revised 2026-04-29):
# Sample at least N questions per teaching objective so every
# objective has variety in the bank. Total bank size scales with
# the course (>= MIN_PER_LESSON × number_of_lessons), rather than
# being capped at a fixed total. The previous fixed-90 cap meant a
# course with 50+ lessons got 1-2 questions per objective, which
# made the per-attempt sampler hit the same questions repeatedly.
SUMMATIVE_MIN_PER_LESSON = 5
SUMMATIVE_PER_ATTEMPT = 30

# Kept for backward compatibility with callers passing target_count.
# Not used as a hard cap any more — see comment above.
SUMMATIVE_TARGET_COUNT = 90


def _ensure_exit_ticket_for(lesson, institution_id: int) -> int:
    """If the lesson has no exit ticket, generate one. Returns the
    number of questions in its bank afterwards (0 on failure)."""
    from apps.tutoring.models import ExitTicket
    from apps.curriculum.content_generator import generate_exit_ticket_for_lesson

    et = ExitTicket.objects.filter(
        lesson=lesson,
        assessment_type=ExitTicket.AssessmentType.EXIT_TICKET,
    ).first()
    if et and et.questions.exists():
        return et.questions.count()

    try:
        result = generate_exit_ticket_for_lesson(lesson, institution_id=institution_id)
    except Exception as e:
        logger.warning(f"summative: exit-ticket generation failed for {lesson.title}: {e}")
        return 0
    if not (result or {}).get('success'):
        logger.warning(
            f"summative: no exit ticket for {lesson.title}: {result.get('error') if result else 'unknown'}"
        )
        return 0
    et = ExitTicket.objects.filter(
        lesson=lesson,
        assessment_type=ExitTicket.AssessmentType.EXIT_TICKET,
    ).first()
    return et.questions.count() if et else 0


def _sample_questions(et, k: int, rng: random.Random) -> List:
    """Pick up to k questions from a lesson exit-ticket bank, biased to
    the same difficulty mix we want overall (~30/45/20)."""
    # Skip data_interpretation — disabled platform-wide.
    questions = list(et.questions.exclude(question_type='data_interpretation'))
    if not questions:
        return []
    if len(questions) <= k:
        rng.shuffle(questions)
        return questions

    by_diff: Dict[str, List] = {'easy': [], 'medium': [], 'hard': []}
    for q in questions:
        by_diff.setdefault(q.difficulty or 'medium', []).append(q)
    for bucket in by_diff.values():
        rng.shuffle(bucket)

    quotas = {
        'easy': max(1, round(k * 0.30)),
        'medium': max(1, round(k * 0.45)),
        'hard': max(0, round(k * 0.20)),
    }
    quotas['medium'] = k - quotas['easy'] - quotas['hard']
    quotas['medium'] = max(0, quotas['medium'])

    picked: List = []
    for diff, want in quotas.items():
        bucket = by_diff.get(diff) or []
        picked.extend(bucket[:want])

    # Top up if quotas under-fill (e.g., bank is heavy easy/medium)
    if len(picked) < k:
        remaining = [q for q in questions if q not in picked]
        rng.shuffle(remaining)
        picked.extend(remaining[: k - len(picked)])
    return picked[:k]


def _process_lesson(lesson_id: int, institution_id: int, k: int, seed_for_lesson: int) -> Dict:
    """Worker: ensure-then-sample for one lesson. Runs in a thread."""
    import django.db
    django.db.connections.close_all()
    from apps.curriculum.models import Lesson
    from apps.tutoring.models import ExitTicket
    from apps.curriculum.content_generator import combined_objectives_for_lesson

    try:
        lesson = Lesson.objects.select_related('unit', 'unit__course').get(id=lesson_id)
    except Lesson.DoesNotExist:
        return {'lesson_id': lesson_id, 'sampled': 0, 'error': 'not_found'}

    _ensure_exit_ticket_for(lesson, institution_id)
    et = ExitTicket.objects.filter(
        lesson=lesson,
        assessment_type=ExitTicket.AssessmentType.EXIT_TICKET,
    ).first()
    if not et:
        return {'lesson_id': lesson_id, 'sampled': 0, 'error': 'no_exit_ticket'}

    rng = random.Random(seed_for_lesson)
    picks = _sample_questions(et, k, rng)
    # combined_objectives_for_lesson is the SINGLE SOURCE OF TRUTH —
    # it now falls back to lesson.objective and lesson.title internally,
    # so every summative + competency-map consumer sees the same tags.
    #
    # Design rule (2026-04-28): summative questions are tagged at
    # LESSON-OBJECTIVE granularity, period. The fine-grained
    # concept_tag on the source ExitTicketQuestion is kept for
    # tutoring-engine scaffolding within a lesson, but never used for
    # cross-attempt reporting. This is the join key the class +
    # student competency map uses, so it has to match the matrix's
    # canonical list (which comes from this same helper).
    objectives = combined_objectives_for_lesson(lesson)
    primary_objective = objectives[0] if objectives else lesson.title

    # Snapshot fields so we can recreate as new summative questions in the parent thread.
    payload = []
    for q in picks:
        payload.append({
            'question_type': q.question_type,
            'question_text': q.question_text,
            'option_a': q.option_a, 'option_b': q.option_b,
            'option_c': q.option_c, 'option_d': q.option_d,
            'correct_answer': q.correct_answer,
            'answer_data': q.answer_data,
            'explanation': q.explanation,
            # concept_tag = lesson-level objective. This is what the
            # competency matrix joins on, so summative questions are
            # tagged at lesson granularity regardless of the source
            # question's tag.
            'concept_tag': primary_objective[:200],
            # enabling_objective = the sub-skill the source question
            # tested. Carried forward so post-summative remediation
            # can target the failing sub-skill (e.g. "you missed
            # 'reverse calculation' on lesson 7"), not just the
            # whole lesson. Empty when the source question pre-dates
            # the field (older content, not yet regenerated).
            'enabling_objective': (q.enabling_objective or '')[:500],
            'difficulty': q.difficulty,
        })

    return {
        'lesson_id': lesson_id,
        'lesson_title': lesson.title,
        'sampled': len(payload),
        'questions': payload,
    }


def generate_summative_for_course(
    course,
    *,
    min_per_lesson: int = SUMMATIVE_MIN_PER_LESSON,
    max_workers: int = 3,
    target_count: int = None,  # deprecated; kept for callsite compatibility
) -> Dict:
    """Build the summative bank by sampling each lesson's exit ticket.

    Sampling policy (revised 2026-04-29): each lesson contributes
    AT LEAST `min_per_lesson` questions to the bank — every teaching
    objective therefore gets variety in the bank rather than landing
    on a single representative question. Total bank size scales with
    the course (>= min_per_lesson × N_lessons).

    The legacy `target_count` parameter is accepted but no longer
    enforced as a hard cap; if the produced bank exceeds it, we don't
    trim. Callers should migrate to passing `min_per_lesson` directly.

    Returns {success, questions_created, lessons_processed, error}.
    """
    from apps.curriculum.models import Lesson
    from apps.tutoring.models import ExitTicket, ExitTicketQuestion
    from apps.accounts.models import Institution
    from django.db import transaction

    institution_id = course.institution_id or Institution.get_global().id

    lessons = list(
        Lesson.objects.filter(unit__course=course)
        .order_by('unit__order_index', 'order_index')
        .values_list('id', flat=True)
    )
    if not lessons:
        return {'success': False, 'error': 'No lessons in this course.'}

    # ≥ min_per_lesson questions per lesson, period. The bank's total
    # size is min_per_lesson × N — no global cap. Lessons whose exit
    # ticket has fewer than min_per_lesson questions contribute what
    # they have (sampler clamps to available).
    per_lesson_k = [min_per_lesson] * len(lessons)
    target_total = min_per_lesson * len(lessons)

    print(
        f"[Summative] {course.title}: {len(lessons)} lessons × "
        f"{min_per_lesson} qs/lesson → bank target {target_total}",
        flush=True,
    )

    # Run lessons in parallel; each call is a self-contained ensure+sample.
    rng_master = random.Random(course.id)
    aggregated_payload: List[dict] = []
    lessons_with_zero: List[int] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for i, lesson_id in enumerate(lessons):
            seed = rng_master.randint(0, 1_000_000)
            futures[pool.submit(_process_lesson, lesson_id, institution_id, per_lesson_k[i], seed)] = lesson_id
        done = 0
        total = len(futures)
        for fut in as_completed(futures):
            lesson_id = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                logger.warning(f"summative lesson {lesson_id} crashed: {e}")
                lessons_with_zero.append(lesson_id)
                done += 1
                continue
            if result.get('sampled'):
                aggregated_payload.extend(result['questions'])
            else:
                lessons_with_zero.append(lesson_id)
            done += 1
            if done % 5 == 0 or done == total:
                print(f"[Summative] {course.title}: {done}/{total} lessons processed, "
                      f"{len(aggregated_payload)} qs aggregated", flush=True)

    if not aggregated_payload:
        return {
            'success': False,
            'error': f'No questions could be sampled (failed lessons: {len(lessons_with_zero)} / {len(lessons)}).',
        }

    # No global trim — the bank scales with the course. Shuffle the
    # aggregated payload so per-lesson questions don't all land
    # adjacent to each other in the bank (the sampler does its own
    # shuffle per attempt, but a shuffled bank is friendlier for
    # teacher review).
    rng_master.shuffle(aggregated_payload)

    with transaction.atomic():
        # Replace the SUMMATIVE bank IN-PLACE so we don't cascade-wipe
        # ExitTicketAttempt rows (baseline / final / retake purposes
        # all live on the summative ticket). Pattern mirrors the
        # lesson exit-ticket regen fix in commit 25c62a2: drop the
        # OLD questions only, keep the ExitTicket row, recreate
        # questions on the same row. ExitTicketAttempt's FK is to
        # ExitTicket (not ExitTicketQuestion), so attempts survive.
        existing = ExitTicket.objects.filter(
            course=course,
            assessment_type=ExitTicket.AssessmentType.SUMMATIVE,
        ).first()
        new_instructions = (
            f"Course summative exam covering all teaching objectives in {course.title}. "
            f"You'll see {SUMMATIVE_PER_ATTEMPT} questions out of "
            f"{len(aggregated_payload)} — stratified so every objective is represented."
        )
        if existing is not None:
            existing.questions.all().delete()
            summative = existing
            summative.question_bank_size = len(aggregated_payload)
            summative.questions_per_attempt = SUMMATIVE_PER_ATTEMPT
            summative.passing_score = int(SUMMATIVE_PER_ATTEMPT * 0.7)
            summative.time_limit_minutes = 60
            summative.instructions = new_instructions
            summative.save(update_fields=[
                'question_bank_size', 'questions_per_attempt',
                'passing_score', 'time_limit_minutes', 'instructions',
                'updated_at',
            ])
        else:
            summative = ExitTicket.objects.create(
                course=course,
                assessment_type=ExitTicket.AssessmentType.SUMMATIVE,
                question_bank_size=len(aggregated_payload),
                questions_per_attempt=SUMMATIVE_PER_ATTEMPT,
                passing_score=int(SUMMATIVE_PER_ATTEMPT * 0.7),
                time_limit_minutes=60,
                is_published=False,
                instructions=new_instructions,
            )
        for i, q in enumerate(aggregated_payload):
            try:
                ExitTicketQuestion.objects.create(
                    exit_ticket=summative,
                    question_type=q.get('question_type') or 'mcq',
                    question_text=(q.get('question_text') or '')[:8000],
                    option_a=(q.get('option_a') or '')[:500],
                    option_b=(q.get('option_b') or '')[:500],
                    option_c=(q.get('option_c') or '')[:500],
                    option_d=(q.get('option_d') or '')[:500],
                    correct_answer=(q.get('correct_answer') or '')[:1].upper(),
                    answer_data=q.get('answer_data') or {},
                    explanation=(q.get('explanation') or '')[:8000],
                    concept_tag=(q.get('concept_tag') or '')[:200],
                    enabling_objective=(q.get('enabling_objective') or '')[:500],
                    difficulty=(q.get('difficulty') or 'medium').lower(),
                    order_index=i,
                )
            except Exception as e:
                logger.warning(f"summative copy q{i} skipped: {e}")

    print(
        f"[Summative] {course.title}: bank built — "
        f"{summative.questions.count()} qs across {len(lessons) - len(lessons_with_zero)} "
        f"contributing lessons", flush=True,
    )
    return {
        'success': True,
        'questions_created': summative.questions.count(),
        'lessons_processed': len(lessons),
        'lessons_with_zero': len(lessons_with_zero),
        'summative_id': summative.id,
    }
