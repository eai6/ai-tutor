"""The two list pages: classes, and all students.

Both used to lead with a denominator that described the catalogue rather than
the student — "0/129" on every student row, counting every published lesson in
the school, and a class card carrying nothing but a head count. These tests
hold the replacements to what they now claim.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from ai_tutor.apps.accounts.models import Institution, Membership, StudentProfile
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.tutoring.models import (SessionParticipant,
                                           StudentLessonProgress, TutorSession)


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Belonie', slug='belonie')


@pytest.fixture
def teacher(db, school):
    user = User.objects.create_user('teach', 't@example.com', 'pw', is_staff=True)
    Membership.objects.create(user=user, institution=school,
                              role=Membership.Role.STAFF)
    return user


@pytest.fixture
def course(db, school):
    course = Course.objects.create(title='Geography S3', institution=school,
                                   grade_level='S3')
    unit = Unit.objects.create(course=course, title='Maps', order_index=1)
    for i in range(4):
        Lesson.objects.create(unit=unit, title=f'Lesson {i}', objective='o',
                              order_index=i, is_published=True)
    return course


def _student(school, name, grade='S3'):
    user = User.objects.create_user(name, f'{name}@example.com', 'pw',
                                    first_name=name.title())
    Membership.objects.create(user=user, institution=school,
                              role=Membership.Role.STUDENT)
    if grade is not None:
        StudentProfile.objects.create(user=user, grade_level=grade)
    return user


@pytest.mark.django_db
class TestTheClassesList:

    def test_a_card_carries_more_than_a_head_count(self, client, teacher,
                                                   school, course):
        """"Which class needs me" cannot be answered by a number of students."""
        amara = _student(school, 'amara')
        StudentLessonProgress.objects.create(
            student=amara, lesson=course.units.first().lessons.first(),
            institution=school, mastery_level='mastered')

        client.force_login(teacher)
        response = client.get(reverse('dashboard:class_list'))

        card = next(c for c in response.context['classes'] if c['grade'] == 'S3')
        assert card['count'] == 1
        assert card['quiet_count'] == 1           # never worked
        assert card['active_this_week'] == 0
        # Deliberately no mastery figure: a class studies several subjects and
        # one percentage across them averages things that share no scale.
        assert 'mastery_pct' not in card

    def test_it_agrees_with_the_class_page(self, client, teacher, school, course):
        """A list card saying 40% beside a detail page saying 12% is worse than
        the card not existing — both read the same helper."""
        amara = _student(school, 'amara')
        StudentLessonProgress.objects.create(
            student=amara, lesson=course.units.first().lessons.first(),
            institution=school, mastery_level='mastered')

        client.force_login(teacher)
        listed = client.get(reverse('dashboard:class_list'))
        detail = client.get(reverse('dashboard:class_detail', args=['S3']))

        card = next(c for c in listed.context['classes'] if c['grade'] == 'S3')
        assert card['active_this_week'] == detail.context['active_this_week']
        assert card['quiet_count'] == len(detail.context['inactive_students'])

    def test_students_without_a_grade_are_a_prompt_not_a_tile(
            self, client, teacher, school, course):
        """They are not a class — they are students who cannot be taught at a
        grade level, which is a thing to fix."""
        _student(school, 'nogrjob', grade=None)

        client.force_login(teacher)
        response = client.get(reverse('dashboard:class_list'))

        assert response.context['unassigned_count'] == 1
        assert not any(c['grade'] == 'Unassigned'
                       for c in response.context['classes'])
        assert 'no grade on their profile' in response.content.decode()


@pytest.mark.django_db
class TestTheStudentsList:

    def test_no_shared_denominator(self, client, teacher, school, course):
        """0/129 counted every published lesson in the school. An S1 student is
        not working toward the S5 lessons, so it read as zero for everyone."""
        _student(school, 'amara')
        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_list'))

        row = response.context['students'][0]
        assert 'lessons_total' not in row
        assert 'mastery_pct' not in row
        assert f"/{Lesson.objects.count()}" not in response.content.decode()

    def test_it_counts_mastered_and_open_separately(self, client, teacher,
                                                    school, course):
        amara = _student(school, 'amara')
        lessons = list(course.units.first().lessons.all())
        StudentLessonProgress.objects.create(
            student=amara, lesson=lessons[0], institution=school,
            mastery_level='mastered')
        for lesson in lessons[1:3]:
            StudentLessonProgress.objects.create(
                student=amara, lesson=lesson, institution=school,
                mastery_level='in_progress')

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_list'))

        row = response.context['students'][0]
        assert row['lessons_mastered'] == 1
        assert row['lessons_open'] == 2

    def test_a_groupmates_session_counts_as_activity(self, client, teacher,
                                                     school, course):
        """A session's student_id is its OWNER, so a student who only ever
        works alongside a groupmate used to read as "Never"."""
        host = _student(school, 'amara')
        joiner = _student(school, 'jeanluc')
        session = TutorSession.objects.create(
            student=host, lesson=course.units.first().lessons.first(),
            institution=school)
        SessionParticipant.objects.create(session=session, student=joiner,
                                          is_active=True)

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_list'))

        rows = {r['user'].id: r for r in response.context['students']}
        assert rows[joiner.id]['last_active'] is not None
        assert rows[joiner.id]['is_quiet'] is False

    def test_a_long_silence_is_flagged(self, client, teacher, school, course):
        amara = _student(school, 'amara')
        s = TutorSession.objects.create(
            student=amara, lesson=course.units.first().lessons.first(),
            institution=school)
        TutorSession.objects.filter(pk=s.pk).update(
            started_at=timezone.now() - timedelta(days=40))

        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_list'))
        assert response.context['students'][0]['is_quiet'] is True

    def test_a_student_who_never_worked_is_quiet(self, client, teacher, school,
                                                 course):
        _student(school, 'amara')
        client.force_login(teacher)
        response = client.get(reverse('dashboard:student_list'))
        row = response.context['students'][0]
        assert row['last_active'] is None
        assert row['is_quiet'] is True

    def test_the_roster_is_not_queried_once_per_student(self, client, teacher,
                                                        school, course,
                                                        django_assert_max_num_queries):
        """It used to run two queries per student. That is fine for five and
        not for a real school."""
        for i in range(12):
            _student(school, f'student{i}')

        client.force_login(teacher)
        with django_assert_max_num_queries(25):
            client.get(reverse('dashboard:student_list'))


@pytest.mark.django_db
class TestTheWeekIsACalendarWeek:
    """A teacher's "this week" runs Sunday to Saturday. A rolling 168-hour
    window reports most of last week's work as current on a Monday morning —
    exactly when someone is checking whether the week has started."""

    def test_sunday_is_the_start_of_the_week(self, school):
        from ai_tutor.apps.dashboard.views import school_week_start
        import zoneinfo
        from datetime import datetime

        school.timezone = 'Indian/Mahe'          # UTC+4
        tz = zoneinfo.ZoneInfo('Indian/Mahe')

        # Wednesday 16 September 2026, local time.
        wednesday = datetime(2026, 9, 16, 10, 0, tzinfo=tz)
        start = school_week_start(school, wednesday)
        assert start.strftime('%A') == 'Sunday'
        assert (start.year, start.month, start.day) == (2026, 9, 13)
        assert (start.hour, start.minute) == (0, 0)

    def test_sunday_itself_starts_its_own_week(self, school):
        from ai_tutor.apps.dashboard.views import school_week_start
        import zoneinfo
        from datetime import datetime

        school.timezone = 'Indian/Mahe'
        tz = zoneinfo.ZoneInfo('Indian/Mahe')
        sunday = datetime(2026, 9, 13, 9, 0, tzinfo=tz)
        start = school_week_start(school, sunday)
        assert (start.month, start.day) == (9, 13), \
            'Sunday morning belongs to the week it begins, not the one before'

    def test_saturday_is_the_last_day_of_the_week(self, school):
        from ai_tutor.apps.dashboard.views import school_week_start
        import zoneinfo
        from datetime import datetime

        school.timezone = 'Indian/Mahe'
        tz = zoneinfo.ZoneInfo('Indian/Mahe')
        saturday = datetime(2026, 9, 19, 23, 0, tzinfo=tz)
        start = school_week_start(school, saturday)
        assert (start.month, start.day) == (9, 13), \
            'Saturday night is still the same week as the Sunday before it'

    def test_the_boundary_is_local_midnight_not_utc(self, school):
        """Seychelles is UTC+4. A UTC cut would put Saturday evening's work into
        the following week for every school east of Greenwich."""
        from ai_tutor.apps.dashboard.views import school_week_start
        import zoneinfo
        from datetime import datetime, timezone as dt_timezone

        school.timezone = 'Indian/Mahe'
        # 21:00 UTC Saturday = 01:00 local Sunday -> the NEW week locally.
        saturday_utc = datetime(2026, 9, 19, 21, 0, tzinfo=dt_timezone.utc)
        start = school_week_start(school, saturday_utc)
        assert (start.month, start.day) == (9, 20), \
            'local Sunday 01:00 belongs to the week beginning that Sunday'

    def test_a_broken_timezone_string_does_not_500(self, school):
        from ai_tutor.apps.dashboard.views import school_week_start
        school.timezone = 'Not/AZone'
        assert school_week_start(school) is not None


@pytest.mark.django_db
class TestTheCourseTableIsAboutContent:
    """Monitor and Report used to sit on these rows. They moved.

    Both pages are class-scoped now — a roster, who has not started, who has
    since moved on — and a course does not name a class: "Geography S1-S5"
    spans five of them. They live on the class page, which knows which one.
    Review stays: reviewing a lesson is a thing you do to the content.
    """

    def test_the_row_offers_review_only(self, client, teacher, school, course):
        lesson = course.units.first().lessons.first()
        client.force_login(teacher)
        response = client.get(reverse('dashboard:course_detail', args=[course.id]))
        body = response.content.decode()

        assert reverse('dashboard:lesson_detail', args=[lesson.id]) in body
        assert reverse('dashboard:lesson_monitor', args=[lesson.id]) not in body
        assert reverse('dashboard:lesson_session_report', args=[lesson.id]) not in body

    def test_the_class_page_carries_them_with_a_class(self, client, teacher,
                                                      school, course):
        """And carries the class with them — a link that lost the ?class= would
        land on the school-wide page and quietly answer a different question."""
        amara = _student(school, 'amara')
        lesson = course.units.first().lessons.first()
        TutorSession.objects.create(student=amara, lesson=lesson,
                                    institution=school)

        client.force_login(teacher)
        response = client.get(reverse('dashboard:class_detail', args=['S3']))
        body = response.content.decode()

        monitor = reverse('dashboard:lesson_monitor', args=[lesson.id])
        report = reverse('dashboard:lesson_session_report', args=[lesson.id])
        assert f'{monitor}?class=S3' in body
        assert f'{report}?class=S3' in body
