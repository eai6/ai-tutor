"""
Generate standardized exit tickets for lessons.

Usage:
    python manage.py generate_exit_tickets --lesson 5
    python manage.py generate_exit_tickets --course "Geography"
    python manage.py generate_exit_tickets --all
"""

from django.core.management.base import BaseCommand, CommandError

from apps.curriculum.models import Lesson, Course
from apps.tutoring.models import ExitTicket


MATH_EXIT_TICKET_PROMPT = """Generate a MATHEMATICS question bank (35 questions) for a summative assessment (exit ticket) on this lesson.

LESSON: {lesson_title}
OBJECTIVE: {lesson_objective}
SUBJECT: {subject}
{exam_context}{seychelles_context}
QUESTION FORMAT MIX — generate in THIS EXACT ORDER:
Questions 1-5: FILL_IN_BLANK (calculation with blank for the answer)
Questions 6-9: MATCHING (match expressions to their solutions/simplified forms)
Questions 10-13: SHORT_ANSWER (multi-step calculation requiring working out)
Questions 14-35: MCQ (multiple choice with numerical/algebraic options)

YOU MUST generate ALL 4 types. DO NOT generate any data_interpretation
questions — that type is disabled platform-wide. The first 13 questions
MUST NOT be MCQ.

MATHEMATICS RULES — EVERY question must:
- Require CALCULATION, not description or explanation
- Have NUMERICAL or ALGEBRAIC answers, not paragraphs of text
- Use command words: "Work out", "Calculate", "Simplify", "Solve", "Find", "Evaluate"
- NOT use: "Explain why", "Describe", "Discuss" (these are for geography/humanities)

REQUIREMENTS:
1. Generate EXACTLY 35 questions
2. Each question MUST have concept_tag = EXACT TEXT of an enabling objective from below
3. EVERY enabling objective must be assessed by at least 1 question
4. Use Seychelles context in word problems (SCR prices, fish catches, island areas)
5. Vary difficulty: easy calculations → harder numbers → word problems → multi-step

OUTPUT FORMAT (JSON array):

MCQ format (numerical/algebraic options):
{{"question_type": "mcq", "question": "Simplify 3(x + 4) - 2x", "option_a": "x + 12", "option_b": "5x + 4", "option_c": "x + 4", "option_d": "3x + 12", "correct": "A", "explanation": "3(x+4) - 2x = 3x + 12 - 2x = x + 12", "difficulty": "easy", "concept_tag": "EXACT TEXT OF AN ENABLING OBJECTIVE"}}

FILL_IN_BLANK format (calculation result):
{{"question_type": "fill_in_blank", "question": "Work out the following:", "answer_data": {{"text_template": "20 + 5 × 3 = ___", "blanks": ["35"], "accept_alternatives": [["35.0"]]}}, "explanation": "Using BIDMAS: multiply first (5×3=15), then add (20+15=35)", "difficulty": "easy", "concept_tag": "EXACT TEXT OF AN ENABLING OBJECTIVE"}}

MATCHING format (expressions to solutions):
{{"question_type": "matching", "question": "Match each expression to its simplified form:", "answer_data": {{"pairs": [{{"left": "3x + 2x", "right": "5x"}}, {{"left": "4y - y", "right": "3y"}}, {{"left": "2(x + 3)", "right": "2x + 6"}}], "distractor_rights": ["6x", "5y"]}}, "explanation": "Combine like terms or expand brackets", "difficulty": "medium", "concept_tag": "EXACT TEXT OF AN ENABLING OBJECTIVE"}}

SHORT_ANSWER format (show working):
{{"question_type": "short_answer", "question": "A fisherman in Seychelles sells tuna at SCR 45 per kg. He catches 3 fish weighing 2.5kg, 4.2kg, and 3.8kg. Calculate the total revenue. Show your working.", "answer_data": {{"model_answer": "Total weight = 2.5 + 4.2 + 3.8 = 10.5 kg. Revenue = 10.5 × 45 = SCR 472.50", "keywords": ["10.5", "472.50", "45"], "min_keywords": 2}}, "explanation": "Add the weights first, then multiply by price per kg", "difficulty": "medium", "concept_tag": "EXACT TEXT OF AN ENABLING OBJECTIVE"}}

DATA_INTERPRETATION is DISABLED. Do not generate questions of that type.

NO FIGURES — questions are TEXT-ONLY. Do NOT emit `figure_spec`,
`figure`, `plot_spec`, `figure_svg`, `figure_url`, or any inline
<svg>. The teacher can attach a generated image (gpt-image-2)
to any question after the fact via the question editor.

PARAMETRIC TEMPLATES (preferred for arithmetic-heavy questions) —
Instead of writing literal numbers in your question and computing
the answer yourself, you MAY emit a `template` object. The
backend samples concrete parameter values and computes the answer
in code — arithmetic errors become impossible. Use this for
question patterns where the same form with different numbers
would be a valid alternate question.

PARAMETRIC FORMAT example (replaces SHORT_ANSWER for arithmetic):
{{"question_type": "short_numeric", "concept_tag": "EXACT EO TEXT", "difficulty": "easy", "template": {{"template_text": "Three angles around a point are {{a}}°, {{b}}°, and x°. Find x.", "parameters": {{"a": {{"type": "int", "min": 30, "max": 150, "step": 5}}, "b": {{"type": "int", "min": 30, "max": 150, "step": 5}}}}, "answer_formula": "360 - a - b", "answer_unit": "°", "explanation_template": "Angles around a point sum to 360°. x = 360 - {{a}} - {{b}} = {{answer}}.", "constraints": ["a + b < 350"]}}}}

Template rules:
- Parameter names in {{braces}} must match exactly between
  template_text, answer_formula, and explanation_template.
- answer_formula uses ONLY + - * / ** ( ) and the parameter
  names. No function calls, no external variables.
- The slot {{answer}} in explanation_template gets the computed
  answer.
- constraints are simple boolean comparisons over parameters
  (e.g. "a + b < 350"); the renderer re-samples until they
  hold.
- DO NOT use templates for word problems whose phrasing depends
  on the actual numbers, or for questions requiring qualitative
  reasoning. Use them for clean arithmetic patterns.

DIFFICULTY DISTRIBUTION (out of 35):
- Questions 1-12: easy (straightforward calculations, single step)
- Questions 13-25: medium (multi-step, word problems with Seychelles context)
- Questions 26-35: hard (complex problems, reverse/inverse, problem solving)

Generate the 35 questions now:"""

EXIT_TICKET_PROMPT = """Generate a mixed-format question bank (35 questions) for a summative assessment (exit ticket) on this lesson.

LESSON: {lesson_title}
OBJECTIVE: {lesson_objective}
SUBJECT: {subject}
{exam_context}{seychelles_context}
QUESTION FORMAT MIX — generate in THIS EXACT ORDER:
Questions 1-5: FILL_IN_BLANK (sentence with blanks to complete)
Questions 6-9: MATCHING (match terms to definitions)
Questions 10-13: SHORT_ANSWER (1-3 sentence written response)
Questions 14-35: MCQ (multiple choice, 4 options each)

YOU MUST generate ALL 4 types. DO NOT generate any data_interpretation
questions — that type is disabled platform-wide. The first 13 questions
MUST NOT be MCQ.

REQUIREMENTS:
1. Generate EXACTLY 35 questions in the format mix above
2. Each question MUST have:
   - concept_tag: MUST be the EXACT TEXT of one of the ENABLING OBJECTIVES listed below (not a short label — use the full objective text so questions link directly to teaching steps)
   - terminal_objective: the exact terminal objective this question assesses (from the lesson objectives)
   - enabling_objective: same as concept_tag — the specific enabling objective this question tests
3. EVERY terminal objective must be assessed by at least 2 questions
4. EVERY enabling objective must be assessed by at least 1 question
4. Use context relevant to Seychelles secondary school students
5. Vary question phrasing — avoid repetitive stems

OUTPUT FORMAT (JSON array — each question has a "question_type" field):

MCQ format:
{{"question_type": "mcq", "question": "What is...?", "option_a": "...", "option_b": "...", "option_c": "...", "option_d": "...", "correct": "B", "explanation": "...", "difficulty": "easy", "concept_tag": "Define the terms Development, Globalization, MEDC, NIC, LEDC"}}

FILL_IN_BLANK format:
{{"question_type": "fill_in_blank", "question": "Complete the sentence:", "answer_data": {{"text_template": "The ___ of a country is measured using ___ per capita figures.", "blanks": ["GNP", "US dollar"], "accept_alternatives": [["gross national product", "Gross National Product"], ["USD", "American dollar"]]}}, "explanation": "...", "difficulty": "easy", "concept_tag": "EXACT TEXT OF AN ENABLING OBJECTIVE FROM THE LIST ABOVE"}}

MATCHING format:
{{"question_type": "matching", "question": "Match each term to its definition:", "answer_data": {{"pairs": [{{"left": "GNP", "right": "Total value of goods and services"}}, {{"left": "HDI", "right": "Measure combining health, education, income"}}], "distractor_rights": ["Population growth rate"]}}, "explanation": "...", "difficulty": "medium", "concept_tag": "EXACT TEXT OF AN ENABLING OBJECTIVE FROM THE LIST ABOVE"}}

SHORT_ANSWER format:
{{"question_type": "short_answer", "question": "Explain why HDI is considered a better measure of development than GNP.", "answer_data": {{"model_answer": "HDI is better because it measures health, education and income, not just economic output.", "keywords": ["health", "education", "income", "not just economic"], "min_keywords": 2}}, "explanation": "...", "difficulty": "hard", "concept_tag": "EXACT TEXT OF AN ENABLING OBJECTIVE FROM THE LIST ABOVE"}}

DATA_INTERPRETATION is DISABLED. Do not generate questions of that type.

NO FIGURES — questions are TEXT-ONLY. Do NOT emit `figure_spec`,
`figure`, `plot_spec`, `figure_svg`, `figure_url`, or any inline
<svg>. The teacher can attach a generated image (gpt-image-2)
to any question after the fact via the question editor.

`data_description` content (when used for non-figure tabular
reference data) uses inline styles only — no external CSS, no
scripts. All HTML renders in a sandboxed iframe.

DIFFICULTY DISTRIBUTION (out of 35):
- Questions 1-12: easy (recall facts)
- Questions 13-25: medium (apply concepts)
- Questions 26-35: hard (analyze/evaluate)

Generate the 35 questions now:"""


class Command(BaseCommand):
    help = 'Generate standardized exit tickets for lessons using AI'

    def add_arguments(self, parser):
        parser.add_argument(
            '--lesson',
            type=int,
            help='Generate exit ticket for a specific lesson ID',
        )
        parser.add_argument(
            '--course',
            type=str,
            help='Generate exit tickets for all lessons in a course',
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Generate exit tickets for all lessons without one',
        )
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='Overwrite existing exit tickets',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be generated without saving',
        )

    def handle(self, *args, **options):
        # Determine which lessons to process
        if options['lesson']:
            lessons = Lesson.objects.filter(id=options['lesson'])
            if not lessons.exists():
                raise CommandError(f"Lesson {options['lesson']} not found")
        elif options['course']:
            lessons = Lesson.objects.filter(
                unit__course__title__icontains=options['course'],
                is_published=True
            )
        elif options['all']:
            # All published lessons without exit tickets
            existing_ids = ExitTicket.objects.values_list('lesson_id', flat=True)
            lessons = Lesson.objects.filter(is_published=True).exclude(id__in=existing_ids)
        else:
            raise CommandError("Specify --lesson, --course, or --all")
        
        self.stdout.write(f"Found {lessons.count()} lessons to process")
        
        for lesson in lessons:
            self.stdout.write(f"\nProcessing: {lesson.title}")
            
            # Check if exit ticket exists
            existing = ExitTicket.objects.filter(lesson=lesson).first()
            if existing and not options['overwrite']:
                self.stdout.write(self.style.WARNING(f"  ⏭️  Skipped (already has exit ticket)"))
                continue
            
            try:
                if options['dry_run']:
                    self.stdout.write(self.style.SUCCESS(f"  ✓ Would generate exit ticket"))
                    continue

                # Use the shared generation function
                from apps.curriculum.content_generator import generate_exit_ticket_for_lesson
                from apps.accounts.models import Institution
                institution_id = (lesson.unit.course.institution_id
                                  if lesson.unit and lesson.unit.course else None)
                institution_id = institution_id or Institution.get_global().id

                # Delete existing if overwriting
                if existing and options['overwrite']:
                    existing.delete()

                result = generate_exit_ticket_for_lesson(lesson, institution_id)

                if result.get('success'):
                    count = result.get('questions_created', 0)
                    if result.get('skipped'):
                        self.stdout.write(self.style.WARNING(f"  ⏭️  Skipped (already has exit ticket)"))
                    else:
                        self.stdout.write(self.style.SUCCESS(f"  ✓ Created exit ticket with {count} questions"))
                else:
                    self.stdout.write(self.style.ERROR(f"  ✗ Error: {result.get('error')}"))

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  ✗ Error: {e}"))

        self.stdout.write(self.style.SUCCESS("\nDone!"))

    # Exit ticket generation logic is in apps.curriculum.content_generator.generate_exit_ticket_for_lesson()