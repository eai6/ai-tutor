"""Backfill StudentLessonProgress.{best_score, attempts_count, last_attempt_at}
from historical ExitTicketAttempt rows.

See memory/lesson_competency_plan.md Phase C2.

Also normalizes best_score from the legacy percentage scale (0-100) to the
new fractional scale (0.0-1.0) for any rows the old code wrote to. Legacy
values are detected as > 1.0 and divided by 100.
"""

from django.db import migrations


def _backfill(apps, schema_editor):
    StudentLessonProgress = apps.get_model("tutoring", "StudentLessonProgress")
    ExitTicketAttempt = apps.get_model("tutoring", "ExitTicketAttempt")
    ExitTicket = apps.get_model("tutoring", "ExitTicket")

    # Normalize legacy best_score percent values -> fraction.
    for sp in StudentLessonProgress.objects.exclude(best_score__isnull=True).iterator():
        if sp.best_score is not None and sp.best_score > 1.0:
            sp.best_score = round(sp.best_score / 100.0, 4)
            sp.save(update_fields=["best_score"])

    # Backfill best_score/attempts_count/last_attempt_at from ExitTicketAttempt.
    for sp in StudentLessonProgress.objects.iterator():
        # Attempts for this student on this lesson (via exit_ticket->lesson).
        attempts = ExitTicketAttempt.objects.filter(
            student=sp.student,
            exit_ticket__lesson=sp.lesson,
        ).order_by("-completed_at")

        count = attempts.count()
        if count == 0:
            continue

        latest = attempts.first()
        # best_score: highest score as fraction of answers length
        best_pct = None
        for att in attempts:
            answers = att.answers or []
            total = len(answers) or 0
            if total <= 0:
                continue
            pct = att.score / total if total else 0.0
            if best_pct is None or pct > best_pct:
                best_pct = pct

        update_fields = []
        if best_pct is not None and (sp.best_score is None or best_pct > (sp.best_score or 0)):
            sp.best_score = round(best_pct, 4)
            update_fields.append("best_score")
        if count and sp.attempts_count != count:
            sp.attempts_count = count
            update_fields.append("attempts_count")
        if latest and sp.last_attempt_at != latest.completed_at:
            sp.last_attempt_at = latest.completed_at
            update_fields.append("last_attempt_at")

        # Re-evaluate mastery against the actual passing threshold of each
        # attempt's exit ticket (in case the prior logic missed it).
        if sp.mastery_level != "mastered":
            for att in attempts:
                answers = att.answers or []
                total = len(answers) or 0
                if total <= 0:
                    continue
                ticket_passing = att.exit_ticket.passing_score or 8
                if att.score >= ticket_passing:
                    sp.mastery_level = "mastered"
                    if "mastery_level" not in update_fields:
                        update_fields.append("mastery_level")
                    break

        if update_fields:
            sp.save(update_fields=update_fields)


def _noop_reverse(apps, schema_editor):
    # Backfill is idempotent and cannot be meaningfully reversed. The new
    # fields themselves are dropped by the reverse of 0015.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("tutoring", "0015_add_competency_fields"),
    ]

    operations = [
        migrations.RunPython(_backfill, _noop_reverse),
    ]
