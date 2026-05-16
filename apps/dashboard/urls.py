"""
Dashboard URL Configuration
"""

from django.urls import include, path
from . import views

app_name = 'dashboard'

urlpatterns = [
    # Home
    path('', views.dashboard_home, name='home'),
    
    # Students
    path('students/', views.student_list, name='student_list'),
    path('students/<int:student_id>/', views.student_detail, name='student_detail'),

    # Student groups (paired/grouped sessions for shared-device schools)
    path('groups/', views.student_groups_list, name='student_groups'),
    path('groups/create/', views.student_group_create, name='student_group_create'),
    path('groups/<int:group_id>/update/', views.student_group_update, name='student_group_update'),
    path('groups/<int:group_id>/archive/', views.student_group_archive, name='student_group_archive'),
    path('groups/session-mode/', views.institution_session_mode, name='institution_session_mode'),
    
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
    path('curriculum/course/<int:course_id>/edit/', views.course_edit, name='course_edit'),
    path('curriculum/course/<int:course_id>/change-institution/', views.course_change_institution, name='course_change_institution'),
    path('curriculum/course/<int:course_id>/delete/', views.course_delete, name='course_delete'),
    path('curriculum/course/<int:course_id>/regenerate-all/', views.course_regenerate_all, name='course_regenerate_all'),
    path('curriculum/course/<int:course_id>/set-duration/', views.course_set_default_duration, name='course_set_default_duration'),
    path('curriculum/course/<int:course_id>/publish-all/', views.course_publish_all, name='course_publish_all'),
    path('curriculum/course/<int:course_id>/unpublish-all/', views.course_unpublish_all, name='course_unpublish_all'),
    path('curriculum/course/<int:course_id>/unit/create/', views.unit_create, name='unit_create'),
    
    # Unit management
    path('curriculum/unit/<int:unit_id>/lesson/create/', views.lesson_create, name='lesson_create'),
    
    # Lesson management
    path('curriculum/lesson/<int:lesson_id>/', views.lesson_detail, name='lesson_detail'),
    path('curriculum/lesson/<int:lesson_id>/regenerate/', views.lesson_regenerate, name='lesson_regenerate'),
    path('curriculum/lesson/<int:lesson_id>/generate/', views.lesson_generate_content, name='lesson_generate_content'),
    path('curriculum/lesson/<int:lesson_id>/publish/', views.lesson_publish, name='lesson_publish'),
    path('curriculum/lesson/<int:lesson_id>/approve/', views.lesson_approve, name='lesson_approve'),
    path('curriculum/lesson/<int:lesson_id>/group-settings/', views.lesson_group_settings, name='lesson_group_settings'),
    path('curriculum/lesson/<int:lesson_id>/edit-objectives/', views.lesson_edit_objectives, name='lesson_edit_objectives'),
    path('curriculum/lesson/<int:lesson_id>/move-objective/', views.lesson_move_objective, name='lesson_move_objective'),

    # Exit-ticket figure editing (teacher dashboard)
    path('exit-tickets/questions/<int:question_id>/figure/',
         views.exit_ticket_figure_edit, name='exit_ticket_figure_edit'),
    path('exit-tickets/questions/<int:question_id>/figure/regenerate/',
         views.exit_ticket_figure_regenerate, name='exit_ticket_figure_regenerate'),
    path('exit-tickets/questions/<int:question_id>/figure/delete/',
         views.exit_ticket_figure_delete, name='exit_ticket_figure_delete'),
    path('curriculum/course/<int:course_id>/subject-type/', views.course_subject_type, name='course_subject_type'),

    # Account deletion (memory)
    path('students/<int:student_id>/delete/', views.delete_student, name='delete_student'),
    path('staff/', views.staff_list, name='staff_list'),
    path('staff/<int:user_id>/delete/', views.delete_staff, name='delete_staff'),
    path('staff/<int:user_id>/reset-password/show/', views.staff_reset_password_show, name='staff_reset_password_show'),
    path('staff/<int:user_id>/reset-password/email/', views.staff_reset_password_email, name='staff_reset_password_email'),
    path('curriculum/step/<int:step_id>/edit/', views.step_edit, name='step_edit'),
    path('curriculum/step/<int:step_id>/regen/', views.lesson_step_regenerate, name='lesson_step_regenerate'),
    path('curriculum/step/<int:step_id>/save-regen/', views.lesson_step_save_regen, name='lesson_step_save_regen'),
    path('curriculum/exit-question/<int:question_id>/edit/', views.exit_question_edit, name='exit_question_edit'),
    path('curriculum/exit-question/<int:question_id>/regen/', views.exit_question_regenerate, name='exit_question_regenerate'),
    path('curriculum/exit-question/<int:question_id>/save-regen/', views.exit_question_save_regen, name='exit_question_save_regen'),
    path('curriculum/exit-question/<int:question_id>/inplace-regen/', views.exit_question_inplace_regen, name='exit_question_inplace_regen'),
    # Content-quality benchmark (Q5)
    path('content-edits/', views.content_edit_events_list, name='content_edit_events_list'),
    path('content-edits/dashboard/', views.content_edit_dashboard, name='content_edit_dashboard'),
    path('content-edits/<int:event_id>/', views.content_edit_event_detail, name='content_edit_event_detail'),
    path('content-edits/export.csv', views.content_edit_events_export_csv, name='content_edit_events_export_csv'),
    path('content-edits/export.jsonl', views.content_edit_events_export_jsonl, name='content_edit_events_export_jsonl'),
    path('curriculum/lesson/<int:lesson_id>/prerequisites/', views.lesson_prerequisite_edit, name='lesson_prerequisite_edit'),
    
    # Weekly assignments — teacher creates/edits per (course, week)
    path('curriculum/course/<int:course_id>/weekly-assignment/', views.weekly_assignment_save, name='weekly_assignment_save'),

    # Bulk Generation
    path('curriculum/course/<int:course_id>/generate-all/', views.course_generate_all, name='course_generate_all'),
    path('curriculum/course/<int:course_id>/generate-media/', views.course_generate_media, name='course_generate_media'),
    path('curriculum/media-progress/<int:upload_id>/', views.media_progress, name='media_progress'),
    path('curriculum/content-progress/<int:upload_id>/', views.content_progress, name='content_progress'),
    path('curriculum/cancel-generation/<int:upload_id>/', views.cancel_generation, name='cancel_generation'),
    path('curriculum/lesson/<int:lesson_id>/cancel/', views.cancel_lesson_generation, name='cancel_lesson_generation'),
    
    # Teaching Materials
    path('materials/process/<int:upload_id>/', views.material_process, name='material_process'),
    path('materials/<int:upload_id>/confirm/', views.material_confirm_processing, name='material_confirm_processing'),
    path('materials/<int:material_id>/delete/', views.material_delete, name='material_delete'),
    path('curriculum/course/<int:course_id>/upload-material/', views.course_upload_material, name='course_upload_material'),
    path('curriculum/course/<int:course_id>/process-materials/', views.process_pending_materials, name='process_pending_materials'),
    path('curriculum/course/<int:course_id>/reindex-materials/', views.reindex_materials, name='reindex_materials'),

    # Classes
    path('classes/', views.class_list, name='class_list'),
    path('classes/promote/', views.promote_students, name='promote_students'),
    path('classes/<str:grade>/', views.class_detail, name='class_detail'),
    
    # Reports
    path('reports/', views.reports_overview, name='reports'),
    path('class/<int:course_id>/readiness/', views.class_readiness_redirect, name='class_readiness'),
    path('lesson/<int:lesson_id>/session-report/', views.lesson_session_report, name='lesson_session_report'),
    path('lesson/<int:lesson_id>/monitor/', views.lesson_live_monitor, name='lesson_monitor'),
    path('lesson/<int:lesson_id>/send-guidance/', views.send_guidance, name='send_guidance'),
    path('session/<int:session_id>/chat-history/', views.session_chat_history, name='session_chat_history'),
    path('session/<int:session_id>/exit-review/', views.session_exit_review, name='session_exit_review'),
    path('exit-attempt/<int:attempt_id>/override/', views.session_exit_review_override, name='session_exit_review_override'),
    
    # Settings
    path('settings/', views.settings_page, name='settings'),

    # Safety / Flagged sessions
    path('flagged/', views.flagged_sessions, name='flagged_sessions'),
    path('flagged/<int:session_id>/', views.flagged_session_detail, name='flagged_session_detail'),
    path('flagged/<int:session_id>/resolve/', views.resolve_flag, name='resolve_flag'),

    # School picker
    path('switch-school/', views.switch_school, name='switch_school'),

    # Feedback / bug reports (Pilot Task 2)
    path('feedback/', views.feedback_list, name='feedback_list'),
    path('feedback/submit/', views.feedback_submit, name='feedback_submit'),
    path('feedback/<int:report_id>/resolve/', views.feedback_resolve, name='feedback_resolve'),

    # Help / FAQ (Pilot Task 3)
    path('help/', views.help_index, name='help_index'),

    # Summative course exams
    path('curriculum/course/<int:course_id>/summative/', views.summative_review, name='summative_review'),
    path('curriculum/course/<int:course_id>/summative/generate/', views.summative_generate, name='summative_generate'),
    path('curriculum/course/<int:course_id>/summative/publish/', views.summative_publish, name='summative_publish'),

    # Competency analytics (course-level baseline → final)
    path('curriculum/course/<int:course_id>/competencies/', views.class_competency, name='class_competency'),
    path('curriculum/course/<int:course_id>/competencies/student/<int:student_id>/', views.student_competency, name='student_competency'),

    # Admin utilities
    path('admin/seed-demo/', views.seed_demo_school, name='seed_demo'),

    # Benchmark annotation UI (super-admin only)
    path('benchmark/', include('apps.benchmark.urls')),
]