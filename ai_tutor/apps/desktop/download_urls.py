"""Public download routes. Separate from urls.py, which is device-local."""
from django.urls import path

from . import public_views

app_name = 'downloads'

urlpatterns = [
    path('', public_views.download_page, name='page'),
    path('<str:platform>/', public_views.download_installer, name='installer'),
]
