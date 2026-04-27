"""Course-level summative exam generator.

Builds a ~90-question bank that mirrors the format and difficulty of
teacher-uploaded exam papers (`TeachingMaterialUpload.material_type =
'question_bank'`). Each student attempt later picks ~30 stratified
across every teaching objective. See `memory/summative_assessments_plan.md`.

Pipeline:
  1. Walk every Lesson in the course → flatten teaching objectives via
     `combined_objectives_for_lesson` (see content_generator.py).
  2. Query the KB for question-bank materials in this course (format
     reference) + textbook/notes/worksheet chunks (content reference).
  3. Build one prompt with: objectives, format samples, content samples,
     DOK balance target.
  4. Single LLM call → 90 questions in the same JSON shape as exit
     tickets (so we can reuse `ExitTicketQuestion`).
  5. Save into a single `ExitTicket(course=course, assessment_type='summative')`.

The 90 → 30 stratified pick is in `summative_selection.py` and runs at
attempt-start, not here.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


SUMMATIVE_TARGET_COUNT = 90
SUMMATIVE_PER_ATTEMPT = 30


SUMMATIVE_PROMPT = """\
Generate a SUMMATIVE EXAM question bank for this entire course.

COURSE: {course_title}
SUBJECT: {subject}
GRADE: {grade}
TARGET TOTAL: {target_count} questions

This is a course-level summative — the questions must cover EVERY
teaching objective listed below. We will sample {per_attempt} questions
per student attempt, stratified so every objective gets at least one
question per attempt.

TEACHING OBJECTIVES (every question's `concept_tag` MUST be the EXACT
text of one of these — every objective gets at least 2 questions in the
bank):
{objectives_block}

QUESTION-FORMAT REFERENCE (real exam papers from this school's question
banks — match the command words, mark allocations, and style):
{format_samples}

CONTENT REFERENCE (excerpts from textbooks / worksheets / notes — pull
specific facts, examples, and numbers from these so the questions feel
grounded in the curriculum the students actually used):
{content_samples}

REQUIREMENTS

1. Generate EXACTLY {target_count} questions. No more, no fewer.
2. Mix question types in roughly this ratio:
   - 55% MCQ (4 options, one correct)
   - 15% short_answer
   - 12% data_interpretation (with a `figure_spec` for a chart, OR a
     `data_description` HTML table when the data is intrinsically tabular)
   - 10% fill_in_blank
   -  8% matching
3. Difficulty mix follows the DOK balance for summatives:
   - ~30% DOK 1 (recall)
   - ~45% DOK 2 (skill / concept)
   - ~20% DOK 3 (strategic thinking)
   -  ~5% DOK 4 (extended thinking) — use sparingly
4. Every question MUST have:
   - `question` (the stem)
   - `question_type` (mcq | short_answer | data_interpretation | fill_in_blank | matching)
   - `concept_tag` (EXACT text of one teaching objective)
   - `difficulty` (easy | medium | hard)
   - `explanation` (2-3 sentences why the answer is correct)
   - `dok_level` (1 | 2 | 3 | 4)
   - For MCQ: `option_a`, `option_b`, `option_c`, `option_d`, `correct` (A/B/C/D)
   - For non-MCQ: `answer_data` per the schema below
5. Coverage: every objective above gets >= 2 questions. Distribute
   widely — no objective should hog more than ~6 questions.
6. Use Seychelles context (real local examples — fish/tuna prices in
   SCR, granite islands, tourism numbers, monsoon rainfall) wherever it
   adds realism without inventing facts.

ANSWER-DATA SCHEMA per non-MCQ type

short_answer: {{ "model_answer": "...", "keywords": ["k1","k2"], "min_keywords": 2 }}

fill_in_blank: {{ "text_template": "The ___ is the largest ___ in Seychelles.", "blanks": ["Mahé","island"], "accept_alternatives": [["Mahe","mahe"], []] }}

matching: {{ "pairs": [{{"left": "Mahé", "right": "granite island"}}, ...], "distractor_rights": ["coral atoll"] }}

data_interpretation: {{
  "figure_spec": {{ "type":"bar"|"line"|"pie"|"scatter", "title":"...", "x_label":"...", "y_label":"...", "labels":[...], "datasets":[{{"label":"...","data":[...]}}], "source":"..." }},
  "model_answer": "...",
  "keywords": ["k1","k2"],
  "min_keywords": 2
}}

OUTPUT — JSON array of {target_count} objects. Return ONLY the JSON.
"""


def _format_objectives(objectives: List[str]) -> str:
    if not objectives:
        return "  (no teaching objectives extracted — refuse to generate)"
    return "\n".join(f"  TS{i+1}: {obj}" for i, obj in enumerate(objectives))


def _gather_objectives(course) -> List[str]:
    """Union of teaching objectives across every lesson in the course,
    deduplicated case-insensitively, preserving first-seen order."""
    from apps.curriculum.content_generator import combined_objectives_for_lesson

    seen = set()
    objectives: List[str] = []
    for unit in course.units.prefetch_related('lessons').order_by('order_index'):
        for lesson in unit.lessons.order_by('order_index'):
            for obj in combined_objectives_for_lesson(lesson):
                key = ' '.join(obj.split()).lower()
                if key in seen:
                    continue
                seen.add(key)
                objectives.append(obj)
    return objectives


def _gather_format_samples(course, kb, n: int = 12) -> str:
    """Pull vision-extracted question_bank items from the KB."""
    try:
        result = kb.query_for_content_generation(
            lesson_title=f"{course.title} summative exam",
            lesson_objective=f"Course-level summative for {course.title}",
            unit_title=course.title,
            subject=course.title,
            grade_level=course.grade_level or '',
            n_results=20,
        )
        if not result or not result.chunks:
            return "(no question-bank materials uploaded — write questions in standard exam format)"

        examples: List[str] = []
        for chunk in result.chunks:
            meta = chunk.get('metadata', {}) or {}
            chunk_type = meta.get('chunk_type', '')
            if chunk_type not in ('vision_question_bank', 'vision_worksheet'):
                continue
            extracted = meta.get('extracted_data', {}) or {}
            q_text = extracted.get('question_text') or chunk.get('content', '')
            q_type = extracted.get('question_type', '')
            cmd = extracted.get('command_word', '')
            marks = extracted.get('marks', '')
            line = f"- [{q_type or 'q'}] " + (f"{cmd}: " if cmd else '') + str(q_text)[:240]
            if marks:
                line += f" ({marks} marks)"
            examples.append(line)
            if len(examples) >= n:
                break

        if not examples:
            return "(no question-bank materials matched — write questions in standard exam format)"
        return "\n".join(examples)
    except Exception as e:
        logger.warning(f"summative format-sample query failed: {e}")
        return "(error fetching format samples — fall back to standard exam format)"


def _gather_content_samples(course, kb, n: int = 12) -> str:
    """Pull textbook/notes/worksheet chunks from the KB for content grounding."""
    try:
        result = kb.query_for_content_generation(
            lesson_title=f"{course.title} summative exam content",
            lesson_objective=f"Sample factual content from the {course.title} curriculum",
            unit_title=course.title,
            subject=course.title,
            grade_level=course.grade_level or '',
            n_results=20,
        )
        if not result or not result.chunks:
            return "(no curriculum content indexed)"

        excerpts: List[str] = []
        for chunk in result.chunks:
            content = (chunk.get('content') or '').strip()
            if not content:
                continue
            excerpts.append(f"- {content[:280]}")
            if len(excerpts) >= n:
                break
        return "\n".join(excerpts) if excerpts else "(no excerpts available)"
    except Exception as e:
        logger.warning(f"summative content-sample query failed: {e}")
        return "(error fetching content samples)"


def generate_summative_for_course(course, *, target_count: int = SUMMATIVE_TARGET_COUNT) -> Dict:
    """Generate (or regenerate) a course-level summative question bank.

    On success, replaces any existing summative ExitTicket for this
    course. Returns a dict {success, questions_created, error}.
    """
    from apps.tutoring.models import ExitTicket, ExitTicketQuestion
    from apps.llm.models import ModelConfig
    from apps.llm.client import get_llm_client
    from apps.llm.prompts import get_prompt_or_default
    from apps.llm.json_utils import parse_llm_json
    from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
    from apps.curriculum.figure_render import render_figure_spec
    from apps.curriculum.dok_framework import dok_guidance_for
    from apps.accounts.models import Institution
    from django.db import transaction

    institution_id = course.institution_id or Institution.get_global().id

    objectives = _gather_objectives(course)
    if not objectives:
        return {'success': False, 'error': 'No teaching objectives across this course.'}

    model_config = ModelConfig.get_for('exit_tickets') or ModelConfig.get_for('content')
    if not model_config:
        return {'success': False, 'error': 'No LLM model configured for exit_tickets/content.'}
    llm_client = get_llm_client(model_config)

    kb = CurriculumKnowledgeBase(institution_id=institution_id)
    format_samples = _gather_format_samples(course, kb)
    content_samples = _gather_content_samples(course, kb)

    prompt = SUMMATIVE_PROMPT.format(
        course_title=course.title,
        subject=course.title,
        grade=course.grade_level or '',
        target_count=target_count,
        per_attempt=SUMMATIVE_PER_ATTEMPT,
        objectives_block=_format_objectives(objectives),
        format_samples=format_samples,
        content_samples=content_samples,
    )

    system_prompt = get_prompt_or_default(
        institution_id, 'exit_ticket_prompt',
        "You are an expert educational assessment designer.",
        json_required=True,
    )
    system_prompt = system_prompt + "\n\n" + dok_guidance_for("assessment")

    print(f"[Summative] {course.title}: requesting {target_count} questions over {len(objectives)} objectives", flush=True)

    try:
        response = llm_client.generate(
            [{"role": "user", "content": prompt}],
            system_prompt=system_prompt,
            max_tokens=32000,
        )
        questions = parse_llm_json(response.content, expect_array=True)
    except Exception as e:
        logger.exception(f"summative LLM call failed for {course.title}")
        return {'success': False, 'error': f'LLM call failed: {e}'}

    if not questions or not isinstance(questions, list):
        return {'success': False, 'error': 'LLM did not return a JSON array of questions.'}

    questions = questions[:target_count]
    if len(questions) < max(30, target_count // 2):
        return {
            'success': False,
            'error': f'Too few questions returned ({len(questions)} of {target_count}).',
        }

    with transaction.atomic():
        # Replace any existing summative for this course.
        ExitTicket.objects.filter(
            course=course,
            assessment_type=ExitTicket.AssessmentType.SUMMATIVE,
        ).delete()

        summative = ExitTicket.objects.create(
            course=course,
            assessment_type=ExitTicket.AssessmentType.SUMMATIVE,
            question_bank_size=target_count,
            questions_per_attempt=SUMMATIVE_PER_ATTEMPT,
            passing_score=int(SUMMATIVE_PER_ATTEMPT * 0.7),  # 70% pass
            time_limit_minutes=60,
            is_published=False,
            instructions=(
                f"Course summative exam covering all teaching objectives in {course.title}. "
                f"You'll see {SUMMATIVE_PER_ATTEMPT} questions out of a {target_count}-question bank — "
                f"questions are stratified so every objective is represented."
            ),
        )

        created = 0
        for i, q in enumerate(questions):
            q_type = (q.get('question_type') or 'mcq').strip() or 'mcq'
            difficulty = (q.get('difficulty') or 'medium').strip().lower()
            if difficulty not in ('easy', 'medium', 'hard'):
                difficulty = 'medium'
            kwargs = {
                'exit_ticket': summative,
                'question_type': q_type,
                'question_text': (q.get('question') or '')[:8000],
                'explanation': (q.get('explanation') or '')[:8000],
                'concept_tag': (q.get('concept_tag') or '')[:200],
                'difficulty': difficulty,
                'order_index': i,
            }

            answer_data: Dict = q.get('answer_data') or {}
            # Preserve dok_level + objective metadata in answer_data for downstream selection.
            if q.get('dok_level') is not None:
                answer_data['dok_level'] = q.get('dok_level')
            if q.get('terminal_objective'):
                answer_data['terminal_objective'] = q['terminal_objective']
            if q.get('enabling_objective'):
                answer_data['enabling_objective'] = q['enabling_objective']

            if q_type == 'mcq':
                kwargs.update({
                    'option_a': str(q.get('option_a', ''))[:500],
                    'option_b': str(q.get('option_b', ''))[:500],
                    'option_c': str(q.get('option_c', ''))[:500],
                    'option_d': str(q.get('option_d', ''))[:500],
                    'correct_answer': (q.get('correct') or 'A')[:1].upper(),
                })
                if answer_data:
                    kwargs['answer_data'] = answer_data
            else:
                # Render figure_spec to inline SVG (server side) so we never trust LLM SVG.
                if 'plot_spec' in answer_data and 'figure_spec' not in answer_data:
                    answer_data['figure_spec'] = answer_data.pop('plot_spec')
                spec = answer_data.get('figure_spec')
                if spec:
                    try:
                        svg = render_figure_spec(spec)
                        if svg:
                            answer_data['figure_svg'] = svg
                        else:
                            answer_data.pop('figure_spec', None)
                    except Exception:
                        answer_data.pop('figure_spec', None)
                kwargs['answer_data'] = answer_data
            try:
                ExitTicketQuestion.objects.create(**kwargs)
                created += 1
            except Exception as e:
                logger.warning(f"summative question {i} skipped: {e}")

    print(f"[Summative] {course.title}: {created} questions saved", flush=True)
    return {
        'success': True,
        'questions_created': created,
        'objectives_covered': len(objectives),
        'summative_id': summative.id,
    }
