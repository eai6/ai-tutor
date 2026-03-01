"""
Signal handlers for curriculum models.

Handles cleanup of files, vectors, and orphaned records when a Course is deleted.
"""

import logging
import os

from django.conf import settings
from django.db.models.signals import pre_delete
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(pre_delete, sender='curriculum.Course')
def cleanup_course_on_delete(sender, instance, **kwargs):
    """
    Clean up associated resources when a Course is deleted.

    Django's CASCADE handles DB records for Units/Lessons/Steps/Sessions,
    but these require manual cleanup:
    1. ChromaDB vectors (no FK relationship)
    2. Teaching material files + records (SET_NULL would orphan them)
    3. Curriculum upload files + records (SET_NULL would orphan them)
    4. Exit ticket question image files (FileField not auto-deleted)
    5. Orphaned MediaAssets only used by this course's steps
    """
    course = instance
    logger.info(f"Cleaning up resources for course: {course.title} (id={course.id})")

    _cleanup_vectors(course)
    _cleanup_teaching_materials(course)
    _cleanup_curriculum_uploads(course)
    _cleanup_exit_ticket_images(course)
    _cleanup_orphaned_media_assets(course)


def _cleanup_vectors(course):
    """Delete ChromaDB vectors indexed from this course's uploads."""
    try:
        from apps.dashboard.models import TeachingMaterialUpload, CurriculumUpload

        # Collect upload IDs whose vectors should be removed
        material_ids = list(
            TeachingMaterialUpload.objects.filter(course=course)
            .values_list('id', flat=True)
        )
        curriculum_ids = list(
            CurriculumUpload.objects.filter(created_course=course)
            .values_list('id', flat=True)
        )
        upload_ids = material_ids + curriculum_ids

        if not upload_ids:
            return

        from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
        kb = CurriculumKnowledgeBase(institution_id=course.institution_id)
        if not kb._chromadb_available:
            return

        collection = kb._get_collection()
        if collection is None:
            return

        # ChromaDB delete supports where filters — delete by upload_id
        for uid in upload_ids:
            try:
                collection.delete(where={"upload_id": uid})
            except Exception as e:
                logger.warning(f"Failed to delete vectors for upload_id={uid}: {e}")

        logger.info(
            f"Deleted vectors for {len(upload_ids)} uploads "
            f"(course {course.id})"
        )
    except Exception as e:
        logger.error(f"Vector cleanup failed for course {course.id}: {e}")


def _cleanup_teaching_materials(course):
    """Delete teaching material upload files and records."""
    try:
        from apps.dashboard.models import TeachingMaterialUpload

        materials = TeachingMaterialUpload.objects.filter(course=course)
        count = 0
        for material in materials:
            _delete_file_at_path(material.file_path)
            count += 1
        materials.delete()

        if count:
            logger.info(f"Deleted {count} teaching material uploads (course {course.id})")
    except Exception as e:
        logger.error(f"Teaching material cleanup failed for course {course.id}: {e}")


def _cleanup_curriculum_uploads(course):
    """Delete curriculum upload files and records."""
    try:
        from apps.dashboard.models import CurriculumUpload

        uploads = CurriculumUpload.objects.filter(created_course=course)
        count = 0
        for upload in uploads:
            _delete_file_at_path(upload.file_path)
            count += 1
        uploads.delete()

        if count:
            logger.info(f"Deleted {count} curriculum uploads (course {course.id})")
    except Exception as e:
        logger.error(f"Curriculum upload cleanup failed for course {course.id}: {e}")


def _cleanup_exit_ticket_images(course):
    """Delete image files from exit ticket questions."""
    try:
        from apps.tutoring.models import ExitTicketQuestion

        questions = ExitTicketQuestion.objects.filter(
            exit_ticket__lesson__unit__course=course
        ).exclude(image='')

        count = 0
        for question in questions:
            if question.image:
                try:
                    question.image.delete(save=False)
                    count += 1
                except Exception as e:
                    logger.warning(f"Failed to delete exit ticket image: {e}")

        if count:
            logger.info(f"Deleted {count} exit ticket images (course {course.id})")
    except Exception as e:
        logger.error(f"Exit ticket image cleanup failed for course {course.id}: {e}")


def _cleanup_orphaned_media_assets(course):
    """Delete MediaAssets only used by this course's lesson steps."""
    try:
        from apps.curriculum.models import LessonStep
        from apps.media_library.models import MediaAsset, StepMedia

        # Find all MediaAsset IDs linked to this course's steps
        course_step_ids = LessonStep.objects.filter(
            lesson__unit__course=course
        ).values_list('id', flat=True)

        course_asset_ids = set(
            StepMedia.objects.filter(lesson_step_id__in=course_step_ids)
            .values_list('media_asset_id', flat=True)
            .distinct()
        )

        if not course_asset_ids:
            return

        # Find which of those are also used by steps OUTSIDE this course
        shared_asset_ids = set(
            StepMedia.objects.filter(media_asset_id__in=course_asset_ids)
            .exclude(lesson_step_id__in=course_step_ids)
            .values_list('media_asset_id', flat=True)
            .distinct()
        )

        # Orphans = used ONLY by this course
        orphan_ids = course_asset_ids - shared_asset_ids

        if not orphan_ids:
            return

        count = 0
        for asset in MediaAsset.objects.filter(id__in=orphan_ids):
            if asset.file:
                try:
                    asset.file.delete(save=False)
                except Exception as e:
                    logger.warning(f"Failed to delete media file {asset.file.name}: {e}")
            asset.delete()
            count += 1

        logger.info(f"Deleted {count} orphaned media assets (course {course.id})")
    except Exception as e:
        logger.error(f"Media asset cleanup failed for course {course.id}: {e}")


def _delete_file_at_path(file_path):
    """Delete a file from disk given a path string (not a FileField)."""
    if not file_path:
        return

    # Handle both absolute and MEDIA_ROOT-relative paths
    if os.path.isabs(file_path):
        full_path = file_path
    else:
        full_path = os.path.join(settings.MEDIA_ROOT, file_path)

    try:
        if os.path.exists(full_path):
            os.remove(full_path)
            logger.debug(f"Deleted file: {full_path}")
    except OSError as e:
        logger.warning(f"Failed to delete file {full_path}: {e}")
