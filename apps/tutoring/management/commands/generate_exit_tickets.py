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


EXIT_TICKET_PROMPT = """Generate a mixed-format question bank (35 questions) for a summative assessment (exit ticket) on this lesson.

LESSON: {lesson_title}
OBJECTIVE: {lesson_objective}
SUBJECT: {subject}
{exam_context}{seychelles_context}
QUESTION FORMAT MIX (EXACTLY 35 questions total):
- 20 MCQ (multiple choice, 4 options each)
- 5 FILL_IN_BLANK (sentence with blanks to complete)
- 4 MATCHING (match terms to definitions)
- 3 SHORT_ANSWER (1-3 sentence written response)
- 3 DATA_INTERPRETATION (analyze data then answer)

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
{{"question_type": "mcq", "question": "What is...?", "option_a": "...", "option_b": "...", "option_c": "...", "option_d": "...", "correct": "B", "explanation": "...", "difficulty": "easy", "concept_tag": "..."}}

FILL_IN_BLANK format:
{{"question_type": "fill_in_blank", "question": "Complete the sentence:", "answer_data": {{"text_template": "The ___ of a country is measured using ___ per capita figures.", "blanks": ["GNP", "US dollar"], "accept_alternatives": [["gross national product", "Gross National Product"], ["USD", "American dollar"]]}}, "explanation": "...", "difficulty": "easy", "concept_tag": "..."}}

MATCHING format:
{{"question_type": "matching", "question": "Match each term to its definition:", "answer_data": {{"pairs": [{{"left": "GNP", "right": "Total value of goods and services"}}, {{"left": "HDI", "right": "Measure combining health, education, income"}}], "distractor_rights": ["Population growth rate"]}}, "explanation": "...", "difficulty": "medium", "concept_tag": "..."}}

SHORT_ANSWER format:
{{"question_type": "short_answer", "question": "Explain why HDI is considered a better measure of development than GNP.", "answer_data": {{"model_answer": "HDI is better because it measures health, education and income, not just economic output.", "keywords": ["health", "education", "income", "not just economic"], "min_keywords": 2}}, "explanation": "...", "difficulty": "hard", "concept_tag": "..."}}

DATA_INTERPRETATION format:
{{"question_type": "data_interpretation", "question": "Study the data below and answer:", "answer_data": {{"data_description": "<table style='width:100%;border-collapse:collapse'><tr style='background:#f4f4f5'><th style='padding:8px 12px;border:1px solid #e4e4e7'>Country</th><th style='padding:8px 12px;border:1px solid #e4e4e7'>GNP ($)</th><th style='padding:8px 12px;border:1px solid #e4e4e7'>HDI</th></tr><tr><td style='padding:8px 12px;border:1px solid #e4e4e7'>Country A</td><td style='padding:8px 12px;border:1px solid #e4e4e7'>500</td><td style='padding:8px 12px;border:1px solid #e4e4e7'>0.40</td></tr><tr><td style='padding:8px 12px;border:1px solid #e4e4e7'>Country B</td><td style='padding:8px 12px;border:1px solid #e4e4e7'>2,000</td><td style='padding:8px 12px;border:1px solid #e4e4e7'>0.65</td></tr><tr><td style='padding:8px 12px;border:1px solid #e4e4e7'>Country C</td><td style='padding:8px 12px;border:1px solid #e4e4e7'>800</td><td style='padding:8px 12px;border:1px solid #e4e4e7'>0.75</td></tr></table>", "model_answer": "Country C has the highest HDI despite not having the highest GNP, showing that wealth alone does not determine development.", "keywords": ["Country C", "highest HDI", "wealth", "development"], "min_keywords": 2}}, "explanation": "...", "difficulty": "hard", "concept_tag": "..."}}

RICH VISUAL CONTENT REQUIREMENTS:
1. DATA_INTERPRETATION questions MUST include rich HTML in data_description:
   - Use HTML tables with inline styles for data (population stats, GNP figures, trade data, etc.)
   - Use <img> tags to reference figures from uploaded textbooks/worksheets when available
   - Create SVG diagrams for simple visuals (bar charts, pie charts, maps) using inline SVG
   Example SVG bar chart:
   <svg width='300' height='150' xmlns='http://www.w3.org/2000/svg'><rect x='20' y='20' width='40' height='100' fill='#3b82f6'/><text x='30' y='140' font-size='10'>A</text><rect x='80' y='50' width='40' height='70' fill='#10b981'/><text x='90' y='140' font-size='10'>B</text><rect x='140' y='80' width='40' height='40' fill='#f59e0b'/><text x='150' y='140' font-size='10'>C</text></svg>

2. At least 2 of the 3 DATA_INTERPRETATION questions must include a visual (table, chart, or diagram)

3. For MATH subjects: include SVG diagrams where relevant:
   - Number lines for number/fraction questions
   - Geometric shapes with labeled dimensions for area/perimeter questions
   - Coordinate grids for algebra/graph questions
   Example: <svg width='200' height='200' xmlns='http://www.w3.org/2000/svg'><rect x='30' y='30' width='120' height='80' fill='none' stroke='#18181b' stroke-width='2'/><text x='70' y='25' font-size='12'>8 cm</text><text x='155' y='75' font-size='12'>5 cm</text></svg>

4. For GEOGRAPHY subjects: include data tables with real Seychelles/world data, and reference any available textbook figures via <img> tags

5. For MCQ questions that involve reading a source: add a "source" field with HTML content:
   {{"question_type": "mcq", "question": "Based on the source above, which country...", "source": "<table>...</table>", "option_a": "...", ...}}

6. Use inline styles ONLY (no external CSS or scripts). All HTML renders in a sandboxed iframe.

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