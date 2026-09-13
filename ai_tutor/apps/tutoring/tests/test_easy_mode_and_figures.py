"""Easy mode, and the figures that stopped appearing.

Two student-facing things that were broken in the same quiet way — the server
produced something the page could not use, and nothing errored.

**Easy mode** is the A-D buttons on a cloud tutor. The machinery all existed;
it was reachable only by running a local model, so no hosted student could get
it. The switch writes a profile row rather than a browser flag, because the
tutor's system prompt reads the same row: buttons the tutor does not know
about is device session 30, where it hinted at one question while the options
on screen belonged to another.

**Figures** have a whole pipeline — catalog, tool, dispatch — and the last hop
emitted ``{'url': ...}`` while addMessage() in chat_tutor.html loops the media
list and draws nothing for an entry whose ``type`` is not image / diagram /
chart / illustration. So every figure the tutor asked for produced an empty
``<div class="message-media">``: no picture, no panel, no console error. The
existing tests asserted the tool dispatched and that a ``media`` key existed —
both true the whole time.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership, StudentProfile
from ai_tutor.apps.curriculum.models import Course, Lesson, LessonStep, Unit
from ai_tutor.apps.tutoring.models import InFlightQuestion, TutorSession


@pytest.fixture
def school(db):
    return Institution.objects.create(name='Belonie', slug='belonie-em')


@pytest.fixture
def lesson(db, school):
    course = Course.objects.create(title='Geography S3', institution=school,
                                   grade_level='S3', is_published=True)
    unit = Unit.objects.create(course=course, title='Maps', order_index=0)
    lesson = Lesson.objects.create(unit=unit, title='Grid references',
                                   objective='o', order_index=0,
                                   is_published=True)
    LessonStep.objects.create(lesson=lesson, order_index=0, phase='explain',
                              teacher_script='s')
    return lesson


@pytest.fixture
def student(db, school):
    user = User.objects.create_user('amara', 'a@example.com', 'pw')
    Membership.objects.create(user=user, institution=school,
                              role=Membership.Role.STUDENT)
    StudentProfile.objects.create(user=user, grade_level='S3')
    return user


@pytest.fixture
def session(db, school, student, lesson):
    return TutorSession.objects.create(institution=school, student=student,
                                       lesson=lesson, engine='simple')


def _url(session):
    return reverse('tutoring:set_answer_surface', args=[session.id])


@pytest.mark.django_db
class TestTheEasyModeSwitch:

    def test_it_turns_easy_mode_on_and_back_off(self, client, student, session):
        client.force_login(student)

        r = client.post(_url(session), json.dumps({'mode': 'easy'}),
                        content_type='application/json')
        assert r.status_code == 200 and r.json()['mode'] == 'easy'
        student.student_profile.refresh_from_db()
        assert student.student_profile.prefers_answer_picker is True

        r = client.post(_url(session), json.dumps({'mode': 'normal'}),
                        content_type='application/json')
        assert r.status_code == 200 and r.json()['mode'] == 'normal'
        student.student_profile.refresh_from_db()
        assert student.student_profile.prefers_answer_picker is False

    def test_it_answers_with_the_question_already_on_screen(
            self, client, student, session):
        """Otherwise the switch takes effect at the start of the next turn:
        the student taps it, nothing changes, and they tap it again."""
        InFlightQuestion.objects.create(
            session=session, question_text='Which axis is the northing?',
            question_type='mcq', reference_answer='B',
            options=['easting', 'northing', 'scale', 'longitude'])

        client.force_login(student)
        r = client.post(_url(session), json.dumps({'mode': 'easy'}),
                        content_type='application/json')

        letters = r.json()['answer_choices']['letters']
        assert [l['letter'] for l in letters] == ['A', 'B', 'C', 'D']
        assert letters[1]['text'] == 'northing'

    def test_no_live_question_means_no_buttons_to_send_back(
            self, client, student, session):
        client.force_login(student)
        r = client.post(_url(session), json.dumps({'mode': 'easy'}),
                        content_type='application/json')
        assert r.json()['answer_choices'] is None

    def test_a_bad_mode_is_refused(self, client, student, session):
        client.force_login(student)
        r = client.post(_url(session), json.dumps({'mode': 'hard'}),
                        content_type='application/json')
        assert r.status_code == 400
        student.student_profile.refresh_from_db()
        assert student.student_profile.prefers_answer_picker is False

    def test_it_is_not_another_student_s_switch(self, client, school, session):
        """The session is looked up by owner. Without that, session_id is a
        number anyone can guess and the preference is not theirs to set."""
        other = User.objects.create_user('jeanluc', 'j@example.com', 'pw')
        Membership.objects.create(user=other, institution=school,
                                  role=Membership.Role.STUDENT)
        client.force_login(other)
        r = client.post(_url(session), json.dumps({'mode': 'easy'}),
                        content_type='application/json')
        assert r.status_code == 404

    def test_the_chat_page_offers_the_switch(self, client, student, lesson):
        client.force_login(student)
        r = client.get(reverse('tutoring:tutor_interface', args=[lesson.id]))
        body = r.content.decode()
        assert r.context['easy_mode_locked'] is False
        assert r.context['easy_mode_on'] is False
        assert 'id="answer-mode-btn"' in body
        assert 'id="answer-mode-menu-item"' in body

    def test_the_page_renders_the_state_the_student_left_it_in(
            self, client, student, lesson):
        student.student_profile.prefers_answer_picker = True
        student.student_profile.save(update_fields=['prefers_answer_picker'])

        client.force_login(student)
        r = client.get(reverse('tutoring:tutor_interface', args=[lesson.id]))
        assert r.context['easy_mode_on'] is True
        assert 'aria-pressed="true"' in r.content.decode()


@pytest.mark.django_db
class TestAFigureReachesTheStudent:

    STEP_MEDIA = {'images': [{
        'type': 'map',
        'url': '/media/media/global/generated_de1f97ac.png',
        'alt_text': 'Map of Mahe showing natural and human features',
        'caption': 'Geography studies both the natural and the human.',
        'description': 'Schematic map of Mahe island',
    }]}

    def _tool_result(self, lesson):
        from ai_tutor.apps.tutoring.simple_tutor.engine import (
            _build_figure_catalog,
        )
        step = lesson.steps.first()
        step.media = self.STEP_MEDIA
        step.save(update_fields=['media'])
        return _build_figure_catalog(step)

    def test_the_catalog_reads_the_key_the_data_actually_uses(self, lesson):
        """The documented shape says 'alt'; every generated row says
        'alt_text'. Reading only 'alt' made the description the caption on
        every real lesson, which is what the tutor picks a figure by."""
        entry = self._tool_result(lesson)[0]
        assert entry['id'] == 1
        assert entry['alt_text'] == 'Map of Mahe showing natural and human features'
        assert entry['description'] == 'Schematic map of Mahe island'

    def test_an_image_without_a_url_is_not_in_the_catalog(self, lesson):
        """~27% of stored entries never got a URL — generation failed or never
        ran. Offering the tutor a figure it cannot display wastes a turn."""
        step = lesson.steps.first()
        step.media = {'images': [{'type': 'map', 'alt_text': 'no url here'}]}
        step.save(update_fields=['media'])
        from ai_tutor.apps.tutoring.simple_tutor.engine import (
            _build_figure_catalog,
        )
        assert _build_figure_catalog(step) == []

    def test_the_payload_carries_the_type_the_page_filters_on(self, lesson):
        """addMessage() draws nothing for an entry whose type is not in
        {image, diagram, chart, illustration}. The payload used to be
        {'url': ...} alone, so every figure rendered as an empty div."""
        from ai_tutor.apps.tutoring.simple_tutor.engine import _figure_media

        media = _figure_media([{'tool': 'request_figure', 'result': {
            'displayed': True,
            'url': '/media/media/global/generated_de1f97ac.png',
            'alt_text': 'Map of Mahe',
            'caption': 'Natural and human features.',
        }}])

        assert media == [{
            'type': 'image',
            'url': '/media/media/global/generated_de1f97ac.png',
            'alt': 'Map of Mahe',
            'caption': 'Natural and human features.',
        }]

    def test_a_refused_or_empty_figure_attaches_nothing(self):
        from ai_tutor.apps.tutoring.simple_tutor.engine import _figure_media
        assert _figure_media([{'tool': 'request_figure', 'result': {
            'displayed': False, 'error': 'figures are disabled'}}]) == []
        assert _figure_media([{'tool': 'request_figure', 'result': {
            'displayed': True, 'url': ''}}]) == []
        assert _figure_media([{'tool': 'record_answer', 'result': {}}]) == []
        assert _figure_media(None) == []

    def test_the_figure_survives_a_reload(self, session, lesson):
        """views._build_history redraws a turn from
        metadata['attached_media']. Nothing wrote it, so a figure appeared
        once and vanished the moment the student came back to the lesson —
        which is exactly when they would want another look at it."""
        from ai_tutor.apps.tutoring.simple_tutor.engine import (
            _persist_tutor_turn,
        )
        from ai_tutor.apps.tutoring.views import _build_session_history

        _persist_tutor_turn(
            session, 'Look at this map.', lesson.steps.first(),
            [{'tool': 'request_figure', 'result': {
                'displayed': True,
                'url': '/media/media/global/generated_de1f97ac.png',
                'alt_text': 'Map of Mahe',
                'caption': 'Natural and human features.',
            }}],
        )

        entry = _build_session_history(session)[-1]
        assert entry['media'] == [{
            'type': 'image',
            'url': '/media/media/global/generated_de1f97ac.png',
            'alt': 'Map of Mahe',
            'caption': 'Natural and human features.',
        }]

    def test_a_text_turn_keeps_the_metadata_shape_it_had(self, session, lesson):
        from ai_tutor.apps.tutoring.models import SessionTurn
        from ai_tutor.apps.tutoring.simple_tutor.engine import (
            _persist_tutor_turn,
        )
        _persist_tutor_turn(session, 'No figure here.',
                            lesson.steps.first(), [])
        turn = SessionTurn.objects.filter(session=session).last()
        assert 'attached_media' not in (turn.metadata or {})
