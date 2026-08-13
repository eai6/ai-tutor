"""
URL patterns for the tutoring app.
"""

from django.urls import path
from . import views

app_name = 'tutoring'

urlpatterns = [
    # Lesson list API
    path('api/lessons/', views.lesson_list, name='lesson_list'),
    path('api/lessons/<int:lesson_id>/competency/', views.lesson_competency, name='lesson_competency'),

    # Chat-based AI Tutor API (active)
    path('api/chat/start/<int:lesson_id>/', views.chat_start_session, name='chat_start_session'),
    path('api/chat/restart/<int:lesson_id>/', views.chat_restart_session, name='chat_restart_session'),
    path('api/chat/<int:session_id>/respond/', views.chat_respond, name='chat_respond'),
    path('api/chat/<int:session_id>/exit-ticket/', views.chat_exit_ticket, name='chat_exit_ticket'),
    path('api/chat/<int:session_id>/review/', views.chat_start_review, name='chat_start_review'),
    path('api/chat/<int:session_id>/difficulty-signal/', views.chat_difficulty_signal, name='chat_difficulty_signal'),
    path('api/chat/<int:session_id>/answer-bank-question/', views.chat_answer_bank_question, name='chat_answer_bank_question'),

    # Group lessons — participant management
    path('api/chat/<int:session_id>/participants/', views.session_participants, name='session_participants'),
    path('api/chat/<int:session_id>/participants/<int:user_id>/', views.session_participant_remove, name='session_participant_remove'),

    # Audio — STT + TTS
    path('api/chat/<int:session_id>/transcribe/', views.transcribe_audio, name='transcribe_audio'),
    path('api/speak/', views.speak_text, name='speak_text'),

    # Image generation
    path('api/generate-image/', views.generate_image, name='generate_image'),

    # Gamification + Personality APIs
    path('api/personality/', views.set_personality, name='set_personality'),
    path('api/gamification/', views.get_gamification_data, name='gamification_data'),
    path('api/leaderboard/', views.leaderboard, name='leaderboard'),

    # HTML views
    path('', views.lesson_catalog, name='catalog'),
    path('lesson/<int:lesson_id>/', views.chat_tutor_interface, name='tutor_interface'),
    path('chat/lesson/<int:lesson_id>/', views.chat_tutor_interface, name='chat_tutor_interface'),
    path('lesson/<int:lesson_id>/pretest/', views.lesson_pretest, name='lesson_pretest'),

    # Course summative exam (student-facing)
    path('summative/<int:course_id>/', views.summative_take, name='summative_take'),
    path('summative/<int:course_id>/submit/', views.summative_submit, name='summative_submit'),
    path('summative/<int:course_id>/review/<int:attempt_id>/', views.summative_review, name='summative_review'),
]
