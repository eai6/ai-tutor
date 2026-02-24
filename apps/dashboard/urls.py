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
    path('curriculum/process/<int:upload_id>/approve/', views.curriculum_approve, name='curriculum_approve'),
    
    # Step-by-step processing API
    path('api/curriculum/<int:upload_id>/process/', views.curriculum_process_api, name='curriculum_process_api'),
    
    # Course management
    path('curriculum/course/<int:course_id>/publish-all/', views.course_publish_all, name='course_publish_all'),
    path('curriculum/course/<int:course_id>/unit/create/', views.unit_create, name='unit_create'),
    
    # Unit management
    path('curriculum/unit/<int:unit_id>/lesson/create/', views.lesson_create, name='lesson_create'),
    
    # Lesson management
    path('curriculum/lesson/<int:lesson_id>/', views.lesson_detail, name='lesson_detail'),
    path('curriculum/lesson/<int:lesson_id>/regenerate/', views.lesson_regenerate, name='lesson_regenerate'),
    path('curriculum/lesson/<int:lesson_id>/generate/', views.lesson_generate_content, name='lesson_generate_content'),
    path('curriculum/lesson/<int:lesson_id>/publish/', views.lesson_publish, name='lesson_publish'),
    path('curriculum/step/<int:step_id>/edit/', views.step_edit, name='step_edit'),
    
    # Bulk Generation
    path('curriculum/course/<int:course_id>/generate-all/', views.course_generate_all, name='course_generate_all'),
    path('curriculum/course/<int:course_id>/generate-media/', views.course_generate_media, name='course_generate_media'),
    path('curriculum/media-progress/<int:upload_id>/', views.media_progress, name='media_progress'),
    path('curriculum/content-progress/<int:upload_id>/', views.content_progress, name='content_progress'),
    
    # Teaching Materials
    path('materials/process/<int:upload_id>/', views.material_process, name='material_process'),
    path('curriculum/course/<int:course_id>/upload-material/', views.course_upload_material, name='course_upload_material'),

    # Classes
    path('classes/', views.class_list, name='class_list'),
    
    # Reports
    path('reports/', views.reports_overview, name='reports'),
    
    # Settings
    path('settings/', views.settings_page, name='settings'),
]