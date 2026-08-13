"""Backfill missing enabling_objective tags on exit-ticket questions.

When a lesson generates an exit ticket but the prompt didn't have a
canonical EO list to draw from (the bug fixed in content_generator.py
2026-05), every question lands with empty enabling_objective. The
questions themselves are fine — only the tags are missing.

This command fixes that without regenerating the questions:

  1. Ensure ``lesson.enabling_objectives`` is populated. If empty,
     run ``LessonContentGenerator._expand_to_granular_subskills``
     to produce 5–8 atomic sub-skills (one LLM call).
  2. For each untagged question, ask the LLM to classify it against
     the granular EO list (one batched LLM call per lesson).
  3. Snap each LLM-emitted tag through ``_normalize_enabling_objective``
     so drift is caught the same way generation does.
  4. Save the results in place — ``ExitTicketAttempt`` history survives.

Usage:
    python manage.py backfill_exit_ticket_eos --lesson 638
    python manage.py backfill_exit_ticket_eos --course 12 --dry-run
    python manage.py backfill_exit_ticket_eos --all
"""

import json
import logging
from typing import Dict, List

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q

from ai_tutor.apps.curriculum.models import Course, Lesson
from ai_tutor.apps.tutoring.models import ExitTicketQuestion

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Backfill empty enabling_objective tags on existing exit-ticket questions."

    def add_arguments(self, parser):
        parser.add_argument('--lesson', type=int, help='Single lesson id')
        parser.add_argument('--course', type=int, help='All lessons in this course')
        parser.add_argument('--all', action='store_true', help='Every lesson with untagged questions')
        parser.add_argument('--dry-run', action='store_true', help='Print plan, do not write')
        parser.add_argument(
            '--limit', type=int, default=0,
            help='Cap how many lessons to process (0 = no cap)',
        )

    def handle(self, *args, **opts):
        lessons = self._select_lessons(opts)
        if not lessons:
            self.stdout.write(self.style.WARNING('No lessons match the filter.'))
            return

        limit = opts.get('limit') or 0
        if limit and len(lessons) > limit:
            lessons = lessons[:limit]

        self.stdout.write(
            f'Processing {len(lessons)} lesson(s)'
            + (' (DRY RUN)' if opts['dry_run'] else '')
        )

        totals = {
            'lessons_processed': 0,
            'lessons_skipped_no_questions': 0,
            'eo_expansion_runs': 0,
            'eo_expansion_failed': 0,
            'questions_tagged': 0,
            'questions_unchanged': 0,
            'questions_dropped': 0,
        }
        for lesson in lessons:
            try:
                stats = self._process_lesson(lesson, dry_run=opts['dry_run'])
            except Exception as e:
                self.stderr.write(self.style.ERROR(
                    f'Lesson {lesson.id} ({lesson.title!r}) crashed: {e}'
                ))
                continue
            for k, v in stats.items():
                totals[k] = totals.get(k, 0) + v

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=== Backfill summary ==='))
        for k, v in totals.items():
            self.stdout.write(f'  {k}: {v}')

    def _select_lessons(self, opts):
        if opts.get('lesson'):
            return list(Lesson.objects.filter(id=opts['lesson']))
        if opts.get('course'):
            return list(Lesson.objects.filter(unit__course_id=opts['course']))
        if opts.get('all'):
            # Lessons that have at least one untagged exit-ticket question
            untagged = ExitTicketQuestion.objects.filter(
                Q(enabling_objective='') | Q(enabling_objective__isnull=True),
                exit_ticket__lesson__isnull=False,
            ).values_list('exit_ticket__lesson_id', flat=True).distinct()
            return list(Lesson.objects.filter(id__in=list(untagged)))
        raise CommandError('Pass --lesson <id>, --course <id>, or --all.')

    def _process_lesson(self, lesson, dry_run: bool) -> Dict[str, int]:
        from ai_tutor.apps.tutoring.models import ExitTicket
        from ai_tutor.apps.curriculum.content_generator import (
            LessonContentGenerator,
            _normalize_enabling_objective,
        )
        from ai_tutor.apps.accounts.models import Institution

        stats = {
            'lessons_processed': 0,
            'lessons_skipped_no_questions': 0,
            'eo_expansion_runs': 0,
            'eo_expansion_failed': 0,
            'questions_tagged': 0,
            'questions_unchanged': 0,
            'questions_dropped': 0,
        }

        et = ExitTicket.objects.filter(lesson=lesson).first()
        if not et:
            stats['lessons_skipped_no_questions'] = 1
            return stats
        untagged = list(et.questions.filter(
            Q(enabling_objective='') | Q(enabling_objective__isnull=True),
        ).order_by('order_index'))
        if not untagged:
            stats['lessons_skipped_no_questions'] = 1
            self.stdout.write(
                f'· Lesson {lesson.id} ({lesson.title!r}): no untagged questions'
            )
            return stats

        self.stdout.write(
            f'· Lesson {lesson.id} ({lesson.title!r}): '
            f'{len(untagged)} untagged questions'
        )

        # Step 1 — populate lesson.enabling_objectives if empty
        if not (lesson.enabling_objectives or []):
            inst_id = (
                lesson.unit.course.institution_id
                or Institution.get_global().id
            )
            self.stdout.write('  → expanding granular EOs (LLM call)...')
            stats['eo_expansion_runs'] = 1
            if not dry_run:
                generator = LessonContentGenerator(institution_id=inst_id)
                unit = lesson.unit
                course = unit.course if unit else None
                curriculum_context = {
                    'subject': course.title if course else '',
                    'grade_level': course.grade_level if course else '',
                    'terminal_objectives': (
                        list(unit.terminal_objectives or []) if unit else []
                    ),
                    'lesson_title': lesson.title,
                    'lesson_objective': lesson.objective or '',
                }
                try:
                    generator._expand_to_granular_subskills(
                        lesson, curriculum_context,
                    )
                    lesson.refresh_from_db()
                except Exception as e:
                    stats['eo_expansion_failed'] = 1
                    self.stderr.write(self.style.ERROR(
                        f'  ✗ EO expansion failed: {e}'
                    ))
                    return stats

        canonical = list(lesson.enabling_objectives or [])
        if not canonical:
            # Fall back to the broad concepts so we tag with SOMETHING.
            broad = []
            if lesson.unit and lesson.unit.terminal_objectives:
                broad.extend(list(lesson.unit.terminal_objectives or []))
            if (lesson.objective or '').strip():
                broad.append(lesson.objective.strip())
            canonical = broad
        if not canonical:
            stats['eo_expansion_failed'] = 1
            self.stderr.write(self.style.WARNING(
                f'  ⚠ no canonical EOs available — skipping lesson'
            ))
            return stats

        self.stdout.write(
            f'  → canonical EO list: {len(canonical)} items'
        )

        # Step 2 — classify untagged questions in one batched LLM call
        classifications = self._classify_questions(
            lesson, untagged, canonical, dry_run=dry_run,
        )

        # Step 3 — snap + persist
        for q, predicted_eo in zip(untagged, classifications):
            normalized, status = _normalize_enabling_objective(
                predicted_eo or '', canonical,
            )
            if not normalized:
                stats['questions_dropped'] += 1
                continue
            if normalized == (q.enabling_objective or ''):
                stats['questions_unchanged'] += 1
                continue
            if dry_run:
                self.stdout.write(
                    f'    [dry] Q{q.order_index + 1}: '
                    f'{(q.question_text or "")[:60]!r} → {normalized!r}'
                )
            else:
                q.enabling_objective = normalized
                q.save(update_fields=['enabling_objective'])
            stats['questions_tagged'] += 1

        stats['lessons_processed'] = 1
        return stats

    def _classify_questions(
        self,
        lesson,
        questions: List[ExitTicketQuestion],
        canonical: List[str],
        dry_run: bool,
    ) -> List[str]:
        """One batched LLM call: ask the model to assign each question
        to exactly one EO from the canonical list. Returns a list of
        EO strings, one per input question (same order)."""
        if dry_run:
            # In dry-run mode, simulate — return the first canonical EO
            # for every question so the caller can show what the snap
            # would do. Real run replaces this with an LLM call.
            return [canonical[0]] * len(questions)

        from ai_tutor.apps.llm.models import ModelConfig
        from ai_tutor.apps.llm.client import get_llm_client
        from ai_tutor.apps.llm.json_utils import parse_llm_json

        model_config = (
            ModelConfig.get_for('exit_tickets')
            or ModelConfig.get_for('tutoring')
        )
        if not model_config:
            self.stderr.write(self.style.ERROR(
                '  ✗ no LLM model configured — cannot classify'
            ))
            return [''] * len(questions)
        client = get_llm_client(model_config)

        eo_block = "\n".join(
            f"  EO{i + 1}: {eo}" for i, eo in enumerate(canonical)
        )
        question_block = "\n".join(
            f"  Q{i + 1}: {(q.question_text or '').strip()[:300]}"
            for i, q in enumerate(questions)
        )
        prompt = f"""For each question below, identify which ENABLING OBJECTIVE
it tests. Output a JSON array of strings — one per question, in the
SAME ORDER. Each string must be the EXACT text of one EO from the list.

LESSON: {lesson.title}
LEARNING OBJECTIVE: {lesson.objective or '(none)'}

ENABLING OBJECTIVES:
{eo_block}

QUESTIONS:
{question_block}

Output ONLY a JSON array of {len(questions)} strings, in question order.
Each string MUST exactly match one of the EO lines above (text only —
no "EO1:" prefix). If a question doesn't fit any EO well, pick the
closest one."""

        try:
            response = client.generate(
                [{"role": "user", "content": prompt}],
                system_prompt=(
                    "You are a precise classifier. Output only a JSON "
                    "array of strings, no prose, no code fence."
                ),
                max_tokens=4000,
            )
            raw = (response.content or '').strip()
            parsed = parse_llm_json(raw, expect_array=True)
        except Exception as e:
            self.stderr.write(self.style.ERROR(
                f'  ✗ classification LLM call failed: {e}'
            ))
            return [''] * len(questions)

        if not isinstance(parsed, list):
            self.stderr.write(self.style.ERROR(
                f'  ✗ classifier returned non-array: {type(parsed)}'
            ))
            return [''] * len(questions)

        # Normalize length — pad with '' or truncate so the caller's
        # zip works.
        out = [str(x).strip() if x else '' for x in parsed]
        if len(out) < len(questions):
            out = out + [''] * (len(questions) - len(out))
        return out[:len(questions)]
