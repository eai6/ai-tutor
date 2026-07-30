from django.urls import path

from apps.desktop import views

app_name = 'desktop'

urlpatterns = [
    path('health/', views.health, name='health'),
    path('setup/', views.setup, name='setup'),
    path('install-model/', views.install_model, name='install_model'),
    path('install-progress/', views.install_progress, name='install_progress'),
    path('import-pack/', views.import_pack, name='import_pack'),
]
