"""Tests for Institution.default_locale + StudentProfile.preferred_locale.

Part of M4 of memory/portuguese_mozambique_pilot_plan.md. Pair with
LocaleResolverMiddleware (apps/accounts/locale_middleware.py) which
walks: course.locale > student.preferred > institution.default >
settings.LANGUAGE_CODE.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase

from apps.accounts.models import Institution, StudentProfile


class InstitutionDefaultLocaleTest(TestCase):
    def test_default_locale_is_en_us(self):
        inst = Institution.objects.create(name='Test School', slug='test-school')
        self.assertEqual(inst.default_locale, 'en-us')

    def test_can_set_pt_mz(self):
        inst = Institution.objects.create(
            name='Escola Moçambique',
            slug='moz-school',
            default_locale='pt-mz',
        )
        inst.refresh_from_db()
        self.assertEqual(inst.default_locale, 'pt-mz')

    def test_choices_match_settings_languages(self):
        choices = dict(Institution._meta.get_field('default_locale').choices)
        self.assertEqual(set(choices.keys()), set(dict(settings.LANGUAGES).keys()))


class StudentProfilePreferredLocaleTest(TestCase):
    def test_default_is_blank(self):
        user = User.objects.create_user(username='alice', password='x')
        profile = StudentProfile.objects.create(user=user)
        # nullable + blank=True; default is None — students never need
        # a preference unless they set one.
        self.assertIn(profile.preferred_locale, ('', None))

    def test_can_set_pt_mz(self):
        user = User.objects.create_user(username='bob', password='x')
        profile = StudentProfile.objects.create(
            user=user, preferred_locale='pt-mz',
        )
        profile.refresh_from_db()
        self.assertEqual(profile.preferred_locale, 'pt-mz')

    def test_choices_match_settings_languages(self):
        choices = dict(StudentProfile._meta.get_field('preferred_locale').choices)
        self.assertEqual(set(choices.keys()), set(dict(settings.LANGUAGES).keys()))
