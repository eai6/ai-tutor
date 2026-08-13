"""URL routes for the mobile REST API.

All routes mounted under /api/v1/ in config/urls.py. JWT auth via
simplejwt; session auth still works for web-originated calls.
"""

from django.urls import path
from drf_spectacular.views import (
    SpectacularAPIView, SpectacularSwaggerView,
)

from apps.api.views import auth as auth_views
from apps.api.views import resources as resource_views
from apps.api.views import sessions as session_views
from apps.api.views import offline_pack as offline_views
from apps.api.views import mobile_models as mobile_model_views
from apps.api.views import sync as sync_views
from apps.api.views import devices


app_name = 'api'

urlpatterns = [
    # Auth
    path('auth/login/', auth_views.login, name='auth_login'),
    path('auth/register/', auth_views.register, name='auth_register'),
    path('auth/refresh/', auth_views.refresh, name='auth_refresh'),
    path('auth/logout/', auth_views.logout, name='auth_logout'),
    path('me/', auth_views.me, name='me'),

    # Resources
    path('courses/', resource_views.CourseList.as_view(), name='course_list'),
    path('lessons/', resource_views.LessonList.as_view(), name='lesson_list'),
    path('lessons/<int:pk>/', resource_views.LessonDetail.as_view(), name='lesson_detail'),
    path('lessons/<int:lesson_id>/steps/', resource_views.LessonStepList.as_view(), name='lesson_steps'),
    path('lessons/<int:lesson_id>/competency/', session_views.lesson_competency, name='lesson_competency'),
    path('lessons/<int:lesson_id>/offline-pack/', offline_views.offline_pack, name='lesson_offline_pack'),

    # Sessions
    path('sessions/', session_views.start_session, name='session_start'),
    path('sessions/<int:pk>/', resource_views.SessionDetail.as_view(), name='session_detail'),
    path('sessions/<int:session_id>/respond/', session_views.respond, name='session_respond'),
    path('sessions/<int:session_id>/exit-ticket/', session_views.submit_exit_ticket, name='session_exit_ticket'),
    path('sessions/<int:session_id>/review/', session_views.start_review, name='session_review'),
    path('sessions/<int:session_id>/sync/', sync_views.sync, name='session_sync'),
    # A whole session that happened offline, uploaded by the student who
    # did it. The offline desktop build's only sync call.
    path('sessions/upload/', sync_views.upload_session, name='session_upload'),

    # Desktop devices. Enrolment is unauthenticated by design — the one-time
    # code IS the credential; see apps/api/views/devices.py.
    path('devices/enrol/', devices.enrol, name='device_enrol'),
    path('devices/check/', devices.device_check, name='device_check'),
    path('devices/sync/', devices.device_sync, name='device_sync'),
    path('sessions/<int:session_id>/turns/', resource_views.SessionTurnList.as_view(), name='session_turns'),

    # Progress
    path('progress/', resource_views.ProgressList.as_view(), name='progress_list'),

    # Mobile model catalog
    path('mobile/models/', mobile_model_views.MobileModelList.as_view(), name='mobile_models'),

    # OpenAPI schema (used by RN's openapi-typescript codegen)
    path('schema/', SpectacularAPIView.as_view(), name='schema'),
    path('docs/', SpectacularSwaggerView.as_view(url_name='api:schema'), name='docs'),
]
