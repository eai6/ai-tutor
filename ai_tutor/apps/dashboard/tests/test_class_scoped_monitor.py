"""The live monitor and the session report, scoped to a class.

Both were a bare ``TutorSession.objects.filter(lesson=lesson)``. Two things
were wrong with that, and the second is why it was reported.

**It crossed schools.** No institution filter at all, so on a platform-wide
course a teacher at one school watched every other school's students work the
same lesson. That is the leak CLAUDE.md's multi-tenancy rule exists to prevent.

**It described a group that no longer exists.** A class roster changes —
students are promoted out, new ones arrive — so a report keyed to whoever once
sat the lesson drifts further from the class it is meant to describe every
term. Scoping to the roster also makes an absence visible: a sessions-only
query can never say who did NOT start.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership, StudentProfile
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.tutoring.models import TutorSession


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Belonie', slug='belonie-cs')


@pytest.fixture
def other_school(db):
    return Institution.objects.create(name='Mont Fleuri', slug='mont-fleuri-cs')


@pytest.fixture
def teacher(db, school):
    """A REGULAR teacher, not a superadmin.

    is_staff=True routes through get_staff_context's superadmin branch, which
    defaults to all-schools mode — so a super-admin seeing every school is
    correct, and a test that used one could never catch the leak. The teacher
    who actually opens these pages in a pilot school is this one.
    """
    user = User.objects.create_user('teach', 't@example.com', 'pw')
    Membership.objects.create(user=user, institution=school,
                              role=Membership.Role.STAFF)
    return user


@pytest.fixture
def lesson(db):
    """Platform-wide course — institution=None — which is the shape that made
    the leak reachable."""
    course = Course.objects.create(title='Geography S1-S5', institution=None,
                                   grade_level='S1,S2,S3,S4,S5')
    unit = Unit.objects.create(course=course, title='Maps', order_index=0)
    return Lesson.objects.create(unit=unit, title='Grid references',
                                 objective='Read a grid reference',
                                 order_index=0, is_published=True)


def _student(school, name, grade='S3'):
    user = User.objects.create_user(name, f'{name}@example.com', 'pw',
                                    first_name=name.title(), last_name='Test')
    Membership.objects.create(user=user, institution=school,
                              role=Membership.Role.STUDENT)
    StudentProfile.objects.create(user=user, grade_level=grade)
    return user


def _worked(student, lesson, school, status='completed'):
    return TutorSession.objects.create(student=student, lesson=lesson,
                                       institution=school, status=status)


def _monitor(client, lesson, grade=None):
    url = reverse('dashboard:lesson_monitor', args=[lesson.id])
    return client.get(f'{url}?class={grade}' if grade else url)


def _report(client, lesson, grade=None):
    url = reverse('dashboard:lesson_session_report', args=[lesson.id])
    return client.get(f'{url}?class={grade}' if grade else url)


@pytest.mark.django_db
class TestItDoesNotCrossSchools:

    def test_the_monitor_shows_only_this_school(self, client, teacher, school,
                                                other_school, lesson):
        mine = _student(school, 'amara')
        theirs = _student(other_school, 'jeanluc')
        _worked(mine, lesson, school)
        _worked(theirs, lesson, other_school)

        client.force_login(teacher)
        names = {s['student_name'] for s in _monitor(client, lesson).context['sessions']}
        assert 'Amara Test' in names
        assert 'Jeanluc Test' not in names, 'another school\'s student is visible'

    def test_the_report_counts_only_this_school(self, client, teacher, school,
                                                other_school, lesson):
        _worked(_student(school, 'amara'), lesson, school)
        _worked(_student(other_school, 'jeanluc'), lesson, other_school)

        client.force_login(teacher)
        assert _report(client, lesson).context['total_students'] == 1


@pytest.mark.django_db
class TestTheRosterLeadsAndPastStudentsFollow:

    def test_a_promoted_student_leaves_the_counts(self, client, teacher, school,
                                                  lesson):
        """They worked the lesson in S3 and are in S4 now. The S3 report is
        about S3 as it stands, so they are not in its numbers."""
        amara = _student(school, 'amara', grade='S3')
        marie = _student(school, 'marie', grade='S3')
        _worked(amara, lesson, school)
        _worked(marie, lesson, school)
        marie.student_profile.grade_level = 'S4'
        marie.student_profile.save(update_fields=['grade_level'])

        client.force_login(teacher)
        report = _report(client, lesson, 'S3')

        assert report.context['total_students'] == 1
        assert [s.id for s in report.context['former_students']] == [marie.id]

        monitor = _monitor(client, lesson, 'S3')
        assert [s['session'].student_id for s in monitor.context['sessions']] == [amara.id]
        assert [s['session'].student_id
                for s in monitor.context['former_sessions']] == [marie.id]

    def test_their_work_is_still_reachable(self, client, teacher, school, lesson):
        """Not deleted — a teacher comparing this term with last should still
        find it, one disclosure away."""
        marie = _student(school, 'marie', grade='S4')
        _worked(marie, lesson, school)

        client.force_login(teacher)
        body = _monitor(client, lesson, 'S3').content.decode()
        assert 'no longer in S3' in body
        assert 'Marie' in body

    def test_a_new_arrival_is_counted_from_the_day_they_arrive(
            self, client, teacher, school, lesson):
        """The roster is read live, so nothing has to be recomputed when a
        student moves into the class."""
        jean = _student(school, 'jeanluc', grade='S2')
        _worked(jean, lesson, school)

        client.force_login(teacher)
        assert _report(client, lesson, 'S3').context['total_students'] == 0

        jean.student_profile.grade_level = 'S3'
        jean.student_profile.save(update_fields=['grade_level'])
        assert _report(client, lesson, 'S3').context['total_students'] == 1


@pytest.mark.django_db
class TestAnAbsenceBecomesVisible:
    """The number the page could not produce before it knew a roster."""

    def test_the_report_knows_exactly_who_did_not_start(self, client, teacher,
                                                        school, lesson):
        """It resolves them individually — the page only renders the count
        (TestTheAbsenceIsACountNotARegister), but the set is what makes the
        count trustworthy, and what a future export would need."""
        _worked(_student(school, 'amara'), lesson, school)
        kelly = _student(school, 'kelly')
        nadia = _student(school, 'nadia')

        client.force_login(teacher)
        report = _report(client, lesson, 'S3')

        assert {s.id for s in report.context['not_started_students']} == {kelly.id, nadia.id}
        assert 'never opened this lesson' in report.content.decode()

    def test_the_monitor_knows_too(self, client, teacher, school, lesson):
        _worked(_student(school, 'amara'), lesson, school)
        kelly = _student(school, 'kelly')

        client.force_login(teacher)
        monitor = _monitor(client, lesson, 'S3')
        assert [s.id for s in monitor.context['not_started']] == [kelly.id]
        assert 'has not opened this lesson' in monitor.content.decode()

    def test_without_a_class_there_is_nobody_to_miss(self, client, teacher,
                                                     school, lesson):
        """No roster, no absence — the school-wide view can only report what
        happened, and saying otherwise would be inventing a denominator."""
        _worked(_student(school, 'amara'), lesson, school)
        _student(school, 'kelly')

        client.force_login(teacher)
        assert _report(client, lesson).context['not_started_students'] == []
        assert _monitor(client, lesson).context['not_started'] == []
        assert _monitor(client, lesson).context['roster_size'] is None


@pytest.mark.django_db
class TestThePicker:

    def test_it_offers_the_classes_that_have_students(self, client, teacher,
                                                      school, lesson):
        _student(school, 'amara', grade='S3')
        _student(school, 'jeanluc', grade='S1')
        _student(school, 'nograde', grade='')

        client.force_login(teacher)
        assert _monitor(client, lesson).context['class_choices'] == ['S1', 'S3']

    def test_the_two_pages_link_to_each_other_within_the_class(
            self, client, teacher, school, lesson):
        """Switching between Monitor and Report must not silently widen the
        scope back to the whole school."""
        _worked(_student(school, 'amara'), lesson, school)
        client.force_login(teacher)

        report_url = reverse('dashboard:lesson_session_report', args=[lesson.id])
        monitor_url = reverse('dashboard:lesson_monitor', args=[lesson.id])
        assert f'{report_url}?class=S3' in _monitor(client, lesson, 'S3').content.decode()
        assert f'{monitor_url}?class=S3' in _report(client, lesson, 'S3').content.decode()

    def test_an_unknown_class_is_an_empty_class_not_an_error(
            self, client, teacher, school, lesson):
        _worked(_student(school, 'amara'), lesson, school)
        client.force_login(teacher)

        response = _report(client, lesson, 'S9')
        assert response.status_code == 200
        assert response.context['total_students'] == 0
        assert response.context['roster_size'] == 0


@pytest.mark.django_db
class TestTheRowAgreesWithTheCards:
    """A session nobody has touched in ten hours kept a green ACTIVE badge
    while the ACTIVE card above it counted 0 — the row and the summary
    contradicting each other on the same screen. The cards were right."""

    def _stale(self, student, lesson, school, minutes):
        from django.utils import timezone
        from datetime import timedelta
        from ai_tutor.apps.tutoring.models import SessionTurn
        session = TutorSession.objects.create(student=student, lesson=lesson,
                                              institution=school, status='active')
        turn = SessionTurn.objects.create(session=session, role='student',
                                          content='hello')
        SessionTurn.objects.filter(pk=turn.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes))
        return session

    def test_a_stale_active_session_reads_idle(self, client, teacher, school,
                                               lesson):
        self._stale(_student(school, 'amara'), lesson, school, minutes=600)

        client.force_login(teacher)
        row = _monitor(client, lesson, 'S3').context['sessions'][0]

        assert row['is_idle'] is True
        assert row['display_status'] == 'idle'
        assert row['status'] == 'active', 'the raw field is untouched'

    def test_the_badge_and_the_card_cannot_disagree(self, client, teacher,
                                                    school, lesson):
        self._stale(_student(school, 'amara'), lesson, school, minutes=600)

        client.force_login(teacher)
        response = _monitor(client, lesson, 'S3')

        counted_active = response.context['active_count']
        badged_active = sum(1 for s in response.context['sessions']
                            if s['display_status'] == 'active')
        assert counted_active == badged_active == 0
        assert response.context['idle_count'] == 1

    def test_a_fresh_session_still_reads_active(self, client, teacher, school,
                                                lesson):
        self._stale(_student(school, 'amara'), lesson, school, minutes=1)

        client.force_login(teacher)
        row = _monitor(client, lesson, 'S3').context['sessions'][0]
        assert row['display_status'] == 'active'
        assert _monitor(client, lesson, 'S3').context['active_count'] == 1

    def test_a_completed_session_reads_completed(self, client, teacher, school,
                                                 lesson):
        _worked(_student(school, 'amara'), lesson, school, status='completed')
        client.force_login(teacher)
        row = _monitor(client, lesson, 'S3').context['sessions'][0]
        assert row['display_status'] == 'completed'


@pytest.mark.django_db
class TestTheAbsenceIsACountNotARegister:
    """The monitor and the report say how many never opened the lesson, not
    who. Both pages are a decision about the class — wait or move on, re-teach
    or advance — and a register in the middle of one is noise. The names live
    on the class page, which is where you go to chase someone."""

    def test_the_monitor_gives_a_number_only(self, client, teacher, school,
                                             lesson):
        _worked(_student(school, 'amara'), lesson, school)
        _student(school, 'kelly')

        client.force_login(teacher)
        response = _monitor(client, lesson, 'S3')
        body = response.content.decode()

        assert len(response.context['not_started']) == 1
        assert 'has not opened this lesson' in body
        assert 'Kelly' not in body

    def test_the_report_gives_a_number_only(self, client, teacher, school,
                                            lesson):
        _worked(_student(school, 'amara'), lesson, school)
        _student(school, 'kelly')

        client.force_login(teacher)
        response = _report(client, lesson, 'S3')
        body = response.content.decode()

        assert len(response.context['not_started_students']) == 1
        assert 'never opened this lesson' in body
        assert 'Kelly' not in body


@pytest.mark.django_db
class TestTheTranscriptOffersTheExitReview:

    def _attempt(self, session, lesson, score=1, passed=False):
        from django.utils import timezone
        from ai_tutor.apps.tutoring.models import ExitTicket, ExitTicketAttempt
        ticket, _ = ExitTicket.objects.get_or_create(
            lesson=lesson, defaults={'passing_score': 8,
                                     'questions_per_attempt': 10})
        return ExitTicketAttempt.objects.create(
            session=session, student=session.student, exit_ticket=ticket,
            answers=[], score=score, passed=passed,
            completed_at=timezone.now())

    def test_a_failed_attempt_still_offers_the_link(self, client, teacher,
                                                    school, lesson):
        """The engine writes engine_state's exit_ticket_score only on a PASS,
        so the score block never renders for a failed attempt — which is the
        one a teacher most wants to read."""
        session = _worked(_student(school, 'amara'), lesson, school)
        self._attempt(session, lesson, score=1, passed=False)

        client.force_login(teacher)
        response = client.get(reverse('dashboard:session_chat_history',
                                      args=[session.id]))

        assert response.context['has_exit_review'] is True
        assert response.context['exit_score'] is None
        assert reverse('dashboard:session_exit_review',
                       args=[session.id]) in response.content.decode()

    def test_no_attempt_means_no_link(self, client, teacher, school, lesson):
        """session_exit_review redirects out to the monitor when there is no
        attempt — a link that bounces a teacher out of the transcript they are
        reading is worse than no link."""
        session = _worked(_student(school, 'amara'), lesson, school)

        client.force_login(teacher)
        response = client.get(reverse('dashboard:session_chat_history',
                                      args=[session.id]))

        assert response.context['has_exit_review'] is False
        assert reverse('dashboard:session_exit_review',
                       args=[session.id]) not in response.content.decode()
