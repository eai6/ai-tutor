from django.apps import AppConfig


class SupportConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ai_tutor.apps.support'
    # Pinned, not left implicit. Django derives the label from the last
    # dotted component of `name`, so it survived the move under ai_tutor/
    # by luck rather than intent. Naming it means a future move cannot
    # silently change it — a changed label makes every deployed database
    # try to re-run this app's migrations.
    label = 'support'
    verbose_name = 'Help Assistant & Support'
