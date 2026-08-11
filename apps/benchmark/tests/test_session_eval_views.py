"""The review gate and the annotation form.

The load-bearing test in this file is
``test_an_unapproved_session_cannot_be_annotated``. Everything upstream —
safety screening, redaction, the residual scan — exists to get a session as far
as a human. This gate is what makes the human's decision binding: without it,
sampling alone would put children's transcripts in front of an annotator.

It is asserted at the VIEW, not in the template, so that a later markup change
cannot quietly remove it.

Plan: memory/session_eval_framework_plan.md
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from apps.benchmark import pedagogy as P
from apps.benchmark.models import SessionEvalAnnotation, SessionEvalItem


@pytest.fixture
def staff(db):
    return User.objects.create_user(username='staffer', password='x',
                                    is_staff=True, is_superuser=True)


def make_item(item_id='SESS_GEO_1', status=SessionEvalItem.Status.PENDING_REVIEW,
              **kwargs):
    return SessionEvalItem.objects.create(
        item_id=item_id,
        session_key=f's_{item_id.lower()}',
        subject='geography',
        engine='simple',
        outcome='passed_exit_ticket',
        turn_count=4,
        transcript=[
            {'turn': 1, 'role': 'tutor', 'content': 'What does the scale bar show?'},
            {'turn': 2, 'role': 'student', 'content': 'distance'},
            {'turn': 3, 'role': 'tutor', 'content': 'Right. Now read the number.'},
            {'turn': 4, 'role': 'student', 'content': '1:50000'},
        ],
        status=status,
        **kwargs,
    )


def all_good_post() -> dict:
    return {d.key: d.desideratum for d in P.DIMENSIONS}


@pytest.mark.django_db
class TestTheReviewGate:
    def test_an_unapproved_session_cannot_be_annotated(self, client, staff):
        """The gate. Sampling produces pending_review; only a person moves it
        to approved. Reaching the annotate URL directly must not bypass that."""
        item = make_item(status=SessionEvalItem.Status.PENDING_REVIEW)
        client.force_login(staff)

        response = client.get(
            reverse('dashboard:benchmark:session_annotate', args=[item.item_id]))

        assert response.status_code == 302
        assert 'review' in response['Location']

    def test_a_rejected_session_cannot_be_annotated(self, client, staff):
        item = make_item(status=SessionEvalItem.Status.REJECTED,
                         reject_reason='classmate named in turn 7')
        client.force_login(staff)

        response = client.get(
            reverse('dashboard:benchmark:session_annotate', args=[item.item_id]))

        assert response.status_code == 302

    def test_posting_an_annotation_to_an_unapproved_session_writes_nothing(
            self, client, staff):
        """A redirect on GET is not enough — the POST handler must refuse too,
        or a crafted form submission walks straight past the gate."""
        item = make_item(status=SessionEvalItem.Status.PENDING_REVIEW)
        client.force_login(staff)

        client.post(
            reverse('dashboard:benchmark:session_annotate', args=[item.item_id]),
            all_good_post())

        assert SessionEvalAnnotation.objects.count() == 0

    def test_approving_makes_it_annotatable(self, client, staff):
        item = make_item()
        client.force_login(staff)

        client.post(
            reverse('dashboard:benchmark:session_review', args=[item.item_id]),
            {'decision': 'approve'})
        item.refresh_from_db()

        assert item.status == SessionEvalItem.Status.APPROVED
        assert item.reviewed_by == staff
        assert item.reviewed_at is not None

        response = client.get(
            reverse('dashboard:benchmark:session_annotate', args=[item.item_id]))
        assert response.status_code == 200

    def test_rejecting_requires_a_reason(self, client, staff):
        """A rejection with no reason is unauditable — we could not later tell
        whether the redactor was at fault or the session was simply unsuitable."""
        item = make_item()
        client.force_login(staff)

        client.post(
            reverse('dashboard:benchmark:session_review', args=[item.item_id]),
            {'decision': 'reject', 'reject_reason': ''})
        item.refresh_from_db()

        assert item.status == SessionEvalItem.Status.PENDING_REVIEW

    def test_rejecting_with_a_reason_records_it(self, client, staff):
        item = make_item()
        client.force_login(staff)

        client.post(
            reverse('dashboard:benchmark:session_review', args=[item.item_id]),
            {'decision': 'reject', 'reject_reason': 'sibling named in turn 3'})
        item.refresh_from_db()

        assert item.status == SessionEvalItem.Status.REJECTED
        assert item.reject_reason == 'sibling named in turn 3'

    def test_the_review_page_surfaces_a_residual_finding(self, client, staff):
        """A reviewer who cannot see what the automated gates found is guessing."""
        item = make_item(redaction_report={
            'residual': ['student_name_survived:Kofi'],
            'replacements': ['[EMAIL]×1'],
            'advisory_names': ['Praslin'],
        })
        client.force_login(staff)

        body = client.get(
            reverse('dashboard:benchmark:session_review',
                    args=[item.item_id])).content.decode()

        assert 'student_name_survived:Kofi' in body
        assert 'Praslin' in body
        assert '[EMAIL]×1' in body


@pytest.mark.django_db
class TestAnnotationForm:
    @pytest.fixture
    def approved(self):
        return make_item(status=SessionEvalItem.Status.APPROVED)

    def test_all_eight_dimensions_are_rendered_with_the_papers_definitions(
            self, client, staff, approved):
        client.force_login(staff)

        body = client.get(reverse('dashboard:benchmark:session_annotate',
                                  args=[approved.item_id])).content.decode()

        for dim in P.DIMENSIONS:
            assert dim.label in body
            assert f'name="{dim.key}"' in body

    def test_saving_a_complete_passing_annotation(self, client, staff, approved):
        client.force_login(staff)

        client.post(reverse('dashboard:benchmark:session_annotate',
                            args=[approved.item_id]), all_good_post())

        annotation = SessionEvalAnnotation.objects.get(item=approved)
        assert annotation.complete is True
        assert annotation.passes is True
        assert annotation.annotator_user == staff
        assert annotation.annotator_role == SessionEvalAnnotation.Annotator.HUMAN

    def test_one_dimension_below_desideratum_fails_the_session(
            self, client, staff, approved):
        client.force_login(staff)
        payload = all_good_post()
        payload['coherence'] = P.TO_SOME_EXTENT

        client.post(reverse('dashboard:benchmark:session_annotate',
                            args=[approved.item_id]), payload)

        assert SessionEvalAnnotation.objects.get(item=approved).passes is False

    def test_neutral_tone_fails_even_though_our_older_judge_accepts_it(
            self, client, staff, approved):
        client.force_login(staff)
        payload = all_good_post()
        payload['tutor_tone'] = P.NEUTRAL

        client.post(reverse('dashboard:benchmark:session_annotate',
                            args=[approved.item_id]), payload)

        assert SessionEvalAnnotation.objects.get(item=approved).passes is False

    def test_values_outside_the_taxonomy_are_dropped_not_stored(
            self, client, staff, approved):
        client.force_login(staff)
        payload = all_good_post()
        payload['coherence'] = 'sort_of'          # not in the taxonomy

        client.post(reverse('dashboard:benchmark:session_annotate',
                            args=[approved.item_id]), payload)

        annotation = SessionEvalAnnotation.objects.get(item=approved)
        assert annotation.coherence == ''
        assert annotation.complete is False

    def test_na_is_rejected_where_the_dimension_does_not_allow_it(
            self, client, staff, approved):
        """Only the two mistake dimensions may be N/A. Coherence always
        applies — accepting N/A there would let an annotator opt out of the
        dimension the whole session-level design exists to measure."""
        client.force_login(staff)
        payload = all_good_post()
        payload['coherence'] = P.NOT_APPLICABLE

        client.post(reverse('dashboard:benchmark:session_annotate',
                            args=[approved.item_id]), payload)

        assert SessionEvalAnnotation.objects.get(item=approved).coherence == ''

    def test_na_is_accepted_on_the_mistake_dimensions(
            self, client, staff, approved):
        client.force_login(staff)
        payload = all_good_post()
        payload['mistake_identification'] = P.NOT_APPLICABLE
        payload['mistake_location'] = P.NOT_APPLICABLE

        client.post(reverse('dashboard:benchmark:session_annotate',
                            args=[approved.item_id]), payload)

        annotation = SessionEvalAnnotation.objects.get(item=approved)
        assert annotation.mistake_identification == P.NOT_APPLICABLE
        assert annotation.passes is True     # excluded, not counted as failure

    def test_an_incomplete_annotation_saves_but_does_not_read_as_passing(
            self, client, staff, approved):
        client.force_login(staff)
        payload = all_good_post()
        del payload['human_likeness']

        client.post(reverse('dashboard:benchmark:session_annotate',
                            args=[approved.item_id]), payload)

        annotation = SessionEvalAnnotation.objects.get(item=approved)
        assert annotation.complete is False

    def test_resaving_updates_rather_than_duplicating(
            self, client, staff, approved):
        client.force_login(staff)
        url = reverse('dashboard:benchmark:session_annotate',
                      args=[approved.item_id])

        client.post(url, all_good_post())
        payload = all_good_post()
        payload['tutor_tone'] = P.NEUTRAL
        client.post(url, payload)

        assert SessionEvalAnnotation.objects.filter(item=approved).count() == 1
        assert SessionEvalAnnotation.objects.get(item=approved).passes is False

    def test_a_scripted_annotator_is_tagged_as_llm_not_human(
            self, client, staff, approved):
        """Same override pattern as the turn-level annotator. Without it an
        agent's rows would contaminate the human gold set that Phase 4's
        agreement statistic is measured against."""
        client.force_login(staff)

        client.post(
            reverse('dashboard:benchmark:session_annotate',
                    args=[approved.item_id])
            + '?annotator_role=llm_judge&annotator_model=gemini-2.5-flash',
            all_good_post())

        annotation = SessionEvalAnnotation.objects.get(item=approved)
        assert annotation.annotator_role == SessionEvalAnnotation.Annotator.LLM_JUDGE
        assert annotation.annotator_model == 'gemini-2.5-flash'

    def test_human_and_llm_annotations_coexist_on_one_session(
            self, client, staff, approved):
        """Cohen's kappa in Phase 4 needs both, side by side."""
        client.force_login(staff)
        url = reverse('dashboard:benchmark:session_annotate',
                      args=[approved.item_id])

        client.post(url, all_good_post())
        client.post(url + '?annotator_role=llm_judge&annotator_model=g', all_good_post())

        assert SessionEvalAnnotation.objects.filter(item=approved).count() == 2


@pytest.mark.django_db
class TestListAndAccess:
    def test_the_list_requires_staff(self, client):
        response = client.get(reverse('dashboard:benchmark:session_list'))
        assert response.status_code in (302, 403)

    def test_the_review_page_requires_staff(self, client):
        item = make_item()
        response = client.get(
            reverse('dashboard:benchmark:session_review', args=[item.item_id]))
        assert response.status_code in (302, 403)

    def test_the_annotate_page_requires_staff(self, client):
        item = make_item(status=SessionEvalItem.Status.APPROVED)
        response = client.get(
            reverse('dashboard:benchmark:session_annotate', args=[item.item_id]))
        assert response.status_code in (302, 403)

    def test_the_list_renders_and_counts_by_status(self, client, staff):
        make_item('SESS_A', status=SessionEvalItem.Status.PENDING_REVIEW)
        make_item('SESS_B', status=SessionEvalItem.Status.APPROVED)
        make_item('SESS_C', status=SessionEvalItem.Status.REJECTED)
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_list'))

        assert response.status_code == 200
        assert response.context['counts']['pending_review'] == 1
        assert response.context['counts']['approved'] == 1
        assert response.context['counts']['rejected'] == 1

    def test_the_list_filters_by_status(self, client, staff):
        make_item('SESS_A', status=SessionEvalItem.Status.PENDING_REVIEW)
        make_item('SESS_B', status=SessionEvalItem.Status.APPROVED)
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_list'),
                              {'status': 'approved'})

        ids = [r['item'].item_id for r in response.context['rows']]
        assert ids == ['SESS_B']

    def test_the_list_never_exposes_a_raw_session_id(self, client, staff):
        """The page an annotator works from must not carry the join key back
        to a student."""
        from apps.accounts.models import Institution, Membership
        from apps.curriculum.models import Course, Lesson, Unit
        from apps.tutoring.models import TutorSession

        school = Institution.objects.create(name='S', slug='s-list')
        course = Course.objects.create(title='C', institution=school)
        unit = Unit.objects.create(course=course, title='U', order_index=1)
        lesson = Lesson.objects.create(unit=unit, title='L', objective='o',
                                       order_index=1)
        user = User.objects.create_user(username='pupilx')
        Membership.objects.create(user=user, institution=school, role='student',
                                  is_active=True)
        session = TutorSession.objects.create(student=user, lesson=lesson,
                                              institution=school)
        make_item('SESS_KEYED', source_session=session)
        client.force_login(staff)

        body = client.get(
            reverse('dashboard:benchmark:session_list')).content.decode()

        assert 'pupilx' not in body

    def test_the_list_paginates(self, client, staff):
        """Session transcripts are 20-40 turns; the turn-level list has no
        pagination and does not need any."""
        for i in range(30):
            make_item(f'SESS_{i}')
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_list'))

        assert response.context['page'].paginator.num_pages == 2
        assert len(response.context['rows']) == 25
