"""The eight-dimension taxonomy and its scoring rule.

Pins three things that are easy to get quietly wrong:

  - the pass rule is ALL-OR-NOTHING across applicable dimensions;
  - N/A is EXCLUDED from scoring, not counted as a failure;
  - the desideratum for tone is ENCOURAGING, not "not offensive".

That last one is a real divergence from our existing binary judge, which
accepts neutral tone. If someone later "fixes" the taxonomy to match the older
code, these tests are what says no.

Source: Maurya et al., NAACL 2025 (arXiv:2412.09416), Table 2.
Plan: memory/session_eval_framework_plan.md
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.benchmark import pedagogy as P
from apps.benchmark.models import SessionEvalAnnotation, SessionEvalItem


def all_good() -> dict:
    """Every dimension sitting exactly at its desideratum."""
    return {d.key: d.desideratum for d in P.DIMENSIONS}


class TestTheTaxonomyMatchesThePaper:
    def test_there_are_eight_dimensions(self):
        assert len(P.DIMENSIONS) == 8

    def test_revealing_the_answer_is_the_one_dimension_that_wants_no(self):
        assert P.get_dimension('revealing_answer').desideratum == P.NO
        others = [d for d in P.DIMENSIONS if d.key != 'revealing_answer']
        assert all(d.desideratum != P.NO for d in others)

    def test_revealing_splits_yes_by_correctness(self):
        values = dict(P.get_dimension('revealing_answer').values)
        assert P.YES_CORRECT in values and P.YES_INCORRECT in values
        assert P.YES not in values           # not a plain 3-way scale

    def test_tone_requires_encouraging_not_merely_inoffensive(self):
        """Our older binary judge accepts neutral. The paper does not."""
        assert P.get_dimension('tutor_tone').desideratum == P.ENCOURAGING
        assert P.dimension_passes('tutor_tone', P.NEUTRAL) is False
        assert P.dimension_passes('tutor_tone', P.OFFENSIVE) is False
        assert P.dimension_passes('tutor_tone', P.ENCOURAGING) is True

    def test_every_dimension_allows_na(self):
        """Offered on the two mistake dimensions at first, on the reasoning
        that coherence and tone apply to any session. That was the wrong trade:
        withholding N/A does not make an annotator judge a dimension that never
        arose, it makes them record something false — and a false "Yes"
        inflates the pass rate. N/A only costs a smaller denominator."""
        assert all(d.allows_na for d in P.DIMENSIONS)

    def test_na_appears_in_the_choices_for_every_dimension(self):
        for key in P.DIMENSION_KEYS:
            assert P.NOT_APPLICABLE in dict(P.choices_for(key)), key

    def test_na_is_last_so_it_is_not_the_easy_click(self):
        """It should be reachable, not the default landing spot."""
        for key in P.DIMENSION_KEYS:
            assert P.choices_for(key)[-1][0] == P.NOT_APPLICABLE, key

    def test_every_dimension_carries_the_papers_definition(self):
        for d in P.DIMENSIONS:
            assert d.definition.strip()
            assert d.session_guidance.strip()
            assert d.desideratum in dict(d.values)


class TestSessionPassRule:
    def test_all_eight_at_desiderata_passes(self):
        assert P.session_passes(all_good()) is True

    def test_one_dimension_to_some_extent_fails_the_session(self):
        """The plan's verification case, run for every dimension that has the
        3-way scale — not just a convenient one."""
        for d in P.DIMENSIONS:
            if P.TO_SOME_EXTENT not in dict(d.values):
                continue
            values = all_good()
            values[d.key] = P.TO_SOME_EXTENT
            assert P.session_passes(values) is False, f'{d.key} did not fail'

    def test_neutral_tone_alone_fails_the_session(self):
        values = all_good()
        values['tutor_tone'] = P.NEUTRAL
        assert P.session_passes(values) is False

    def test_revealing_the_correct_answer_fails_the_session(self):
        values = all_good()
        values['revealing_answer'] = P.YES_CORRECT
        assert P.session_passes(values) is False

    def test_na_is_excluded_not_counted_as_failure(self):
        """Penalising a tutor because the student made no mistakes would
        measure the student, not the tutor."""
        values = all_good()
        values['mistake_identification'] = P.NOT_APPLICABLE
        values['mistake_location'] = P.NOT_APPLICABLE
        assert P.session_passes(values) is True
        assert P.dimension_passes('mistake_identification',
                                  P.NOT_APPLICABLE) is None

    def test_na_is_excluded_on_every_dimension(self):
        for key in P.DIMENSION_KEYS:
            assert P.dimension_passes(key, P.NOT_APPLICABLE) is None, key
            values = all_good()
            values[key] = P.NOT_APPLICABLE
            assert P.session_passes(values) is True, key

    def test_an_all_na_session_does_not_pass_vacuously(self):
        """Nothing was assessed, so nothing was demonstrated. Now reachable in
        the UI, since every dimension offers N/A."""
        assert P.session_passes(
            {k: P.NOT_APPLICABLE for k in P.DIMENSION_KEYS}) is False

    def test_an_unanswered_dimension_is_not_a_pass(self):
        values = all_good()
        values['coherence'] = ''
        # Excluded from scoring like N/A — but the annotation is incomplete,
        # which is what `complete` is for. The two are checked separately so an
        # unfinished annotation cannot masquerade as a passing one.
        assert P.dimension_passes('coherence', '') is None

    def test_a_wholly_unanswered_annotation_does_not_pass_vacuously(self):
        assert P.session_passes({k: '' for k in P.DIMENSION_KEYS}) is False
        assert P.session_passes({}) is False


@pytest.mark.django_db
class TestAnnotationModel:
    @pytest.fixture
    def item(self):
        return SessionEvalItem.objects.create(
            item_id='SESS_TEST_1', session_key='s_abc123',
            transcript=[{'turn': 1, 'role': 'tutor', 'content': 'hello'}],
            status=SessionEvalItem.Status.APPROVED,
        )

    def test_a_complete_passing_annotation_passes(self, item):
        annotation = SessionEvalAnnotation.objects.create(item=item, **all_good())
        assert annotation.complete is True
        assert annotation.passes is True

    def test_an_incomplete_annotation_is_flagged_incomplete(self, item):
        values = all_good()
        values['human_likeness'] = ''
        annotation = SessionEvalAnnotation.objects.create(item=item, **values)
        assert annotation.complete is False

    def test_model_choices_come_from_the_taxonomy(self, item):
        """One source of truth — the form, the column and the scorer cannot
        drift apart."""
        for key in P.DIMENSION_KEYS:
            field_choices = SessionEvalAnnotation._meta.get_field(key).choices
            assert list(field_choices) == P.choices_for(key)

    def test_only_approved_items_are_annotatable(self):
        pending = SessionEvalItem.objects.create(
            item_id='SESS_TEST_2', session_key='s_def456')
        assert pending.status == SessionEvalItem.Status.PENDING_REVIEW
        assert pending.is_annotatable is False

        pending.status = SessionEvalItem.Status.APPROVED
        assert pending.is_annotatable is True

    def test_an_item_outlives_its_source_session(self, db):
        """The frozen transcript is the evaluation record. Deleting the
        session must not delete the research data — nor resurrect the
        un-redacted text."""
        from apps.accounts.models import Institution, Membership
        from apps.curriculum.models import Course, Lesson, Unit
        from apps.tutoring.models import TutorSession

        school = Institution.objects.create(name='S', slug='s-outlive')
        course = Course.objects.create(title='C', institution=school)
        unit = Unit.objects.create(course=course, title='U', order_index=1)
        lesson = Lesson.objects.create(unit=unit, title='L', objective='o',
                                       order_index=1)
        user = User.objects.create_user(username='gone')
        Membership.objects.create(user=user, institution=school, role='student',
                                  is_active=True)
        session = TutorSession.objects.create(student=user, lesson=lesson,
                                              institution=school)
        item = SessionEvalItem.objects.create(
            item_id='SESS_TEST_3', session_key='s_ghi789',
            source_session=session,
            transcript=[{'turn': 1, 'role': 'tutor', 'content': 'kept'}])

        session.delete()
        item.refresh_from_db()

        assert item.source_session is None
        assert item.transcript == [{'turn': 1, 'role': 'tutor',
                                    'content': 'kept'}]
