"""
URL patterns for accounts app - authentication routes.
"""

from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

app_name = 'accounts'

urlpatterns = [
    # Landing page
    path('', views.landing_page, name='landing'),

    # Student auth
    path('student/login/', views.student_login, name='student_login'),
    path('student/register/', views.student_register, name='student_register'),

    # Staff (Teacher/Admin) auth
    path('staff/login/', views.staff_login, name='staff_login'),
    path('staff/register/', views.staff_self_register, name='staff_self_register'),
    path('staff/register/<str:token>/', views.staff_register, name='staff_register'),

    # Staff invitation (admin only)
    path('staff/invite/', views.invite_staff, name='invite_staff'),

    # Bulk student registration (admin/staff only)
    path('students/bulk-upload/', views.bulk_student_upload, name='bulk_student_upload'),

    # Password reset (Django built-in views with custom templates)
    path('password-reset/', auth_views.PasswordResetView.as_view(
        template_name='accounts/password_reset.html',
        email_template_name='accounts/password_reset_email.html',
        subject_template_name='accounts/password_reset_subject.txt',
        success_url='/password-reset/done/',
    ), name='password_reset'),
    path('password-reset/done/', auth_views.PasswordResetDoneView.as_view(
        template_name='accounts/password_reset_done.html',
    ), name='password_reset_done'),
    path('password-reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(
        template_name='accounts/password_reset_confirm.html',
        success_url='/password-reset/complete/',
    ), name='password_reset_confirm'),
    path('password-reset/complete/', auth_views.PasswordResetCompleteView.as_view(
        template_name='accounts/password_reset_complete.html',
    ), name='password_reset_complete'),

    # Logout
    path('logout/', views.logout_view, name='logout'),

    # Self-service account deletion
    path('delete-account/', views.delete_account, name='delete_account'),

    # Legacy routes (redirect)
    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
]
