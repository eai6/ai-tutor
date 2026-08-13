from django.apps import AppConfig


class DesktopConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ai_tutor.apps.desktop'
    # Pinned, not left implicit. Django derives the label from the last
    # dotted component of `name`, so it survived the move under ai_tutor/
    # by luck rather than intent. Naming it means a future move cannot
    # silently change it — a changed label makes every deployed database
    # try to re-run this app's migrations.
    label = 'desktop'
    verbose_name = 'Desktop (offline build)'

    def ready(self):
        """Queue finished sessions for sync — packaged offline builds only.

        Connected here rather than called from the tutoring engine so that
        `apps.tutoring` carries no knowledge of the desktop build. On a server
        DESKTOP_BUILD is False and nothing is connected at all, so the hosted
        app pays nothing for this.
        """
        from django.conf import settings

        if not getattr(settings, 'DESKTOP_BUILD', False):
            return

        from django.db.models.signals import post_save
        from ai_tutor.apps.desktop.session_sync import on_session_saved

        post_save.connect(
            on_session_saved,
            sender='tutoring.TutorSession',
            dispatch_uid='desktop_session_sync',
        )
