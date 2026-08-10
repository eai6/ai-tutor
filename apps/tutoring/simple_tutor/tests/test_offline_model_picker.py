"""Student-selectable offline model.

`tutor_mode` chooses online-vs-offline. This is the separate question of WHICH
local model the offline path uses, once a device has more than one installed
(4B / 8B / 14B on the desktop build).

The test that matters most is `test_adding_a_model_does_not_move_existing_students`:
installing a second local model must not silently re-point every student who
never expressed a preference. It did — `_first_active` had no ORDER BY, so
adding rows moved the default from the 4B to a 14B that was still downloading.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase

from apps.accounts.models import Institution, StudentProfile
from apps.curriculum.models import Course, Lesson, Unit
from apps.llm.models import ModelConfig
from apps.tutoring.models import TutorSession
from apps.tutoring.simple_tutor.model_choice import (
    describe_for_student, local_options, resolve_for_session,
)

User = get_user_model()
_n = {'i': 0}


# ModelConfig.institution is NOT NULL, so every row needs one.
_INST = {'obj': None}


def _inst():
    if _INST['obj'] is None:
        _INST['obj'] = Institution.objects.create(name='MP', slug='mp-models')
    return _INST['obj']


def _local(name, *, active=True):
    return ModelConfig.objects.create(
        provider='local_ollama', purpose='tutoring', model_name=name,
        is_active=active, institution=_inst(),
    )


def _cloud(name='claude-opus-4-7'):
    return ModelConfig.objects.create(
        provider='anthropic', purpose='tutoring', model_name=name,
        is_active=True, institution=_inst(),
    )


def _session():
    _n['i'] += 1
    i = _n['i']
    _INST['obj'] = None
    inst = Institution.objects.create(name=f'S{i}', slug=f's{i}')
    _INST['obj'] = inst
    user = User.objects.create_user(username=f'stu-mp-{i}', password='x')
    profile = StudentProfile.objects.create(user=user)
    course = Course.objects.create(title=f'C{i}', institution=inst,
                                   grade_level='S3', is_published=True)
    unit = Unit.objects.create(course=course, title='U', order_index=0)
    lesson = Lesson.objects.create(unit=unit, title='L', objective='o',
                                   order_index=0, is_published=True)
    session = TutorSession.objects.create(
        institution=inst, student=user, lesson=lesson, engine='simple')
    return session, profile


class OfflineModelPickerTest(DjangoTestCase):

    def test_student_choice_is_honoured(self):
        session, profile = _session()
        small = _local('qwen3-4b-jetson')
        big = _local('qwen3:14b')
        _cloud()
        profile.tutor_mode = 'offline'
        profile.offline_model = big
        profile.save()
        self.assertEqual(resolve_for_session(session).model_name, 'qwen3:14b')

        profile.offline_model = small
        profile.save()
        self.assertEqual(resolve_for_session(session).model_name, 'qwen3-4b-jetson')

    def test_adding_a_model_does_not_move_existing_students(self):
        """A student who never picked must keep the tutor they had.

        _first_active had no ORDER BY, so the default was whatever the DB
        happened to return first — installing a 14B silently moved every
        unpicked student onto it, while it was still downloading.
        """
        session, profile = _session()
        _local('qwen3-4b-jetson')          # installed first
        _cloud()
        profile.tutor_mode = 'offline'
        profile.save()
        before = resolve_for_session(session).model_name

        _local('qwen3:14b')                # installed later
        _local('qwen3:8b')
        self.assertEqual(resolve_for_session(session).model_name, before)

    def test_retired_model_falls_back_instead_of_breaking(self):
        """A model can be removed after a student selected it. Handing the
        engine a config that no longer runs is worse than falling back."""
        session, profile = _session()
        _local('qwen3-4b-jetson')
        gone = _local('qwen3:14b')
        _cloud()
        profile.tutor_mode = 'offline'
        profile.offline_model = gone
        profile.save()

        gone.is_active = False
        gone.save(update_fields=['is_active'])
        self.assertEqual(resolve_for_session(session).model_name, 'qwen3-4b-jetson')

    def test_picker_hidden_with_only_one_local_model(self):
        session, profile = _session()
        _local('qwen3-4b-jetson')
        _cloud()
        self.assertFalse(describe_for_student(profile)['offline_options_available'])

    def test_picker_shown_with_two(self):
        session, profile = _session()
        _local('qwen3-4b-jetson')
        _local('qwen3:8b')
        _cloud()
        d = describe_for_student(profile)
        self.assertTrue(d['offline_options_available'])
        self.assertEqual(len(d['offline_options']), 2)

    def test_choice_applies_on_an_offline_only_device(self):
        """The desktop build has no cloud tutor. resolve_for_session used to
        defer whenever either side was missing, which would have ignored the
        student's pick on exactly the device the picker exists for."""
        session, profile = _session()
        _local('qwen3-4b-jetson')
        big = _local('qwen3:14b')
        profile.tutor_mode = 'offline'
        profile.offline_model = big
        profile.save()
        self.assertEqual(resolve_for_session(session).model_name, 'qwen3:14b')

    def test_inactive_models_are_not_offered(self):
        session, profile = _session()
        _local('qwen3-4b-jetson')
        _local('qwen3:14b', active=False)
        self.assertEqual([o.model_name for o in local_options()],
                         ['qwen3-4b-jetson'])
