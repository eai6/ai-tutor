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
        # Check file exists
        import os
        if not os.path.exists(upload.file_path):
            upload.status = 'failed'
            upload.error_message = f"File not found: {upload.file_path}. The file may need to be re-uploaded."
            upload.save()
            upload.add_log(f"FAILED: File not found on disk. Please re-upload this material.")
            return

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
        print(f"[Material] Step 2 done: {upload.extracted_text_length} chars, {upload.chunks_created} chunks, {figures_indexed} figures", flush=True)
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
        print(f"[Material] FAILED {upload.original_filename}: {e}", flush=True)
        import traceback; traceback.print_exc()
        upload.status = 'failed'
        upload.error_message = str(e)[:500]
        upload.save()
        upload.add_log(f"FAILED: {e}")
        logger.error(f"Teaching material processing failed for upload {upload_id}: {e}")
        raise


def process_teaching_material_fast(upload_id: int):
    """Fast processing: text extraction + KB indexing only, no LLM vision.

    Good for textbooks and notes where structured extraction isn't needed.
    """
    from apps.dashboard.models import TeachingMaterialUpload
    from apps.curriculum.knowledge_base import CurriculumKnowledgeBase

    upload = TeachingMaterialUpload.objects.get(id=upload_id)

    try:
        # Check file exists
        import os
        if not os.path.exists(upload.file_path):
            upload.status = 'failed'
            upload.error_message = f"File not found: {upload.file_path}. The file may need to be re-uploaded."
            upload.save()
            upload.add_log(f"FAILED: File not found on disk. Please re-upload this material.")
            return {'error': 'file_not_found'}

        upload.status = 'processing'
        upload.save(update_fields=['status'])
        print(f"[MaterialFast] Processing {upload.original_filename}", flush=True)
        upload.add_log("Starting fast processing (text only)...")

        from apps.accounts.models import Institution
        kb = CurriculumKnowledgeBase(institution_id=upload.institution_id or Institution.get_global().id)

        result = kb.index_teaching_material(
            file_path=upload.file_path,
            subject=upload.subject_name,
            grade_level=upload.grade_level,
            material_title=upload.title,
            material_type=upload.material_type,
            upload_id=upload.id,
        )

        upload.extracted_text_length = result.get('text_length', 0)
        upload.chunks_created = result.get('chunks_indexed', 0)
        upload.figures_extracted = result.get('figures_indexed', 0)
        upload.status = 'completed'
        upload.completed_at = timezone.now()

        if not upload.course:
            upload.course = _find_matching_course(upload)
        upload.save()

        print(f"[MaterialFast] Done {upload.original_filename}: {upload.chunks_created} chunks, {upload.figures_extracted} figures", flush=True)
        upload.add_log(f"Completed: {upload.chunks_created} chunks, {upload.figures_extracted} figures")
        return result

    except Exception as e:
        upload.status = 'failed'
        upload.error_message = str(e)
        upload.save()
        print(f"[MaterialFast] FAILED {upload.original_filename}: {e}", flush=True)
        upload.add_log(f"FAILED: {e}")
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

    # Type-specific extraction prompts (improved based on testing)
    PROMPTS = {
        'worksheet': f"""Analyze this {subject} worksheet for {grade_level} students and extract ALL questions and activities.

For EACH question or activity, extract:
- "question_number": the question/activity number (e.g., "1", "2a", "Activity 3")
- "question_text": the FULL question text including all instructions
- "question_type": classify PRECISELY as one of:
  "multiple_choice" (A/B/C/D options),
  "fill_in_blank" (complete a sentence/paragraph with missing words),
  "definition_recall" (define a term or concept),
  "short_answer" (write 1-3 sentences),
  "matching" (match items from two columns/lists),
  "calculation" (compute a numerical answer),
  "diagram_interpretation" (analyze a figure/map/chart),
  "data_analysis" (interpret data in a table/graph),
  "true_false" (true or false),
  "labeling" (label parts of a diagram),
  "essay" (extended written response)
- "answer": the correct answer if visible (from answer key or word bank)
- "marks": mark allocation as a number if shown, or null if not shown
- "figure_description": describe any diagram/figure/table/map associated with the question. If none, set to null
- "vocabulary_terms": key subject-specific terms used in the question
- "command_word": the main action verb (define, describe, explain, compare, list, state, name, identify, give, use, etc.)

IMPORTANT:
- Distinguish between "fill_in_blank" (gap-fill in a sentence) and "definition_recall" (define a term)
- If a worksheet has Parts (A, B, C, D) or Activities, extract questions from ALL parts
- If an activity has multiple sub-tasks (match these, fill in these, answer these), extract EACH sub-task as a separate item
- A word-search with 15 definitions = 15 separate definition_recall items
- A table to complete with 5 rows = 5 separate items
- Extract MORE items rather than fewer — granularity is better

Return a JSON array. Extract EVERY question from EVERY part — do not skip any.""",

        'question_bank': f"""Analyze this {subject} exam paper for {grade_level} students. Extract EVERY question including all sub-parts.

For EACH question and sub-question, extract:
- "question_number": the full number including sub-parts (e.g., "1", "2(i)", "3a(ii)", "Part 1 Q5")
- "question_text": the FULL question text
- "question_type": classify as:
  "multiple_choice" (circle/underline A/B/C/D),
  "short_answer" (1-2 line answer),
  "structured" (guided multi-part question),
  "source_based" (analyze a source then answer),
  "map_reading" (use an OS/topographic map),
  "fill_in_blank" (complete sentences),
  "matching" (match columns/items),
  "labeling" (identify features on a diagram),
  "data_analysis" (interpret table/graph data),
  "essay" (extended writing, 3+ marks)
- "marks": the mark allocation as a NUMBER (from the [N] brackets). Must be an integer, not null
- "command_word": the main instruction verb (State, Name, Identify, Describe, Explain, Suggest, Give, Draw, Compare, Discuss, Using information from...)
- "source_description": describe the source material if the question references one (map extract, photograph, diagram, table, bar chart, text extract). Set to null if no source
- "topic": which curriculum topic this question tests (weather, industry, trade, rivers, coasts, volcanoes, map skills, population, tourism, etc.)
- "section": which section of the paper (e.g., "Section A Part 1", "Section B Q3")

CRITICAL: Extract EVERY sub-part as a separate item. Q1(i), Q1(ii) are separate items. Include questions that reference diagrams/photographs even if you cannot see the image clearly — describe what the source appears to be.

Return a JSON array.""",

        'textbook': f"""Analyze these {subject} textbook pages for {grade_level} students. Extract every piece of educational content.

For EACH distinct concept, definition, example, or figure, create a separate item with:
- "item_type": "definition", "concept", "example", "worked_example", "figure", "activity", "key_fact", "local_context"
- "title": short title for this item
- "content": the full text content
- "vocabulary_terms": key terms defined or used
- "figure_description": describe any diagram/map/chart (null if none)
- "seychelles_context": any Seychelles-specific data or examples (null if none)

Extract each definition, each example, and each key fact as SEPARATE items. Do NOT group multiple concepts into one item.

Return a JSON array.""",

        'notes': f"""Analyze these {subject} teaching notes for {grade_level} students. Extract every piece of content as individual items.

For EACH concept, definition, fact, example, or activity, create a separate item:
- "item_type": "definition", "key_fact", "explanation", "example", "local_example", "activity", "statistic", "comparison"
- "title": short descriptive title
- "content": the full text
- "vocabulary_terms": subject-specific terms used
- "seychelles_data": any Seychelles-specific statistics, examples, or facts (null if none)
- "related_topic": which curriculum topic this relates to

IMPORTANT: Extract EACH definition as a separate item. Each statistic as a separate item. Each Seychelles-specific fact as a separate item. The more granular, the better — we need these for content generation.

For example, if notes define "GNP", "HDI", "MEDC", "LEDC" — that is 4 separate definition items, NOT one grouped item.

Return a JSON array.""",
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

        for attempt in range(2):
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
                    print(f"[MaterialVision] Batch {batch_start//batch_size+1}: {len(items)} items", flush=True)
                else:
                    print(f"[MaterialVision] Batch {batch_start//batch_size+1}: empty/invalid JSON, attempt {attempt+1}", flush=True)
                    if attempt == 0:
                        continue  # Retry once
                break
            except Exception as e:
                print(f"[MaterialVision] Batch {batch_start//batch_size+1} attempt {attempt+1} failed: {e}", flush=True)
                if attempt == 0:
                    continue  # Retry once
                break

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
