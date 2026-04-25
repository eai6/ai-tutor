"""
Management command to seed a demonstration school with realistic student data.

Creates "Seychelles Pilot School" with 15 students, a teacher, and populates
TutorSession + ExitTicketAttempt records with a realistic BE/AE/ME/EE spread
for presenting competency-based lesson reports.

Usage:
    python manage.py seed_demo_school
    python manage.py seed_demo_school --reset   # Clear existing demo data first
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.text import slugify

from apps.accounts.models import Institution, Membership, StudentProfile
from apps.curriculum.models import Course, Lesson
from apps.tutoring.models import (
    TutorSession,
    ExitTicket,
    ExitTicketAttempt,
    StudentLessonProgress,
)

DEMO_INSTITUTION_SLUG = "seychelles-pilot-school"
DEMO_INSTITUTION_NAME = "Seychelles Pilot School"
DEMO_TEACHER_USERNAME = "demo_teacher"
DEMO_TEACHER_PASSWORD = "demo123"

STUDENTS = [
    {"first": "Marie-Claire", "last": "Joubert"},
    {"first": "Andre", "last": "Morel"},
    {"first": "Sophie", "last": "Payet"},
    {"first": "Jacques", "last": "Rene"},
    {"first": "Elise", "last": "Faure"},
    {"first": "Patrick", "last": "Hoareau"},
    {"first": "Nathalie", "last": "Sinon"},
    {"first": "David", "last": "Labrosse"},
    {"first": "Camille", "last": "Rosette"},
    {"first": "Michel", "last": "Vel"},
    {"first": "Isabelle", "last": "Quatre"},
    {"first": "Jean-Luc", "last": "Confait"},
    {"first": "Anais", "last": "Dookhan"},
    {"first": "Pierre", "last": "Ernesta"},
    {"first": "Lea", "last": "Vidot"},
]

# Performance tiers: (label, EO_pct_range, exit_score_range, session_status, time_minutes_range)
# EO pct is the fraction of enabling objectives covered
TIERS = {
    "EE": {
        "eo_pct": (1.0, 1.0),
        "score_range": (9, 10),
        "status": "completed",
        "time_range": (3, 5),
    },
    "ME": {
        "eo_pct": (0.80, 1.0),
        "score_range": (8, 9),
        "status": "completed",
        "time_range": (6, 10),
    },
    "AE": {
        "eo_pct": (0.50, 0.79),
        "score_range": (5, 7),
        "status": "completed",
        "time_range": (8, 15),
    },
    "BE": {
        "eo_pct": (0.10, 0.49),
        "score_range": (3, 5),
        "status": "active",
        "time_range": (10, 20),
    },
}


def _assign_tiers(student_count=15):
    """Assign performance tiers to students for a realistic distribution.

    Returns a list of tier keys, one per student index.
    EE: 2-3, ME: 4-5, AE: 4-5, BE: 2-3
    """
    tiers = (
        ["EE"] * 3
        + ["ME"] * 4
        + ["AE"] * 5
        + ["BE"] * 3
    )
    # Trim or pad to exactly student_count
    tiers = tiers[:student_count]
    random.shuffle(tiers)
    return tiers


class Command(BaseCommand):
    help = "Seed a demo school with realistic student data for competency reports."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete all existing demo data before seeding.",
        )

    def handle(self, *args, **options):
        if options["reset"]:
            self._reset()

        institution = self._create_institution()
        teacher = self._create_teacher(institution)
        students = self._create_students(institution)
        self._seed_sessions(institution, students)

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Institution: {institution.name} (slug={institution.slug})"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"Teacher login: {DEMO_TEACHER_USERNAME} / {DEMO_TEACHER_PASSWORD}"
        ))

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def _reset(self):
        self.stdout.write("Resetting demo data...")
        inst = Institution.objects.filter(slug=DEMO_INSTITUTION_SLUG).first()
        if inst:
            # Delete sessions and attempts tied to this institution
            TutorSession.objects.filter(institution=inst).delete()
            StudentLessonProgress.objects.filter(institution=inst).delete()
            # Delete memberships + users
            for m in Membership.objects.filter(institution=inst):
                user = m.user
                m.delete()
                # Only delete user if they have no other memberships
                if not Membership.objects.filter(user=user).exists():
                    user.delete()
            inst.delete()
            self.stdout.write(self.style.WARNING("  Deleted existing demo institution and related data."))
        else:
            self.stdout.write("  No existing demo data found.")

    # ------------------------------------------------------------------
    # Institution
    # ------------------------------------------------------------------

    def _create_institution(self):
        institution, created = Institution.objects.get_or_create(
            slug=DEMO_INSTITUTION_SLUG,
            defaults={
                "name": DEMO_INSTITUTION_NAME,
                "timezone": "Indian/Mahe",
                "is_active": True,
            },
        )
        verb = "Created" if created else "Found existing"
        self.stdout.write(f"  {verb} institution: {institution.name}")
        return institution

    # ------------------------------------------------------------------
    # Teacher
    # ------------------------------------------------------------------

    def _create_teacher(self, institution):
        user, created = User.objects.get_or_create(
            username=DEMO_TEACHER_USERNAME,
            defaults={
                "first_name": "Demo",
                "last_name": "Teacher",
                "email": "demo.teacher@pilot.sc",
                "is_staff": False,
            },
        )
        if created:
            user.set_password(DEMO_TEACHER_PASSWORD)
            user.save()

        Membership.objects.get_or_create(
            user=user,
            institution=institution,
            defaults={"role": Membership.Role.STAFF},
        )
        verb = "Created" if created else "Found existing"
        self.stdout.write(f"  {verb} teacher: {user.username}")
        return user

    # ------------------------------------------------------------------
    # Students
    # ------------------------------------------------------------------

    def _create_students(self, institution):
        users = []
        for idx, s in enumerate(STUDENTS, start=1):
            username = f"demo_{s['first'].lower().replace('-', '')}_{s['last'].lower()}"
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "first_name": s["first"],
                    "last_name": s["last"],
                    "email": f"{username}@pilot.sc",
                },
            )
            if created:
                user.set_password("student123")
                user.save()

            Membership.objects.get_or_create(
                user=user,
                institution=institution,
                defaults={"role": Membership.Role.STUDENT},
            )

            profile, _ = StudentProfile.objects.get_or_create(
                user=user,
                defaults={
                    "student_id": f"SPS-2026-{idx:03d}",
                    "school": str(institution.id),
                    "grade_level": "S3",
                },
            )
            users.append(user)

        self.stdout.write(f"  Created/verified {len(users)} student accounts.")
        return users

    # ------------------------------------------------------------------
    # Sessions & exit ticket attempts
    # ------------------------------------------------------------------

    def _seed_sessions(self, institution, students):
        """Create TutorSession + ExitTicketAttempt records for eligible lessons."""

        # Find lessons with enough steps AND an exit ticket
        eligible_lessons = []

        # Prefer Geography S3, but fall back to any course
        courses = list(Course.objects.filter(title__icontains="geography")) or list(
            Course.objects.all()[:3]
        )

        for course in courses:
            lessons = (
                Lesson.objects.filter(unit__course=course, content_status="ready")
                .prefetch_related("steps")
            )
            for lesson in lessons:
                step_count = lesson.steps.count()
                has_exit_ticket = ExitTicket.objects.filter(lesson=lesson).exists()
                if step_count >= 5 and has_exit_ticket:
                    eligible_lessons.append(lesson)

        if not eligible_lessons:
            # Relaxed: try any lesson with >= 5 steps (ignore exit ticket requirement)
            for course in Course.objects.all():
                lessons = Lesson.objects.filter(
                    unit__course=course, content_status="ready"
                ).prefetch_related("steps")
                for lesson in lessons:
                    if lesson.steps.count() >= 5:
                        eligible_lessons.append(lesson)
                    if len(eligible_lessons) >= 6:
                        break
                if len(eligible_lessons) >= 6:
                    break

        if not eligible_lessons:
            self.stdout.write(self.style.WARNING(
                "  No eligible lessons found (need >= 5 steps). "
                "Skipping session seeding."
            ))
            return

        self.stdout.write(f"  Found {len(eligible_lessons)} eligible lesson(s).")

        now = timezone.now()
        session_count = 0
        attempt_count = 0

        for lesson in eligible_lessons:
            exit_ticket = ExitTicket.objects.filter(lesson=lesson).first()
            eos = lesson.enabling_objectives or []
            steps = list(lesson.steps.all().order_by("order_index"))
            total_steps = len(steps)

            # Collect enabling objectives from steps if lesson-level is empty
            if not eos:
                eos = list({
                    s.enabling_objective
                    for s in steps
                    if s.enabling_objective
                })

            tiers = _assign_tiers(len(students))

            for student, tier_key in zip(students, tiers):
                tier = TIERS[tier_key]

                # Determine how many EOs this student covered
                eo_pct = random.uniform(*tier["eo_pct"])
                covered_count = max(1, int(len(eos) * eo_pct)) if eos else 0
                covered_eos = random.sample(eos, min(covered_count, len(eos))) if eos else []

                # Determine step progress
                if tier_key == "BE":
                    steps_done = max(1, int(total_steps * random.uniform(0.2, 0.5)))
                elif tier_key == "AE":
                    steps_done = max(1, int(total_steps * random.uniform(0.5, 0.8)))
                else:
                    steps_done = total_steps

                # Session timing
                days_ago = random.randint(1, 14)
                session_start = now - timedelta(days=days_ago, hours=random.randint(0, 8))
                duration_minutes = random.uniform(*tier["time_range"])
                session_end = session_start + timedelta(minutes=duration_minutes)

                status = tier["status"]
                is_completed = status == "completed"

                engine_state = {
                    "session_state": "COMPLETED" if is_completed else "TUTORING",
                    "current_topic_index": steps_done,
                    "covered_enabling_objectives": covered_eos,
                    "total_enabling_objectives": eos,
                    "eo_pct_achieved": round(eo_pct * 100, 1),
                    "tier": tier_key,
                }

                # Check for existing session to maintain idempotency
                session, s_created = TutorSession.objects.get_or_create(
                    institution=institution,
                    student=student,
                    lesson=lesson,
                    defaults={
                        "status": status,
                        "current_step_index": steps_done - 1,
                        "engine_state": engine_state,
                        "mastery_achieved": tier_key in ("EE", "ME"),
                        "started_at": session_start,
                        "ended_at": session_end if is_completed else None,
                        "started_lesson_at": session_start,
                        "completed_lesson_at": session_end if is_completed else None,
                    },
                )
                if s_created:
                    # Fix auto_now_add started_at via update
                    TutorSession.objects.filter(pk=session.pk).update(
                        started_at=session_start,
                    )
                    session_count += 1

                # Create exit ticket attempt (only if exit ticket exists and session is completed)
                if exit_ticket and is_completed:
                    score = random.randint(*tier["score_range"])
                    passed = score >= exit_ticket.passing_score

                    # Build fake answers dict
                    questions = list(exit_ticket.questions.all().order_by("order_index"))
                    answers = {}
                    correct_so_far = 0
                    for q in questions:
                        if correct_so_far < score:
                            # This one is correct
                            answers[str(q.id)] = {
                                "answer": q.correct_answer or "A",
                                "correct": True,
                            }
                            correct_so_far += 1
                        else:
                            # Wrong answer
                            wrong = random.choice(
                                [c for c in ["A", "B", "C", "D"] if c != (q.correct_answer or "A")]
                            ) if q.correct_answer else "B"
                            answers[str(q.id)] = {
                                "answer": wrong,
                                "correct": False,
                            }

                    et_start = session_end - timedelta(minutes=duration_minutes * 0.3)

                    _, a_created = ExitTicketAttempt.objects.get_or_create(
                        exit_ticket=exit_ticket,
                        student=student,
                        session=session,
                        defaults={
                            "score": score,
                            "passed": passed,
                            "answers": answers,
                            "completed_at": session_end,
                        },
                    )
                    if a_created:
                        # Fix auto_now_add started_at
                        ExitTicketAttempt.objects.filter(
                            exit_ticket=exit_ticket,
                            student=student,
                            session=session,
                        ).update(started_at=et_start)
                        attempt_count += 1

                # Upsert StudentLessonProgress
                mastery = "mastered" if tier_key in ("EE", "ME") else (
                    "in_progress" if tier_key == "AE" else "not_started"
                )
                StudentLessonProgress.objects.update_or_create(
                    institution=institution,
                    student=student,
                    lesson=lesson,
                    defaults={
                        "mastery_level": mastery,
                        "attempts_count": random.randint(1, 3),
                        "best_score": round(eo_pct, 4),
                        "last_session_at": session_end if is_completed else session_start,
                    },
                )

            self.stdout.write(
                f"    Lesson '{lesson.title}': "
                f"{len(students)} sessions, "
                f"{sum(1 for t in tiers if t == 'EE')} EE / "
                f"{sum(1 for t in tiers if t == 'ME')} ME / "
                f"{sum(1 for t in tiers if t == 'AE')} AE / "
                f"{sum(1 for t in tiers if t == 'BE')} BE"
            )

        self.stdout.write(
            f"  Total: {session_count} sessions, {attempt_count} exit ticket attempts created."
        )
