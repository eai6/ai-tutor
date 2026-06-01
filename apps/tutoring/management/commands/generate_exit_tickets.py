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
CONCEPTUAL INTEGRITY (READ BEFORE WRITING ANY QUESTION)
═══════════════════════════════════════════════════════════════════════

A question is BROKEN if its premise contradicts the lesson's core rule,
even when the arithmetic "works out". The student is being taught the
RULE; the question's setup must respect it.

Rules of thumb:

A. If the lesson states "X equals Y" as a fact (e.g., angles around a
   point = 360°, angles on a straight line = 180°, interior angles of
   a triangle = 180°), then EVERY problem premise must be consistent
   with that fact. Do NOT pose "Three angles around a point are 120°,
   130°, 140°" — that sum is 390° and violates the rule.

B. NEVER ask "What is the sum of these angles?" when the lesson's
   defining rule already gives that sum. The answer is the rule
   itself, not arithmetic on the given values. Acceptable framings:
   "Find the missing angle x" / "Each of N equal angles measures…" /
   "If two angles are A and B, what is the third…".

C. For templated math, ENFORCE the integrity rule in the
   `constraints` list. Examples:

   - Lesson: angles around a point.
     Template: "Three angles around a point are {{a}}°, {{b}}°, and x°. Find x."
     constraints MUST include: ["a + b < 360", "a > 0", "b > 0"]
     (so a missing angle exists AND every value is positive).

   - Lesson: angles around a point, equal partition.
     Template: "{{n}} equal angles around a point. What is each?"
     constraints: ["360 % n == 0"] when integer answers are required,
     or simply ["n >= 2"] when decimals are acceptable.

   - Lesson: angles on a straight line.
     Template: "Two angles on a straight line are {{a}}° and x°. Find x."
     constraints: ["a > 0", "a < 180"] (so x exists and is positive).

D. If you cannot write a constraint that PROVES the premise is
   consistent with the lesson's rule, the template is unsafe — pick
   a different question shape.

═══════════════════════════════════════════════════════════════════════
COMMON FAILURE MODES — fix these BEFORE you emit the template
═══════════════════════════════════════════════════════════════════════

The validator rejects templates that fall into the following traps.
Reading these will save you from re-trying.

P1. EXPLANATION SLOT SYNTAX. `explanation_template` slots are
    DECLARED PARAMETER NAMES ONLY, plus the special `{{answer}}` slot.
    You may NOT put arithmetic inside slot braces.
    ❌ "By Pythagoras: c = √({{a*a}} + {{b*b}}) = {{answer}}"
       (rejected: 'a*a' is not a parameter)
    ✓ "By Pythagoras: c = √({{a}}² + {{b}}²) ≈ {{answer}}"
       (the ² is just a literal character; {{a}} and {{b}} reference
       declared parameters; {{answer}} reads the computed value)
    ✓ "Each interior angle = 180 × ({{n}} - 2) ÷ {{n}} = {{answer}}°"
       (n is a declared param; arithmetic stays OUTSIDE the braces
       as plain text)

P2. CONSTRAINT-RANGE CONSISTENCY. Your `constraints` list must be
    SATISFIABLE given your `parameters` ranges. The renderer samples
    parameter values from the ranges and rejects those that violate
    the constraints; if your ranges and constraints disagree, every
    sample fails.
    ❌ parameters: {{"a": {{"min": 10, "max": 50}}}}, constraints: ["a > 100"]
       (rejected: max(a)=50 makes "a > 100" unsatisfiable)
    ❌ parameters: {{"angle": {{"min": 30, "max": 150}}}}, constraints:
       ["360 % (180 - angle) == 0"]
       (rejected: only a few angles in [30,150] satisfy this — the
       sampler can't find one in 50 tries)
    ✓ parameters: {{"a": {{"min": 110, "max": 170}}}}, constraints: ["a > 100"]
       (range and constraint agree)
    ✓ Use simpler constraints like ["a > 0", "a + b < 350"] over
       complex divisibility/modulo predicates that prune the
       sample space too aggressively.

P3. TRIANGLE-INEQUALITY TEMPLATES. Don't pose "find the area of a
    triangle with sides a, b, c" with random sides — most random
    triples violate a + b > c. Use a different formulation:
    ❌ parameters: a/b/c each in [3, 30], constraint:
       "a + b > c and b + c > a and a + c > b"
       (most samples violate the inequality; rejected after 50 tries)
    ✓ Pose Heron's law on a known-valid triple (e.g., let
       a=base, b=height, derived) OR use a different shape (rectangle,
       trapezoid with explicit parallel sides + height) where the
       parameters are independent.

P4. CO-INTERIOR / SAME-SIDE INTERIOR ANGLES. "Two parallel lines
    cut by a transversal — find the co-interior angle to a°" needs
    a constraint like "a > 0 and a < 180" — but if your parameter
    range is already (10, 170) the constraint is redundant. Drop the
    redundant constraint OR widen the parameter range.

P5. POLYGON SIDE-COUNT QUESTIONS. "Find the number of sides given
    each interior angle of θ°" only has integer solutions for
    θ ∈ {{60, 90, 108, 120, 128.57, 135, 140, 144, 147.27, 150, ...}}.
    If you sample θ ∈ [60, 170], most samples don't yield integer
    sides. Either restrict parameters to a hand-picked list (use
    "step": that lands on valid values) OR pick a different
    question shape (give n, find θ — always integer).

P6. SLOT NAMES vs ANSWER LABELS. The `{{answer}}` slot in
    `explanation_template` is the SINGLE computed answer (or, for
    fill_in_blank, "answer1 / answer2 / …"). Don't reference
    `{{angle}}`, `{{result}}`, or any made-up name. Only declared
    parameters and `{{answer}}`.

═══════════════════════════════════════════════════════════════════════
REQUIREMENTS
═══════════════════════════════════════════════════════════════════════
1. Generate EXACTLY 35 question_type: "mcq" entries, ALL templated.
   Updated 2026-06-01: math exit tickets are MCQ-only — fill_in_blank,
   matching, and short_numeric formats are DISABLED. Pick MCQ
   templates that combine the formula system (parameters +
   answer_formula + distractor_formulas) with 4 student-visible
   options A/B/C/D.

   Use the MCQ worked example pattern: declare `parameters`,
   `template_text`, `answer_formula`, four `distractor_formulas`
   (each a pure arithmetic expression in the same parameters),
   and the `correct` letter A/B/C/D. The backend samples
   parameters, computes the correct answer + three distractors,
   shuffles them into A/B/C/D, and records which slot ends up
   correct.

2. Each question MUST have BOTH:
   - concept_tag: broad learning objective (the lesson's main objective)
   - enabling_objective: EXACT TEXT of one ENABLING OBJECTIVE from below
     (the specific sub-skill — copy verbatim). This is the field
     remediation uses to target the failing sub-skill.
3. EVERY enabling objective must be assessed by at least 1 question
4. Use context appropriate to the student's setting. Vary phrasing.
5. Vary difficulty: easy calculations → harder numbers → word problems → multi-step
6. NO data_interpretation. NO figures (text-only).
7. Diversify the templates — don't emit 35 sum-to-360 questions.
   Use the full range of patterns the lesson objective covers.
8. Every template's `constraints` list must enforce conceptual
   integrity (see CONCEPTUAL INTEGRITY above). A template without
   guard constraints WILL be rejected.

<distribution>
The correct-answer letter must spread roughly evenly across A, B, C,
and D over the 35 templated MCQs. Target: 8-9 correct per letter.
Hard cap: no letter exceeds 11 correct answers (~31% of the bank).
Audits found ~60% of correct answers landing on B in prior generations
because format examples used B as a placeholder — do NOT let that
placeholder bias the actual `correct` values you emit. After drafting,
tally the `correct` field across all 35 and re-letter overflowing
questions (swap which slot is correct; the underlying formula stays
the same). The post-gen verifier
(apps/curriculum/mcq_distribution.py) also runs and logs a warning
if any letter exceeds 35%.
</distribution>

DIFFICULTY DISTRIBUTION (out of 35):
- Questions 1-12: easy (single-step formula, small integers)
- Questions 13-25: medium (multi-step formula, applied context)
- Questions 26-35: hard (compound formulas, reverse/inverse, sqrt/percent)

Generate the question bank now (JSON array of 35 templated MCQs):"""

# ──────────────────────────────────────────────────────────────────────
# EXIT_TICKET_PROMPT_V2 (2026-06-01) — MCQ-ONLY exit tickets.
#
# Major rewrite per Edward's directive: "exit tickets from now on
# should only be mcq questions … make sure the proportions are not
# biased to one letter like b". Three changes vs the prior format:
#
#   1. MCQ-only output. Drops fill_in_blank, matching, short_answer.
#      Why: the tutoring engine grades MCQs deterministically (letter
#      match — Tier 1 grader, perfect agreement with intent); the
#      other formats need the embedding-gate / verifier-LLM tier
#      which surfaces partial-verdict edge cases. For pilot scale we
#      want the cleaner signal.
#   2. XML-structured system prompt (per claude-prompting-expert
#      conventions): <role>, <rules>, <distribution>,
#      <output_format>, <enabling_objectives_block>. Mirrors the
#      shape of apps/tutoring/simple_tutor/prompts.py for parity.
#   3. Stronger A/B/C/D balance rule with explicit two-pass approach:
#      (i) write the bank caring only about pedagogical correctness;
#      (ii) self-audit by tallying letter counts; (iii) re-letter
#      any block of options exceeding a threshold by permuting which
#      option ends up at A/B/C/D. The post-gen verifier in
#      apps/curriculum/mcq_distribution.py also runs and logs a
#      warning if any letter still exceeds 35% of the bank.
#
# The locale block (apps/curriculum/locale_prompts.py) gets appended
# to the SYSTEM portion at the caller — not baked in here — so the
# template stays locale-agnostic.
# ──────────────────────────────────────────────────────────────────────

EXIT_TICKET_PROMPT = """Generate a 35-question MCQ-only question bank for a summative assessment (exit ticket) on this lesson.

<lesson>
LESSON: {lesson_title}
OBJECTIVE: {lesson_objective}
SUBJECT: {subject}
{exam_context}{seychelles_context}
</lesson>

<rules>
- Generate EXACTLY 35 multiple-choice questions. No other formats —
  no fill_in_blank, no matching, no short_answer, no
  data_interpretation, no short_numeric (those types are disabled
  platform-wide for exit tickets as of 2026-06-01).
- Each MCQ has FOUR options labelled A, B, C, D — exactly four, never
  three, never five.
- Each MCQ has EXACTLY ONE correct answer. The other three options
  are PLAUSIBLE distractors (common misconceptions or near-misses),
  not obvious filler.
- Each question MUST carry both:
    - concept_tag: the BROAD learning objective. Use the exact text
      of the lesson's main learning objective.
    - enabling_objective: the SPECIFIC sub-skill. MUST be the EXACT
      TEXT of one of the ENABLING OBJECTIVES listed below — copy
      verbatim, including capitalisation and punctuation. This is
      what the remediation flow uses to target the failing
      sub-skill, so getting it right matters.
- concept_tag and enabling_objective are DIFFERENT fields. concept_tag
  is the broad grouping; enabling_objective is the narrow sub-skill.
  Do not put the same value in both.
- EVERY enabling objective must be assessed by at least 1 question
  — distribute coverage across all of them.
- Use context appropriate to the student's setting. Vary question
  phrasing — avoid repetitive stems.
- NO FIGURES — questions are TEXT-ONLY. Do NOT emit `figure_spec`,
  `figure`, `plot_spec`, `figure_svg`, `figure_url`, or any inline
  <svg>. Teachers attach a generated image after the fact via the
  question editor when needed.
</rules>

<distribution>
The correct-answer letter MUST be spread roughly evenly across A, B, C,
and D over the 35 questions. Target: 8-9 correct answers per letter.
Hard cap: no letter exceeds 11 correct answers (≈ 31% of the bank).

This is non-negotiable. Audits of prior generations found ~60% of
correct answers landed on B because format examples used B as a
placeholder. To prevent that bias:

  1. First draft: write each question caring ONLY about pedagogical
     correctness — pick the answer the student should know,
     regardless of which letter it ends up on.
  2. After drafting all 35: TALLY how many correct answers landed
     at A, B, C, D respectively.
  3. If any letter has more than 11 correct answers, RE-LETTER the
     overflowing questions by swapping the correct option with one
     of the distractors. The correct CONTENT stays the same — only
     which letter (A/B/C/D) it sits at changes. Re-tally and repeat
     until every letter has at most 11.

DIFFICULTY DISTRIBUTION (out of 35):
- Questions 1-12: easy (recall facts, definitions, simple
  identification)
- Questions 13-25: medium (apply concepts, interpret examples)
- Questions 26-35: hard (analyze, evaluate, multi-step reasoning)
</distribution>

<output_format>
Return a JSON array of 35 objects. Each object has exactly these
fields:

  {{
    "question_type": "mcq",
    "question": "<the stem>",
    "option_a": "<text>",
    "option_b": "<text>",
    "option_c": "<text>",
    "option_d": "<text>",
    "correct": "<A|B|C|D>",
    "explanation": "<one-sentence why-the-correct-is-correct>",
    "difficulty": "<easy|medium|hard>",
    "concept_tag": "<broad learning objective>",
    "enabling_objective": "<EXACT verbatim sub-skill from list>"
  }}

NOTE on `"correct"`: the literal letter you write here determines
the answer key. Do NOT default to "B" — pick the letter that holds
the correct answer for THIS question after you've applied the
distribution discipline in <distribution> above.
</output_format>

Generate the 35 MCQs now:"""


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