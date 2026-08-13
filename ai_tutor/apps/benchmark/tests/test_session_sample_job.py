"""Dashboard-triggered sampling.

Sampling from a button is not the same problem as sampling from a shell. Three
things only bite in the web version, and each has a test here:

- **Two replicas.** The service runs more than one ECS task, so checking
  "is a run in progress?" before inserting is not atomic. A partial unique
  constraint makes the second insert fail instead.
- **An abandoned run.** A deploy kills the worker mid-flight and the row stays
  RUNNING forever — the same trap as `content_status='generating'`, which
  CLAUDE.md says still needs a manual reset today. Here it self-heals.
- **One bad session.** A single screening failure must not abort a 200-session
  run and lose the work already done.

Plan: memory/session_eval_framework_plan.md
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from ai_tutor.apps.accounts.models import Institution, Membership
from ai_tutor.apps.benchmark import session_sampling as S
from ai_tutor.apps.benchmark.models import SessionEvalItem, SessionSampleRun
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.tutoring.models import SessionTurn, TutorSession


@pytest.fixture
def staff(db):
    return User.objects.create_user(username='sampler', password='x',
                                    is_staff=True, is_superuser=True)


@pytest.fixture
def school(db):
    return Institution.objects.create(name='School', slug='job-school')


@pytest.fixture
def lesson(school):
    course = Course.objects.create(title='Geo', institution=school,
                                   subject_type='geography')
    unit = Unit.objects.create(course=course, title='Maps', order_index=1)
    return Lesson.objects.create(unit=unit, title='Maps', objective='o',
                                 order_index=1, is_published=True)


def make_session(school, lesson, username):
    user = User.objects.create_user(username=username)
    Membership.objects.create(user=user, institution=school, role='student',
                              is_active=True)
    session = TutorSession.objects.create(student=user, lesson=lesson,
                                          institution=school)
    for role, text in [('tutor', 'What does the scale bar show?'),
                       ('student', 'distance'),
                       ('tutor', 'Right. Read the number on it.'),
                       ('student', 'ok 1:50000')]:
        SessionTurn.objects.create(session=session, role=role, content=text)
    return session


@pytest.fixture
def no_llm(monkeypatch):
    """Stub the LLM name pass — these tests must not make network calls."""
    monkeypatch.setattr(S, 'llm_name_candidates',
                        lambda transcript, llm_client=None: ([], ''))


@pytest.fixture(autouse=True)
def captured_async(monkeypatch):
    """Never let a view start a real thread during tests.

    A real thread touches the SQLite test database from outside the test's
    transaction and dies with "database table is locked" — and worse, it can
    leave rows behind that make an unrelated later test flaky. View tests
    record the call instead; the job body is exercised directly in TestTheJob.
    """
    calls = []
    monkeypatch.setattr('ai_tutor.apps.dashboard.background_tasks.run_async',
                        lambda f, *a, **k: calls.append(a))
    return calls


@pytest.mark.django_db
class TestOnlyOneRunAtATime:
    def test_a_second_running_row_is_rejected_by_the_database(self):
        """An .exists() check would not survive two replicas racing. This is a
        constraint, so the race is impossible rather than unlikely."""
        SessionSampleRun.objects.create(requested_limit=10)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                SessionSampleRun.objects.create(requested_limit=10)

    def test_finished_runs_do_not_block_a_new_one(self):
        """The constraint is partial — only RUNNING rows are exclusive."""
        SessionSampleRun.objects.create(
            requested_limit=10, status=SessionSampleRun.Status.COMPLETED)
        SessionSampleRun.objects.create(
            requested_limit=10, status=SessionSampleRun.Status.FAILED)

        SessionSampleRun.objects.create(requested_limit=10)   # must not raise

        assert SessionSampleRun.objects.count() == 3

    def test_the_view_reports_a_conflict_instead_of_a_500(
            self, client, staff, no_llm):
        SessionSampleRun.objects.create(requested_limit=10)
        client.force_login(staff)

        response = client.post(
            reverse('dashboard:benchmark:session_sample_create'),
            {'limit': 10}, follow=True)

        assert response.status_code == 200
        assert any('already in progress' in str(m)
                   for m in response.context['messages'])
        assert SessionSampleRun.objects.count() == 1


@pytest.mark.django_db
class TestStaleRunsSelfHeal:
    def test_an_abandoned_run_is_reclaimed(self):
        """A deploy kills the worker mid-run. Without this the button is dead
        forever and the only fix is a shell — the exact trap that
        content_status='generating' still has."""
        run = SessionSampleRun.objects.create(requested_limit=10)
        SessionSampleRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - SessionSampleRun.STALE_AFTER
            - timedelta(minutes=1))

        SessionSampleRun.reclaim_stale()
        run.refresh_from_db()

        assert run.status == SessionSampleRun.Status.FAILED
        assert 'Abandoned' in run.error

    def test_a_recent_run_is_left_alone(self):
        """Reclaiming an in-flight run would let a second start alongside it."""
        run = SessionSampleRun.objects.create(requested_limit=10)

        SessionSampleRun.reclaim_stale()
        run.refresh_from_db()

        assert run.status == SessionSampleRun.Status.RUNNING

    def test_the_list_page_reclaims_on_read(self, client, staff):
        """Otherwise the page shows 'Sampling…' indefinitely and the only way
        to discover it is stuck is to try to start another."""
        run = SessionSampleRun.objects.create(requested_limit=10)
        SessionSampleRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - SessionSampleRun.STALE_AFTER
            - timedelta(minutes=1))
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_list'))

        assert response.context['run_active'] is False
        run.refresh_from_db()
        assert run.status == SessionSampleRun.Status.FAILED

    def test_a_stale_run_does_not_block_starting_a_new_one(
            self, client, staff, no_llm):
        old = SessionSampleRun.objects.create(requested_limit=10)
        SessionSampleRun.objects.filter(pk=old.pk).update(
            started_at=timezone.now() - SessionSampleRun.STALE_AFTER
            - timedelta(minutes=1))
        client.force_login(staff)

        client.post(reverse('dashboard:benchmark:session_sample_create'),
                    {'limit': 5})

        assert SessionSampleRun.objects.count() == 2


@pytest.mark.django_db
class TestTheJob:
    def test_it_creates_items_and_records_the_run(self, school, lesson, no_llm):
        for i in range(3):
            make_session(school, lesson, f'pupil{i}')
        run = SessionSampleRun.objects.create(requested_limit=10, keep_count=5)

        S.run_sample_job(run.id, limit=10, keep=5)
        run.refresh_from_db()

        assert run.status == SessionSampleRun.Status.COMPLETED
        assert run.created_items == 3
        assert run.candidates == 3
        assert run.screened == 3
        assert run.finished_at is not None
        assert SessionEvalItem.objects.count() == 3

    def test_everything_it_creates_awaits_review(self, school, lesson, no_llm):
        """The whole point of the gate: a button press must not be able to
        produce something an annotator can open."""
        make_session(school, lesson, 'pupilA')
        run = SessionSampleRun.objects.create(requested_limit=10)

        S.run_sample_job(run.id, limit=10, keep=5)

        assert set(SessionEvalItem.objects.values_list('status', flat=True)) == {
            SessionEvalItem.Status.PENDING_REVIEW}

    def test_it_does_not_resample_an_already_sampled_session(
            self, school, lesson, no_llm):
        """Sampling twice should find new sessions, not duplicate old ones."""
        make_session(school, lesson, 'pupilB')

        first = SessionSampleRun.objects.create(requested_limit=10)
        S.run_sample_job(first.id, limit=10, keep=5)
        SessionSampleRun.objects.all().update(
            status=SessionSampleRun.Status.COMPLETED)

        second = SessionSampleRun.objects.create(requested_limit=10)
        S.run_sample_job(second.id, limit=10, keep=5)
        second.refresh_from_db()

        assert second.created_items == 0
        assert SessionEvalItem.objects.count() == 1

    def test_keep_caps_how_many_are_created(self, school, lesson, no_llm):
        for i in range(5):
            make_session(school, lesson, f'many{i}')
        run = SessionSampleRun.objects.create(requested_limit=50, keep_count=2)

        S.run_sample_job(run.id, limit=50, keep=2)
        run.refresh_from_db()

        assert run.candidates == 5      # all screened…
        assert run.created_items == 2   # …2 kept

    def test_rejections_are_counted_not_silently_dropped(
            self, school, lesson, no_llm):
        """A rejection rate that moves is how we notice the redactor breaking,
        so it has to be visible."""
        flagged = make_session(school, lesson, 'flagged')
        flagged.is_flagged = True
        flagged.save()
        make_session(school, lesson, 'clean')
        run = SessionSampleRun.objects.create(requested_limit=10)

        S.run_sample_job(run.id, limit=10, keep=5)
        run.refresh_from_db()

        assert run.rejections == {'safety:session_flagged': 1}
        assert run.created_items == 1

    def test_one_bad_session_does_not_abort_the_run(
            self, school, lesson, monkeypatch):
        """Losing 199 sessions' work because the 43rd raised would be a bad
        trade, and the LLM pass is a network call — it will raise sometimes."""
        make_session(school, lesson, 'ok1')
        make_session(school, lesson, 'boom')
        make_session(school, lesson, 'ok2')

        calls = {'n': 0}
        real = S.screen_and_prepare

        def flaky(session, *args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 2:
                raise RuntimeError('provider exploded')
            return real(session, use_llm=False)

        monkeypatch.setattr(S, 'screen_and_prepare', flaky)
        run = SessionSampleRun.objects.create(requested_limit=10)

        S.run_sample_job(run.id, limit=10, keep=5)
        run.refresh_from_db()

        assert run.status == SessionSampleRun.Status.COMPLETED
        assert run.created_items == 2
        assert run.rejections['screening_error'] == 1

    def test_a_hard_failure_closes_the_run_rather_than_leaving_it_running(
            self, school, lesson, monkeypatch):
        """A thread that dies with the row RUNNING blocks the button until the
        stale timeout. Every exit path must close the row."""
        make_session(school, lesson, 'pupilC')
        monkeypatch.setattr(
            S, 'candidate_sessions',
            lambda **kw: (_ for _ in ()).throw(RuntimeError('db gone')))
        run = SessionSampleRun.objects.create(requested_limit=10)

        S.run_sample_job(run.id, limit=10, keep=5)
        run.refresh_from_db()

        assert run.status == SessionSampleRun.Status.FAILED
        assert 'db gone' in run.error
        assert run.finished_at is not None

    def test_a_missing_run_row_is_survived(self):
        """Deleted mid-flight; the thread must not raise into nothing."""
        S.run_sample_job(999999, limit=10, keep=3)


@pytest.mark.django_db
class TestTheSampleForm:
    def test_the_limit_is_clamped(self, client, staff, captured_async):
        """Still bounded — one LLM call per candidate screened, so an
        unbounded box is an unbounded bill. The cap is high enough to cover the
        whole dataset, and since draw_pool shuffles first, a lower value costs
        precision in nothing but sample size."""
        client.force_login(staff)

        client.post(reverse('dashboard:benchmark:session_sample_create'),
                    {'limit': '999999', 'keep': '99999'})

        (_run_id, limit, keep, _inst, _start, _end, _course,
         _engine) = captured_async[0]
        assert limit == 5000
        assert keep == 1000

    def test_junk_input_falls_back_to_the_default(self, client, staff,
                                                  captured_async):
        client.force_login(staff)

        client.post(reverse('dashboard:benchmark:session_sample_create'),
                    {'limit': 'abc', 'keep': ''})

        (_run_id, limit, keep, _inst, _start, _end, _course,
         _engine) = captured_async[0]
        assert limit == 200
        assert keep == 20

    def test_a_zero_or_negative_limit_becomes_one(self, client, staff,
                                                  captured_async):
        client.force_login(staff)

        client.post(reverse('dashboard:benchmark:session_sample_create'),
                    {'limit': '-5'})

        assert captured_async[0][1] == 1     # limit

    def test_it_requires_staff(self, client):
        response = client.post(
            reverse('dashboard:benchmark:session_sample_create'), {'limit': 5})
        assert response.status_code in (302, 403)
        assert SessionSampleRun.objects.count() == 0

    def test_get_is_not_allowed(self, client, staff):
        client.force_login(staff)
        response = client.get(
            reverse('dashboard:benchmark:session_sample_create'))
        assert response.status_code == 405

    def test_the_page_offers_the_button_not_a_shell_command(
            self, client, staff, school, lesson):
        make_session(school, lesson, 'pupilD')
        client.force_login(staff)

        body = client.get(
            reverse('dashboard:benchmark:session_list')).content.decode()

        assert 'Sample sessions' in body
        assert 'manage.py sample_sessions' not in body

    def test_the_button_is_disabled_while_a_run_is_active(self, client, staff):
        SessionSampleRun.objects.create(requested_limit=10)
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_list'))

        assert response.context['run_active'] is True
        assert 'Sampling…' in response.content.decode()

    def test_a_conflict_leaves_the_transaction_usable(self, client, staff):
        """The IntegrityError is caught inside a savepoint. Without one, on
        PostgreSQL the surrounding transaction is poisoned and the very next
        query — writing the warning message — raises TransactionManagementError,
        turning a handled conflict into a 500. SQLite tolerates it, so this
        needed reasoning rather than local observation."""
        SessionSampleRun.objects.create(requested_limit=10)
        client.force_login(staff)

        client.post(reverse('dashboard:benchmark:session_sample_create'),
                    {'limit': 10})

        # A query after the caught error must still work.
        assert SessionSampleRun.objects.count() == 1


@pytest.mark.django_db
class TestRepeatedSamplingIsAdditive:
    """The bug this class exists for, observed on 2026-08-11.

    Already-sampled sessions used to stay in the candidate pool. Ordering is
    stable, so they filled their strata buckets first, consumed the per-stratum
    quota, and were then dropped by the duplicate check — a run that screened 20
    sessions, paid for 20 LLM calls, and created nothing. Clicking Sample again
    did nothing, forever, while the page claimed it would pick up new ones.
    """

    def test_a_second_run_picks_up_new_sessions(self, school, lesson, no_llm):
        for i in range(3):
            make_session(school, lesson, f'first{i}')

        run1 = SessionSampleRun.objects.create(requested_limit=50, keep_count=3)
        S.run_sample_job(run1.id, limit=50, keep=3)
        SessionSampleRun.objects.all().update(
            status=SessionSampleRun.Status.COMPLETED)
        assert SessionEvalItem.objects.count() == 3

        # New sessions arrive; the quota must be spent on them.
        for i in range(3):
            make_session(school, lesson, f'second{i}')

        run2 = SessionSampleRun.objects.create(requested_limit=50, keep_count=3)
        S.run_sample_job(run2.id, limit=50, keep=3)
        run2.refresh_from_db()

        assert run2.created_items == 3
        assert SessionEvalItem.objects.count() == 6

    def test_sampled_sessions_are_not_re_screened(self, school, lesson, no_llm):
        """They should not even reach the LLM pass — that is the wasted spend."""
        make_session(school, lesson, 'once')
        run1 = SessionSampleRun.objects.create(requested_limit=50)
        S.run_sample_job(run1.id, limit=50, keep=3)
        SessionSampleRun.objects.all().update(
            status=SessionSampleRun.Status.COMPLETED)

        run2 = SessionSampleRun.objects.create(requested_limit=50)
        S.run_sample_job(run2.id, limit=50, keep=3)
        run2.refresh_from_db()

        assert run2.candidates == 0
        assert run2.screened == 0

    def test_the_eligible_count_excludes_what_is_already_sampled(
            self, client, staff, school, lesson, no_llm):
        make_session(school, lesson, 'sampled')
        make_session(school, lesson, 'fresh')
        run = SessionSampleRun.objects.create(requested_limit=50, keep_count=1)
        S.run_sample_job(run.id, limit=50, keep=1)
        SessionSampleRun.objects.all().update(
            status=SessionSampleRun.Status.COMPLETED)
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_list'))

        assert response.context['already_sampled'] == 1
        assert response.context['eligible'] == 1     # the one still untouched


@pytest.mark.django_db
class TestSelectionIsARandomDraw:
    """Stratified selection was removed on 2026-08-11.

    A quota per subject|engine|outcome guarantees coverage of rare conditions,
    but over-represents them by construction — so a pass rate over such a
    sample is not an estimate of the production rate, which is the number this
    study reports. Selection is now uniform over everything that clears
    screening.
    """

    def test_it_does_not_just_take_the_newest(self, school, lesson, no_llm):
        """The candidate queryset is ordered -started_at. Taking a slice of it
        would bias the sample toward whatever happened most recently — a
        curriculum change or a bad week would dominate the gold set."""
        sessions = [make_session(school, lesson, f'ord{i}') for i in range(10)]
        newest_five = {s.id for s in sessions[-5:]}

        run = SessionSampleRun.objects.create(requested_limit=50, keep_count=5)
        S.run_sample_job(run.id, limit=50, keep=5)

        chosen = set(SessionEvalItem.objects.values_list(
            'source_session_id', flat=True))
        assert len(chosen) == 5
        assert chosen != newest_five

    def test_the_same_run_id_reproduces_the_same_draw(self, school, lesson,
                                                      no_llm):
        """Seeded from the run id, so a failed run can be re-run and examined
        rather than producing a different sample each time."""
        for i in range(8):
            make_session(school, lesson, f'seed{i}')

        run_a = SessionSampleRun.objects.create(requested_limit=50, keep_count=3)
        S.run_sample_job(run_a.id, limit=50, keep=3)
        first = set(SessionEvalItem.objects.values_list(
            'source_session_id', flat=True))

        SessionEvalItem.objects.all().delete()
        SessionSampleRun.objects.all().update(
            status=SessionSampleRun.Status.COMPLETED)
        S.run_sample_job(run_a.id, limit=50, keep=3)
        second = set(SessionEvalItem.objects.values_list(
            'source_session_id', flat=True))

        assert first == second

    def test_different_runs_draw_differently(self, school, lesson, no_llm):
        for i in range(12):
            make_session(school, lesson, f'diff{i}')

        run_a = SessionSampleRun.objects.create(requested_limit=50, keep_count=3)
        S.run_sample_job(run_a.id, limit=50, keep=3)
        first = set(SessionEvalItem.objects.values_list(
            'source_session_id', flat=True))

        SessionEvalItem.objects.all().delete()
        SessionSampleRun.objects.all().update(
            status=SessionSampleRun.Status.COMPLETED)
        run_b = SessionSampleRun.objects.create(requested_limit=50, keep_count=3)
        S.run_sample_job(run_b.id, limit=50, keep=3)
        second = set(SessionEvalItem.objects.values_list(
            'source_session_id', flat=True))

        assert first != second

    def test_a_rare_condition_is_not_given_an_artificial_quota(
            self, school, no_llm):
        """Under the old stratified rule, 1 rare session among 9 common ones
        was guaranteed a slot. Under a uniform draw it is not — which is the
        point: the sample should look like the population."""
        from ai_tutor.apps.curriculum.models import Course, Lesson, Unit

        maths = Course.objects.create(title='Maths', institution=school,
                                      subject_type='math')
        m_unit = Unit.objects.create(course=maths, title='U', order_index=1)
        rare_lesson = Lesson.objects.create(unit=m_unit, title='Rare',
                                            objective='o', order_index=1)
        geo = Course.objects.create(title='Geo', institution=school,
                                    subject_type='geography')
        g_unit = Unit.objects.create(course=geo, title='U', order_index=1)
        common_lesson = Lesson.objects.create(unit=g_unit, title='Common',
                                              objective='o', order_index=1)

        make_session(school, rare_lesson, 'rare0')
        for i in range(9):
            make_session(school, common_lesson, f'common{i}')

        # Repeat over several run ids; a quota would put the rare subject in
        # every single draw.
        appearances = 0
        for run_id_seed in range(6):
            SessionEvalItem.objects.all().delete()
            SessionSampleRun.objects.all().delete()
            run = SessionSampleRun.objects.create(requested_limit=50,
                                                  keep_count=2)
            S.run_sample_job(run.id, limit=50, keep=2)
            if SessionEvalItem.objects.filter(subject='math').exists():
                appearances += 1

        assert appearances < 6


@pytest.mark.django_db
class TestThePoolIsNotJustTheNewest:
    """The bias bug, found in production on 2026-08-11.

    `candidate_sessions` is ordered `-started_at`. Slicing it — which is what
    `[:limit]` did — screened only the most recent `limit` sessions. With 1001
    eligible and a cap of 500, the older half of the pilot could never be
    sampled at all, while the page claimed a uniform draw that estimates the
    population. A term boundary or a curriculum change would have silently
    defined the entire gold set.

    `draw_pool` shuffles before slicing, so `limit` controls cost and nothing
    else.
    """

    def _spread(self, school, lesson, n):
        """n sessions with distinct started_at, oldest first."""
        made = []
        for i in range(n):
            s = make_session(school, lesson, f'spread{i}')
            TutorSession.objects.filter(pk=s.pk).update(
                started_at=timezone.now() - timedelta(days=n - i))
            made.append(s)
        return made

    def test_a_limit_smaller_than_the_pool_still_reaches_old_sessions(
            self, school, lesson):
        sessions = self._spread(school, lesson, 20)
        oldest_half = {s.id for s in sessions[:10]}

        # Screen only 5 of 20, across several seeds.
        reached_old = False
        for seed in range(8):
            pool = S.draw_pool(S.candidate_sessions(), 5, seed=seed)
            if oldest_half & {s.id for s in pool}:
                reached_old = True
                break

        assert reached_old, 'the pool never reached the older half'

    def test_slicing_the_queryset_directly_would_have_failed_this(
            self, school, lesson):
        """Pins the old behaviour as wrong, so nobody 'simplifies' draw_pool
        back into a slice."""
        sessions = self._spread(school, lesson, 20)
        oldest_half = {s.id for s in sessions[:10]}

        naive = list(S.candidate_sessions()[:5])

        assert not (oldest_half & {s.id for s in naive})

    def test_the_draw_is_reproducible_for_a_seed(self, school, lesson):
        self._spread(school, lesson, 15)

        a = [s.id for s in S.draw_pool(S.candidate_sessions(), 5, seed=42)]
        b = [s.id for s in S.draw_pool(S.candidate_sessions(), 5, seed=42)]

        assert a == b

    def test_different_seeds_draw_different_pools(self, school, lesson):
        self._spread(school, lesson, 30)

        a = {s.id for s in S.draw_pool(S.candidate_sessions(), 5, seed=1)}
        b = {s.id for s in S.draw_pool(S.candidate_sessions(), 5, seed=2)}

        assert a != b

    def test_a_limit_above_the_pool_returns_everything(self, school, lesson):
        self._spread(school, lesson, 6)

        pool = S.draw_pool(S.candidate_sessions(), 500, seed=0)

        assert len(pool) == 6


@pytest.mark.django_db
class TestFilters:
    def _dated(self, school, lesson, username, days_ago):
        s = make_session(school, lesson, username)
        TutorSession.objects.filter(pk=s.pk).update(
            started_at=timezone.now() - timedelta(days=days_ago))
        return s

    def test_start_and_end_scope_the_pool(self, school, lesson):
        from datetime import date
        self._dated(school, lesson, 'old', 60)
        recent = self._dated(school, lesson, 'recent', 5)

        today = timezone.now().date()
        got = S.candidate_sessions(start=today - timedelta(days=30), end=today)

        assert [s.id for s in got] == [recent.id]

    def test_end_is_inclusive_of_the_whole_day(self, school, lesson):
        """A session at 14:00 on the end date must be included — comparing a
        datetime against a date would silently drop it."""
        s = self._dated(school, lesson, 'sameday', 0)
        day = TutorSession.objects.get(pk=s.pk).started_at.date()

        assert S.candidate_sessions(start=day, end=day).count() == 1

    def test_course_scopes_the_pool(self, school, lesson):
        from ai_tutor.apps.curriculum.models import Course, Lesson, Unit

        other = Course.objects.create(title='Other', institution=school,
                                      subject_type='math')
        unit = Unit.objects.create(course=other, title='U', order_index=1)
        other_lesson = Lesson.objects.create(unit=unit, title='L',
                                             objective='o', order_index=1)
        keep = make_session(school, lesson, 'inscope')
        make_session(school, other_lesson, 'outofscope')

        got = S.candidate_sessions(course_id=lesson.unit.course_id)

        assert [s.id for s in got] == [keep.id]

    def test_the_job_honours_the_filters(self, school, lesson, no_llm):
        from ai_tutor.apps.curriculum.models import Course, Lesson, Unit

        other = Course.objects.create(title='Other', institution=school,
                                      subject_type='math')
        unit = Unit.objects.create(course=other, title='U', order_index=1)
        other_lesson = Lesson.objects.create(unit=unit, title='L',
                                             objective='o', order_index=1)
        make_session(school, lesson, 'wanted')
        make_session(school, other_lesson, 'unwanted')

        run = SessionSampleRun.objects.create(requested_limit=50, keep_count=50)
        S.run_sample_job(run.id, limit=50, keep=50,
                         course_id=lesson.unit.course_id)
        run.refresh_from_db()

        assert run.candidates == 1
        assert run.created_items == 1
        assert SessionEvalItem.objects.get().source_session.student.username == 'wanted'

    def test_a_malformed_date_scopes_to_everything_not_nothing(
            self, client, staff, captured_async):
        """Returning no sessions for a typo looks identical to 'no data', which
        would send someone hunting for a bug that isn't there."""
        client.force_login(staff)

        client.post(reverse('dashboard:benchmark:session_sample_create'),
                    {'limit': 10, 'start': 'not-a-date'})

        (_run, _limit, _keep, _inst, start, end, _course,
         _engine) = captured_async[0]
        assert start is None and end is None

    def test_swapped_dates_are_corrected_rather_than_returning_nothing(
            self, client, staff, captured_async):
        client.force_login(staff)

        client.post(reverse('dashboard:benchmark:session_sample_create'),
                    {'limit': 10, 'start': '2026-08-01', 'end': '2026-07-01'})

        (_run, _limit, _keep, _inst, start, end, _course,
         _engine) = captured_async[0]
        assert start.isoformat() == '2026-07-01'
        assert end.isoformat() == '2026-08-01'

    def test_the_run_records_what_it_was_scoped_to(self, client, staff,
                                                   captured_async):
        client.force_login(staff)

        client.post(reverse('dashboard:benchmark:session_sample_create'),
                    {'limit': 10, 'start': '2026-07-01', 'end': '2026-08-01'})

        run = SessionSampleRun.objects.get()
        assert run.filter_start.isoformat() == '2026-07-01'
        assert run.filter_end.isoformat() == '2026-08-01'


@pytest.mark.django_db
class TestTheHeartbeat:
    def test_a_long_but_healthy_run_is_not_reclaimed(self):
        """The old rule measured from started_at, so a 1000-session run would
        have been marked failed mid-flight — and a second run could then start
        alongside it, doubling the LLM spend."""
        run = SessionSampleRun.objects.create(requested_limit=1000)
        SessionSampleRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - timedelta(hours=3),
            last_progress_at=timezone.now(),        # still ticking
        )

        SessionSampleRun.reclaim_stale()
        run.refresh_from_db()

        assert run.status == SessionSampleRun.Status.RUNNING

    def test_a_silent_run_is_reclaimed(self):
        run = SessionSampleRun.objects.create(requested_limit=1000)
        SessionSampleRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - timedelta(hours=3),
            last_progress_at=timezone.now() - SessionSampleRun.STALE_AFTER
            - timedelta(minutes=1),
        )

        SessionSampleRun.reclaim_stale()
        run.refresh_from_db()

        assert run.status == SessionSampleRun.Status.FAILED

    def test_a_run_that_died_before_its_first_batch_is_reclaimed(self):
        """last_progress_at is null until the first progress write; without the
        started_at fallback such a run would never be reclaimed."""
        run = SessionSampleRun.objects.create(requested_limit=1000)
        SessionSampleRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - SessionSampleRun.STALE_AFTER
            - timedelta(minutes=1),
            last_progress_at=None,
        )

        SessionSampleRun.reclaim_stale()
        run.refresh_from_db()

        assert run.status == SessionSampleRun.Status.FAILED

    def test_the_job_updates_the_heartbeat(self, school, lesson, no_llm):
        for i in range(6):
            make_session(school, lesson, f'beat{i}')
        run = SessionSampleRun.objects.create(requested_limit=50, keep_count=6)

        S.run_sample_job(run.id, limit=50, keep=6)
        run.refresh_from_db()

        assert run.last_progress_at is not None


@pytest.mark.django_db
class TestEngineFilter:
    """`simple` is the current engine and is the default everywhere.

    The trap this class guards: most HISTORICAL sessions are v1, so a `simple`
    default can legitimately match nothing. A silent zero is indistinguishable
    from a broken sampler, and a default that hides an existing v1 batch looks
    like data loss. Both are surfaced rather than left to be discovered.
    """

    def _simple(self, school, lesson, username):
        s = make_session(school, lesson, username)
        TutorSession.objects.filter(pk=s.pk).update(engine='simple')
        return s

    def test_the_pool_can_be_scoped_to_one_engine(self, school, lesson):
        v1 = make_session(school, lesson, 'legacy')          # default engine
        simple = self._simple(school, lesson, 'current')

        assert [s.id for s in S.candidate_sessions(engine='simple')] == [simple.id]
        assert [s.id for s in S.candidate_sessions(engine='v1')] == [v1.id]

    def test_no_engine_means_all_engines(self, school, lesson):
        make_session(school, lesson, 'legacy')
        self._simple(school, lesson, 'current')

        assert S.candidate_sessions().count() == 2

    def test_the_job_honours_it(self, school, lesson, no_llm):
        make_session(school, lesson, 'legacy')
        self._simple(school, lesson, 'current')
        run = SessionSampleRun.objects.create(requested_limit=50, keep_count=50)

        S.run_sample_job(run.id, limit=50, keep=50, engine='simple')
        run.refresh_from_db()

        assert run.candidates == 1
        assert SessionEvalItem.objects.get().engine == 'simple'

    def test_the_per_engine_counts_expose_an_empty_pool(self, school, lesson):
        """The number that stops 'simple: 0' looking like a bug."""
        make_session(school, lesson, 'legacy')
        make_session(school, lesson, 'legacy2')

        counts = S.eligible_by_engine()

        assert counts == {'v1': 2}
        assert counts.get('simple', 0) == 0

    def test_the_per_engine_counts_sum_to_the_eligible_count(self, school,
                                                             lesson):
        """The invariant that caught a real bug. Chaining
        `.values('engine').annotate(...)` onto candidate_sessions REGROUPS the
        queryset, so the min-turns filter lands on the engine group instead of
        each session — it reported 18 across engines where eligible was 13.
        A breakdown that does not add up to the headline is worse than none."""
        for i in range(4):
            make_session(school, lesson, f'sum_v1_{i}')
        s = make_session(school, lesson, 'sum_simple')
        TutorSession.objects.filter(pk=s.pk).update(engine='simple')
        # A session too short to qualify, to make the min-turns filter matter.
        short = TutorSession.objects.create(
            student=User.objects.create_user(username='too_short'),
            lesson=lesson, institution=school)
        SessionTurn.objects.create(session=short, role='tutor', content='hi')

        counts = S.eligible_by_engine()

        assert sum(counts.values()) == S.candidate_sessions().count()
        assert counts == {'simple': 1, 'v1': 4}

    def test_the_sample_form_defaults_to_simple(self, client, staff,
                                                captured_async):
        client.force_login(staff)

        client.post(reverse('dashboard:benchmark:session_sample_create'),
                    {'limit': 10, 'engine': 'simple'})

        assert captured_async[0][7] == 'simple'

    def test_an_unknown_engine_falls_back_to_all(self, client, staff,
                                                 captured_async):
        """A typo must not silently scope sampling to nothing."""
        client.force_login(staff)

        client.post(reverse('dashboard:benchmark:session_sample_create'),
                    {'limit': 10, 'engine': 'tutoring_agentic_harness'})

        assert captured_async[0][7] == ''

    def test_the_run_records_the_engine_it_was_scoped_to(self, client, staff,
                                                        captured_async):
        client.force_login(staff)

        client.post(reverse('dashboard:benchmark:session_sample_create'),
                    {'limit': 10, 'engine': 'simple'})

        assert SessionSampleRun.objects.get().filter_engine == 'simple'


@pytest.mark.django_db
class TestTheListEngineFilter:
    def _item(self, item_id, engine):
        return SessionEvalItem.objects.create(
            item_id=item_id, session_key=f's_{item_id.lower()}',
            engine=engine, turn_count=4,
            transcript=[{'turn': 1, 'role': 'tutor', 'content': 'x'}])

    def test_it_defaults_to_simple(self, client, staff):
        self._item('SESS_V1', 'v1')
        simple = self._item('SESS_SIMPLE', 'simple')
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_list'))

        assert response.context['filters']['engine'] == 'simple'
        assert [r['item'].item_id for r in response.context['rows']] == [
            simple.item_id]

    def test_it_says_how_many_it_is_hiding(self, client, staff):
        """Otherwise a v1 batch appears to have vanished and the natural
        conclusion is that sampling deleted it."""
        self._item('SESS_V1_A', 'v1')
        self._item('SESS_V1_B', 'v1')
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_list'))

        assert response.context['hidden_by_engine'] == 2
        assert 'are hidden' in response.content.decode()

    def test_all_engines_shows_everything_and_hides_the_warning(
            self, client, staff):
        self._item('SESS_V1', 'v1')
        self._item('SESS_SIMPLE', 'simple')
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_list'),
                              {'engine': 'all'})

        assert len(response.context['rows']) == 2
        assert response.context['hidden_by_engine'] == 0

    def test_an_unknown_value_falls_back_to_the_default(self, client, staff):
        self._item('SESS_SIMPLE', 'simple')
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_list'),
                              {'engine': 'tutoring_agentic_harness'})

        assert response.context['filters']['engine'] == 'simple'

    def test_pagination_keeps_the_engine_filter(self, client, staff):
        """Page 2 resetting to the default would silently change the result
        set mid-browse."""
        for i in range(30):
            self._item(f'SESS_V1_{i}', 'v1')
        client.force_login(staff)

        body = client.get(reverse('dashboard:benchmark:session_list'),
                          {'engine': 'all'}).content.decode()

        assert 'engine=all' in body
