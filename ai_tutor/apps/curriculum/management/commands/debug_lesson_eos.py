"""Print the exact EO/TO state for a lesson + its exit-ticket questions.

Used to diagnose why exit-ticket questions are showing the broad
"Angles around a point" tag instead of granular sub-skills. Read-only
— no writes.

Usage:
    python manage.py debug_lesson_eos --lesson 638
"""

from django.core.management.base import BaseCommand

from ai_tutor.apps.curriculum.models import Lesson


class Command(BaseCommand):
    help = "Print EO/TO state for a lesson and its exit-ticket question tags."

    def add_arguments(self, parser):
        parser.add_argument('--lesson', type=int, required=True)

    def handle(self, *args, **opts):
        lesson_id = opts['lesson']
        lesson = Lesson.objects.filter(id=lesson_id).first()
        if not lesson:
            self.stderr.write(f'No lesson with id {lesson_id}')
            return

        self.stdout.write(self.style.SUCCESS(f'=== Lesson {lesson.id}: {lesson.title!r} ==='))
        self.stdout.write(f'objective: {lesson.objective!r}')
        self.stdout.write(f'content_status: {lesson.content_status}')
        self.stdout.write(f'updated_at: {lesson.updated_at}')

        eos = list(lesson.enabling_objectives or [])
        self.stdout.write(f'\nlesson.enabling_objectives ({len(eos)} items):')
        for i, eo in enumerate(eos, 1):
            self.stdout.write(f'  EO{i}: {eo!r}')

        unit = lesson.unit
        if unit:
            self.stdout.write(f'\nunit: {unit.id}: {unit.title!r}')
            tos = list(unit.terminal_objectives or [])
            self.stdout.write(f'unit.terminal_objectives ({len(tos)} items):')
            for i, to in enumerate(tos, 1):
                self.stdout.write(f'  TO{i}: {to!r}')
            unit_eos = list(unit.enabling_objectives or [])
            self.stdout.write(f'unit.enabling_objectives ({len(unit_eos)} items):')
            for i, eo in enumerate(unit_eos, 1):
                self.stdout.write(f'  unit_EO{i}: {eo!r}')

        # Exit-ticket question tags
        from ai_tutor.apps.tutoring.models import ExitTicket, ExitTicketQuestion
        et = ExitTicket.objects.filter(lesson=lesson).first()
        if et:
            qs = list(et.questions.all().order_by('order_index'))
            self.stdout.write(f'\nexit-ticket: {et.id}, {len(qs)} questions')
            from collections import Counter
            tag_counts = Counter((q.enabling_objective or '<empty>') for q in qs)
            self.stdout.write('enabling_objective tag distribution:')
            for tag, count in tag_counts.most_common():
                self.stdout.write(f'  {count:3d} × {tag!r}')
            concept_counts = Counter((q.concept_tag or '<empty>') for q in qs)
            self.stdout.write('concept_tag distribution:')
            for tag, count in concept_counts.most_common():
                self.stdout.write(f'  {count:3d} × {tag!r}')
            if qs:
                first = qs[0]
                self.stdout.write(f'\nQ1 created: {first.created_at if hasattr(first, "created_at") else "(no field)"}')
                last = qs[-1]
                self.stdout.write(f'Q{len(qs)} order_index: {last.order_index}')
        else:
            self.stdout.write(self.style.WARNING('\nNo ExitTicket on this lesson.'))

        # Lesson steps' enabling_objective fields
        self.stdout.write(f'\nLesson steps + their enabling_objective:')
        for step in lesson.steps.all().order_by('order_index'):
            eo = step.enabling_objective or '<empty>'
            self.stdout.write(f'  step {step.order_index} ({step.step_type}): {eo!r}')
