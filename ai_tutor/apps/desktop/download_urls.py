"""Public download routes. Separate from urls.py, which is device-local."""
from django.urls import path

from . import public_views

app_name = 'downloads'

urlpatterns = [
    path('', public_views.download_page, name='page'),
    # Server artefacts before the platform catch-all, or 'server' would be
    # read as a desktop platform name and 404.
    path('server/<str:artefact>/', public_views.download_server, name='server'),
    path('<str:platform>/', public_views.download_installer, name='installer'),
]
