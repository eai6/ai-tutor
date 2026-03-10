"""
URL patterns for accounts app - authentication routes.
"""

from django.urls import path
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
    
    # Logout
    path('logout/', views.logout_view, name='logout'),
    
    # Legacy routes (redirect)
    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
]
