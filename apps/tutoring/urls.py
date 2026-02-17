"""
URL patterns for the tutoring app.
"""

from django.urls import path
from . import views

app_name = 'tutoring'

urlpatterns = [
    # API endpoints
    path('api/lessons/', views.lesson_list, name='lesson_list'),
    path('api/session/start/<int:lesson_id>/', views.start_session, name='start_session'),
    path('api/session/<int:session_id>/answer/', views.submit_answer, name='submit_answer'),
    path('api/session/<int:session_id>/answer/stream/', views.submit_answer_stream, name='submit_answer_stream'),
    path('api/session/<int:session_id>/advance/', views.advance_step, name='advance_step'),
    path('api/session/<int:session_id>/status/', views.session_status, name='session_status'),
    
    # HTML views
    path('', views.lesson_catalog, name='catalog'),
    path('lesson/<int:lesson_id>/', views.tutor_interface, name='tutor_interface'),
]