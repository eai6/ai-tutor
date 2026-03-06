"""
URL patterns for the tutoring app.
"""

from django.urls import path
from . import views

app_name = 'tutoring'

urlpatterns = [
    # Lesson list API
    path('api/lessons/', views.lesson_list, name='lesson_list'),

    # Chat-based AI Tutor API (active)
    path('api/chat/start/<int:lesson_id>/', views.chat_start_session, name='chat_start_session'),
    path('api/chat/<int:session_id>/respond/', views.chat_respond, name='chat_respond'),
    path('api/chat/<int:session_id>/exit-ticket/', views.chat_exit_ticket, name='chat_exit_ticket'),
    path('api/chat/<int:session_id>/review/', views.chat_start_review, name='chat_start_review'),

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
]
