"""Normalize existing LessonStep.choices to letter-prefixed form.

Mirrors the save-hook in ``apps/curriculum/models.py::LessonStep._normalize_mcq_choices``.
Idempotent — re-running is safe; choices already prefixed pass through unchanged.

See design/tasks/pose-question-two-phase-commit-fixes-plan.md Fix 1.
"""

import re

from django.db import migrations


_MCQ_LETTER_RE = re.compile(r"^\s*([A-Da-d])\s*[).:\-]")
_LETTERS = ["A", "B", "C", "D", "E", "F"]


def _normalize_choices(choices):
    if not isinstance(choices, list):
        return choices, False
    normalized = []
    changed = False
    for i, choice in enumerate(choices):
        if not isinstance(choice, str):
            normalized.append(choice)
            continue
        stripped = choice.strip()
        if not stripped:
            normalized.append(stripped)
            if stripped != choice:
                changed = True
            continue
        if _MCQ_LETTER_RE.match(stripped):
            normalized.append(stripped)
            if stripped != choice:
                changed = True
        else:
            prefix = _LETTERS[i] if i < len(_LETTERS) else f"Option{i + 1}"
            normalized.append(f"{prefix}) {stripped}")
            changed = True
    return normalized, changed


def normalize_existing_mcq_rows(apps, schema_editor):
    LessonStep = apps.get_model("curriculum", "LessonStep")
    qs = LessonStep.objects.filter(answer_type="multiple_choice").exclude(
        choices__isnull=True
    )
    to_update = []
    for step in qs.iterator():
        new_choices, changed = _normalize_choices(step.choices)
        if changed:
            step.choices = new_choices
            to_update.append(step)
    if to_update:
        LessonStep.objects.bulk_update(to_update, ["choices"], batch_size=200)


def reverse_noop(apps, schema_editor):
    # Letter prefixes are content; reversing would require remembering
    # the pre-migration form. Treat as a no-op — the save-hook re-applies
    # the normalization on the next save anyway.
    return


class Migration(migrations.Migration):

    dependencies = [
        ("curriculum", "0028_course_prerequisites_enabled"),
    ]

    operations = [
        migrations.RunPython(normalize_existing_mcq_rows, reverse_noop),
    ]
