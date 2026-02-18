"""
URL patterns for the tutoring app.
"""

from django.urls import path
from . import views

app_name = 'tutoring'

urlpatterns = [
    # Original API endpoints
    path('api/lessons/', views.lesson_list, name='lesson_list'),
    path('api/session/start/<int:lesson_id>/', views.start_session, name='start_session'),
    path('api/session/<int:session_id>/answer/', views.submit_answer, name='submit_answer'),
    path('api/session/<int:session_id>/answer/stream/', views.submit_answer_stream, name='submit_answer_stream'),
    path('api/session/<int:session_id>/advance/', views.advance_step, name='advance_step'),
    path('api/session/<int:session_id>/status/', views.session_status, name='session_status'),
    
    # V2 structured session endpoints
    path('api/v2/session/start/<int:lesson_id>/', views.start_structured_session, name='start_structured_session'),
    path('api/v2/session/<int:session_id>/input/', views.structured_session_input, name='structured_session_input'),
    path('api/v2/session/<int:session_id>/input/stream/', views.structured_session_input_stream, name='structured_session_input_stream'),
    
    # V3 Step-based API
    path('api/v3/session/start/<int:lesson_id>/', views.start_session_v3, name='start_session_v3'),
    path('api/v3/session/<int:session_id>/input/', views.session_input_v3, name='session_input_v3'),
    path('api/v3/session/<int:session_id>/advance/', views.session_advance_v3, name='session_advance_v3'),
    
    # Chat-based AI Tutor API (NEW - recommended)
    path('api/chat/start/<int:lesson_id>/', views.chat_start_session, name='chat_start_session'),
    path('api/chat/<int:session_id>/respond/', views.chat_respond, name='chat_respond'),
    path('api/chat/<int:session_id>/exit-ticket/', views.chat_exit_ticket, name='chat_exit_ticket'),
    
    # Image generation
    path('api/generate-image/', views.generate_image, name='generate_image'),
    
    # HTML views
    path('', views.lesson_catalog, name='catalog'),
    path('lesson/<int:lesson_id>/', views.chat_tutor_interface, name='tutor_interface'),  # Chat is now default!
    path('v2/lesson/<int:lesson_id>/', views.tutor_interface_v2, name='tutor_interface_v2'),
    path('v3/lesson/<int:lesson_id>/', views.tutor_interface_v3, name='tutor_interface_v3'),
    path('chat/lesson/<int:lesson_id>/', views.chat_tutor_interface, name='chat_tutor_interface'),
]