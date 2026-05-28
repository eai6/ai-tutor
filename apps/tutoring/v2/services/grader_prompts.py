"""Prompts consumed by StudentGrader — Phase 2 §2.1.

Kept separate from the service logic so the prompts can be audited,
versioned, and unit-tested independently.

Design conventions, grounded in ``prompting-fundamentals-expert`` +
``gemini-prompting-expert`` (CLAUDE.md non-negotiable):

  - Direct task statement, no flowery role priming. Gemini 3 docs
    warn against persona priming; the same principle ports cleanly
    to Claude / OpenAI / Llama.
  - Positive instructions ("respond with X"), not negative
    ("don't…"). Gemini's docs explicitly warn negatives "over-index"
    and hurt arithmetic/logic.
  - Query at the END of long contexts (KB chunks, transcript). The
    pre-pose prompt + non-math grounded prompt both place the
    decision question after the source material.
  - Structured JSON output via ``response_schema`` / ``output_config``
    when shape matters (the DSL extraction prompt). All prompts that
    expect JSON also include the schema in the instructions so model
    families without constrained decoding still produce valid JSON.
  - Strict ternary verdicts (CORRECT | PARTIAL | WRONG): the grader
    MUST return one of these three for every gradable student turn.
    Tie-break biases (PARTIAL over WRONG when uncertain; PARTIAL
    over CORRECT when uncertain) are stated in each verdict-emitting
    prompt below per v2-prune-plan §4.1.
"""

from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────
# Math path — DSL extraction
# ──────────────────────────────────────────────────────────────────────

MATH_DSL_SYSTEM = """\
You decompose a math problem into a constrained JSON DSL that a
Python interpreter executes.

Output a JSON object with these top-level keys:

  variables  — a mapping of variable names to numeric values that
               appear in the visible problem text. Every value must
               be derivable from the problem statement; do not invent
               numbers that are not named or implied by the problem.

  expression — (single-answer problems) a tree of operations.
  expressions — (multi-answer problems) an array of named expression
                trees, one per required answer slot. Use this form
                when the problem asks for more than one numeric value
                (e.g. "Calculate the loss amount AND the loss
                percentage", "Find the area AND the perimeter"). Each
                entry is an object: {"name": "<short slot name>",
                "expression": <node>}. Slot names should be plain
                words a student would use: "loss_amount",
                "loss_percentage", "area", "perimeter", etc.

  Provide EITHER ``expression`` OR ``expressions`` — not both. Use
  ``expressions`` whenever the problem text names two or more
  distinct quantities the student must produce.

Each expression node is ONE of:
  * a bare number (e.g. 42, 3.14)
  * a variable reference: {"var": "name"}
  * an operation: {"op": "<opcode>", "args": [<node>, ...]}
  * a solve: {"op": "solve", "equation": "<eq>", "var": "<name>"}

Whitelisted opcodes: add, sub, mul, div, neg, abs, pow, sqrt, log,
exp, sin, cos, tan, min, max, round, eq, lt, lte, gt, gte, solve.

For ``solve``, use sympy-compatible syntax in ``equation``
(e.g. "2*x + 3 = 11"). The ``var`` field names the variable to
solve for.

Return JSON only — no prose, no markdown fences.
"""

# Few-shot examples — pinned format. Per gemini-prompting-expert:
# Gemini follows the format exactly including punctuation quirks.
# Per prompting-fundamentals-expert: keep recency-bias in mind, place
# the most representative example last.
MATH_DSL_FEW_SHOT = """\
Example 1
Problem: What is 12 + 13?
Output: {"variables": {"a": 12, "b": 13}, "expression": {"op": "add", "args": [{"var": "a"}, {"var": "b"}]}}

Example 2
Problem: The angles in a triangle sum to 180°. Two of the angles are 40° and 75°. What is the third angle?
Output: {"variables": {"total": 180, "a": 40, "b": 75}, "expression": {"op": "sub", "args": [{"var": "total"}, {"var": "a"}, {"var": "b"}]}}

Example 3
Problem: Solve for x:  2x + 3 = 11.
Output: {"variables": {}, "expression": {"op": "solve", "equation": "2*x + 3 = 11", "var": "x"}}

Example 4
Problem: A trader buys spices for 120 SCR per package and sells them for 90 SCR per package. Calculate the loss amount and the loss percentage.
Output: {"variables": {"cp": 120, "sp": 90}, "expressions": [{"name": "loss_amount", "expression": {"op": "sub", "args": [{"var": "cp"}, {"var": "sp"}]}}, {"name": "loss_percentage", "expression": {"op": "mul", "args": [{"op": "div", "args": [{"op": "sub", "args": [{"var": "cp"}, {"var": "sp"}]}, {"var": "cp"}]}, 100]}}]}
"""


def render_math_dsl_user_prompt(problem_text: str) -> str:
    """Render the user-turn prompt for math DSL extraction.

    Query placed AFTER the few-shot block per prompting-fundamentals
    structure-conventions guidance (role → task → examples → input).
    """
    return (
        f"{MATH_DSL_FEW_SHOT}\n"
        f"Problem: {problem_text.strip()}\n"
        f"Output:"
    )


# ──────────────────────────────────────────────────────────────────────
# Student-claims extractor (LLM-B in the Two-LLM math grader)
#
# See design/tasks/two-llm-grader-implementation-plan.md.
#
# Pairs with MATH_DSL_SYSTEM (LLM-A canonical extractor). LLM-A reads
# the QUESTION and emits a canonical value; LLM-B reads the STUDENT
# response and emits a claim graph the Python comparator verifies.
# Together they replace the regex-based _parse_student_math_value
# chain that mis-parsed word-form numerics ("eight"), multi-slot prose
# ("profit is 9 and percentage is 50%"), and intermediate-vs-final
# values ("… got 16 … is eight").
#
# Schema (subject-agnostic):
#   variables    — any numeric values the student named while working.
#   claims       — discrete arithmetic / logical assertions the student
#                  made, each with an expression tree (reusing the
#                  MATH_DSL_SYSTEM grammar) and an asserted value so the
#                  comparator can detect a step-level arithmetic error
#                  separately from a conclusion-level error.
#   conclusion   — the student's final stated answer:
#                    answer_extracted_value  scalar | [scalar, ...] | null
#                    answer_extracted_label  "yes" | "no" | "A".."D"
#                                             | "" (when not applicable)
#                    statement               short prose summary
#                    is_attempt              false for meta input
#                                             ("idk", "explain please")
#   domain_check_required — true when the answer needs a domain check
#                           (e.g. negative length rejected). Reserved.
# ──────────────────────────────────────────────────────────────────────

STUDENT_CLAIMS_SYSTEM = """\
You read a student's response to a math problem and emit a structured
JSON object that a deterministic Python comparator can verify.

Output a single JSON object with these top-level keys:

  variables  — a mapping of variable names to numeric values the
               student named while working (may be empty).

  claims     — an ordered list of the student's individual arithmetic
               or logical steps. Each entry is an object:

                 {
                   "id": "c1",
                   "description": "<one short phrase, what the
                                   student claimed in this step>",
                   "expression": <DSL node>,
                   "asserted_value": <number>
                 }

               The ``expression`` field uses the same DSL grammar as
               the question-side canonical extractor:

                 * a bare number (e.g. 25, 3.14)
                 * a variable reference: {"var": "name"}
                 * an operation: {"op": "<opcode>", "args": [<node>, ...]}

               Whitelisted opcodes: add, sub, mul, div, neg, abs,
               pow, sqrt, log, exp, sin, cos, tan, min, max, round,
               eq, lt, lte, gt, gte.

               The ``asserted_value`` is the result the STUDENT
               claimed in that step (not the result Python would
               compute) — that's how the comparator detects a
               specific arithmetic mistake.

               Use an empty list when the student gave only a final
               answer with no shown working.

  conclusion — an object describing the student's final stated answer:

                 {
                   "statement": "<one short phrase paraphrasing the
                                  student's bottom-line answer>",
                   "answer_extracted_value": <number | [n, n, ...] | null>,
                   "answer_extracted_label": "<yes|no|true|false|A|B|C|D|>",
                   "is_attempt": <true|false>
                 }

               ``answer_extracted_value`` is the numeric value(s) the
               student stated as their final answer. For multi-slot
               questions where the student supplies two or more
               distinct quantities (e.g. "profit is 9 and percentage
               is 50%"), emit a list. Use null when the student gave
               no numeric answer.

               ``answer_extracted_label`` is filled for Yes/No,
               True/False, or single-letter multiple-choice answers.
               Leave empty otherwise. Normalise to lowercase for
               yes/no/true/false and uppercase A-D for MCQ letters.

               ``is_attempt`` is false when the student did NOT
               attempt an answer — e.g. "I don't know", "please
               explain", "what does percent mean?", "give me a hint".
               Set to true otherwise, even for clearly wrong attempts.

  domain_check_required — boolean. True only when the student named
               a value that violates the problem's domain (e.g.
               negative length, fractional person count). Default
               false.

Read the student response charitably. Treat word-form numerics as
their numeric equivalent:

  * "eight" → 8
  * "twenty-five" → 25
  * "half" → 0.5
  * "two and a half" → 2.5
  * "thirty-three percent" → 33

When the student shows working and then states a different final
answer, the final stated answer goes in ``conclusion``; each working
step goes in ``claims``. Do NOT confuse intermediate working values
with the student's bottom-line answer.

Return JSON only — no prose, no markdown fences.
"""

# Few-shot examples. Per gemini-prompting-expert: Gemini follows the
# format exactly including punctuation quirks. Per prompting-fundamentals
# guidance: place the most representative example last (recency bias).
STUDENT_CLAIMS_FEW_SHOT = """\
Example 1 (bare numeric — correct addition)
Question: What is 12 + 13?
Student: 25
Output: {"variables": {}, "claims": [], "conclusion": {"statement": "25", "answer_extracted_value": 25, "answer_extracted_label": "", "is_attempt": true}, "domain_check_required": false}

Example 2 (word-form answer with intermediate working)
Question: 2x = 16. Solve for x.
Student: I multiplied the variable by two and got 16 which means that the hidden variable is eight
Output: {"variables": {"x": 8}, "claims": [{"id": "c1", "description": "2 times 8 equals 16", "expression": {"op": "mul", "args": [2, 8]}, "asserted_value": 16}], "conclusion": {"statement": "the hidden variable is eight", "answer_extracted_value": 8, "answer_extracted_label": "", "is_attempt": true}, "domain_check_required": false}

Example 3 (Pythagoras proof — claims with conclusion label)
Question: Sides 5, 7, 9 — is the triangle right-angled?
Student: 5^2 + 7^2 = 25 + 49 = 74, 9^2 = 81, 74 != 81 so NOT right-angled.
Output: {"variables": {}, "claims": [{"id": "c1", "description": "5 squared is 25", "expression": {"op": "pow", "args": [5, 2]}, "asserted_value": 25}, {"id": "c2", "description": "7 squared is 49", "expression": {"op": "pow", "args": [7, 2]}, "asserted_value": 49}, {"id": "c3", "description": "25 plus 49 is 74", "expression": {"op": "add", "args": [25, 49]}, "asserted_value": 74}, {"id": "c4", "description": "9 squared is 81", "expression": {"op": "pow", "args": [9, 2]}, "asserted_value": 81}], "conclusion": {"statement": "not right-angled", "answer_extracted_value": null, "answer_extracted_label": "no", "is_attempt": true}, "domain_check_required": false}

Example 4 (multi-slot prose — two values)
Question: Buys 18 SCR, sells 27 SCR. Find profit per item and profit percentage.
Student: profit is 9 and percentage is 50%
Output: {"variables": {}, "claims": [], "conclusion": {"statement": "profit 9 and percentage 50", "answer_extracted_value": [9, 50], "answer_extracted_label": "", "is_attempt": true}, "domain_check_required": false}

Example 5 (meta input — not an attempt)
Question: Solve x + 8 = 23.
Student: i dont know how to do this. what is an equation?
Output: {"variables": {}, "claims": [], "conclusion": {"statement": "asks for help with the concept", "answer_extracted_value": null, "answer_extracted_label": "", "is_attempt": false}, "domain_check_required": false}

Example 6 (arithmetic-step error — distinguishes from conclusion error)
Question: Sides 5, 7, 9 — is the triangle right-angled?
Student: 5^2 + 7^2 = 25 + 49 = 70, 9^2 = 81, 70 != 81 so not right-angled.
Output: {"variables": {}, "claims": [{"id": "c1", "description": "5 squared is 25", "expression": {"op": "pow", "args": [5, 2]}, "asserted_value": 25}, {"id": "c2", "description": "7 squared is 49", "expression": {"op": "pow", "args": [7, 2]}, "asserted_value": 49}, {"id": "c3", "description": "25 plus 49 is 70 (student computed it wrong)", "expression": {"op": "add", "args": [25, 49]}, "asserted_value": 70}, {"id": "c4", "description": "9 squared is 81", "expression": {"op": "pow", "args": [9, 2]}, "asserted_value": 81}], "conclusion": {"statement": "not right-angled", "answer_extracted_value": null, "answer_extracted_label": "no", "is_attempt": true}, "domain_check_required": false}
"""


def render_student_claims_user_prompt(
    *,
    problem_text: str,
    student_response: str,
) -> str:
    """Render the user-turn prompt for student-claims extraction.

    Per prompting-fundamentals query-at-end structure: few-shot
    examples FIRST, the actual question + student response LAST.
    """
    return (
        f"{STUDENT_CLAIMS_FEW_SHOT}\n"
        f"Question: {problem_text.strip()}\n"
        f"Student: {student_response.strip()}\n"
        f"Output:"
    )


# ──────────────────────────────────────────────────────────────────────
# Non-math student-response parser (LLM-B for the non-math path)
#
# Mirrors the math LLM-B (STUDENT_CLAIMS_SYSTEM) but emits TEXTUAL
# claims instead of arithmetic expressions. Subject-agnostic — works
# for geography, science, language, definitions, prose explanations.
#
# Output is consumed by NON_MATH_JUDGE_SYSTEM (LLM-C) — separating
# student parsing from judgement so LLM-C only has to decide semantic
# alignment, not first interpret messy student prose.
# ──────────────────────────────────────────────────────────────────────

STUDENT_RESPONSE_SYSTEM = """\
You read a student's response to a non-math question and emit a
structured JSON object that a downstream judge will consume.

Output a single JSON object with these top-level keys:

  is_attempt — true when the student attempted an answer; false when
               the response is meta — a help-request, a clarification
               ask, an "I don't know", a stalling phrase, or asks
               about a concept rather than answering the question.
               Examples of is_attempt=false:
                 "i don't understand", "what is condensation",
                 "explain", "show me", "I'm stuck", "huh?", "idk",
                 "give me a hint", "what does that mean".
               Examples of is_attempt=true (even when likely wrong):
                 "i think it's A", "condensation maybe", "the sky".

  hedge_marker — true when the student signalled the answer is a
                 guess or random pick, regardless of its correctness.
                 Examples: "guess B", "i dunno but A", "random pick",
                 "just picking C", "no idea but B", "could be A".
                 false otherwise. Use sparingly — only when the
                 student explicitly acknowledges low confidence.

  claims — an ordered list of the discrete assertions the student
           made. Each entry is an object:
             {"id": "s1", "text": "<one short paraphrase of the
                                    student's claim, in their voice>"}
           Use an empty list when the student gave only a single-word
           or single-phrase answer with no surrounding explanation.

  conclusion — the student's bottom-line answer:
                 {
                   "stated_answer": "<short paraphrase of the final
                                     answer, or the answer phrase
                                     itself>",
                   "answer_label":  "<yes|no|true|false|A|B|C|D|>",
                   "denies_canonical": <true|false>
                 }
               ``answer_label`` is filled for Yes/No, True/False, or
               single-letter multiple-choice answers. Use lowercase
               for yes/no/true/false and uppercase A-D for MCQ
               letters. Leave empty for free-text / explanation
               answers.

               ``denies_canonical`` is true when the student
               explicitly states the answer is NOT a specific thing
               (e.g. "it's not evaporation", "definitely not B").
               Default false.

Read the student response charitably. Strip filler ("hmm", "okay",
"i think") and reduce to substance. Treat paraphrases as equivalent
to the canonical concept ("water cooling" ≡ "vapor cools").

Return JSON only — no prose, no markdown fences.
"""


# Few-shot. Per prompting-fundamentals query-last structure: examples
# first, the live question + student response last in the user prompt.
STUDENT_RESPONSE_FEW_SHOT = """\
Example 1 (meta input — not an attempt)
Question: Which stage of the water cycle is condensation?
Student: i dont understand. what is condensation
Output: {"is_attempt": false, "hedge_marker": false, "claims": [], "conclusion": {"stated_answer": "", "answer_label": "", "denies_canonical": false}}

Example 2 (self-reported guess)
Question: Which letter (A/B/C/D) shows the condensation stage?
Student: guess B
Output: {"is_attempt": true, "hedge_marker": true, "claims": [], "conclusion": {"stated_answer": "B", "answer_label": "B", "denies_canonical": false}}

Example 3 (free-text correct with explanation)
Question: What is condensation?
Student: condensation is when water vapor cools down and forms tiny droplets in the air
Output: {"is_attempt": true, "hedge_marker": false, "claims": [{"id": "s1", "text": "condensation is water vapor cooling"}, {"id": "s2", "text": "forms tiny droplets in the air"}], "conclusion": {"stated_answer": "condensation is vapor cooling to droplets", "answer_label": "", "denies_canonical": false}}

Example 4 (denies canonical)
Question: Is groundwater the end of the hydrological cycle?
Student: no, the water keeps moving — it doesn't just stop underground
Output: {"is_attempt": true, "hedge_marker": false, "claims": [{"id": "s1", "text": "water keeps moving, doesn't stop underground"}], "conclusion": {"stated_answer": "no, the cycle continues", "answer_label": "no", "denies_canonical": true}}

Example 5 (wrong free-text — misconception)
Question: What is condensation?
Student: condensation is when rain falls from clouds
Output: {"is_attempt": true, "hedge_marker": false, "claims": [{"id": "s1", "text": "condensation is when rain falls from clouds"}], "conclusion": {"stated_answer": "rain falling from clouds", "answer_label": "", "denies_canonical": false}}

Example 6 (T/F with rationale)
Question: True or false — large-scale maps cover smaller areas.
Student: True - large-scale maps show smaller areas in more detail
Output: {"is_attempt": true, "hedge_marker": false, "claims": [{"id": "s1", "text": "large-scale maps show smaller areas in more detail"}], "conclusion": {"stated_answer": "true", "answer_label": "true", "denies_canonical": false}}
"""


def render_student_response_user_prompt(
    *,
    question_stem: str,
    student_response: str,
) -> str:
    """Render the user-turn prompt for non-math student-response extraction."""
    return (
        f"{STUDENT_RESPONSE_FEW_SHOT}\n"
        f"Question: {question_stem.strip()}\n"
        f"Student: {student_response.strip()}\n"
        f"Output:"
    )


# ──────────────────────────────────────────────────────────────────────
# Non-math judge (LLM-C — replaces the grounded adjudicator + verifier)
#
# Reads:
#   * question stem
#   * KB chunks (or none — falls back to Google-grounding via Gemini)
#   * LLM-B's STRUCTURED student output (claims + conclusion)
#
# Emits the same shape as the legacy grounded adjudicator so the
# downstream consumer (GradingResult / StudentSafeFeedback) is
# unchanged. The structural improvement is the INPUT shape — LLM-C
# never sees raw student prose, only structured claims, so it doesn't
# conflate parsing errors with judgement errors.
# ──────────────────────────────────────────────────────────────────────

NON_MATH_JUDGE_SYSTEM = """\
You judge whether a student's structured response answers a question
correctly, given the question and (optionally) a set of grounding
sources.

You receive a STRUCTURED student response, not raw prose — claims and
a conclusion already extracted by an upstream parser. Trust that
extraction: do not re-parse the student's wording. Judge the substance
of what the structured response asserts.

Source preference order:
  1. Prefer the supplied grounding sources when they cover the
     question — cite them in the ``citation`` field as [KB-N].
  2. When the supplied sources do not cover the question, use your
     own well-established knowledge of the subject. Set citation
     empty in that case and use confidence to reflect that the
     judgement is from general knowledge.

Verdict decision — you MUST return CORRECT, PARTIAL, or WRONG.
There is no fourth option.

  * correct   — the student's conclusion (and supporting claims, if
                any) asserts the same answer as the canonical, read
                charitably. Equivalent phrasings, paraphrases, and
                rich free-text explanations all count when they reach
                the correct conclusion.
  * partial   — the student covers part of a multi-claim canonical
                (e.g. one slot of a multi-part question), or names
                the correct general concept but misses a required
                qualifier.
  * wrong     — the student asserts a different answer than the
                canonical. Use ``reason_code="known_misconception"``
                when the student's answer matches a recognised wrong
                pattern (e.g. confused condensation with
                precipitation). Use
                ``reason_code="denies_canonical"`` when the structured
                input's ``denies_canonical`` was true AND the
                canonical IS the thing being denied.

Tie-break rule: if you cannot decide between PARTIAL and WRONG,
return PARTIAL so the tutor can credit whatever the student named.
If you cannot decide between CORRECT and PARTIAL, return PARTIAL so
the next turn extends rather than closes the topic.

Output a JSON object with these keys:

  verdict             — "correct" | "partial" | "wrong".
  private_canonical   — the correct answer in your own words (one
                         short sentence). System-private; never shown
                         to the student verbatim.
  what_right          — short phrase the tutor can use to credit what
                         the student got right (empty if wrong). DO
                         NOT include the canonical answer in this
                         field.
  what_missing        — short phrase the tutor can use to surface
                         what's still missing (empty if fully
                         correct). DO NOT include the canonical
                         answer in this field.
  first_misconception — short, redacted hint at the first conceptual
                         slip (empty if not wrong). DO NOT include
                         the canonical answer in this field.
  citation            — verbatim quote (≤30 words) from one of the
                         sources, with the source label in brackets
                         (e.g. "[KB-3]"). Empty when no source
                         applies.
  reason_code         — "" | "known_misconception" | "denies_canonical"
                         | "off_topic". Optional structured diagnostic
                         the engine branches on.

Return JSON only — no prose, no markdown fences.
"""


def render_non_math_judge_user_prompt(
    *,
    question_stem: str,
    student_response_dsl: dict,
    sources: list[str],
) -> str:
    """Render the user-turn prompt for the non-math judge (LLM-C).

    Sources first, structured student input + decision instruction last
    per the query-at-end rule. The structured student input is rendered
    as compact JSON so the model reads it as data, not prose.
    """
    import json as _json
    blocks = []
    for i, src in enumerate(sources or [], start=1):
        snippet = (src or "").strip()
        if not snippet:
            continue
        blocks.append(f"[KB-{i}]\n{snippet}")
    grounding_block = "\n\n".join(blocks) if blocks else "(no sources provided)"
    return (
        f"Grounding sources:\n\n{grounding_block}\n\n"
        f"---\n"
        f"Question: {question_stem.strip()}\n\n"
        f"Structured student response (already parsed by upstream LLM):\n"
        f"{_json.dumps(student_response_dsl, indent=2)}\n\n"
        f"---\n"
        f"Judge the structured student response against the question "
        f"and emit the JSON object specified."
    )


# ──────────────────────────────────────────────────────────────────────
# Pre-pose derivability check
# ──────────────────────────────────────────────────────────────────────

PRE_POSE_SYSTEM = """\
You decide whether the canonical answer to an assessment question
can be derived strictly from what the student can see — the visible
question prompt, any attached figure description, and the recent
conversation transcript.

Hidden knowledge-base chunks must NOT factor into your decision; the
student does not see them.

Return a JSON object:

  derivable  — true | false
  reason     — one short sentence. When derivable=false, name what
               piece of information would have to be added to the
               visible prompt for the answer to be derivable.
"""


def render_pre_pose_user_prompt(
    *,
    visible_prompt: str,
    attached_figure_description: str,
    recent_transcript: list[str],
    canonical: str,
) -> str:
    """Render the user-turn prompt for the pre-pose derivability check.

    Visible context is placed FIRST and exhaustively; the decision
    question (canonical + derivability ask) is placed LAST.
    """
    transcript_block = "\n".join(
        f"  - {t.strip()}" for t in (recent_transcript or []) if t and t.strip()
    ) or "  (no recent transcript)"
    figure_block = (attached_figure_description or "").strip() or "(no attached figure)"
    return (
        f"Visible question prompt:\n{visible_prompt.strip()}\n\n"
        f"Attached figure description (what the student can see in the figure):\n"
        f"{figure_block}\n\n"
        f"Recent transcript (most recent first):\n{transcript_block}\n\n"
        f"---\n"
        f"Canonical answer the system intends to grade against: {canonical.strip()}\n\n"
        f"Based ONLY on the visible context above, is this canonical "
        f"derivable? Emit the JSON object specified."
    )


# ──────────────────────────────────────────────────────────────────────
# Post-render question extractor (Phase 4 — Fix 2c)
#
# Runs AFTER the StudentTutor produces a turn and BEFORE the conformance
# classifier. Identifies every distinct action prompt in the rendered
# tutor text — a "question, instruction, or pose-the-tool-stem the
# student is expected to act on this turn".
#
# Enforces:
#   - One action prompt per turn (Principle #5 Minimising Cognitive Load
#     Ch.14 — one idea per turn).
#   - The active-end rule (Principle #1 Active Learning Ch.10 — every
#     tutor turn ends with an action the student takes).
#
# Subject-agnostic: works for math, geography, language, any subject.
# Replaces a regex-based stacked-question detector with a Haiku call so
# the rule generalises across phrasings ("which of the following…",
# "try …", "now you do it", "fill in the blank", "say it back to me").
# ──────────────────────────────────────────────────────────────────────

QUESTION_EXTRACTOR_SYSTEM = """\
You read a tutor's reply to a student and list every distinct action
prompt — a question, fill-in, choice, instruction, or "now you try"
ask that the student is expected to act on this turn.

What COUNTS as an action prompt:
  - A direct question ("What is the easting?")
  - A multiple-choice ask with options ("Which is bigger: A or B?")
  - A fill-in-blank ask ("The ___ comes first in a six-figure ref.")
  - A "now you try" / "your turn" / "show your working" ask
  - A retrieval ask ("Say back the rule in your own words.")
  - A choose-and-explain ("Pick one and tell me why.")

What does NOT count as an action prompt:
  - A rhetorical question used for emphasis ("Make sense?")
  - A check-in tag ("Right?", "OK?") with no answer requirement
  - A statement framed as a question for tone ("Ready to try one?")
    when the answer is not used by the tutor
  - Listing options as part of an explanation without asking the
    student to pick

Return a JSON object:

  action_count       — the number of distinct action prompts (0, 1,
                       2, 3, …).
  primary_action     — the SINGLE action prompt the student should
                       respond to this turn, in 1 short sentence
                       quoting (or closely paraphrasing) the tutor.
                       Empty string when action_count == 0.
  has_active_end     — true if the last sentence of the tutor turn is
                       an action prompt; false if the turn ends on a
                       statement, explanation, or trailing colon.
  stacked_examples   — when action_count > 1, list each action prompt
                       in order as a short string (each ≤25 words).
                       Empty list otherwise.

Return JSON only — no prose, no markdown fences.
"""


def render_question_extractor_user_prompt(
    *,
    tutor_text: str,
    selected_move: str,
) -> str:
    """Render the user-turn prompt for the question extractor.

    The tutor text is placed FIRST as the long content; the decision
    instruction LAST. The selected move is included so the extractor
    can weight ambiguous cases (e.g., ``explain``'s closing prompt is
    still expected to be an action; ``close_topic`` may legitimately
    end without a new ask).
    """
    return (
        f"Selected move: {selected_move.strip() or 'unknown'}\n\n"
        f"Tutor turn:\n{tutor_text.strip()}\n\n"
        f"---\n"
        f"List the action prompts in this turn and emit the JSON "
        f"object specified."
    )


# ──────────────────────────────────────────────────────────────────────
# Tutor-claim adjudication
# ──────────────────────────────────────────────────────────────────────

TUTOR_CLAIM_SYSTEM = """\
You adjudicate a factual or arithmetic claim made by a tutor in its
own explanation.

Source preference order:
  1. Prefer the supplied grounding sources when they cover the
     claim — cite them in the ``citation`` field as [KB-N].
  2. When the supplied sources do not cover the claim, judge using
     your own well-established knowledge of the subject. This is
     standard for explanations of basic curriculum content
     (arithmetic facts, scientific processes, textbook
     definitions) where a KB chunk may not have been authored but
     the claim is uncontroversial. Leave the citation field empty
     in that case.
  3. Return ``status = "unverified"`` only when neither the
     sources nor well-established knowledge let you adjudicate
     confidently — i.e. the claim is genuinely contested,
     speculative, or outside settled subject knowledge.

Return a JSON object:

  status    — one of: "supported", "contradicted", "unverified".
  citation  — verbatim quote (≤30 words) from one of the grounding
              sources that supports your judgement, with the source
              label in brackets. Empty when no source applies
              (general-knowledge route or genuine "unverified").
"""


def render_tutor_claim_user_prompt(
    *,
    claim: str,
    sources: list[str],
) -> str:
    """Render the user-turn prompt for tutor-claim adjudication."""
    blocks = []
    for i, src in enumerate(sources or [], start=1):
        snippet = (src or "").strip()
        if not snippet:
            continue
        blocks.append(f"[KB-{i}]\n{snippet}")
    grounding_block = "\n\n".join(blocks) if blocks else "(no sources provided)"
    return (
        f"Grounding sources:\n\n{grounding_block}\n\n"
        f"---\n"
        f"Tutor claim: {claim.strip()}\n\n"
        f"Based on the grounding sources above, adjudicate the claim "
        f"and emit the JSON object specified."
    )
