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
Questions 10-12: SHORT_ANSWER (multi-step calculation requiring working out)
Questions 13-15: DATA_INTERPRETATION (read data from table/chart and calculate)
Questions 16-35: MCQ (multiple choice with numerical/algebraic options)

YOU MUST generate ALL 5 types. The first 15 questions MUST NOT be MCQ.

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

DATA_INTERPRETATION format (PREFERRED — interactive plot via plot_spec):
{{"question_type": "data_interpretation", "question": "Study the chart of weekly fish prices and calculate the average:", "answer_data": {{"plot_spec": {{"type": "line", "title": "Tuna prices in Seychelles, weeks 1–6", "x_label": "Week", "y_label": "Price (SCR / kg)", "labels": ["W1", "W2", "W3", "W4", "W5", "W6"], "datasets": [{{"label": "Tuna", "data": [42, 45, 50, 48, 46, 44]}}], "source": "Hypothetical data"}}, "model_answer": "Total = 42+45+50+48+46+44 = 275. Mean = 275 / 6 ≈ 45.83 SCR/kg", "keywords": ["275", "45.83", "mean"], "min_keywords": 2}}, "explanation": "Sum the weekly prices and divide by 6", "difficulty": "medium", "concept_tag": "EXACT TEXT OF AN ENABLING OBJECTIVE"}}

DATA_INTERPRETATION fallback (only for genuinely tabular non-chartable data):
{{"question_type": "data_interpretation", "question": "Study the data and calculate:", "answer_data": {{"data_description": "<table style='width:100%;border-collapse:collapse'>...</table>", "model_answer": "...", "keywords": [...], "min_keywords": 2}}, "explanation": "...", "difficulty": "medium", "concept_tag": "..."}}

PLOT_SPEC SCHEMA (for the math exit ticket too — interactive Chart.js):
- type ∈ {{"bar","line","pie","doughnut","scatter"}}
- For non-scatter: provide labels (categorical x-axis) + each dataset has data: [number, ...] aligned with labels.
- For scatter: each dataset has points: [[x,y], [x,y], ...] (omit data + labels).
- Always include a meaningful title. Bar/line/scatter should also include x_label and y_label.
- Numbers must be plain numbers, no commas, no units inside the value (units belong in y_label).

VISUAL CONTENT — MANDATORY FOR GRAPH/GEOMETRY/DATA TOPICS:
For questions about scatter diagrams, graphs, geometry, or data:
- MCQ questions MUST include a "source" field with an SVG diagram showing the visual
- DATA_INTERPRETATION questions MUST include SVG charts in data_description
- Use the source field for MCQ: {{"question_type": "mcq", "source": "<svg>...</svg>", "question": "Based on the diagram above...", ...}}

SVG GRIDLINE REQUIREMENT (MANDATORY for any scatter/coordinate/line plot):
All scatter plots, coordinate grids, and line graphs MUST include:
- Visible gridlines at every major tick on BOTH axes (stroke='#d4d4d8' stroke-width='1')
- Tick marks with numeric labels on BOTH axes
- Plot area large enough that gridlines are readable (min 300x240)
Gridlines make points readable. A plot without gridlines is unacceptable — the student cannot read coordinates off a blank background.

SVG EXAMPLES:
Scatter diagram with plotted points (NOTE the gridlines — copy this pattern):
<svg width='360' height='290' xmlns='http://www.w3.org/2000/svg'><rect x='40' y='10' width='300' height='240' fill='#fafafa' stroke='#e4e4e7'/><line x1='70' y1='10' x2='70' y2='250' stroke='#d4d4d8' stroke-width='1'/><line x1='100' y1='10' x2='100' y2='250' stroke='#d4d4d8' stroke-width='1'/><line x1='130' y1='10' x2='130' y2='250' stroke='#d4d4d8' stroke-width='1'/><line x1='160' y1='10' x2='160' y2='250' stroke='#d4d4d8' stroke-width='1'/><line x1='190' y1='10' x2='190' y2='250' stroke='#d4d4d8' stroke-width='1'/><line x1='220' y1='10' x2='220' y2='250' stroke='#d4d4d8' stroke-width='1'/><line x1='250' y1='10' x2='250' y2='250' stroke='#d4d4d8' stroke-width='1'/><line x1='280' y1='10' x2='280' y2='250' stroke='#d4d4d8' stroke-width='1'/><line x1='310' y1='10' x2='310' y2='250' stroke='#d4d4d8' stroke-width='1'/><line x1='340' y1='10' x2='340' y2='250' stroke='#d4d4d8' stroke-width='1'/><line x1='40' y1='50' x2='340' y2='50' stroke='#d4d4d8' stroke-width='1'/><line x1='40' y1='90' x2='340' y2='90' stroke='#d4d4d8' stroke-width='1'/><line x1='40' y1='130' x2='340' y2='130' stroke='#d4d4d8' stroke-width='1'/><line x1='40' y1='170' x2='340' y2='170' stroke='#d4d4d8' stroke-width='1'/><line x1='40' y1='210' x2='340' y2='210' stroke='#d4d4d8' stroke-width='1'/><line x1='40' y1='250' x2='340' y2='250' stroke='#18181b' stroke-width='2'/><line x1='40' y1='250' x2='40' y2='10' stroke='#18181b' stroke-width='2'/><text x='180' y='280' font-size='11' text-anchor='middle'>Temperature (°C)</text><text x='15' y='130' font-size='11' text-anchor='middle' transform='rotate(-90,15,130)'>Fish catch (kg)</text><circle cx='80' cy='210' r='4' fill='#3b82f6'/><circle cx='120' cy='180' r='4' fill='#3b82f6'/><circle cx='180' cy='140' r='4' fill='#3b82f6'/><circle cx='240' cy='90' r='4' fill='#3b82f6'/><circle cx='300' cy='50' r='4' fill='#3b82f6'/><text x='35' y='265' font-size='10'>0</text><text x='100' y='265' font-size='10'>5</text><text x='160' y='265' font-size='10'>10</text><text x='220' y='265' font-size='10'>15</text><text x='280' y='265' font-size='10'>20</text><text x='28' y='253' font-size='10'>0</text><text x='20' y='213' font-size='10'>20</text><text x='20' y='173' font-size='10'>40</text><text x='20' y='133' font-size='10'>60</text><text x='20' y='93' font-size='10'>80</text><text x='15' y='53' font-size='10'>100</text></svg>

Coordinate grid (NOTE the gridlines):
<svg width='260' height='260' xmlns='http://www.w3.org/2000/svg'><rect x='30' y='10' width='210' height='210' fill='#fafafa' stroke='#e4e4e7'/><line x1='72' y1='10' x2='72' y2='220' stroke='#d4d4d8' stroke-width='1'/><line x1='114' y1='10' x2='114' y2='220' stroke='#d4d4d8' stroke-width='1'/><line x1='156' y1='10' x2='156' y2='220' stroke='#d4d4d8' stroke-width='1'/><line x1='198' y1='10' x2='198' y2='220' stroke='#d4d4d8' stroke-width='1'/><line x1='240' y1='10' x2='240' y2='220' stroke='#d4d4d8' stroke-width='1'/><line x1='30' y1='178' x2='240' y2='178' stroke='#d4d4d8' stroke-width='1'/><line x1='30' y1='136' x2='240' y2='136' stroke='#d4d4d8' stroke-width='1'/><line x1='30' y1='94' x2='240' y2='94' stroke='#d4d4d8' stroke-width='1'/><line x1='30' y1='52' x2='240' y2='52' stroke='#d4d4d8' stroke-width='1'/><line x1='30' y1='10' x2='240' y2='10' stroke='#d4d4d8' stroke-width='1'/><line x1='30' y1='220' x2='240' y2='220' stroke='#18181b' stroke-width='2'/><line x1='30' y1='220' x2='30' y2='10' stroke='#18181b' stroke-width='2'/><text x='25' y='235' font-size='10'>0</text><text x='67' y='235' font-size='10'>2</text><text x='109' y='235' font-size='10'>4</text><text x='151' y='235' font-size='10'>6</text><text x='193' y='235' font-size='10'>8</text><text x='232' y='235' font-size='10'>10</text><text x='18' y='223' font-size='10'>0</text><text x='18' y='181' font-size='10'>2</text><text x='18' y='139' font-size='10'>4</text><text x='18' y='97' font-size='10'>6</text><text x='18' y='55' font-size='10'>8</text><text x='13' y='13' font-size='10'>10</text></svg>

Geometric shape:
<svg width='200' height='150' xmlns='http://www.w3.org/2000/svg'><rect x='30' y='30' width='120' height='80' fill='none' stroke='#18181b' stroke-width='2'/><text x='70' y='25' font-size='12'>8 cm</text><text x='155' y='75' font-size='12'>5 cm</text></svg>

AT LEAST 5 of the 20 MCQ questions (Q16-35) must include a source diagram.
ALL 3 DATA_INTERPRETATION questions (Q13-15) must include a visual.

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
Questions 10-12: SHORT_ANSWER (1-3 sentence written response)
Questions 13-15: DATA_INTERPRETATION (analyze data then answer)
Questions 16-35: MCQ (multiple choice, 4 options each)

YOU MUST generate ALL 5 types. The first 15 questions MUST NOT be MCQ.

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

DATA_INTERPRETATION format (PREFERRED — interactive plot):
{{"question_type": "data_interpretation", "question": "Study the chart and answer:", "answer_data": {{"plot_spec": {{"type": "bar", "title": "GNP and HDI for three countries", "x_label": "Country", "y_label": "Value", "labels": ["Country A", "Country B", "Country C"], "datasets": [{{"label": "GNP ($)", "data": [500, 2000, 800]}}, {{"label": "HDI (×1000)", "data": [400, 650, 750]}}], "source": "Hypothetical data"}}, "model_answer": "Country C has the highest HDI despite not having the highest GNP, showing that wealth alone does not determine development.", "keywords": ["Country C", "highest HDI", "wealth", "development"], "min_keywords": 2}}, "explanation": "...", "difficulty": "hard", "concept_tag": "EXACT TEXT OF AN ENABLING OBJECTIVE FROM THE LIST ABOVE"}}

DATA_INTERPRETATION fallback (when a plot doesn't fit — use ONLY when the data is intrinsically tabular and not chartable):
{{"question_type": "data_interpretation", "question": "Study the table and answer:", "answer_data": {{"data_description": "<table style='width:100%;border-collapse:collapse'>...</table>", "model_answer": "...", "keywords": [...], "min_keywords": 2}}, "explanation": "...", "difficulty": "hard", "concept_tag": "..."}}

RICH VISUAL CONTENT REQUIREMENTS:
1. DATA_INTERPRETATION questions MUST include either a `plot_spec` (preferred) OR a `data_description`:
   - PREFERRED: emit a `plot_spec` JSON object that the frontend renders as an interactive Chart.js plot.
   - `plot_spec.type` ∈ {{"bar","line","pie","doughnut","scatter"}}
   - For non-scatter: `labels` is the categorical axis; each dataset has `data: [number, ...]` matching `labels` length.
   - For scatter: each dataset has `points: [[x,y], [x,y], ...]` instead of `data`.
   - Always include a meaningful `title`. For bar/line/scatter, include `x_label` and `y_label`.
   - Numbers must be plain numbers (no commas, no units in the value).
   - Cite a `source` when the data is from a real dataset; otherwise leave blank or set to "Hypothetical data".
   - FALLBACK to `data_description` (HTML table or `<img>` reference) only when a plot genuinely cannot represent the data.

2. At least 2 of the 3 DATA_INTERPRETATION questions must include a `plot_spec` chart (the third can be tabular if the data is non-chartable).

3. Do NOT use SVG bar charts in `data_description` — Chart.js plot_spec replaces them.

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