"""Give math courses a subject_code where only subject_type said so.

subject_code is becoming the single stored subject
(memory/subject_grade_unification_plan.md). Before the database filters can
read it alone, the courses classified only through subject_type need the code.

math is the ONE direction that maps without ambiguity. The others do not:
'humanities' is geography OR history, 'science' is physics OR chemistry OR
biology, 'language' is english OR french. Those need
`python manage.py backfill_course_subjects`, which reads the title and is
reviewable with --dry-run; guessing them inside a migration would bake a
heuristic into schema history.
"""
from django.db import migrations


def fill_math_code(apps, schema_editor):
    Course = apps.get_model('curriculum', 'Course')
    Course.objects.filter(subject_type='math', subject_code='').update(
        subject_code='mathematics')


def unfill(apps, schema_editor):
    # Reversible: only rows this migration could have written are cleared.
    Course = apps.get_model('curriculum', 'Course')
    Course.objects.filter(subject_type='math',
                          subject_code='mathematics').update(subject_code='')


class Migration(migrations.Migration):

    dependencies = [('curriculum', '0035_add_lesson_retired_at')]

    operations = [migrations.RunPython(fill_math_code, unfill)]
