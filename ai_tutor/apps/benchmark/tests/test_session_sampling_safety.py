"""Child-protection gates on session sampling.

These transcripts belong to secondary-school children. The requirement is
absolute: no session reaches an annotator carrying a name, a personal
identifier, or anything the safety system objected to.

This file exists to prove that, not to describe it. Every test seeds the exact
thing that must be excluded and asserts it was — including the reason, so a
gate that fires for the wrong cause does not read as a pass.

Nothing here makes a network call. The gate tests run with use_llm=False; the
LLM name pass is covered at the bottom of this file with a stub standing in for
the model, pinning behaviour verified against real Gemini on 2026-08-11.

Plan: memory/session_eval_framework_plan.md
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from ai_tutor.apps.accounts.models import Institution, Membership, StudentProfile
from ai_tutor.apps.benchmark import session_sampling as S
from ai_tutor.apps.benchmark.models import SessionEvalItem
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.safety.models import SafetyAuditLog
from ai_tutor.apps.tutoring.models import SessionTurn, TutorSession


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Pilot School', slug='pilot-school')


@pytest.fixture
def lesson(school):
    course = Course.objects.create(title='Geography', institution=school,
                                   subject_type='geography')
    unit = Unit.objects.create(course=course, title='Maps', order_index=1)
    return Lesson.objects.create(unit=unit, title='Reading Maps',
                                 objective='Read a map', order_index=1,
                                 is_published=True)


def make_student(school, username='pupil', first_name='', last_name=''):
    user = User.objects.create_user(username=username, first_name=first_name,
                                    last_name=last_name)
    Membership.objects.create(user=user, institution=school, role='student',
                              is_active=True)
    return user


def make_session(school, lesson, student, turns=None, **kwargs):
    """A clean six-turn session unless told otherwise."""
    session = TutorSession.objects.create(
        student=student, lesson=lesson, institution=school, **kwargs)
    turns = turns or [
        ('tutor', 'Look at the map. What does the scale bar tell you?'),
        ('student', 'how far things are?'),
        ('tutor', 'Right — it converts map distance to real distance.'),
        ('student', 'so 1cm is 1km'),
        ('tutor', 'That depends on the scale shown. Check the bar again.'),
        ('student', 'ok it says 1:50000'),
    ]
    for role, content in turns:
        SessionTurn.objects.create(session=session, role=role, content=content)
    return session


@pytest.mark.django_db
class TestSafetyExclusion:
    """The three cases the plan names. All three must be rejected."""

    def test_a_flagged_session_is_rejected(self, school, lesson):
        student = make_student(school, 'flagged_sess')
        session = make_session(school, lesson, student)
        session.is_flagged = True
        session.flag_reason = 'self_harm'
        session.save()

        record = S.screen_and_prepare(session, use_llm=False)

        assert record['status'] == SessionEvalItem.Status.REJECTED
        assert record['reject_reason'] == 'safety:session_flagged'
        assert record['transcript'] == []      # nothing retained

    def test_a_session_with_a_flagged_turn_is_rejected(self, school, lesson):
        student = make_student(school, 'flagged_turn')
        session = make_session(school, lesson, student)
        turn = session.turns.filter(role='student').first()
        turn.is_flagged = True
        turn.flag_type = 'harmful'
        turn.save()

        record = S.screen_and_prepare(session, use_llm=False)

        assert record['reject_reason'] == 'safety:turn_flagged'

    def test_a_suspended_student_is_rejected(self, school, lesson):
        student = make_student(school, 'suspended')
        StudentProfile.objects.update_or_create(
            user=student, defaults={'is_tutor_suspended': True})
        session = make_session(school, lesson, student)

        record = S.screen_and_prepare(session, use_llm=False)

        assert record['reject_reason'] == 'safety:student_suspended'

    def test_an_audit_log_entry_is_rejected(self, school, lesson):
        student = make_student(school, 'audited')
        session = make_session(school, lesson, student)
        SafetyAuditLog.objects.create(event_type='content_flagged',
                                      session_id=session.id)

        record = S.screen_and_prepare(session, use_llm=False)

        assert record['reject_reason'] == 'safety:safety_audit_log'

    def test_an_unrecognised_flag_type_still_rejects(self, school, lesson):
        """The dashboard counts only ('harmful', 'inappropriate',
        'manipulation'). The safety judge writes categories[0], which is not
        constrained to that tuple — so copying the dashboard's narrow filter
        here would let an unlisted category through to an annotator."""
        student = make_student(school, 'novel_flag')
        session = make_session(school, lesson, student)
        turn = session.turns.filter(role='student').first()
        turn.is_flagged = True
        turn.flag_type = 'grooming_risk'          # not in the dashboard tuple
        turn.save()

        record = S.screen_and_prepare(session, use_llm=False)

        assert record['reject_reason'] == 'safety:turn_flagged'

    def test_a_clean_session_survives_but_is_not_approved(self, school, lesson):
        """The gates must not reject everything — otherwise the tests above
        pass for the wrong reason. And a survivor still needs a human."""
        student = make_student(school, 'clean')
        session = make_session(school, lesson, student)

        record = S.screen_and_prepare(session, use_llm=False)

        assert record['reject_reason'] == ''
        assert record['status'] == SessionEvalItem.Status.PENDING_REVIEW
        assert record['status'] != SessionEvalItem.Status.APPROVED
        assert len(record['transcript']) == 6


@pytest.mark.django_db
class TestNameRedaction:
    def test_the_students_own_name_is_removed(self, school, lesson):
        student = make_student(school, 'jbaptiste', 'Jeanne', 'Baptiste')
        session = make_session(school, lesson, student, turns=[
            ('tutor', 'Welcome back Jeanne! Ready to look at the map?'),
            ('student', 'yes'),
            ('tutor', 'Good work Jeanne. What does the scale bar show?'),
            ('student', 'distance'),
        ])

        record = S.screen_and_prepare(session, use_llm=False)
        blob = ' '.join(t['content'] for t in record['transcript'])

        assert record['reject_reason'] == ''
        assert 'Jeanne' not in blob
        assert '[STUDENT]' in blob

    def test_it_is_case_insensitive(self, school, lesson):
        student = make_student(school, 'mpierre', 'Marie', 'Pierre')
        session = make_session(school, lesson, student, turns=[
            ('tutor', 'Hello Marie, shall we start?'),
            ('student', 'its marie again'),
            ('tutor', 'Good to see you. Look at the scale bar.'),
            ('student', 'MARIE here, ok'),
        ])

        record = S.screen_and_prepare(session, use_llm=False)
        blob = ' '.join(t['content'] for t in record['transcript'])

        assert record['reject_reason'] == ''
        for spelling in ('Marie', 'marie', 'MARIE'):
            assert spelling not in blob

    def test_a_surname_and_a_username_are_both_removed(self, school, lesson):
        student = make_student(school, 'thomas.rene', 'Thomas', 'Rene')
        session = make_session(school, lesson, student, turns=[
            ('tutor', 'Thomas Rene, welcome.'),
            ('student', 'my login is thomas.rene'),
            ('tutor', 'Look at the scale bar please.'),
            ('student', 'ok'),
        ])

        record = S.screen_and_prepare(session, use_llm=False)
        blob = ' '.join(t['content'] for t in record['transcript'])

        assert record['reject_reason'] == ''
        assert 'Thomas' not in blob and 'Rene' not in blob
        assert 'thomas' not in blob.lower()

    def test_contact_details_are_removed(self, school, lesson):
        student = make_student(school, 'contact')
        session = make_session(school, lesson, student, turns=[
            ('tutor', 'What does the scale bar show?'),
            ('student', 'email me at kid@school.sc or call 248 251 4433'),
            ('tutor', 'Let us keep to the lesson. Look at the bar.'),
            ('student', 'ok'),
        ])

        record = S.screen_and_prepare(session, use_llm=False)
        blob = ' '.join(t['content'] for t in record['transcript'])

        assert record['reject_reason'] == ''
        assert 'kid@school.sc' not in blob
        assert '251 4433' not in blob
        assert '[EMAIL]' in blob and '[PHONE]' in blob

    def test_place_names_are_kept(self, school, lesson):
        """Over-redaction has a cost too: strip the geography out of a
        geography session and there is no pedagogy left to judge."""
        student = make_student(school, 'geo', 'Ana', 'Silva')
        session = make_session(school, lesson, student, turns=[
            ('tutor', 'Find Mahe on the map. What ocean surrounds Seychelles?'),
            ('student', 'the Indian Ocean'),
            ('tutor', 'Correct. Now locate Praslin to the north east.'),
            ('student', 'found it'),
        ])

        record = S.screen_and_prepare(session, use_llm=False)
        blob = ' '.join(t['content'] for t in record['transcript'])

        assert record['reject_reason'] == ''
        for place in ('Mahe', 'Seychelles', 'Indian Ocean', 'Praslin'):
            assert place in blob


@pytest.mark.django_db
class TestResidualScanIsLoadBearing:
    """If redaction ever regresses, the residual scan must catch it. These
    tests break the redactor on purpose and assert the scan notices — without
    them, the scan could be a no-op and every test above would still pass."""

    def test_it_catches_a_name_the_redactor_missed(self, school, lesson,
                                                   monkeypatch):
        student = make_student(school, 'regress', 'Kofi', 'Mensah')
        session = make_session(school, lesson, student, turns=[
            ('tutor', 'Hello Kofi, look at the map.'),
            ('student', 'ok'),
            ('tutor', 'What does the scale bar show?'),
            ('student', 'distance'),
        ])

        # Simulate the redactor silently failing.
        monkeypatch.setattr(S, 'redact_text', lambda text, variants: (text, []))

        record = S.screen_and_prepare(session, use_llm=False)

        assert record['reject_reason'] == 'residual_identifier'
        assert any('Kofi' in f for f in record['redaction_report']['residual'])
        assert record['transcript'] == []

    def test_the_scan_reports_nothing_on_a_clean_session(self, school, lesson):
        student = make_student(school, 'clean2', 'Ama', 'Owusu')
        session = make_session(school, lesson, student)

        findings = S.residual_scan(S.build_transcript(session), student)

        assert findings == []


@pytest.mark.django_db
class TestTheLLMGateFailsClosed:
    def test_an_unavailable_model_rejects_rather_than_degrades(
            self, school, lesson, monkeypatch):
        """The regex pass cannot catch a classmate's name. If the LLM pass is
        unavailable, the session must be rejected — not quietly passed through
        on the weaker check."""
        student = make_student(school, 'llmdown')
        session = make_session(school, lesson, student)

        monkeypatch.setattr(S, 'llm_name_candidates',
                            lambda transcript, llm_client=None: ([], 'api_down'))

        record = S.screen_and_prepare(session, use_llm=True)

        assert record['reject_reason'] == 'redaction_unavailable'
        assert record['transcript'] == []

    def test_a_name_only_the_model_finds_is_redacted(self, school, lesson,
                                                     monkeypatch):
        """A classmate's name: not in the database, so no lookup can find it."""
        student = make_student(school, 'peer', 'Zoe', 'Adams')
        session = make_session(school, lesson, student, turns=[
            ('tutor', 'What does the scale bar show?'),
            ('student', 'Rushad told me it means distance'),
            ('tutor', 'That is right. Now check the number on the bar.'),
            ('student', 'ok'),
        ])

        monkeypatch.setattr(
            S, 'llm_name_candidates',
            lambda transcript, llm_client=None: (['Rushad'], ''))

        record = S.screen_and_prepare(session, use_llm=True)
        blob = ' '.join(t['content'] for t in record['transcript'])

        assert record['reject_reason'] == ''
        assert 'Rushad' not in blob
        assert '[STUDENT]' in blob

    def test_the_model_never_rewrites_the_transcript(self, school, lesson,
                                                     monkeypatch):
        """We judge this text for pedagogical quality. If the redactor
        paraphrased it we would be measuring the redactor."""
        student = make_student(school, 'verbatim')
        session = make_session(school, lesson, student)
        original = [t['content'] for t in S.build_transcript(session)]

        monkeypatch.setattr(S, 'llm_name_candidates',
                            lambda transcript, llm_client=None: ([], ''))

        record = S.screen_and_prepare(session, use_llm=True)

        assert [t['content'] for t in record['transcript']] == original


@pytest.mark.django_db
class TestSessionKeys:
    def test_the_key_is_not_derivable_from_the_session_id(self, school, lesson):
        """A substring check is worthless here — a single-digit id turns up in
        a hex digest by chance. The property that matters is that the mapping
        depends on a secret, so a recipient holding the key cannot compute
        which session it was."""
        student = make_student(school, 'keyed')
        session = make_session(school, lesson, student)

        key = S.session_key(session.id)

        assert key.startswith('s_')
        assert key != f's_{session.id}'

        # Same id, different salt → different key. That is what makes the key
        # non-invertible without the salt, and what stops two exports being
        # joined into a longitudinal record of one child.
        original_salt = S._RUN_SALT
        try:
            S._RUN_SALT = 'a-different-salt'
            assert S.session_key(session.id) != key
        finally:
            S._RUN_SALT = original_salt

    def test_different_sessions_get_different_keys(self, school, lesson):
        a = make_session(school, lesson, make_student(school, 'k1'))
        b = make_session(school, lesson, make_student(school, 'k2'))

        assert S.session_key(a.id) != S.session_key(b.id)

    def test_the_same_session_keys_stably_within_a_run(self, school, lesson):
        """Needed so two annotations of one session can be joined."""
        student = make_student(school, 'stable')
        session = make_session(school, lesson, student)

        assert S.session_key(session.id) == S.session_key(session.id)


@pytest.mark.django_db
class TestCandidateSelection:
    def test_synthetic_sessions_are_excluded(self, school, lesson):
        """Simulator sessions would tell us about the persona generator, not
        about what the tutor does with real students."""
        real = make_session(school, lesson, make_student(school, 'real'))
        make_session(school, lesson, make_student(school, 'sim'),
                     is_synthetic=True)

        ids = set(S.candidate_sessions().values_list('id', flat=True))

        assert real.id in ids
        assert len(ids) == 1

    def test_very_short_sessions_are_excluded(self, school, lesson):
        student = make_student(school, 'short')
        make_session(school, lesson, student, turns=[
            ('tutor', 'Look at the map.'),
            ('student', 'ok'),
        ])

        assert S.candidate_sessions().count() == 0


@pytest.mark.django_db
class TestLLMNameExpansion:
    """Behaviours verified against real Gemini on 2026-08-11, pinned here with
    a stub so they run offline and so a regression is caught rather than
    rediscovered."""

    def _with_model_returning(self, monkeypatch, names):
        class _Result:
            def __init__(self, names):
                self.names = names

        def fake_structured_completion(client, model, **kw):
            return _Result(names)

        monkeypatch.setattr(
            'ai_tutor.apps.tutoring.judges._instructor_helper.get_instructor_from_client',
            lambda c: object())
        monkeypatch.setattr(
            'ai_tutor.apps.tutoring.judges._instructor_helper.structured_completion',
            fake_structured_completion)

    def test_a_full_name_is_expanded_into_its_parts(self, monkeypatch):
        """The model reports 'Fatima Kabir' where she introduces herself, but
        the session refers to her as 'Fatima' later. Replacing only the full
        string would leave the bare first name in the transcript."""
        self._with_model_returning(monkeypatch, ['Fatima Kabir'])

        names, err = S.llm_name_candidates(
            [{'role': 'student', 'content': 'x'}], llm_client=object())

        assert err == ''
        assert set(names) == {'Fatima Kabir', 'Fatima', 'Kabir'}

    def test_expanded_parts_are_actually_redacted(self, school, lesson,
                                                  monkeypatch):
        student = make_student(school, 'expand')
        session = make_session(school, lesson, student, turns=[
            ('tutor', 'What does the scale bar show?'),
            ('student', 'My name is Fatima Kabir'),
            ('tutor', 'Welcome. Look at the bar please.'),
            ('student', 'Fatima here, it says 1:50000'),
        ])
        monkeypatch.setattr(
            S, 'llm_name_candidates',
            lambda transcript, llm_client=None: (
                ['Fatima Kabir', 'Fatima', 'Kabir'], ''))

        record = S.screen_and_prepare(session, use_llm=True)
        blob = ' '.join(t['content'] for t in record['transcript'])

        assert record['reject_reason'] == ''
        assert 'Fatima' not in blob and 'Kabir' not in blob

    def test_short_tokens_are_not_turned_into_redaction_targets(self, monkeypatch):
        """A 2-character target would match inside ordinary words and gut the
        transcript."""
        self._with_model_returning(monkeypatch, ['Al', 'Jo Ng'])

        names, _ = S.llm_name_candidates(
            [{'role': 'student', 'content': 'x'}], llm_client=object())

        assert all(len(n) >= 3 for n in names)
        assert 'Al' not in names

    def test_the_stored_report_records_a_count_not_the_names(
            self, school, lesson, monkeypatch):
        """Writing the discovered names into redaction_report would put the
        identifiers straight back into the row an annotator reads."""
        student = make_student(school, 'nocount')
        session = make_session(school, lesson, student, turns=[
            ('tutor', 'What does the scale bar show?'),
            ('student', 'Rushad said distance'),
            ('tutor', 'Correct. Check the number.'),
            ('student', 'ok'),
        ])
        monkeypatch.setattr(S, 'llm_name_candidates',
                            lambda transcript, llm_client=None: (['Rushad'], ''))

        record = S.screen_and_prepare(session, use_llm=True)
        report = repr(record['redaction_report'])

        assert record['redaction_report']['llm_names_found'] == 1
        assert 'Rushad' not in report
