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

    def test_na_is_accepted_on_every_dimension(self, client, staff, approved):
        """N/A was refused outside the two mistake dimensions, to stop an
        annotator opting out of coherence. Wrong trade: withholding it does not
        make someone judge a dimension that never arose, it makes them record
        something false — and a false "Yes" inflates the pass rate."""
        client.force_login(staff)
        payload = {k: P.NOT_APPLICABLE for k in P.DIMENSION_KEYS}

        client.post(reverse('dashboard:benchmark:session_annotate',
                            args=[approved.item_id]), payload)

        annotation = SessionEvalAnnotation.objects.get(item=approved)
        for key in P.DIMENSION_KEYS:
            assert getattr(annotation, key) == P.NOT_APPLICABLE, key
        # Answered everywhere, but nothing scorable — so it does not pass.
        assert annotation.complete is True
        assert annotation.passes is False

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


@pytest.mark.django_db
class TestApproveAndAnnotateInOneGo:
    """Review and annotation merged onto one screen.

    Two pages is the right shape when a safety reviewer and a subject annotator
    are different people. With one person doing both it is navigation cost, so
    a single submit approves and judges.

    The risk of merging is that the safety decision degrades into a side effect
    of the annotation. These tests pin the three rules that stop it.
    """

    def test_approving_with_dimensions_saves_both(self, client, staff):
        item = make_item()
        client.force_login(staff)

        client.post(
            reverse('dashboard:benchmark:session_review', args=[item.item_id]),
            {'decision': 'approve', **all_good_post()})

        item.refresh_from_db()
        assert item.status == SessionEvalItem.Status.APPROVED
        annotation = SessionEvalAnnotation.objects.get(item=item)
        assert annotation.complete is True
        assert annotation.passes is True
        assert annotation.annotator_user == staff

    def test_rejecting_never_saves_an_annotation(self, client, staff):
        """RULE 1. A session judged unsafe must leave no judgement behind, even
        if the dimensions were filled in before the reviewer noticed the
        problem."""
        item = make_item()
        client.force_login(staff)

        client.post(
            reverse('dashboard:benchmark:session_review', args=[item.item_id]),
            {'decision': 'reject', 'reject_reason': 'classmate named in turn 7',
             **all_good_post()})

        item.refresh_from_db()
        assert item.status == SessionEvalItem.Status.REJECTED
        assert SessionEvalAnnotation.objects.count() == 0

    def test_rejecting_later_retracts_an_existing_annotation(self, client, staff):
        """A session can be approved and judged, then found unsafe on a second
        look. The annotation must not survive the retraction."""
        item = make_item()
        client.force_login(staff)
        url = reverse('dashboard:benchmark:session_review', args=[item.item_id])

        client.post(url, {'decision': 'approve', **all_good_post()})
        assert SessionEvalAnnotation.objects.count() == 1

        client.post(url, {'decision': 'reject',
                          'reject_reason': 'sibling named in turn 3'})

        assert SessionEvalAnnotation.objects.count() == 0
        item.refresh_from_db()
        assert item.status == SessionEvalItem.Status.REJECTED

    def test_approving_with_no_dimensions_creates_no_annotation(
            self, client, staff):
        """RULE 2. A fast safety pass over many sessions must not litter the
        table with empty rows, which the scorer would then count as
        incomplete."""
        item = make_item()
        client.force_login(staff)

        client.post(
            reverse('dashboard:benchmark:session_review', args=[item.item_id]),
            {'decision': 'approve'})

        item.refresh_from_db()
        assert item.status == SessionEvalItem.Status.APPROVED
        assert SessionEvalAnnotation.objects.count() == 0

    def test_a_partial_annotation_saves_and_is_flagged_incomplete(
            self, client, staff):
        item = make_item()
        client.force_login(staff)
        payload = all_good_post()
        del payload['human_likeness']

        client.post(
            reverse('dashboard:benchmark:session_review', args=[item.item_id]),
            {'decision': 'approve', **payload})

        annotation = SessionEvalAnnotation.objects.get(item=item)
        assert annotation.complete is False

    def test_values_outside_the_taxonomy_are_still_dropped(self, client, staff):
        item = make_item()
        client.force_login(staff)
        payload = all_good_post()
        payload['coherence'] = 'sort_of'

        client.post(
            reverse('dashboard:benchmark:session_review', args=[item.item_id]),
            {'decision': 'approve', **payload})

        assert SessionEvalAnnotation.objects.get(item=item).coherence == ''

    def test_na_is_accepted_on_any_dimension(self, client, staff):
        item = make_item()
        client.force_login(staff)
        payload = all_good_post()
        payload['coherence'] = P.NOT_APPLICABLE

        client.post(
            reverse('dashboard:benchmark:session_review', args=[item.item_id]),
            {'decision': 'approve', **payload})

        annotation = SessionEvalAnnotation.objects.get(item=item)
        assert annotation.coherence == P.NOT_APPLICABLE
        assert annotation.passes is True     # excluded, not counted as failure

    def test_a_missing_rejection_reason_changes_nothing(self, client, staff):
        """RULE 3. The reason requirement survives the merge — and a failed
        rejection must not approve-by-accident or save an annotation."""
        item = make_item()
        client.force_login(staff)

        client.post(
            reverse('dashboard:benchmark:session_review', args=[item.item_id]),
            {'decision': 'reject', 'reject_reason': '', **all_good_post()})

        item.refresh_from_db()
        assert item.status == SessionEvalItem.Status.PENDING_REVIEW
        assert SessionEvalAnnotation.objects.count() == 0

    def test_it_advances_to_the_next_pending_session(self, client, staff):
        """The point of the merge: judge one, land on the next."""
        first = make_item('SESS_FIRST')
        second = make_item('SESS_SECOND')
        client.force_login(staff)

        response = client.post(
            reverse('dashboard:benchmark:session_review', args=[first.item_id]),
            {'decision': 'approve', **all_good_post()})

        assert second.item_id in response['Location']

    def test_it_does_not_bounce_back_to_the_session_just_decided(
            self, client, staff):
        """The only pending item is the one being decided; it must not be
        offered again."""
        only = make_item('SESS_ONLY')
        client.force_login(staff)

        response = client.post(
            reverse('dashboard:benchmark:session_review', args=[only.item_id]),
            {'decision': 'approve'})

        assert only.item_id not in response['Location']

    def test_the_form_prefills_an_existing_annotation(self, client, staff):
        item = make_item()
        client.force_login(staff)
        url = reverse('dashboard:benchmark:session_review', args=[item.item_id])
        payload = all_good_post()
        payload['tutor_tone'] = P.NEUTRAL
        client.post(url, {'decision': 'approve', **payload})

        response = client.get(url)

        tone = next(d for d in response.context['dimensions']
                    if d['key'] == 'tutor_tone')
        checked = [o['value'] for o in tone['options'] if o['checked']]
        assert checked == [P.NEUTRAL]

    def test_the_page_renders_all_eight_dimensions(self, client, staff):
        item = make_item()
        client.force_login(staff)

        body = client.get(reverse('dashboard:benchmark:session_review',
                                  args=[item.item_id])).content.decode()

        for dim in P.DIMENSIONS:
            assert f'name="{dim.key}"' in body

    def test_the_standalone_annotate_page_still_works(self, client, staff):
        """Kept for re-annotating an already-approved session and for the
        scripted llm_judge role, which never goes through review."""
        item = make_item(status=SessionEvalItem.Status.APPROVED)
        client.force_login(staff)

        client.post(
            reverse('dashboard:benchmark:session_annotate', args=[item.item_id]),
            all_good_post())

        assert SessionEvalAnnotation.objects.get(item=item).passes is True


@pytest.mark.django_db
class TestNavigationRespectsTheFilter:
    """Save & next must stay inside the set you are working through.

    Reported from production: filtering to `simple` and annotating, then
    pressing Save & next, landed on a `v1` session. With 959 pending and 114 on
    another engine, an annotator would silently leave the set they chose and
    only notice after judging several sessions from it.
    """

    def _item(self, item_id, engine='simple',
              status=SessionEvalItem.Status.PENDING_REVIEW, subject='geography'):
        return SessionEvalItem.objects.create(
            item_id=item_id, session_key=f's_{item_id.lower()}',
            engine=engine, subject=subject, turn_count=4, status=status,
            transcript=[{'turn': 1, 'role': 'tutor', 'content': 'x'}])

    def test_review_advances_within_the_engine_filter(self, client, staff):
        first = self._item('SESS_S1', 'simple')
        self._item('SESS_V1', 'v1')                 # older is irrelevant
        second = self._item('SESS_S2', 'simple')
        client.force_login(staff)

        response = client.post(
            reverse('dashboard:benchmark:session_review', args=[first.item_id])
            + '?engine=simple',
            {'decision': 'approve', 'engine': 'simple'})

        assert second.item_id in response['Location']

    def test_review_does_not_escape_into_another_engine(self, client, staff):
        """The v1 session is the only one left; the chain must end rather than
        hand over a session from outside the filter."""
        only = self._item('SESS_ONLY_SIMPLE', 'simple')
        self._item('SESS_STRAY_V1', 'v1')
        client.force_login(staff)

        response = client.post(
            reverse('dashboard:benchmark:session_review', args=[only.item_id]),
            {'decision': 'approve', 'engine': 'simple'})

        assert 'SESS_STRAY_V1' not in response['Location']
        assert response['Location'].endswith('/sessions/?engine=simple')

    def test_the_filter_survives_the_hop(self, client, staff):
        """One hop keeping the filter is not enough — it has to persist, or
        the chain silently widens at the second session."""
        first = self._item('SESS_A', 'simple')
        self._item('SESS_B', 'simple')
        client.force_login(staff)

        response = client.post(
            reverse('dashboard:benchmark:session_review', args=[first.item_id]),
            {'decision': 'approve', 'engine': 'simple'})

        assert 'engine=simple' in response['Location']

    def test_annotate_save_and_next_respects_the_filter(self, client, staff):
        first = self._item('SESS_AS1', 'simple',
                           SessionEvalItem.Status.APPROVED)
        self._item('SESS_AV1', 'v1', SessionEvalItem.Status.APPROVED)
        second = self._item('SESS_AS2', 'simple',
                            SessionEvalItem.Status.APPROVED)
        client.force_login(staff)

        response = client.post(
            reverse('dashboard:benchmark:session_annotate',
                    args=[first.item_id]),
            {'save_and_next': '1', 'engine': 'simple', **all_good_post()})

        assert second.item_id in response['Location']
        assert 'engine=simple' in response['Location']

    def test_a_plain_save_keeps_the_filter_too(self, client, staff):
        """Saving without advancing re-renders the same page; losing the
        filter there would break the NEXT Save & next."""
        item = self._item('SESS_PLAIN', 'simple',
                          SessionEvalItem.Status.APPROVED)
        client.force_login(staff)

        response = client.post(
            reverse('dashboard:benchmark:session_annotate', args=[item.item_id]),
            {'engine': 'simple', **all_good_post()})

        assert 'engine=simple' in response['Location']

    def test_the_subject_filter_is_honoured_as_well(self, client, staff):
        first = self._item('SESS_G1', subject='geography')
        self._item('SESS_M1', subject='math')
        second = self._item('SESS_G2', subject='geography')
        client.force_login(staff)

        response = client.post(
            reverse('dashboard:benchmark:session_review', args=[first.item_id]),
            {'decision': 'approve', 'subject': 'geography'})

        assert second.item_id in response['Location']

    def test_no_filter_still_walks_everything(self, client, staff):
        """Unscoped navigation must not become accidentally scoped."""
        first = self._item('SESS_N1', 'simple')
        second = self._item('SESS_N2', 'v1')
        client.force_login(staff)

        response = client.post(
            reverse('dashboard:benchmark:session_review', args=[first.item_id]),
            {'decision': 'approve'})

        assert second.item_id in response['Location']

    def test_engine_all_means_unscoped(self, client, staff):
        first = self._item('SESS_ALL1', 'simple')
        second = self._item('SESS_ALL2', 'v1')
        client.force_login(staff)

        response = client.post(
            reverse('dashboard:benchmark:session_review', args=[first.item_id]),
            {'decision': 'approve', 'engine': 'all'})

        assert second.item_id in response['Location']

    def test_the_pending_count_is_scoped_to_the_filter(self, client, staff):
        """'959 awaiting review' while working through twelve is a lie about
        how much work is left."""
        item = self._item('SESS_C1', 'simple')
        self._item('SESS_C2', 'simple')
        for i in range(5):
            self._item(f'SESS_CV{i}', 'v1')
        client.force_login(staff)

        response = client.get(
            reverse('dashboard:benchmark:session_review', args=[item.item_id])
            + '?engine=simple')

        assert response.context['pending_count'] == 2

    def test_the_remaining_count_is_scoped_too(self, client, staff):
        item = self._item('SESS_R1', 'simple', SessionEvalItem.Status.APPROVED)
        self._item('SESS_R2', 'simple', SessionEvalItem.Status.APPROVED)
        self._item('SESS_RV', 'v1', SessionEvalItem.Status.APPROVED)
        client.force_login(staff)

        response = client.get(
            reverse('dashboard:benchmark:session_annotate', args=[item.item_id])
            + '?engine=simple')

        assert response.context['remaining'] == 2

    def test_the_list_links_carry_the_filter(self, client, staff):
        self._item('SESS_LINK', 'simple')
        client.force_login(staff)

        body = client.get(reverse('dashboard:benchmark:session_list'),
                          {'engine': 'simple'}).content.decode()

        assert 'SESS_LINK/review/?engine=simple' in body


@pytest.mark.django_db
class TestSkipWithoutDeciding:
    """Previous / Skip on the review page.

    The hazard: the whole page is one form, so a submit button labelled "Next"
    would post the annotation. These are links, and the tests assert that
    skipping records nothing at all.
    """

    def _item(self, item_id, engine='simple',
              status=SessionEvalItem.Status.PENDING_REVIEW):
        return SessionEvalItem.objects.create(
            item_id=item_id, session_key=f's_{item_id.lower()}',
            engine=engine, turn_count=4, status=status,
            transcript=[{'turn': 1, 'role': 'tutor', 'content': 'x'}])

    def test_skipping_changes_nothing(self, client, staff):
        first = self._item('SESS_P1')
        second = self._item('SESS_P2')
        client.force_login(staff)

        client.get(reverse('dashboard:benchmark:session_review',
                           args=[first.item_id]))
        client.get(reverse('dashboard:benchmark:session_review',
                           args=[second.item_id]))

        first.refresh_from_db()
        assert first.status == SessionEvalItem.Status.PENDING_REVIEW
        assert first.reviewed_by is None
        assert SessionEvalAnnotation.objects.count() == 0

    def test_it_offers_the_neighbours_in_queue_order(self, client, staff):
        first = self._item('SESS_Q1')
        middle = self._item('SESS_Q2')
        last = self._item('SESS_Q3')
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_review',
                                      args=[middle.item_id]))

        assert response.context['prev_item'].item_id == first.item_id
        assert response.context['next_item'].item_id == last.item_id
        assert response.context['position'] == 2
        assert response.context['total_in_scope'] == 3

    def test_the_ends_have_no_neighbour(self, client, staff):
        first = self._item('SESS_E1')
        self._item('SESS_E2')
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_review',
                                      args=[first.item_id]))

        assert response.context['prev_item'] is None
        assert response.context['next_item'] is not None

    def test_skipping_stays_inside_the_engine_filter(self, client, staff):
        first = self._item('SESS_SK1', 'simple')
        self._item('SESS_SKV', 'v1')
        third = self._item('SESS_SK2', 'simple')
        client.force_login(staff)

        response = client.get(
            reverse('dashboard:benchmark:session_review', args=[first.item_id])
            + '?engine=simple')

        assert response.context['next_item'].item_id == third.item_id
        assert response.context['total_in_scope'] == 2

    def test_the_skip_link_carries_the_filter_forward(self, client, staff):
        first = self._item('SESS_CARRY1', 'simple')
        self._item('SESS_CARRY2', 'simple')
        client.force_login(staff)

        body = client.get(
            reverse('dashboard:benchmark:session_review', args=[first.item_id])
            + '?engine=simple').content.decode()

        assert 'SESS_CARRY2/review/?engine=simple' in body

    def test_it_walks_the_same_status_not_a_mixed_queue(self, client, staff):
        """Browsing pending sessions should not drop you into an approved
        batch — they are different jobs."""
        pending = self._item('SESS_ST1')
        self._item('SESS_ST_APPROVED', status=SessionEvalItem.Status.APPROVED)
        client.force_login(staff)

        response = client.get(reverse('dashboard:benchmark:session_review',
                                      args=[pending.item_id]))

        assert response.context['next_item'] is None
        assert response.context['total_in_scope'] == 1

    def test_the_pager_is_links_not_submit_buttons(self, client, staff):
        """A submit button inside the page-wide form would post the annotation.
        Pressing Skip must never record a judgement."""
        first = self._item('SESS_LINKS1')
        self._item('SESS_LINKS2')
        client.force_login(staff)

        body = client.get(reverse('dashboard:benchmark:session_review',
                                  args=[first.item_id])).content.decode()

        # Split on the ELEMENT, not the string — the stylesheet mentions
        # 'pager-bar' first.
        pager = body.split('class="pager-bar"')[1].split('</div>')[0]
        assert '<a href=' in pager
        assert 'type="submit"' not in pager
