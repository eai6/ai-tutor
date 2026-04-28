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

DATA_INTERPRETATION format — emit a structured `figure_spec`; the server renders it as a correct SVG chart:
{{"question_type": "data_interpretation", "question": "Study the chart of weekly fish prices and calculate the average:", "answer_data": {{"figure_spec": {{"type": "line", "title": "Tuna prices in Seychelles, weeks 1-6", "x_label": "Week", "y_label": "Price (SCR / kg)", "labels": ["W1", "W2", "W3", "W4", "W5", "W6"], "datasets": [{{"label": "Tuna", "data": [42, 45, 50, 48, 46, 44]}}], "source": "Hypothetical data"}}, "model_answer": "Total = 42+45+50+48+46+44 = 275. Mean = 275 / 6 ≈ 45.83 SCR/kg", "keywords": ["275", "45.83", "mean"], "min_keywords": 2}}, "explanation": "Sum the weekly prices and divide by 6", "difficulty": "medium", "concept_tag": "EXACT TEXT OF AN ENABLING OBJECTIVE"}}

DATA_INTERPRETATION fallback (only for genuinely tabular non-chartable data, e.g. 2-column reference tables):
{{"question_type": "data_interpretation", "question": "Study the data and calculate:", "answer_data": {{"data_description": "<table style='width:100%;border-collapse:collapse'>...</table>", "model_answer": "...", "keywords": [...], "min_keywords": 2}}, "explanation": "...", "difficulty": "medium", "concept_tag": "..."}}

FIGURE CATALOG — the ONLY valid figures. Server renders each from a
structured spec. NEVER emit raw <svg>. NEVER reference curriculum-page
or worksheet images. NEVER set figure_url. The catalog is closed: if
no kind fits the question, OMIT the figure.

Set `answer_data.figure_spec` to {{ "kind": "<one_of_below>", ...fields }}.

CHARTS — for data:
  bar, line, pie, doughnut, scatter, histogram

GEOMETRY — math shapes:
  rectangle {{width,height,units?}}, square {{side,units?}},
  triangle {{type:"right"|"equilateral"|"isoceles"|"scalene", sides?[3], angles?[3], units?}},
  circle {{radius? OR diameter?, units?}}, regular_polygon {{sides:3-12, side_length?}},
  parallelogram {{base,height}}, trapezium {{a,b,h}},
  cuboid {{length,width,height}}, cylinder {{radius,height}},
  compound_shape {{parts:[{{kind:"rectangle",x,y,width,height}}, ...]}}

ANGLES — geometry-of-angles questions:
  angle {{degrees}},
  straight_line_angles {{angles:[]}} (sum 180),
  point_angles {{angles:[], labels?}} (sum 360 — USE THIS for "angles around a point", NOT bar),
  triangle_angles {{angles:[3]}} (sum 180),
  parallel_lines {{configuration:"alternate"|"corresponding"|"co-interior", known_angle}},
  polygon_angles {{sides:3-12}}

NUMBER & COORDS:
  number_line {{min,max,step?,marks?:[{{value,label?,type?}}]}},
  fraction_bar {{numerator,denominator}},
  coordinate_grid {{xmin,xmax,ymin,ymax,points?,lines?,curves?}}

STATS:
  box_plot {{min,q1,median,q3,max}},
  stem_leaf {{rows:[{{stem,leaves:[]}}], key?}},
  pictogram {{rows:[{{label,count}}], symbol?, key?}}

GEOGRAPHY (mostly static — `kind` alone is enough):
  earth_layers, volcano_cross,
  plate_boundary {{type:"convergent"|"divergent"|"transform"}},
  river_profile {{stage:"upper"|"middle"|"lower"}},
  meander_oxbow,
  coastal_features {{feature:"headland_bay"|"cliff_platform"|"spit"|"stack_arch"}},
  weathering {{type:"freeze_thaw"|"exfoliation"|"chemical"}},
  rock_cycle, water_cycle, compass_rose, seychelles_map, lat_long_grid

Rules:
- Numbers are plain numbers — no commas, no units inside values.
- pie / point_angles values must sum to 100 (or 360 for angles).
- For charts (bar/line/scatter/histogram) include a `title`; bar/line
  also include `x_label`, `y_label`.
- **NEVER REVEAL THE ANSWER in the figure or data_description**. If the
  question asks the student to *calculate* X, the figure / table shown
  to them MUST NOT include X. Examples:
    - Q: "Calculate the area of each plot and find the largest."
      Show only Length and Width columns. NEVER include an Area column.
    - Q: "What is the average tuna price across the 6 weeks?"
      Show the 6 weekly prices. NEVER show the average.
    - Q: "Find the missing angle x in this triangle (other angles 48° and 67°)."
      Label only 48° and 67° on the figure. NEVER label x.
  If the data inherently contains the answer (e.g. "which student
  scored highest?" with a score table), that's fine — but a calculate-
  the-result question must hide the result.

- **GEOMETRY ≠ BAR CHART**. Do NOT use bar/line charts to display
  angles, side lengths, or other geometric measurements. Anti-patterns
  (DO NOT generate these):
    - "Bar chart of two angles per triangle" — use a table
      (data_description) OR one `triangle` figure per question (with
      angle labels), one question per triangle.
    - "Bar chart of side lengths" — use a `triangle` / `rectangle` /
      `polygon` figure with sides labelled.
    - "Bar chart of angle measurements per shape" — useless visual,
      doesn't communicate geometry. Use the geometry templates.
  Bar/line/scatter are for *quantitative comparison data* (populations,
  prices, scores). They are NOT for showing values that belong on a
  geometric figure.

CHART-TYPE GUIDE — pick the right shape, not just any shape:
- pie: parts of a whole. Values sum to a meaningful total (100% of a budget,
  360° of angles around a point, market share, percentage breakdown).
  IF VALUES SUM TO 100 OR 360, USE PIE — NEVER bar.
- bar: comparing magnitudes that don't share a total (population by city,
  test score per student, height of buildings).
- line: a trend over an ordered axis, usually time (prices over weeks,
  temperature over months).
- scatter: relationship / correlation between two quantitative variables.

VISUAL CONTENT RULES:
- Chartable data → emit `figure_spec` with the right chart type per the guide above.
- NOT EVERY FIGURE IS A CHART. For geometry questions (a specific angle's
  measure, a triangle's interior angles, a labelled rectangle/circle),
  emit a precise inline SVG inside `data_description` rather than forcing a
  bar/line `figure_spec` that won't communicate geometry. EXCEPTION: when
  the question is "angles around a point sum to 360°" or any sectors-of-a-
  circle question, use `figure_spec.type='pie'` — pie correctly visualises
  proportions of a circle.
- Non-chartable tabular reference data → emit `data_description` (HTML table only).
- DO NOT emit inline `<svg>` charts (bar/line/pie). DO NOT emit `plot_spec` (legacy name).

Geometric shape (math only — inline SVG inside `data_description`):
<svg width='220' height='180' xmlns='http://www.w3.org/2000/svg'><rect x='30' y='30' width='120' height='80' fill='none' stroke='#18181b' stroke-width='2'/><text x='70' y='25' font-size='12'>8 cm</text><text x='155' y='75' font-size='12'>5 cm</text></svg>

ALL 3 DATA_INTERPRETATION questions (Q13-15) must include a `figure_spec` OR a `data_description` (HTML table or inline geometry SVG).

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

DATA_INTERPRETATION format — emit a structured `figure_spec`; the server renders it as a correct SVG chart:
{{"question_type": "data_interpretation", "question": "Study the chart and answer:", "answer_data": {{"figure_spec": {{"type": "bar", "title": "GNP and HDI for three countries", "x_label": "Country", "y_label": "Value", "labels": ["Country A", "Country B", "Country C"], "datasets": [{{"label": "GNP ($)", "data": [500, 2000, 800]}}, {{"label": "HDI (×1000)", "data": [400, 650, 750]}}], "source": "Hypothetical data"}}, "model_answer": "Country C has the highest HDI despite not having the highest GNP, showing that wealth alone does not determine development.", "keywords": ["Country C", "highest HDI", "wealth", "development"], "min_keywords": 2}}, "explanation": "...", "difficulty": "hard", "concept_tag": "EXACT TEXT OF AN ENABLING OBJECTIVE FROM THE LIST ABOVE"}}

DATA_INTERPRETATION fallback (use ONLY when the data is intrinsically a tabular reference, not a chart):
{{"question_type": "data_interpretation", "question": "Study the table and answer:", "answer_data": {{"data_description": "<table style='width:100%;border-collapse:collapse'>...</table>", "model_answer": "...", "keywords": [...], "min_keywords": 2}}, "explanation": "...", "difficulty": "hard", "concept_tag": "..."}}

FIGURE CATALOG — the ONLY valid figures. Server renders each from a
structured spec. NEVER emit raw <svg>. NEVER reference curriculum-page
or worksheet images. NEVER set figure_url. The catalog is closed: if
no kind fits the question, OMIT the figure.

Set `answer_data.figure_spec` to {{ "kind": "<one_of_below>", ...fields }}.

CHARTS — for data:
  bar, line, pie, doughnut, scatter, histogram

GEOMETRY — math shapes:
  rectangle {{width,height,units?}}, square {{side,units?}},
  triangle {{type:"right"|"equilateral"|"isoceles"|"scalene", sides?[3], angles?[3], units?}},
  circle {{radius? OR diameter?, units?}}, regular_polygon {{sides:3-12, side_length?}},
  parallelogram {{base,height}}, trapezium {{a,b,h}},
  cuboid {{length,width,height}}, cylinder {{radius,height}},
  compound_shape {{parts:[{{kind:"rectangle",x,y,width,height}}, ...]}}

ANGLES — geometry-of-angles questions:
  angle {{degrees}},
  straight_line_angles {{angles:[]}} (sum 180),
  point_angles {{angles:[], labels?}} (sum 360 — USE THIS for "angles around a point", NOT bar),
  triangle_angles {{angles:[3]}} (sum 180),
  parallel_lines {{configuration:"alternate"|"corresponding"|"co-interior", known_angle}},
  polygon_angles {{sides:3-12}}

NUMBER & COORDS:
  number_line {{min,max,step?,marks?:[{{value,label?,type?}}]}},
  fraction_bar {{numerator,denominator}},
  coordinate_grid {{xmin,xmax,ymin,ymax,points?,lines?,curves?}}

STATS:
  box_plot {{min,q1,median,q3,max}},
  stem_leaf {{rows:[{{stem,leaves:[]}}], key?}},
  pictogram {{rows:[{{label,count}}], symbol?, key?}}

GEOGRAPHY (mostly static — `kind` alone is enough):
  earth_layers, volcano_cross,
  plate_boundary {{type:"convergent"|"divergent"|"transform"}},
  river_profile {{stage:"upper"|"middle"|"lower"}},
  meander_oxbow,
  coastal_features {{feature:"headland_bay"|"cliff_platform"|"spit"|"stack_arch"}},
  weathering {{type:"freeze_thaw"|"exfoliation"|"chemical"}},
  rock_cycle, water_cycle, compass_rose, seychelles_map, lat_long_grid

Rules:
- Numbers are plain numbers — no commas, no units inside values.
- pie / point_angles values must sum to 100 (or 360 for angles).
- For charts (bar/line/scatter/histogram) include a `title`; bar/line
  also include `x_label`, `y_label`.
- **NEVER REVEAL THE ANSWER in the figure or data_description**. If the
  question asks the student to *calculate* X, the figure / table shown
  to them MUST NOT include X. Examples:
    - Q: "Calculate the area of each plot and find the largest."
      Show only Length and Width columns. NEVER include an Area column.
    - Q: "What is the average tuna price across the 6 weeks?"
      Show the 6 weekly prices. NEVER show the average.
    - Q: "Find the missing angle x in this triangle (other angles 48° and 67°)."
      Label only 48° and 67° on the figure. NEVER label x.
  If the data inherently contains the answer (e.g. "which student
  scored highest?" with a score table), that's fine — but a calculate-
  the-result question must hide the result.

- **GEOMETRY ≠ BAR CHART**. Do NOT use bar/line charts to display
  angles, side lengths, or other geometric measurements. Anti-patterns
  (DO NOT generate these):
    - "Bar chart of two angles per triangle" — use a table
      (data_description) OR one `triangle` figure per question (with
      angle labels), one question per triangle.
    - "Bar chart of side lengths" — use a `triangle` / `rectangle` /
      `polygon` figure with sides labelled.
    - "Bar chart of angle measurements per shape" — useless visual,
      doesn't communicate geometry. Use the geometry templates.
  Bar/line/scatter are for *quantitative comparison data* (populations,
  prices, scores). They are NOT for showing values that belong on a
  geometric figure.

CHART-TYPE GUIDE — pick the right shape, not just any shape:
- pie: parts of a whole. Values sum to a meaningful total (100% of a budget,
  360° of angles around a point, market share, percentage breakdown).
  IF VALUES SUM TO 100 OR 360, USE PIE — NEVER bar.
- bar: comparing magnitudes that don't share a total (population by city,
  test score per student, height of buildings).
- line: a trend over an ordered axis, usually time (prices over weeks,
  temperature over months).
- scatter: relationship / correlation between two quantitative variables.

VISUAL CONTENT RULES:
1. DATA_INTERPRETATION questions MUST have either a `figure_spec` (chart),
   `data_description` (HTML table), or, for geometry questions, an inline
   geometry SVG (see rule 3). They must NOT be all-text.

2. NOT EVERY FIGURE IS A CHART. If the question is about **geometry** (a
   specific angle's measure, a triangle's interior angles, a labelled
   rectangle/circle), do NOT emit a bar/line/scatter `figure_spec`. Either:
     a) Emit a small inline geometry SVG in `data_description` (preferred
        for visualising one specific shape; see rule 3), OR
     b) Use `figure_spec.type='pie'` if the question is "angles around a
        point sum to 360°" or "sectors of a circle" — that IS proportional
        data and pie charts visualise it correctly.
   Bar charts of "angle in degrees per zone" do not communicate angle —
   skip them.

3. For MATH geometry shapes (specific angles, triangles, rectangles,
   circles, polygons): emit precise inline SVG inside `data_description`.
   Position every element with calculated coordinates; never invent positions.
   Example: <svg width='220' height='220' xmlns='http://www.w3.org/2000/svg'>
     <rect x='30' y='30' width='120' height='80' fill='none' stroke='#18181b' stroke-width='2'/>
     <text x='70' y='25' font-size='12'>8 cm</text>
     <text x='155' y='75' font-size='12'>5 cm</text>
   </svg>

4. DO NOT emit inline `<svg>` chart code (bar/line/pie). DO NOT emit
   `plot_spec` (legacy — use `figure_spec`). Charts are rendered by the
   server from `figure_spec`, not drawn by the LLM.

5. For GEOGRAPHY subjects: use `figure_spec` for any chart-able data. Use
   pie when values sum to 100%. For raw tables of facts, use
   `data_description` HTML tables.

6. For MCQ questions that need a chart: set `figure_spec` in answer_data so
   the server renders an SVG. Apply the same chart-type guide above.
   {{"question_type": "mcq", "question": "Based on the chart above, which country...", "answer_data": {{"figure_spec": {{"type": "bar", ...}}}}, "option_a": "...", ...}}

6. `data_description` content uses inline styles ONLY (no external CSS, no scripts). All HTML renders in a sandboxed iframe.

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