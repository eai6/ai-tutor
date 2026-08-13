"""Backfill SessionParticipant rows for every existing TutorSession.

Every session gets exactly one SessionParticipant pointing to the session's
primary student, marked is_primary=True and is_active=True.

See memory/group_lessons_plan.md Phase G1.
"""

from django.db import migrations


def _backfill(apps, schema_editor):
    TutorSession = apps.get_model("tutoring", "TutorSession")
    SessionParticipant = apps.get_model("tutoring", "SessionParticipant")

    created = 0
    for session in TutorSession.objects.all().iterator():
        _, new = SessionParticipant.objects.get_or_create(
            session=session,
            student_id=session.student_id,
            defaults={
                "is_active": True,
                "is_primary": True,
            },
        )
        if new:
            created += 1


def _noop_reverse(apps, schema_editor):
    # Forward-only backfill. Reversing this migration drops the table
    # entirely (handled by 0017 reverse), so data loss is inherent.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("tutoring", "0017_group_lessons"),
    ]

    operations = [
        migrations.RunPython(_backfill, _noop_reverse),
    ]
