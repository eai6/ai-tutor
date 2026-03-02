"""
Teaching Material Processing Pipeline

Processes uploaded teaching materials (textbooks, references, worksheets):
1. Extract text from PDF/DOCX
2. Chunk and index into ChromaDB knowledge base
3. Update status and stats on the TeachingMaterialUpload record
"""

import logging
from django.utils import timezone

logger = logging.getLogger(__name__)


def process_teaching_material(upload_id: int):
    """
    Process a teaching material upload: extract text, chunk, and index.

    Args:
        upload_id: TeachingMaterialUpload record ID
    """
    from apps.dashboard.models import TeachingMaterialUpload
    from apps.curriculum.knowledge_base import CurriculumKnowledgeBase

    upload = TeachingMaterialUpload.objects.get(id=upload_id)

    try:
        # Update status
        upload.status = 'processing'
        upload.save(update_fields=['status'])
        upload.add_log("Starting processing...")

        # Index into knowledge base
        from apps.accounts.models import Institution
        kb = CurriculumKnowledgeBase(institution_id=upload.institution_id or Institution.get_global().id)

        upload.add_log(f"Extracting text from {upload.original_filename}...")

        result = kb.index_teaching_material(
            file_path=upload.file_path,
            subject=upload.subject_name,
            grade_level=upload.grade_level,
            material_title=upload.title,
            material_type=upload.material_type,
            upload_id=upload.id,
        )

        # Update with results
        figures_indexed = result.get('figures_indexed', 0)
        upload.extracted_text_length = result.get('text_length', 0)
        upload.chunks_created = result.get('chunks_indexed', 0)
        upload.figures_extracted = figures_indexed
        upload.status = 'completed'
        upload.completed_at = timezone.now()
        upload.save()

        figures_msg = f", {figures_indexed} figures extracted" if figures_indexed else ""
        upload.add_log(
            f"Completed: {upload.extracted_text_length} chars extracted, "
            f"{upload.chunks_created} chunks indexed{figures_msg}"
        )

        logger.info(
            f"Teaching material processed: {upload.title} "
            f"({upload.chunks_created} chunks, {figures_indexed} figures)"
        )
        return result

    except Exception as e:
        upload.status = 'failed'
        upload.error_message = str(e)
        upload.save()
        upload.add_log(f"FAILED: {e}")
        logger.error(f"Teaching material processing failed for upload {upload_id}: {e}")
        raise
