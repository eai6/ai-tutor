"""Tests for the offline-pack + sync endpoints + mobile model catalog."""

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from ai_tutor.apps.accounts.models import Institution, Membership
from ai_tutor.apps.curriculum.models import Course, Unit, Lesson, LessonStep
from ai_tutor.apps.tutoring.models import (
    TutorSession, SessionParticipant, SessionTurn, LessonPackVersion,
)
from ai_tutor.apps.llm.models import MobileInferenceModel


class OfflinePackTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name='I', slug='i')
        cls.user = User.objects.create_user(username='u', password='pw-123456')
        Membership.objects.create(user=cls.user, institution=cls.institution, role='student')
        cls.course = Course.objects.create(
            institution=cls.institution, title='Geo', grade_level='S3', is_published=True,
        )
        cls.unit = Unit.objects.create(course=cls.course, title='U', order_index=0)
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title='L', objective='o', order_index=0, is_published=True,
        )
        LessonStep.objects.create(
            lesson=cls.lesson, order_index=0, step_type='teach',
            teacher_script='Welcome', question='', answer_type='none',
        )

    def _client(self):
        c = APIClient()
        token = c.post('/api/v1/auth/login/', {'username': 'u', 'password': 'pw-123456'}, format='json').json()['access']
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return c

    def test_offline_pack_creates_version_and_returns_payload(self):
        c = self._client()
        resp = c.get(f'/api/v1/lessons/{self.lesson.id}/offline-pack/')
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body['lesson_id'], self.lesson.id)
        self.assertEqual(body['pack_version'], 1)
        self.assertIn('policy', body)
        self.assertIn('content', body)
        self.assertIn('media_manifest', body)
        self.assertEqual(body['content']['lesson']['title'], 'L')
        self.assertEqual(len(body['content']['steps']), 1)
        self.assertEqual(LessonPackVersion.objects.filter(lesson=self.lesson).count(), 1)

    def test_offline_pack_reuses_latest_version(self):
        c = self._client()
        c.get(f'/api/v1/lessons/{self.lesson.id}/offline-pack/')
        c.get(f'/api/v1/lessons/{self.lesson.id}/offline-pack/')
        # Second call should reuse, not create v2
        self.assertEqual(LessonPackVersion.objects.filter(lesson=self.lesson).count(), 1)

    def test_offline_pack_refresh_creates_new_version(self):
        c = self._client()
        c.get(f'/api/v1/lessons/{self.lesson.id}/offline-pack/')
        c.get(f'/api/v1/lessons/{self.lesson.id}/offline-pack/?refresh=1')
        self.assertEqual(LessonPackVersion.objects.filter(lesson=self.lesson).count(), 2)

    def test_offline_pack_policy_v2_fields(self):
        """v2 policy ships pieces the on-device runner needs to drive
        the tutor without hitting the server."""
        c = self._client()
        resp = c.get(f'/api/v1/lessons/{self.lesson.id}/offline-pack/')
        policy = resp.json()['policy']
        self.assertEqual(policy['version'], 2)
        self.assertIn('system_prompt_template', policy)
        self.assertIn('context_chunks', policy)
        self.assertIsInstance(policy['context_chunks'], list)
        self.assertGreater(len(policy['system_prompt_template']), 100)
        # 'teach' step has no answer expected → evaluator_kind 'none'
        step0 = policy['steps'][0]
        self.assertEqual(step0['evaluator_kind'], 'none')
        self.assertIn('step_id', step0)


class SyncEndpointTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(name='S', slug='s')
        cls.user = User.objects.create_user(username='u2', password='pw-123456')
        Membership.objects.create(user=cls.user, institution=cls.institution, role='student')
        cls.course = Course.objects.create(
            institution=cls.institution, title='Math', grade_level='S3', is_published=True,
        )
        cls.unit = Unit.objects.create(course=cls.course, title='U', order_index=0)
        cls.lesson = Lesson.objects.create(
            unit=cls.unit, title='L', objective='o', order_index=0, is_published=True,
        )
        cls.session = TutorSession.objects.create(
            institution=cls.institution, student=cls.user, lesson=cls.lesson,
            status='active', engine_state={},
        )
        SessionParticipant.objects.create(
            session=cls.session, student=cls.user, is_primary=True, is_active=True,
        )

    def _client(self):
        c = APIClient()
        token = c.post('/api/v1/auth/login/', {'username': 'u2', 'password': 'pw-123456'}, format='json').json()['access']
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return c

    def test_sync_creates_turns_and_dedupes(self):
        c = self._client()
        payload = {
            'turns': [
                {'role': 'student', 'content': 'hi', 'client_uuid': 'u-1', 'client_generated_at': '2026-04-25T10:00:00Z'},
                {'role': 'tutor', 'content': 'hello', 'client_uuid': 't-1', 'generated_offline': True, 'offline_model_id': 'qwen-3-5-2b-q4'},
            ],
        }
        resp = c.post(f'/api/v1/sessions/{self.session.id}/sync/', payload, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body['turns_created'], 2)

        # Replay the same payload — should be deduped
        resp2 = c.post(f'/api/v1/sessions/{self.session.id}/sync/', payload, format='json')
        self.assertEqual(resp2.json()['turns_created'], 0)
        self.assertEqual(resp2.json()['turns_skipped_duplicate'], 2)
        self.assertEqual(SessionTurn.objects.filter(session=self.session).count(), 2)

    def test_sync_engine_state_lww(self):
        c = self._client()
        # First sync: state with timestamp T1
        resp1 = c.post(
            f'/api/v1/sessions/{self.session.id}/sync/',
            {
                'engine_state': {'current_topic_index': 2, 'phase': 'practice'},
                'engine_state_client_at': '2026-04-25T10:00:00Z',
            },
            format='json',
        )
        self.assertTrue(resp1.json()['state_updated'])

        # Second sync with EARLIER timestamp: should NOT overwrite
        resp2 = c.post(
            f'/api/v1/sessions/{self.session.id}/sync/',
            {
                'engine_state': {'current_topic_index': 0, 'phase': 'warmup'},
                'engine_state_client_at': '2026-04-25T09:00:00Z',
            },
            format='json',
        )
        self.assertFalse(resp2.json()['state_updated'])

        self.session.refresh_from_db()
        self.assertEqual(self.session.engine_state.get('current_topic_index'), 2)

    def test_turns_endpoint_returns_session_history(self):
        c = self._client()
        SessionTurn.objects.create(session=self.session, role='student', content='hi')
        SessionTurn.objects.create(session=self.session, role='tutor', content='hello back')
        resp = c.get(f'/api/v1/sessions/{self.session.id}/turns/')
        self.assertEqual(resp.status_code, 200, resp.content)
        rows = resp.json()['results']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['role'], 'student')
        self.assertEqual(rows[1]['content'], 'hello back')

    def test_turns_endpoint_hides_other_users_session(self):
        other = User.objects.create_user(username='other2', password='pw-123456')
        Membership.objects.create(user=other, institution=self.institution, role='student')
        SessionTurn.objects.create(session=self.session, role='student', content='private')
        c = APIClient()
        token = c.post('/api/v1/auth/login/', {'username': 'other2', 'password': 'pw-123456'}, format='json').json()['access']
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        resp = c.get(f'/api/v1/sessions/{self.session.id}/turns/')
        # Empty results, not 403 — we filter via queryset rather than 403.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['results'], [])

    def test_sync_rejects_other_users_session(self):
        other = User.objects.create_user(username='other', password='pw-123456')
        Membership.objects.create(user=other, institution=self.institution, role='student')
        c = APIClient()
        token = c.post('/api/v1/auth/login/', {'username': 'other', 'password': 'pw-123456'}, format='json').json()['access']
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        resp = c.post(f'/api/v1/sessions/{self.session.id}/sync/', {'turns': []}, format='json')
        self.assertEqual(resp.status_code, 403)


class MobileModelCatalogTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='m', password='pw-123456')
        institution = Institution.objects.create(name='I', slug='m-inst')
        Membership.objects.create(user=self.user, institution=institution, role='student')

        MobileInferenceModel.objects.create(
            id='qwen-3-5-2b-q4', display_name='Qwen 3.5 2B (Q4)',
            family='qwen_3_5', size_mb=1500, ram_required_mb=3000,
            download_url='https://example.com/qwen.gguf', runtime='llama_cpp',
            recommended_tier='mid',
            capabilities={'text': True, 'image': True, 'audio': False, 'video': False},
        )
        MobileInferenceModel.objects.create(
            id='gemma-3n-e4b', display_name='Gemma 3n E4B',
            family='gemma_3n', size_mb=5000, ram_required_mb=8000,
            download_url='https://example.com/gemma.gguf', runtime='llama_cpp',
            recommended_tier='flagship',
            capabilities={'text': True, 'image': True, 'audio': True, 'video': True},
        )

    def _client(self):
        c = APIClient()
        token = c.post('/api/v1/auth/login/', {'username': 'm', 'password': 'pw-123456'}, format='json').json()['access']
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return c

    def test_lists_all_models_no_tier(self):
        resp = self._client().get('/api/v1/mobile/models/')
        self.assertEqual(resp.status_code, 200)
        ids = {m['id'] for m in resp.json()['results']}
        self.assertEqual(ids, {'qwen-3-5-2b-q4', 'gemma-3n-e4b'})

    def test_filters_by_device_tier_low(self):
        # 'low' tier devices should not see flagship models
        resp = self._client().get('/api/v1/mobile/models/?device_tier=low')
        ids = {m['id'] for m in resp.json()['results']}
        self.assertNotIn('gemma-3n-e4b', ids)
