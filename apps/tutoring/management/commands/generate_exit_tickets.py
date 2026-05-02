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

═══════════════════════════════════════════════════════════════════════
MANDATORY: every math question MUST emit a `template` object.
═══════════════════════════════════════════════════════════════════════

You DO NOT compute numeric answers yourself. You define HOW to compute
them as a `template` (parameter ranges + formula). The backend
samples parameter values from the ranges and runs the formula in code.
This means arithmetic errors are impossible — the answer is whatever
the formula evaluates to with the chosen parameters, by construction.

A question without a `template` is REJECTED. There is no "free-form"
math escape hatch.

For each question you emit:
  • Pick a question pattern that fits the lesson objective.
  • Write the `template_text` with named slots in {{single braces}}.
  • Declare the `parameters` (name → type / min / max / step).
  • Write `answer_formula` — a pure arithmetic expression in those
    parameter names that the backend computes deterministically.
  • Write `explanation_template` — same slot syntax, plus the
    special slot {{answer}} which gets filled with the computed
    answer at render time.
  • Optionally declare `constraints` — boolean expressions that
    must hold over the parameters; the backend re-samples until
    they do.

═══════════════════════════════════════════════════════════════════════
ALLOWED FORMULA SYNTAX
═══════════════════════════════════════════════════════════════════════

Operators:  +  -  *  /  //  %  **  ( )

Functions (positional args only — no kwargs):
  Powers / roots:  sqrt, pow, exp, log, ln, log10, log2
  Trig:            sin, cos, tan, asin, acos, atan, atan2,
                   sinh, cosh, tanh, radians, degrees
  Comparison:      min, max, abs, clamp(x, lo, hi)
  Rounding:        round, floor, ceil, trunc
  Number theory:   gcd, lcm, factorial
  Aggregates:      sum, len, mean, median, mode, stdev, variance
                   (use with [list, of, params] notation)

Constants: pi, e, tau

Anything outside this whitelist (attribute access, list comprehensions,
custom functions, lambda, builtins like `eval`, etc.) is rejected by
the formula sandbox and will fail validation.

═══════════════════════════════════════════════════════════════════════
WORKED EXAMPLES (5 patterns covering different shapes)
═══════════════════════════════════════════════════════════════════════

1) PURE SUM — angles around a point:
{{"question_type": "short_numeric", "concept_tag": "broad learning objective", "enabling_objective": "EXACT EO TEXT", "difficulty": "easy",
  "template": {{
    "template_text": "Three angles around a point are {{a}}°, {{b}}°, and x°. Find x.",
    "parameters": {{
      "a": {{"type": "int", "min": 30, "max": 150, "step": 5}},
      "b": {{"type": "int", "min": 30, "max": 150, "step": 5}}
    }},
    "answer_formula": "360 - a - b",
    "answer_unit": "°",
    "explanation_template": "Angles around a point sum to 360°. x = 360 - {{a}} - {{b}} = {{answer}}.",
    "constraints": ["a + b < 350"]
  }}}}

2) PRODUCT — area of a rectangle:
{{"question_type": "short_numeric", "concept_tag": "broad learning objective", "enabling_objective": "EXACT EO TEXT", "difficulty": "easy",
  "template": {{
    "template_text": "A rectangular plot is {{l}} m long and {{w}} m wide. Find its area.",
    "parameters": {{
      "l": {{"type": "int", "min": 4, "max": 30}},
      "w": {{"type": "int", "min": 4, "max": 30}}
    }},
    "answer_formula": "l * w",
    "answer_unit": " m²",
    "explanation_template": "Area = length × width = {{l}} × {{w}} = {{answer}}.",
    "constraints": ["l != w"]
  }}}}

3) LINEAR EQUATION — solve ax + b = c:
{{"question_type": "short_numeric", "concept_tag": "broad learning objective", "enabling_objective": "EXACT EO TEXT", "difficulty": "medium",
  "template": {{
    "template_text": "Solve for x: {{a}}x + {{b}} = {{c}}.",
    "parameters": {{
      "a": {{"type": "int", "min": 2, "max": 9}},
      "b": {{"type": "int", "min": 1, "max": 20}},
      "c": {{"type": "int", "min": 10, "max": 80}}
    }},
    "answer_formula": "(c - b) / a",
    "explanation_template": "Rearrange: x = (c - b) / a = ({{c}} - {{b}}) / {{a}} = {{answer}}.",
    "constraints": ["(c - b) % a == 0", "c > b"]
  }}}}

4) PERCENT — percent of a Seychelles-context value:
{{"question_type": "short_numeric", "concept_tag": "broad learning objective", "enabling_objective": "EXACT EO TEXT", "difficulty": "medium",
  "template": {{
    "template_text": "A fisherman sells {{kg}} kg of tuna. He gives {{p}}% to the cooperative. How many kg does he keep?",
    "parameters": {{
      "kg": {{"type": "int", "min": 20, "max": 200, "step": 10}},
      "p": {{"type": "int", "min": 5, "max": 40, "step": 5}}
    }},
    "answer_formula": "kg * (100 - p) / 100",
    "answer_unit": " kg",
    "explanation_template": "He keeps (100 - {{p}})% = {{kg}} × (100 - {{p}}) / 100 = {{answer}}.",
    "constraints": ["(kg * (100 - p)) % 100 == 0"]
  }}}}

5) PYTHAGORAS — hypotenuse via sqrt:
{{"question_type": "short_numeric", "concept_tag": "broad learning objective", "enabling_objective": "EXACT EO TEXT", "difficulty": "medium",
  "template": {{
    "template_text": "A right triangle has legs of {{a}} and {{b}}. Find the hypotenuse.",
    "parameters": {{
      "a": {{"type": "int", "min": 3, "max": 15}},
      "b": {{"type": "int", "min": 3, "max": 15}}
    }},
    "answer_formula": "sqrt(a*a + b*b)",
    "explanation_template": "By Pythagoras: c = √({{a}}² + {{b}}²) = √({{a}}² + {{b}}²) ≈ {{answer}}.",
    "constraints": ["a != b"]
  }}}}

═══════════════════════════════════════════════════════════════════════
ADDITIONAL TEMPLATE TYPES (MCQ / fill_in_blank / matching / short_answer)
═══════════════════════════════════════════════════════════════════════

Beyond `short_numeric`, four more templated formats are available.
EACH is fully computable — the backend renders the prose AND
computes the correct answer from your formula(s). Pick whichever
format best fits the question pattern.

6) MCQ — templated multiple choice (4 options, 1 correct, 3 distractors)
{{"question_type": "mcq", "concept_tag": "broad learning objective", "enabling_objective": "EXACT EO TEXT", "difficulty": "easy",
  "template": {{
    "template_text": "Three angles around a point are {{a}}°, {{b}}°, and x°. What is x?",
    "parameters": {{
      "a": {{"type": "int", "min": 30, "max": 150, "step": 5}},
      "b": {{"type": "int", "min": 30, "max": 150, "step": 5}}
    }},
    "correct_formula": "360 - a - b",
    "distractor_formulas": ["a + b - 90", "180 - a + b", "a * 2 + b"],
    "answer_unit": "°",
    "explanation_template": "x = 360 - {{a}} - {{b}} = {{answer}}.",
    "constraints": ["a + b < 350"]
  }}}}

  Distractor formulas MUST yield values DIFFERENT from
  correct_formula across all parameter samples (the validator
  rejects "plausible" distractors that secretly equal the correct
  answer for some sample). The backend randomises which letter
  (A/B/C/D) the correct answer lands at per render.

7) FILL_IN_BLANK — one or more `___` slots, each with its own formula
{{"question_type": "fill_in_blank", "concept_tag": "broad learning objective", "enabling_objective": "EXACT EO TEXT", "difficulty": "medium",
  "template": {{
    "template_text": "Two angles around a point are {{a}}° and {{b}}°. The third angle is ___° and the sum of all three angles is ___°.",
    "parameters": {{
      "a": {{"type": "int", "min": 30, "max": 150, "step": 5}},
      "b": {{"type": "int", "min": 30, "max": 150, "step": 5}}
    }},
    "blank_formulas": ["360 - a - b", "360"],
    "answer_unit": "°",
    "explanation_template": "Third angle = 360° - {{a}}° - {{b}}° = {{answer}}.",
    "constraints": ["a + b < 350"]
  }}}}

  Number of `___` in template_text MUST equal len(blank_formulas).

8) MATCHING — N pairs (left ↔ right) sampled from one formula pair
{{"question_type": "matching", "concept_tag": "broad learning objective", "enabling_objective": "EXACT EO TEXT", "difficulty": "medium",
  "template": {{
    "framing_text": "Match each angle pair to its sum.",
    "parameters": {{
      "a": {{"type": "int", "min": 10, "max": 80, "step": 5}},
      "b": {{"type": "int", "min": 10, "max": 80, "step": 5}}
    }},
    "pair_count": 4,
    "left_formula": "{{a}}° + {{b}}°",
    "right_formula": "a + b",
    "answer_unit": "°",
    "distractor_count": 2,
    "explanation_template": "Each right is the sum of the two angles on its left."
  }}}}

  pair_count is 4-6. left_formula is a STRING template (uses
  {{param}} substitution); right_formula is ARITHMETIC. The
  backend samples N times to produce distinct pairs + extra
  distractor right-side options.

9) SHORT_ANSWER — two-field: deterministic final answer + LLM-reviewed working
{{"question_type": "short_answer", "concept_tag": "broad learning objective", "enabling_objective": "EXACT EO TEXT", "difficulty": "hard",
  "template": {{
    "template_text": "Three angles around a point are {{a}}°, {{b}}°, and x°. Find x and show your working.",
    "parameters": {{
      "a": {{"type": "int", "min": 30, "max": 150, "step": 5}},
      "b": {{"type": "int", "min": 30, "max": 150, "step": 5}}
    }},
    "final_answer_formula": "360 - a - b",
    "canonical_working": "Step 1: Angles around a point sum to 360°. Step 2: x = 360° - {{a}}° - {{b}}° = {{answer}}.",
    "answer_unit": "°",
    "constraints": ["a + b < 350"]
  }}}}

  The student fills TWO boxes: a final-answer box (graded
  deterministically vs final_answer_formula) and a working box
  (LLM-reviewed against canonical_working — the LLM compares the
  student's prose against your reference text, never authoring its
  own answer).

═══════════════════════════════════════════════════════════════════════
QUESTION TYPE FIELD + FORMAT MIX
═══════════════════════════════════════════════════════════════════════

Locked format mix for math exit tickets (35 total):
  - 15 MCQ              (question_type: "mcq")
  - 7  fill_in_blank    (question_type: "fill_in_blank")
  - 6  matching         (question_type: "matching")
  - 7  short_numeric    (question_type: "short_numeric")

Note: short_answer is OPTIONAL — use it sparingly when a question
genuinely benefits from showing working AND the canonical_working
is precise enough for the LLM to compare against.

If a question genuinely cannot be templated (e.g., qualitative
reasoning, proof, multi-step word problem with branching), it is
NOT appropriate for a math exit ticket. SKIP it. The bank target
is 35; the floor is 25 — better fewer high-quality templated
questions than free-form math the system can't verify.

═══════════════════════════════════════════════════════════════════════
REQUIREMENTS
═══════════════════════════════════════════════════════════════════════
1. Generate up to 35 questions, ALL templated. Bank floor is 25.
   Format mix: 15 MCQ / 7 fill_in_blank / 6 matching / 7 short_numeric.
2. Each question MUST have BOTH:
   - concept_tag: broad learning objective (the lesson's main objective)
   - enabling_objective: EXACT TEXT of one ENABLING OBJECTIVE from below
     (the specific sub-skill — copy verbatim). This is the field
     remediation uses to target the failing sub-skill.
3. EVERY enabling objective must be assessed by at least 1 question
4. Use Seychelles context in word problems (SCR prices, fish catches, island areas)
5. Vary difficulty: easy calculations → harder numbers → word problems → multi-step
6. NO data_interpretation. NO figures (text-only).
7. Diversify the templates — don't emit 35 sum-to-360 questions.
   Use the full range of patterns the lesson objective covers.

DIFFICULTY DISTRIBUTION (out of 35):
- Questions 1-12: easy (single-step formula, small integers)
- Questions 13-25: medium (multi-step formula, Seychelles context)
- Questions 26-35: hard (compound formulas, reverse/inverse, sqrt/percent)

Generate the question bank now (JSON array of templated questions):"""

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
   - concept_tag: the BROAD learning objective the question is grouped
     under. Use the exact text of the lesson's main learning objective.
   - enabling_objective: the SPECIFIC enabling objective (sub-objective)
     this question tests. MUST be the EXACT TEXT of one of the ENABLING
     OBJECTIVES listed below — copy verbatim, including capitalisation
     and punctuation. This is the field remediation uses to target the
     failing sub-skill, so getting it right matters.
   - terminal_objective: the terminal objective this question assesses
     (from the lesson objectives, optional).
3. EVERY enabling objective must be assessed by at least 1 question
   — distribute coverage across all of them.
4. enabling_objective and concept_tag are DIFFERENT fields:
     - concept_tag is the BROAD grouping (the learning objective)
     - enabling_objective is the NARROW sub-objective (one of EO1..EON)
   Do not put the same value in both.
4. Use context relevant to Seychelles secondary school students
5. Vary question phrasing — avoid repetitive stems

OUTPUT FORMAT (JSON array — each question has a "question_type" field):

MCQ format:
{{"question_type": "mcq", "question": "What is...?", "option_a": "...", "option_b": "...", "option_c": "...", "option_d": "...", "correct": "B", "explanation": "...", "difficulty": "easy", "concept_tag": "Understand development indicators", "enabling_objective": "Define the terms Development, Globalization, MEDC, NIC, LEDC"}}

FILL_IN_BLANK format:
{{"question_type": "fill_in_blank", "question": "Complete the sentence:", "answer_data": {{"text_template": "The ___ of a country is measured using ___ per capita figures.", "blanks": ["GNP", "US dollar"], "accept_alternatives": [["gross national product", "Gross National Product"], ["USD", "American dollar"]]}}, "explanation": "...", "difficulty": "easy", "concept_tag": "broad learning objective text", "enabling_objective": "EXACT TEXT OF ONE EO FROM THE LIST ABOVE"}}

MATCHING format:
{{"question_type": "matching", "question": "Match each term to its definition:", "answer_data": {{"pairs": [{{"left": "GNP", "right": "Total value of goods and services"}}, {{"left": "HDI", "right": "Measure combining health, education, income"}}], "distractor_rights": ["Population growth rate"]}}, "explanation": "...", "difficulty": "medium", "concept_tag": "broad learning objective text", "enabling_objective": "EXACT TEXT OF ONE EO FROM THE LIST ABOVE"}}

SHORT_ANSWER format:
{{"question_type": "short_answer", "question": "Explain why HDI is considered a better measure of development than GNP.", "answer_data": {{"model_answer": "HDI is better because it measures health, education and income, not just economic output.", "keywords": ["health", "education", "income", "not just economic"], "min_keywords": 2}}, "explanation": "...", "difficulty": "hard", "concept_tag": "broad learning objective text", "enabling_objective": "EXACT TEXT OF ONE EO FROM THE LIST ABOVE"}}

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