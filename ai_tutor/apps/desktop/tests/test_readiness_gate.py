"""A lesson must not start on a device that is still being set up.

Without the retrieval encoder the tutor still answers — ``_retrieve_kb``
catches its own failure and returns nothing — so an ungrounded session is
indistinguishable from a healthy one. These tests pin the two properties that
make the gate worth having: it is closed on a half-installed desktop, and it
does not exist on the hosted app.
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership
from ai_tutor.apps.curriculum.models import Course, Lesson, Unit
from ai_tutor.apps.desktop import readiness


class ReadinessGateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(name='Gate School', slug='gate-school')
        cls.student = User.objects.create_user('gate-student', password='pw')
        Membership.objects.create(
            user=cls.student, institution=cls.inst, role=Membership.Role.STUDENT,
        )
        course = Course.objects.create(institution=cls.inst, title='Geo')
        unit = Unit.objects.create(course=course, title='U1', order_index=0)
        cls.lesson = Lesson.objects.create(
            unit=unit, title='L1', order_index=0, is_published=True,
        )

    def setUp(self):
        readiness.invalidate()
        self.client = Client()
        self.client.force_login(self.student)

    def tearDown(self):
        readiness.invalidate()

    # ── hosted app ────────────────────────────────────────────────────

    def test_gate_is_absent_on_the_hosted_app(self):
        """Seychelles and Mozambique have their assets by construction."""
        self.assertFalse(readiness.gate_enabled())
        self.assertEqual(readiness.lesson_prerequisites(), (True, []))

    @override_settings(DESKTOP_BUILD=False)
    def test_hosted_start_is_not_gated(self):
        with patch('ai_tutor.apps.desktop.provisioning.model_installed',
                   return_value=False):
            response = self.client.post(
                reverse('tutoring:chat_start_session', args=[self.lesson.id]),
                data='{}', content_type='application/json')
        self.assertNotEqual(response.status_code, 409)

    # ── desktop ───────────────────────────────────────────────────────

    @override_settings(DESKTOP_BUILD=True)
    def test_missing_tutor_model_closes_the_gate(self):
        with patch('ai_tutor.apps.desktop.provisioning.model_installed',
                   return_value=False), \
             patch('ai_tutor.apps.desktop.assets.missing_required',
                   return_value=[]):
            ready, missing = readiness.lesson_prerequisites()
        self.assertFalse(ready)
        self.assertIn('Tutor model', missing)

    @override_settings(DESKTOP_BUILD=True)
    def test_missing_encoder_closes_the_gate(self):
        encoder = type('A', (), {'label': 'Content search encoder'})()
        with patch('ai_tutor.apps.desktop.provisioning.model_installed',
                   return_value=True), \
             patch('ai_tutor.apps.desktop.assets.missing_required',
                   return_value=[encoder]):
            ready, missing = readiness.lesson_prerequisites()
        self.assertFalse(ready)
        self.assertEqual(missing, ['Content search encoder'])

    @override_settings(DESKTOP_BUILD=True)
    def test_a_missing_voice_does_not_close_the_gate(self):
        """Speech is optional; blocking lessons on it would cost more than it
        protects. Exercises the real registry, not a mock."""
        from ai_tutor.apps.desktop import assets
        piper = assets.by_key('piper')
        self.assertFalse(piper.required_for_lessons)
        self.assertNotIn(piper, assets.missing_required())

    @override_settings(DESKTOP_BUILD=True)
    def test_start_endpoint_refuses_with_the_missing_list(self):
        with patch('ai_tutor.apps.desktop.provisioning.model_installed',
                   return_value=False), \
             patch('ai_tutor.apps.desktop.assets.missing_required',
                   return_value=[]):
            response = self.client.post(
                reverse('tutoring:chat_start_session', args=[self.lesson.id]),
                data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertTrue(body['setup_required'])
        self.assertIn('Tutor model', body['missing'])

    @override_settings(DESKTOP_BUILD=True)
    def test_lesson_page_redirects_to_setup(self):
        """A bookmark or a stale tab must not walk past the gate either."""
        with patch('ai_tutor.apps.desktop.provisioning.model_installed',
                   return_value=False), \
             patch('ai_tutor.apps.desktop.assets.missing_required',
                   return_value=[]):
            response = self.client.get(
                reverse('tutoring:tutor_interface', args=[self.lesson.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('desktop:setup'))

    @override_settings(DESKTOP_BUILD=True)
    def test_it_fails_closed_when_the_check_itself_breaks(self):
        """The conditions that break the check are the ones that break a
        lesson, so an error must not read as ready."""
        with patch('ai_tutor.apps.desktop.provisioning.model_installed',
                   side_effect=RuntimeError('ollama gone')):
            ready, missing = readiness.lesson_prerequisites()
        self.assertFalse(ready)
        self.assertTrue(missing)

    @override_settings(DESKTOP_BUILD=True)
    def test_installing_an_asset_is_visible_without_waiting_for_the_cache(self):
        with patch('ai_tutor.apps.desktop.provisioning.model_installed',
                   return_value=False), \
             patch('ai_tutor.apps.desktop.assets.missing_required',
                   return_value=[]):
            self.assertFalse(readiness.lesson_prerequisites()[0])

        readiness.invalidate()
        with patch('ai_tutor.apps.desktop.provisioning.model_installed',
                   return_value=True), \
             patch('ai_tutor.apps.desktop.assets.missing_required',
                   return_value=[]):
            self.assertTrue(readiness.lesson_prerequisites()[0])
