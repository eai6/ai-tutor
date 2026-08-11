"""Scoring and export for session-level evaluation.

Two things here are easy to get wrong in ways that would misreport the study:

**Denominators.** N/A and unanswered must sit in neither numerator nor
denominator. Counting N/A as failure penalises the tutor for the student never
erring; counting unanswered as failure reports annotator throughput as tutor
quality.

**Cohen's κ when both raters used one category.** po = pe = 1 gives 0/0. The
common shortcut returns 0.0, which calls *perfect* agreement chance-level — the
exact inversion of the truth. This is not hypothetical for this taxonomy:
'revealing_answer' is 'No' for most well-behaved sessions.

The export tests mirror `apps/dashboard/tests/test_aggregate_export.py`: plant
identifying data, assert none of it survives.

Plan: memory/session_eval_framework_plan.md
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from apps.benchmark import pedagogy as P
from apps.benchmark import session_scoring as SS
from apps.benchmark.models import SessionEvalAnnotation, SessionEvalItem


def all_good() -> dict:
    return {d.key: d.desideratum for d in P.DIMENSIONS}


@pytest.fixture
def staff(db):
    return User.objects.create_user(username='scorer', password='x',
                                    is_staff=True, is_superuser=True)


def make_item(item_id='SESS_1', **kwargs):
    kwargs.setdefault('subject', 'geography')
    kwargs.setdefault('engine', 'simple')
    kwargs.setdefault('outcome', 'passed_exit_ticket')
    return SessionEvalItem.objects.create(
        item_id=item_id, session_key=f's_{item_id.lower()}',
        status=SessionEvalItem.Status.APPROVED,
        turn_count=4,
        transcript=[{'turn': 1, 'role': 'tutor', 'content': 'Scale bar?'},
                    {'turn': 2, 'role': 'student', 'content': 'distance'}],
        **kwargs)


def annotate(item, user=None, role=SessionEvalAnnotation.Annotator.HUMAN,
             model='', **overrides):
    values = all_good()
    values.update(overrides)
    return SessionEvalAnnotation.objects.create(
        item=item, annotator_user=user, annotator_role=role,
        annotator_model=model, **values)


@pytest.mark.django_db
class TestDimensionStats:
    def test_na_is_in_neither_numerator_nor_denominator(self, staff):
        """Two sessions where the student never erred, one where the tutor got
        it right. Mistake identification should read 100%, not 33%."""
        for i in range(2):
            annotate(make_item(f'SESS_NA{i}'), staff,
                     mistake_identification=P.NOT_APPLICABLE)
        annotate(make_item('SESS_OK'), staff)

        stats = SS.dimension_stats(list(SessionEvalAnnotation.objects.all()))
        row = stats['mistake_identification']

        assert row['not_applicable'] == 2
        assert row['scored'] == 1
        assert row['pass_rate'] == 1.0
        assert row['pass_pct'] == 100

    def test_unanswered_is_excluded_too_but_counted_separately(self, staff):
        """Excluded from the rate, but visible — collapsing it into N/A would
        hide an annotator skipping questions."""
        annotate(make_item('SESS_BLANK'), staff, coherence='')
        annotate(make_item('SESS_FULL'), staff)

        row = SS.dimension_stats(
            list(SessionEvalAnnotation.objects.all()))['coherence']

        assert row['unanswered'] == 1
        assert row['not_applicable'] == 0
        assert row['scored'] == 1
        assert row['pass_rate'] == 1.0

    def test_it_distinguishes_to_some_extent_from_no(self, staff):
        """Both fail, but they are very different failures and a bare pass rate
        flattens them together."""
        annotate(make_item('SESS_SOME'), staff, coherence=P.TO_SOME_EXTENT)
        annotate(make_item('SESS_NO'), staff, coherence=P.NO)

        row = SS.dimension_stats(
            list(SessionEvalAnnotation.objects.all()))['coherence']

        assert row['fail'] == 2
        assert row['distribution'] == {P.TO_SOME_EXTENT: 1, P.NO: 1}

    def test_a_dimension_with_nothing_scorable_reports_none_not_zero(self, staff):
        """0% would say the tutor failed. None says we did not measure it."""
        annotate(make_item('SESS_ALLNA'), staff,
                 mistake_identification=P.NOT_APPLICABLE)

        row = SS.dimension_stats(
            list(SessionEvalAnnotation.objects.all()))['mistake_identification']

        assert row['scored'] == 0
        assert row['pass_rate'] is None
        assert row['pass_pct'] is None

    def test_tone_at_neutral_counts_as_a_failure(self, staff):
        annotate(make_item('SESS_TONE'), staff, tutor_tone=P.NEUTRAL)

        row = SS.dimension_stats(
            list(SessionEvalAnnotation.objects.all()))['tutor_tone']

        assert row['fail'] == 1 and row['pass'] == 0


@pytest.mark.django_db
class TestSessionStats:
    def test_incomplete_annotations_are_excluded_from_the_denominator(self, staff):
        """An unfinished annotation has not judged the session. Counting it as
        a failure reports annotator throughput as tutor quality."""
        annotate(make_item('SESS_DONE'), staff)
        annotate(make_item('SESS_PART'), staff, human_likeness='')

        stats = SS.session_stats(list(SessionEvalAnnotation.objects.all()))

        assert stats['total'] == 2
        assert stats['complete'] == 1
        assert stats['incomplete'] == 1
        assert stats['pass_rate'] == 1.0

    def test_pass_rate_is_none_when_nothing_is_complete(self, staff):
        annotate(make_item('SESS_X'), staff, coherence='')

        stats = SS.session_stats(list(SessionEvalAnnotation.objects.all()))

        assert stats['pass_rate'] is None
        assert stats['pass_pct'] is None

    def test_one_dimension_below_desideratum_fails_the_session(self, staff):
        annotate(make_item('SESS_A'), staff)
        annotate(make_item('SESS_B'), staff, actionability=P.TO_SOME_EXTENT)

        stats = SS.session_stats(list(SessionEvalAnnotation.objects.all()))

        assert stats['passed'] == 1
        assert stats['pass_pct'] == 50

    def test_slices_split_by_the_items_attribute(self, staff):
        annotate(make_item('SESS_G', subject='geography'), staff)
        annotate(make_item('SESS_M', subject='math'), staff,
                 coherence=P.NO)

        slices = SS.slice_stats(list(SessionEvalAnnotation.objects.all()),
                                lambda i: i.subject)

        assert slices['geography']['pass_pct'] == 100
        assert slices['math']['pass_pct'] == 0


class TestCohensKappa:
    """No DB needed — this is arithmetic, and the arithmetic is the risk."""

    def test_perfect_agreement_with_variance(self):
        pairs = [('yes', 'yes')] * 5 + [('no', 'no')] * 5
        result = SS.cohens_kappa(pairs)
        assert result['kappa'] == pytest.approx(1.0)
        assert result['observed'] == 1.0

    def test_total_disagreement_is_negative(self):
        pairs = [('yes', 'no')] * 5 + [('no', 'yes')] * 5
        assert SS.cohens_kappa(pairs)['kappa'] == pytest.approx(-1.0)

    def test_kappa_is_undefined_not_zero_when_one_category_is_used(self):
        """THE case. Both raters said 'no' to everything: po = pe = 1, so κ is
        0/0. Returning 0.0 would report perfect agreement as chance-level —
        precisely backwards — and 'revealing_answer' is 'no' for most sessions,
        so this arises in normal use rather than as a pathology."""
        result = SS.cohens_kappa([('no', 'no')] * 10)

        assert result['kappa'] is None
        assert result['kappa'] != 0
        assert result['observed'] == 1.0
        assert result['undefined_reason'] == 'no_category_variance'

    def test_no_overlap_is_reported_as_such(self):
        result = SS.cohens_kappa([])
        assert result['kappa'] is None
        assert result['n'] == 0
        assert result['undefined_reason'] == 'no_overlapping_annotations'

    def test_chance_level_agreement_is_near_zero(self):
        pairs = ([('yes', 'yes')] * 25 + [('yes', 'no')] * 25 +
                 [('no', 'yes')] * 25 + [('no', 'no')] * 25)
        assert SS.cohens_kappa(pairs)['kappa'] == pytest.approx(0.0, abs=0.01)

    def test_a_known_value(self):
        """Worked by hand against the standard formula.

        n = 10; agreements = 5 + 3, so po = 0.80.
        Rater A: yes 6, no 4. Rater B: yes 6, no 4.
        pe = 0.6·0.6 + 0.4·0.4 = 0.52.
        κ = (0.80 − 0.52) / (1 − 0.52) = 0.28 / 0.48 = 0.5833…
        """
        pairs = ([('yes', 'yes')] * 5 + [('no', 'no')] * 3 +
                 [('yes', 'no')] * 1 + [('no', 'yes')] * 1)
        result = SS.cohens_kappa(pairs)
        assert result['observed'] == pytest.approx(0.80)
        assert result['expected'] == pytest.approx(0.52)
        assert result['kappa'] == pytest.approx(0.28 / 0.48)


@pytest.mark.django_db
class TestAgreementStats:
    def test_only_sessions_annotated_by_both_are_compared(self, staff):
        shared = make_item('SESS_SHARED')
        annotate(shared, staff)
        annotate(shared, role=SessionEvalAnnotation.Annotator.LLM_JUDGE,
                 model='gemini')
        annotate(make_item('SESS_HUMAN_ONLY'), staff)

        result = SS.agreement_stats(list(SessionEvalAnnotation.objects.all()))

        assert result['items_compared'] == 1

    def test_a_disagreement_shows_up_on_the_right_dimension(self, staff):
        for i in range(4):
            item = make_item(f'SESS_D{i}')
            annotate(item, staff, coherence=P.YES if i < 2 else P.NO)
            annotate(item, role=SessionEvalAnnotation.Annotator.LLM_JUDGE,
                     model='g', coherence=P.NO if i < 2 else P.YES)

        result = SS.agreement_stats(list(SessionEvalAnnotation.objects.all()))

        assert result['dimensions']['coherence']['observed'] == 0.0
        assert result['dimensions']['coherence']['kappa'] < 0

    def test_the_session_verdict_is_compared_too(self, staff):
        item = make_item('SESS_V')
        annotate(item, staff)
        annotate(item, role=SessionEvalAnnotation.Annotator.LLM_JUDGE,
                 model='g', tutor_tone=P.NEUTRAL)

        result = SS.agreement_stats(list(SessionEvalAnnotation.objects.all()))

        assert result['session_verdict']['n'] == 1
        assert result['session_verdict']['observed'] == 0.0

    def test_llm_annotations_do_not_pollute_the_human_headline(self, staff):
        """The paper found LLM judges unreliable on this taxonomy. Mixing them
        into the top-line pass rate would launder that uncertainty."""
        item = make_item('SESS_MIX')
        annotate(item, staff, coherence=P.NO)             # human: fail
        annotate(item, role=SessionEvalAnnotation.Annotator.LLM_JUDGE,
                 model='g')                               # llm: pass

        metrics = SS.compute_metrics()

        assert metrics['human_only']['passed'] == 0
        assert metrics['overall']['passed'] == 1


@pytest.mark.django_db
class TestExport:
    def _rows(self, client, staff, **params):
        client.force_login(staff)
        response = client.get(
            reverse('dashboard:benchmark:session_export_jsonl'), params)
        # StreamingHttpResponse has no .content — it must be drained.
        body = b''.join(response.streaming_content).decode()
        return [json.loads(line) for line in body.splitlines() if line.strip()]

    def test_the_redaction_report_is_never_exported(self, client, staff):
        """advisory_names holds capitalised tokens lifted from the transcript
        for the REVIEWER. Reviewer-facing is not release-facing."""
        item = make_item('SESS_R', redaction_report={
            'advisory_names': ['Praslin', 'Kofi'],
            'residual': [], 'replacements': ['name×2'],
        })
        annotate(item, staff)

        rows = self._rows(client, staff)

        assert 'redaction_report' not in rows[0]
        assert 'Kofi' not in json.dumps(rows[0])
        assert 'advisory_names' not in json.dumps(rows[0])

    def test_no_raw_session_id_student_or_username(self, client, staff):
        from apps.accounts.models import Institution, Membership
        from apps.curriculum.models import Course, Lesson, Unit
        from apps.tutoring.models import TutorSession

        school = Institution.objects.create(name='S', slug='s-exp')
        course = Course.objects.create(title='C', institution=school)
        unit = Unit.objects.create(course=course, title='U', order_index=1)
        lesson = Lesson.objects.create(unit=unit, title='L', objective='o',
                                       order_index=1)
        pupil = User.objects.create_user(username='DistinctivePupil',
                                         first_name='Distinctive',
                                         email='pupil@school.test')
        Membership.objects.create(user=pupil, institution=school,
                                  role='student', is_active=True)
        session = TutorSession.objects.create(student=pupil, lesson=lesson,
                                              institution=school)
        annotate(make_item('SESS_ID', source_session=session), staff)

        blob = json.dumps(self._rows(client, staff))

        assert 'DistinctivePupil' not in blob
        assert 'Distinctive' not in blob
        assert 'pupil@school.test' not in blob
        assert 'source_session' not in blob
        assert '"scorer"' not in blob          # the annotator's username

    def test_the_annotator_is_an_opaque_index(self, client, staff):
        other = User.objects.create_user(username='SecondAnnotator',
                                         is_staff=True)
        item = make_item('SESS_TWO')
        annotate(item, staff)
        annotate(item, other)

        rows = self._rows(client, staff)
        annotators = {r['annotator'] for r in rows}

        assert annotators == {'human_1', 'human_2'}
        assert 'SecondAnnotator' not in json.dumps(rows)

    def test_the_transcript_and_verdicts_are_present(self, client, staff):
        """Anonymisation is only half the job — the file has to be usable."""
        annotate(make_item('SESS_USE'), staff, coherence=P.TO_SOME_EXTENT)

        row = self._rows(client, staff)[0]

        assert row['transcript']
        assert row['session_passes'] is False
        assert row['annotations']['coherence'] == P.TO_SOME_EXTENT
        assert row['per_dimension_pass']['coherence'] is False
        assert row['per_dimension_pass']['tutor_tone'] is True
        assert row['taxonomy'] == 'maurya_et_al_naacl_2025'

    def test_na_exports_as_none_not_false(self, client, staff):
        """A recipient recomputing pass rates must be able to exclude N/A. If
        it exported as False they would recompute a lower rate than we report."""
        annotate(make_item('SESS_NAX'), staff,
                 mistake_identification=P.NOT_APPLICABLE)

        row = self._rows(client, staff)[0]

        assert row['per_dimension_pass']['mistake_identification'] is None
        assert row['annotations']['mistake_identification'] == P.NOT_APPLICABLE

    def test_an_incomplete_annotation_exports_passes_as_none(self, client, staff):
        annotate(make_item('SESS_INC'), staff, human_likeness='')

        row = self._rows(client, staff)[0]

        assert row['complete'] is False
        assert row['session_passes'] is None

    def test_the_complete_filter_works(self, client, staff):
        annotate(make_item('SESS_C1'), staff)
        annotate(make_item('SESS_C2'), staff, coherence='')

        assert len(self._rows(client, staff)) == 2
        assert len(self._rows(client, staff, complete='yes')) == 1

    def test_the_role_filter_works(self, client, staff):
        item = make_item('SESS_ROLE')
        annotate(item, staff)
        annotate(item, role=SessionEvalAnnotation.Annotator.LLM_JUDGE, model='g')

        rows = self._rows(client, staff, annotator_role='human')

        assert len(rows) == 1
        assert rows[0]['annotator_role'] == 'human'

    def test_it_requires_staff(self, client):
        response = client.get(reverse('dashboard:benchmark:session_export_jsonl'))
        assert response.status_code in (302, 403)

    def test_it_downloads_as_ndjson(self, client, staff):
        annotate(make_item('SESS_DL'), staff)
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_export_jsonl'))

        assert response['Content-Type'] == 'application/x-ndjson'
        assert 'attachment; filename=' in response['Content-Disposition']


@pytest.mark.django_db
class TestScoresPage:
    def test_it_renders_with_no_data(self, client, staff):
        """The empty state is the state it will be in first."""
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_scores'))

        assert response.status_code == 200
        assert response.context['metrics']['human_only']['pass_rate'] is None

    def test_it_reports_the_dimensions_in_the_papers_order(self, client, staff):
        annotate(make_item('SESS_ORDER'), staff)
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_scores'))

        keys = [r['key'] for r in response.context['dimension_rows']]
        assert keys == list(P.DIMENSION_KEYS)

    def test_it_requires_staff(self, client):
        response = client.get(reverse('dashboard:benchmark:session_scores'))
        assert response.status_code in (302, 403)

    def test_it_shows_the_human_pass_rate(self, client, staff):
        annotate(make_item('SESS_P1'), staff)
        annotate(make_item('SESS_P2'), staff, coherence=P.NO)
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_scores'))

        assert response.context['metrics']['human_only']['pass_pct'] == 50


@pytest.mark.django_db
class TestSessionKeyClaims:
    """What the export copy promises about session_key must be true.

    Overstating a privacy property in user-facing text is worse than not
    claiming it — someone downstream will rely on it.
    """

    def test_the_key_is_stable_across_exports_within_a_release(
            self, client, staff):
        """It HAS to be: annotations join to sessions by this key. An earlier
        draft of the export copy claimed the opposite."""
        annotate(make_item('SESS_STABLE'), staff)
        client.force_login(staff)
        url = reverse('dashboard:benchmark:session_export_jsonl')

        def keys():
            body = b''.join(client.get(url).streaming_content).decode()
            return [json.loads(l)['session_key'] for l in body.splitlines() if l]

        assert keys() == keys()

    def test_two_annotations_of_one_session_share_its_key(self, client, staff):
        item = make_item('SESS_JOIN')
        annotate(item, staff)
        annotate(item, role=SessionEvalAnnotation.Annotator.LLM_JUDGE, model='g')
        client.force_login(staff)

        body = b''.join(client.get(
            reverse('dashboard:benchmark:session_export_jsonl')
        ).streaming_content).decode()
        keys = {json.loads(l)['session_key'] for l in body.splitlines() if l}

        assert len(keys) == 1

    def test_a_later_sampling_run_would_give_a_different_key(self):
        """This is the property that stops two releases being linked."""
        from apps.benchmark import session_sampling as S

        original = S._RUN_SALT
        first = S.session_key(4321)
        try:
            S._RUN_SALT = 'a-later-run'
            assert S.session_key(4321) != first
        finally:
            S._RUN_SALT = original


@pytest.mark.django_db
class TestAnnotatorNumbering:
    def test_humans_and_llms_are_numbered_within_their_own_prefix(self, staff):
        other = User.objects.create_user(username='second', is_staff=True)
        item = make_item('SESS_NUM')
        annotate(item, staff)
        annotate(item, other)
        annotate(item, role=SessionEvalAnnotation.Annotator.LLM_JUDGE, model='g')

        rows = SS.export_rows(list(
            SessionEvalAnnotation.objects.select_related('item').order_by('id')))

        assert {r['annotator'] for r in rows} == {'human_1', 'human_2', 'llm_1'}
