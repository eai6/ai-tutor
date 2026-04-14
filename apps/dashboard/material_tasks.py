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
    Process a teaching material upload:
    1. LLM vision extraction (structured data from images)
    2. Text extraction, chunking, and KB indexing
    3. Figure extraction
    4. Worksheet-to-objective matching

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

        # Step 1: LLM vision extraction for rich structured data
        print(f"[Material] Step 1: Vision extraction for {upload.original_filename} (type={upload.material_type})", flush=True)
        if upload.file_path and upload.file_path.lower().endswith('.pdf'):
            try:
                vision_data = extract_material_with_vision(
                    file_path=upload.file_path,
                    material_type=upload.material_type,
                    subject=upload.subject_name,
                    grade_level=upload.grade_level,
                )
                if vision_data:
                    upload.add_log(f"Vision extraction: {len(vision_data)} items extracted")
                    print(f"[Material] Vision: {len(vision_data)} items from {upload.original_filename}", flush=True)
                    _index_vision_data(upload, vision_data)
                else:
                    print(f"[Material] Vision: no items extracted", flush=True)
            except Exception as e:
                upload.add_log(f"Vision extraction skipped: {e}")
                print(f"[Material] Vision FAILED for {upload.original_filename}: {e}", flush=True)
        else:
            print(f"[Material] Skipping vision (not PDF)", flush=True)

        # Step 2: Standard text extraction and KB indexing
        print(f"[Material] Step 2: Text extraction + KB indexing for {upload.original_filename}", flush=True)
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


def extract_material_with_vision(file_path: str, material_type: str, subject: str, grade_level: str) -> list:
    """
    Use LLM vision to extract structured data from a teaching material PDF.

    Different prompts per material type to extract the most relevant information:
    - Worksheet: questions with format types, answer keys, vocabulary
    - Exam paper: questions with mark allocations, command words, source materials
    - Textbook: key concepts, definitions, worked examples
    - Notes: teaching sequences, emphasis areas, local examples
    """
    import base64
    import fitz
    from apps.llm.models import ModelConfig
    from apps.llm.client import get_llm_client

    try:
        doc = fitz.open(file_path)
    except Exception as e:
        logger.error(f"Could not open PDF for vision extraction: {e}")
        return []

    config = ModelConfig.get_for('generation')
    if not config:
        return []

    client = get_llm_client(config)
    is_anthropic = config.provider == 'anthropic'

    # Render pages
    MAX_IMAGE_BYTES = 4_500_000
    page_images = []
    for page in doc:
        pix = page.get_pixmap(dpi=150)
        img_bytes = pix.tobytes("jpeg", jpg_quality=85)
        if len(img_bytes) > MAX_IMAGE_BYTES:
            pix = page.get_pixmap(dpi=100)
            img_bytes = pix.tobytes("jpeg", jpg_quality=75)
        if len(img_bytes) > MAX_IMAGE_BYTES:
            continue
        page_images.append({
            'b64': base64.b64encode(img_bytes).decode('utf-8'),
            'media_type': 'image/jpeg',
        })

    if not page_images:
        return []

    # Type-specific extraction prompts
    PROMPTS = {
        'worksheet': f"""Analyze this {subject} worksheet for {grade_level} students and extract ALL questions.

For EACH question, extract:
- "question_number": the question number
- "question_text": the full question text
- "question_type": one of "multiple_choice", "fill_in_blank", "short_answer", "matching", "calculation", "diagram_interpretation", "data_analysis", "essay", "true_false"
- "answer": the correct answer if visible (from answer key)
- "marks": mark allocation if shown
- "figure_description": describe any diagram/figure/table associated with the question
- "vocabulary_terms": key subject terms used in the question
- "command_word": the action verb (define, describe, explain, compare, calculate, etc.)

Return a JSON array of question objects. Extract EVERY question — do not skip any.""",

        'question_bank': f"""Analyze this {subject} exam paper / question bank for {grade_level} students.

For EACH question, extract:
- "question_number": the full question number (e.g., "1a", "2bi")
- "question_text": the full question text
- "question_type": "multiple_choice", "short_answer", "structured", "source_based", "essay", "calculation", "data_analysis"
- "marks": mark allocation (e.g., 2, 3, 6)
- "command_word": the action verb (state, describe, explain, suggest, evaluate, etc.)
- "source_description": if the question references a source (map, table, diagram, photo), describe it
- "answer_guidance": model answer or marking points if visible
- "topic": what curriculum topic this tests

Return a JSON array. Extract EVERY question including sub-parts (a, b, c, i, ii).""",

        'textbook': f"""Analyze these {subject} textbook pages for {grade_level} students.

Extract:
- "key_concepts": list of key concepts/definitions taught
- "worked_examples": any worked examples with step-by-step solutions
- "vocabulary": key terms with definitions
- "figures": description of each diagram, map, chart, or image
- "activities": any student activities or exercises
- "local_context": any Seychelles-specific examples or data

Return a JSON array of extracted items.""",

        'notes': f"""Analyze these {subject} teacher notes for {grade_level} students.

Extract:
- "topics": main topics covered
- "key_points": key teaching points
- "explanations": detailed explanations of concepts
- "examples": examples used (especially local/Seychelles context)
- "activities": suggested student activities
- "emphasis": concepts that receive extra emphasis or repetition

Return a JSON array of extracted items.""",
    }

    prompt = PROMPTS.get(material_type, PROMPTS.get('textbook'))
    system_prompt = (
        "You are an expert at analyzing educational documents. "
        "Extract ALL content with perfect accuracy. Return ONLY valid JSON."
    )

    all_items = []
    batch_size = 5  # Smaller batches for materials (often denser than curriculum)

    for batch_start in range(0, len(page_images), batch_size):
        batch = page_images[batch_start:batch_start + batch_size]

        content_blocks = []
        for pg in batch:
            if is_anthropic:
                content_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": pg['media_type'],
                        "data": pg['b64'],
                    }
                })
            else:
                content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{pg['media_type']};base64,{pg['b64']}"},
                })
        content_blocks.append({"type": "text", "text": prompt})

        try:
            response = client.generate(
                messages=[{"role": "user", "content": content_blocks}],
                system_prompt=system_prompt,
                max_tokens=8000,
            )

            from apps.llm.json_utils import parse_llm_json
            items = parse_llm_json(response.content, expect_array=True)
            if items and isinstance(items, list):
                all_items.extend(items)
        except Exception as e:
            logger.warning(f"Material vision extraction batch failed: {e}")
            continue

    print(f"[MaterialVision] {material_type}: extracted {len(all_items)} items from {len(page_images)} pages", flush=True)
    return all_items


def _index_vision_data(upload, vision_data: list):
    """Index vision-extracted structured data into the KB as enriched chunks."""
    import hashlib
    from apps.accounts.models import Institution
    from apps.curriculum.knowledge_base import CurriculumKnowledgeBase, CurriculumChunk

    institution_id = upload.institution_id or Institution.get_global().id
    kb = CurriculumKnowledgeBase(institution_id=institution_id)

    chunks = []
    for i, item in enumerate(vision_data):
        # Build rich content from the extracted data
        content_parts = []
        for key, value in item.items():
            if isinstance(value, list):
                content_parts.append(f"{key}: {', '.join(str(v) for v in value)}")
            elif value:
                content_parts.append(f"{key}: {value}")
        content = "\n".join(content_parts)

        chunk_id = hashlib.md5(
            f"{upload.id}:vision:{i}:{content[:80]}".encode()
        ).hexdigest()[:16]

        chunks.append(CurriculumChunk(
            id=chunk_id,
            content=content,
            metadata={
                "subject": upload.subject_name,
                "grade_level": upload.grade_level,
                "section": f"Vision-extracted {upload.material_type} item {i+1}",
                "chunk_type": f"vision_{upload.material_type}",
                "source_file": upload.original_filename,
                "upload_id": upload.id,
                "institution_id": institution_id,
                "material_type": upload.material_type,
                "material_title": upload.title,
                # Store the structured data for programmatic access
                "extracted_data": item,
            },
        ))

    if chunks:
        result = kb._index_chunks(chunks)
        logger.info(f"Indexed {result.get('indexed', 0)} vision-extracted chunks for {upload.title}")


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
    worksheet_result = kb.query_for_content_generation(
        lesson_title=upload.title,
        lesson_objective=f"{upload.subject_name} worksheet exercises questions",
        unit_title='',
        subject=upload.subject_name,
        grade_level=upload.grade_level or '',
        n_results=20,
    )
    worksheet_chunks = {'chunks': [c for c in (worksheet_result.chunks if worksheet_result else [])]}
    if not worksheet_chunks.get('chunks'):
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
