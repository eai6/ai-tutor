from django.apps import AppConfig
import logging

logger = logging.getLogger(__name__)


class CurriculumConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.curriculum'
    
    def ready(self):
        """Called when Django starts - preload heavy models and register signals."""
        import os
        import apps.curriculum.signals  # noqa: F401 — registers signal handlers

        # Only preload in the main process (not in migrations, shell, etc.)
        # Check for RUN_MAIN to avoid double-loading in dev server
        if os.environ.get('RUN_MAIN') == 'true' or not os.environ.get('DJANGO_SETTINGS_MODULE'):
            self._preload_embedding_model()
    
    def _preload_embedding_model(self):
        """Preload the sentence transformer model to avoid first-request delay."""
        try:
            from sentence_transformers import SentenceTransformer
            
            logger.info("Preloading embedding model (all-MiniLM-L6-v2)...")
            
            # This loads the model into memory
            _ = SentenceTransformer('all-MiniLM-L6-v2')
            
            logger.info("Embedding model preloaded successfully!")
            
        except ImportError:
            logger.warning("sentence-transformers not installed - skipping preload")
        except Exception as e:
            logger.warning(f"Could not preload embedding model: {e}")