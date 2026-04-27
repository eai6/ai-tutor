"""Seed PlatformTerms v1 — the initial pilot consent text."""
from django.db import migrations


TERMS_V1_BODY = """\
**By using AI Tutor you agree to the following.**

### 1. AI behavior and accuracy

AI Tutor uses large language models to teach. AI systems can produce
incorrect, unexpected, or off-topic responses. Despite the safety
measures we put in place, the AI may in rare cases produce content
that bypasses its guardrails. We do not warrant the correctness,
accuracy, or appropriateness of every AI-generated response. Treat
AI explanations as a starting point, not a final source of truth.

### 2. Adult supervision is expected

This platform is designed for school students to use **under the
supervision of an adult** — a teacher, parent, or guardian. Students
should not be left unsupervised on the platform for extended periods.
Schools, parents, and guardians are responsible for monitoring their
students' use of the platform and stepping in if anything seems
inappropriate.

### 3. Liability

We provide AI Tutor in good faith for educational purposes. To the
extent allowed by law, we are not liable for any losses, harms, or
damages arising from AI behavior, content shown by the platform, or
how students use it. By continuing to use the platform you accept
this AI-related risk on behalf of yourself and (where applicable) the
student you supervise.

### 4. Data we collect

We collect and store account information, lesson progress, chat
transcripts, and assessment results. We use this data to operate the
platform, adapt the tutor to each student, and report progress to the
student's school. We do not sell student data. School staff and
platform administrators can view your usage data for their school.

### 5. Reporting concerns

If the AI says something inappropriate, click the flag icon under the
message **and** tell your teacher or supervising adult. You can also
use the **Help / Feedback** button at the bottom of every page to
report bugs and feedback directly to us.

### 6. Updates to these terms

We may update these terms. When the version changes you'll be asked
to agree again on next login.

---

**By clicking "I agree" you confirm that you have read and accepted
these terms** — and where applicable, that the supervising adult
(teacher, parent, or guardian) has reviewed them.
"""


def seed_terms_v1(apps, schema_editor):
    PlatformTerms = apps.get_model('accounts', 'PlatformTerms')
    PlatformTerms.objects.get_or_create(
        version=1,
        defaults={
            'title': "AI Tutor — Terms & Important Notes",
            'summary': (
                "I understand AI Tutor uses LLMs that can occasionally behave "
                "unexpectedly, students should be supervised by an adult, and "
                "the platform is provided as-is."
            ),
            'body': TERMS_V1_BODY,
            'is_active': True,
            'effective_date': None,
        },
    )


def remove_terms_v1(apps, schema_editor):
    PlatformTerms = apps.get_model('accounts', 'PlatformTerms')
    PlatformTerms.objects.filter(version=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0017_platform_terms'),
    ]

    operations = [
        migrations.RunPython(seed_terms_v1, remove_terms_v1),
    ]
