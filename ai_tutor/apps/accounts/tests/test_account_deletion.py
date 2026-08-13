"""Tests for the three account-deletion paths:
  - student self-delete (with password re-auth)
  - teacher deletes a student in their institution
  - platform admin deletes a staff account

Audit log entries assert the action was recorded for compliance.
"""

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse

from ai_tutor.apps.accounts.models import Institution, Membership
from ai_tutor.apps.safety.models import SafetyAuditLog


class SelfDeleteTest(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(name='S', slug='s')
        self.user = User.objects.create_user(username='student1', password='pw-12345')
        Membership.objects.create(user=self.user, institution=self.institution, role='student')
        self.client = Client()
        self.client.force_login(self.user)

    def test_get_renders_confirmation_page(self):
        resp = self.client.get(reverse('accounts:delete_account'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Delete your account')

    def test_post_with_correct_password_deletes(self):
        resp = self.client.post(
            reverse('accounts:delete_account'),
            {'password': 'pw-12345', 'confirm': 'DELETE'},
            follow=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(User.objects.filter(username='student1').exists())
        self.assertTrue(
            SafetyAuditLog.objects.filter(event_type='account_deleted').exists()
        )

    def test_post_with_wrong_password_keeps_user(self):
        resp = self.client.post(
            reverse('accounts:delete_account'),
            {'password': 'wrong', 'confirm': 'DELETE'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(User.objects.filter(username='student1').exists())

    def test_post_without_typing_delete_keyword_keeps_user(self):
        resp = self.client.post(
            reverse('accounts:delete_account'),
            {'password': 'pw-12345', 'confirm': 'remove'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(User.objects.filter(username='student1').exists())


class TeacherDeleteStudentTest(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(name='School', slug='school')
        self.other = Institution.objects.create(name='Other', slug='other')
        self.teacher = User.objects.create_user(username='teach', password='pw')
        Membership.objects.create(user=self.teacher, institution=self.institution, role='staff')
        self.student = User.objects.create_user(username='kid', password='pw')
        Membership.objects.create(user=self.student, institution=self.institution, role='student')
        self.outside_student = User.objects.create_user(username='outside', password='pw')
        Membership.objects.create(user=self.outside_student, institution=self.other, role='student')
        self.client = Client()
        self.client.force_login(self.teacher)

    def test_teacher_can_delete_student_in_their_school(self):
        resp = self.client.post(reverse('dashboard:delete_student', args=[self.student.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(User.objects.filter(username='kid').exists())

    def test_teacher_cannot_delete_student_in_other_school(self):
        resp = self.client.post(
            reverse('dashboard:delete_student', args=[self.outside_student.id])
        )
        # Redirected with error; user still exists.
        self.assertTrue(User.objects.filter(username='outside').exists())

    def test_teacher_cannot_delete_themselves_via_endpoint(self):
        resp = self.client.post(reverse('dashboard:delete_student', args=[self.teacher.id]))
        self.assertTrue(User.objects.filter(username='teach').exists())

    def test_teacher_cannot_delete_a_staff_account(self):
        other_staff = User.objects.create_user(username='other-staff', password='pw')
        Membership.objects.create(user=other_staff, institution=self.institution, role='staff')
        resp = self.client.post(reverse('dashboard:delete_student', args=[other_staff.id]))
        self.assertTrue(User.objects.filter(username='other-staff').exists())


class AdminDeleteStaffTest(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(name='S2', slug='s2')
        self.admin = User.objects.create_user(
            username='admin1', password='pw', is_staff=True,
        )
        Membership.objects.create(user=self.admin, institution=self.institution, role='staff')
        self.teacher = User.objects.create_user(username='teach2', password='pw')
        Membership.objects.create(user=self.teacher, institution=self.institution, role='staff')
        self.client = Client()
        self.client.force_login(self.admin)

    def test_admin_can_delete_staff(self):
        resp = self.client.post(reverse('dashboard:delete_staff', args=[self.teacher.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(User.objects.filter(username='teach2').exists())
        self.assertTrue(
            SafetyAuditLog.objects.filter(
                event_type='account_deleted',
                details__mode='admin_deletes_staff',
            ).exists()
        )

    def test_admin_cannot_delete_self_via_endpoint(self):
        resp = self.client.post(reverse('dashboard:delete_staff', args=[self.admin.id]))
        self.assertTrue(User.objects.filter(username='admin1').exists())

    def test_non_admin_blocked(self):
        # A non-admin staff account should be redirected, not allowed to delete.
        regular = User.objects.create_user(username='regular', password='pw')
        Membership.objects.create(user=regular, institution=self.institution, role='staff')
        client = Client()
        client.force_login(regular)
        resp = client.post(reverse('dashboard:delete_staff', args=[self.teacher.id]))
        # Teacher still exists.
        self.assertTrue(User.objects.filter(username='teach2').exists())


class StaffListTest(TestCase):
    def setUp(self):
        self.institution = Institution.objects.create(name='SL', slug='sl')
        self.admin = User.objects.create_user(username='adm', password='pw', is_staff=True)
        Membership.objects.create(user=self.admin, institution=self.institution, role='staff')
        self.teacher = User.objects.create_user(username='teach3', password='pw')
        Membership.objects.create(user=self.teacher, institution=self.institution, role='staff')
        self.client = Client()
        self.client.force_login(self.admin)

    def test_staff_list_renders_for_admin(self):
        resp = self.client.get(reverse('dashboard:staff_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'teach3')
