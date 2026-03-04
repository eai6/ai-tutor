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

        # Auto-link to matching course if not already linked
        if not upload.course:
            upload.course = _find_matching_course(upload)

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


def _find_matching_course(upload):
    """Find a course matching this material's subject and institution."""
    import re
    from apps.curriculum.models import Course
    from django.db.models import Q

    raw = (upload.subject_name or '').split('(')[0].strip()  # "Geography1 (S1,...)" → "Geography1"
    # Strip trailing digits: "Geography1" → "Geography"
    subject = re.sub(r'\d+$', '', raw).strip()
    if not subject:
        return None

    q = Q(title__icontains=subject)
    if upload.institution_id:
        q &= Q(institution_id=upload.institution_id)
    else:
        q &= Q(institution__isnull=True)

    return Course.objects.filter(q).first()


def link_unlinked_materials():
    """Link all unlinked teaching materials to matching courses. Idempotent."""
    from apps.dashboard.models import TeachingMaterialUpload

    unlinked = TeachingMaterialUpload.objects.filter(course__isnull=True)
    linked = 0
    for upload in unlinked:
        course = _find_matching_course(upload)
        if course:
            upload.course = course
            upload.save(update_fields=['course'])
            linked += 1
            logger.info(f"Linked '{upload.title}' → '{course.title}'")

    logger.info(f"Linked {linked}/{unlinked.count()} unlinked materials")
    return linked
