from django.apps import AppConfig


class DesktopConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.desktop'
    verbose_name = 'Desktop (offline build)'
