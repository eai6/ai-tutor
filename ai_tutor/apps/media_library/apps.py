from django.apps import AppConfig


class MediaLibraryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ai_tutor.apps.media_library'
    # Pinned, not left implicit. Django derives the label from the last
    # dotted component of `name`, so it survived the move under ai_tutor/
    # by luck rather than intent. Naming it means a future move cannot
    # silently change it — a changed label makes every deployed database
    # try to re-run this app's migrations.
    label = 'media_library'
