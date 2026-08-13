from django.apps import AppConfig
import logging

logger = logging.getLogger(__name__)


class CurriculumConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ai_tutor.apps.curriculum'
    # Pinned, not left implicit. Django derives the label from the last
    # dotted component of `name`, so it survived the move under ai_tutor/
    # by luck rather than intent. Naming it means a future move cannot
    # silently change it — a changed label makes every deployed database
    # try to re-run this app's migrations.
    label = 'curriculum'
    
    def ready(self):
        """Called when Django starts - preload heavy models and register signals."""
        import os
        import ai_tutor.apps.curriculum.signals  # noqa: F401 — registers signal handlers

        # Only preload in the main process (not in migrations, shell, etc.)
        # Check for RUN_MAIN to avoid double-loading in dev server
        if os.environ.get('RUN_MAIN') == 'true' or not os.environ.get('DJANGO_SETTINGS_MODULE'):
            self._preload_embedding_model()
    
    def _preload_embedding_model(self):
        """Warm the configured encoder so the first request doesn't pay for it.

        Goes through ``kb_storage.embed`` rather than importing
        sentence-transformers directly, so it warms whichever backend
        ``settings.EMBEDDING_BACKEND`` selects. Naming the library here would
        import torch on the offline desktop build, which ships onnxruntime
        precisely to avoid that — and torch isn't installed there, so the old
        code silently warmed nothing while looking like it had.

        Worth warming: measured cold load is 5814 ms for sentence-transformers
        and 144 ms for ONNX.
        """
        from django.conf import settings
        backend = getattr(settings, 'EMBEDDING_BACKEND', 'local')
        try:
            from ai_tutor.apps.curriculum.kb_storage import embed

            logger.info("Preloading embedding model (backend=%s)...", backend)
            embed(['warmup'])
            logger.info("Embedding model preloaded successfully!")
        except ImportError:
            logger.warning("Embedding backend %r unavailable - skipping preload",
                           backend)
        except Exception as e:
            logger.warning(f"Could not preload embedding model: {e}")