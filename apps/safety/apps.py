"""
Safety app configuration.
"""

from django.apps import AppConfig


class SafetyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.safety'
    verbose_name = 'Safety & Privacy'
    
    def ready(self):
        # Import signal handlers if needed
        pass
