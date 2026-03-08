"""
Management command to classify existing units by grade level.

Three strategies (applied in order):
1. Title-prefix parsing: "S1: Map Skills" → grade_level = "S1"
2. LLM classification (--reclassify): asks the LLM which grade(s) each unit belongs to
3. Fallback: leave empty (visible to all grades)

Run with:
  python manage.py classify_unit_grades                  # title-prefix only
  python manage.py classify_unit_grades --reclassify     # use LLM for ambiguous units
  python manage.py classify_unit_grades --reclassify --all  # reclassify ALL units (even already-set)
"""

import re
import json
from django.core.management.base import BaseCommand
from apps.curriculum.models import Course, Unit


class Command(BaseCommand):
    help = 'Classify units by grade level (title-prefix parsing + optional LLM)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would be changed without saving',
        )
        parser.add_argument(
            '--reclassify', action='store_true',
            help='Use LLM to classify units that cannot be parsed from title',
        )
        parser.add_argument(
            '--all', action='store_true',
            help='Reclassify ALL units (including those already set). Requires --reclassify.',
        )
        parser.add_argument(
            '--course-id', type=int, default=None,
            help='Only classify units in this course',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        use_llm = options['reclassify']
        reclassify_all = options['all']
        course_id = options['course_id']

        qs = Unit.objects.select_related('course')
        if course_id:
            qs = qs.filter(course_id=course_id)
        if not reclassify_all:
            qs = qs.filter(grade_level='')

        units = list(qs.order_by('course_id', 'order_index'))
        if not units:
            self.stdout.write('No units to classify.')
            return

        self.stdout.write(f'Found {len(units)} unit(s) to classify.\n')

        # Phase 1: title-prefix parsing
        resolved = {}  # unit.id → grade
        needs_llm = {}  # course_id → [unit, ...]
        for unit in units:
            m = re.match(r'^(S\d+)\s*:', unit.title)
            if m:
                resolved[unit.id] = (m.group(1), 'title prefix')
            elif use_llm:
                needs_llm.setdefault(unit.course_id, []).append(unit)
            # else: leave empty (visible to all)

        # Phase 2: LLM classification per course
        if needs_llm:
            for cid, course_units in needs_llm.items():
                course = course_units[0].course
                llm_results = self._classify_with_llm(course, course_units)
                for unit in course_units:
                    grade = llm_results.get(unit.id, '')
                    if grade:
                        resolved[unit.id] = (grade, 'LLM')

        # Apply
        updated = 0
        for unit in units:
            if unit.id in resolved:
                grade, method = resolved[unit.id]
                if not dry_run:
                    unit.grade_level = grade
                    unit.save(update_fields=['grade_level'])
                updated += 1
                self.stdout.write(f'  {unit.course.title} > {unit.title} → {grade} ({method})')
            else:
                self.stdout.write(f'  {unit.course.title} > {unit.title} → skipped (no match)')

        prefix = '[DRY RUN] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(f'\n{prefix}Updated {updated}/{len(units)} units.'))

    def _classify_with_llm(self, course, units):
        """Use LLM to classify which grade level(s) each unit belongs to."""
        from apps.curriculum.utils import parse_grade_level_string
        from apps.llm.models import ModelConfig
        from apps.llm.client import get_llm_client

        config = ModelConfig.get_for('generation')
        if not config:
            self.stderr.write('No LLM model configured for generation. Skipping LLM classification.')
            return {}

        client = get_llm_client(config)
        grades = parse_grade_level_string(course.grade_level)
        if not grades:
            return {}

        grade_list = ', '.join(grades)

        # Build unit list with lesson titles for context
        unit_lines = []
        for unit in units:
            lessons = list(unit.lessons.values_list('title', flat=True)[:10])
            lessons_str = '; '.join(lessons) if lessons else '(no lessons yet)'
            unit_lines.append(f'  ID={unit.id}: "{unit.title}" — Lessons: {lessons_str}')

        prompt = f"""You are a curriculum specialist. This course "{course.title}" covers grades {grade_list}.

Classify each unit below into the SPECIFIC grade level(s) it belongs to.
Use only these grades: {grade_list}

Units:
{chr(10).join(unit_lines)}

Return a JSON object mapping unit ID to grade_level string.
- Use a single grade if the unit clearly targets one grade (e.g. "S1")
- Use comma-separated grades only if the unit genuinely spans multiple grades (e.g. "S1,S2")
- Do NOT assign all grades to every unit — that defeats the purpose of classification

Example: {{"123": "S1", "124": "S2", "125": "S2,S3"}}

Return ONLY the JSON object, no other text."""

        self.stdout.write(f'  Classifying {len(units)} units in "{course.title}" via LLM...')

        try:
            response = client.generate(
                messages=[{"role": "user", "content": prompt}],
                system_prompt="You are a curriculum classification expert. Return only valid JSON.",
                max_tokens=2048,
            )
            content = response.content.strip()
            self.stdout.write(f'  LLM response ({response.tokens_out} tokens): {content[:200]}')
            # Clean markdown fences
            if '```' in content:
                parts = content.split('```')
                for part in parts:
                    part = part.strip()
                    if part.startswith('json'):
                        part = part[4:].strip()
                    if part.startswith('{'):
                        content = part
                        break
            result = json.loads(content)
            # Convert string keys to int
            return {int(k): v for k, v in result.items()}
        except Exception as e:
            self.stderr.write(f'  LLM classification failed: {e}')
            self.stderr.write(f'  Raw response: {response.content[:300] if response else "None"}')
            return {}
