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

        # Cross-document matching: enrich lessons with worksheet metadata (P2.2)
        if upload.material_type in ('worksheet', 'question_bank') and upload.course:
            try:
                matched = _match_worksheet_to_objectives(upload, kb)
                if matched:
                    upload.add_log(f"Matched worksheet to {matched} lesson(s)")
            except Exception as e:
                logger.warning(f"Worksheet matching failed: {e}")

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


def _match_worksheet_to_objectives(upload, kb) -> int:
    """Match worksheet content to curriculum objectives via KB semantic similarity.

    Enriches lesson metadata with worksheet-derived calibration:
    - vocabulary_register: terms and their usage level
    - question_formats: what question types appear
    - concept_emphasis: which objectives get most practice

    Returns number of lessons matched.
    """
    from apps.curriculum.models import Lesson
    from apps.llm.models import ModelConfig
    from apps.llm.client import get_llm_client

    course = upload.course
    if not course:
        return 0

    lessons = Lesson.objects.filter(unit__course=course).order_by('unit__order_index', 'order_index')
    if not lessons.exists():
        return 0

    # Query KB for worksheet chunks
    worksheet_chunks = kb.query(
        query_text=f"{upload.title} {upload.subject_name} worksheet exercises questions",
        n_results=20,
    )
    if not worksheet_chunks or not worksheet_chunks.get('chunks'):
        return 0

    worksheet_text = "\n".join(
        c.get('content', '')[:500] for c in worksheet_chunks['chunks'][:10]
        if c.get('content')
    )
    if not worksheet_text.strip():
        return 0

    # Use LLM to match worksheet content to lessons
    model_config = ModelConfig.get_for('generation')
    if not model_config:
        return 0
    llm_client = get_llm_client(model_config)

    lesson_list = "\n".join(
        f"{i+1}. [{l.id}] {l.title} — {l.objective[:100]}"
        for i, l in enumerate(lessons[:20])
    )

    prompt = f"""Analyze this worksheet content and match it to the most relevant lessons.

WORKSHEET: {upload.title}
CONTENT SAMPLE:
{worksheet_text[:3000]}

AVAILABLE LESSONS:
{lesson_list}

For each lesson that the worksheet content is relevant to, provide:
- "lesson_id": the ID number from the list above
- "vocabulary": list of key terms used in the worksheet for this topic
- "question_formats": list of question types found (e.g., "multiple_choice", "fill_in_blank", "calculation", "word_problem", "matching", "diagram_interpretation")
- "emphasis_score": 0.0-1.0 how much the worksheet focuses on this lesson's topic

Return a JSON array. Only include lessons with emphasis_score >= 0.3.
Return ONLY valid JSON."""

    try:
        response = llm_client.generate(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="You are a curriculum alignment expert. Match worksheet content to lesson objectives precisely. Return only valid JSON.",
            max_tokens=4000,
        )

        from apps.llm.json_utils import parse_llm_json
        matches = parse_llm_json(response.content, expect_array=True)
        if not matches or not isinstance(matches, list):
            return 0

        matched_count = 0
        for match in matches:
            lesson_id = match.get('lesson_id')
            if not lesson_id:
                continue
            try:
                lesson = Lesson.objects.get(id=lesson_id, unit__course=course)
                metadata = lesson.metadata or {}
                metadata['worksheet_calibration'] = {
                    'source': upload.title,
                    'vocabulary': match.get('vocabulary', []),
                    'question_formats': match.get('question_formats', []),
                    'emphasis_score': match.get('emphasis_score', 0.5),
                }
                lesson.metadata = metadata
                lesson.save(update_fields=['metadata'])
                matched_count += 1
            except Lesson.DoesNotExist:
                continue

        return matched_count

    except Exception as e:
        logger.warning(f"Worksheet-to-objective matching failed: {e}")
        return 0


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
