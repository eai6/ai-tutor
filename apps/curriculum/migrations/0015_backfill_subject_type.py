"""Backfill Course.subject_type from existing titles using a keyword
heuristic — the same heuristic the legacy is_math property used, plus
science/humanities/language coverage.

Existing rows without a subject_type get one. Rows that already have a
value are left alone (idempotent).

See memory/math_tutor_fix_plan.md M8.
"""

from django.db import migrations


_KEYWORD_MAP = (
    # (subject_type, keywords)
    ('math', ('math', 'maths', 'mathematics', 'algebra', 'geometry',
              'calculus', 'arithmetic', 'trigonometry', 'statistics',
              'fraction')),
    ('science', ('science', 'physics', 'chemistry', 'biology', 'earth',
                 'astronomy', 'geology', 'ecology', 'anatomy')),
    ('humanities', ('history', 'geography', 'civics', 'economics',
                    'philosophy', 'religion', 'social studies')),
    ('language', ('english', 'french', 'spanish', 'language', 'literature',
                  'grammar', 'reading', 'writing', 'kreol', 'creole')),
)


def _classify(title: str) -> str:
    title_lower = (title or '').lower()
    for subject_type, keywords in _KEYWORD_MAP:
        if any(kw in title_lower for kw in keywords):
            return subject_type
    return 'other'


def _backfill(apps, schema_editor):
    Course = apps.get_model('curriculum', 'Course')
    updated = 0
    for course in Course.objects.filter(subject_type='').iterator():
        course.subject_type = _classify(course.title)
        course.save(update_fields=['subject_type'])
        updated += 1


def _noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('curriculum', '0014_add_subject_type'),
    ]

    operations = [
        migrations.RunPython(_backfill, _noop_reverse),
    ]
