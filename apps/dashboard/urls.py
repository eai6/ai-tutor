"""
Dashboard URL Configuration
"""

from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    # Home
    path('', views.dashboard_home, name='home'),
    
    # Students
    path('students/', views.student_list, name='student_list'),
    path('students/<int:student_id>/', views.student_detail, name='student_detail'),
    
    # Curriculum
    path('curriculum/', views.curriculum_list, name='curriculum_list'),
    path('curriculum/course/<int:course_id>/', views.course_detail, name='course_detail'),
    path('curriculum/upload/', views.curriculum_upload, name='curriculum_upload'),
    path('curriculum/process/<int:upload_id>/', views.curriculum_process, name='curriculum_process'),
    path('curriculum/generate/<int:upload_id>/', views.curriculum_generate, name='curriculum_generate'),
    
    # Classes
    path('classes/', views.class_list, name='class_list'),
    
    # Reports
    path('reports/', views.reports_overview, name='reports'),
    
    # Settings
    path('settings/', views.settings_page, name='settings'),
]
