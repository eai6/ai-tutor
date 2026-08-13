"""Tests for Course.locale — the primary locale signal at runtime.

Part of M4 of memory/portuguese_mozambique_pilot_plan.md. The field
drives per-course tutor response language; the engine reads it via
``apps/tutoring/simple_tutor/engine.py::_course_locale``.
"""
from __future__ import annotations

from django.conf import settings
from django.test import TestCase

from ai_tutor.apps.curriculum.models import Course


class CourseLocaleFieldTest(TestCase):
    def test_default_locale_is_en_us(self):
        course = Course.objects.create(title='Test Course')
        self.assertEqual(course.locale, 'en-us')

    def test_can_set_pt_mz(self):
        course = Course.objects.create(title='Biologia', locale='pt-mz')
        course.refresh_from_db()
        self.assertEqual(course.locale, 'pt-mz')

    def test_field_choices_match_settings_languages(self):
        choices = dict(Course._meta.get_field('locale').choices)
        self.assertEqual(set(choices.keys()), set(dict(settings.LANGUAGES).keys()))

    def test_get_locale_display_renders_label(self):
        """The picker must show country-forward labels, not raw codes —
        see auto-memory/feedback_locale_picker_country_forward.md."""
        course = Course.objects.create(title='Biologia', locale='pt-mz')
        # Label must mention country (Moçambique) prominently.
        label = course.get_locale_display()
        self.assertIn('Moçambique', label)

    def test_no_courses_with_empty_locale_after_default(self):
        """The migration backfills empty rows to 'en-us'; new rows pick
        up the default. Either way no Course should ever carry an
        empty locale."""
        Course.objects.create(title='A')
        Course.objects.create(title='B', locale='pt-mz')
        self.assertEqual(Course.objects.filter(locale='').count(), 0)
