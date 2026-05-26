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
  - First-class ``unverified``: every prompt has an explicit "if
    you cannot decide, return unverified" branch — the conservative
    escape valve per analysis §3 + §7 item 1.
"""

from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────
# Math path — DSL extraction
# ──────────────────────────────────────────────────────────────────────

MATH_DSL_SYSTEM = """\
You decompose a math problem into a constrained JSON DSL that a
Python interpreter executes.

Output a JSON object with exactly two top-level keys:

  variables  — a mapping of variable names to numeric values that
               appear in the visible problem text. Every value must
               be derivable from the problem statement; do not invent
               numbers that are not named or implied by the problem.

  expression — a tree of operations. Each node is ONE of:
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
# Non-math grounded adjudication — KB-cited or Google-grounded
# ──────────────────────────────────────────────────────────────────────

NON_MATH_GROUNDED_SYSTEM = """\
You judge whether a student's free-text answer is correct, given the
question and (optionally) a set of grounding sources.

Source preference order:
  1. Prefer the supplied grounding sources when they cover the
     question — cite them in the ``citation`` field as [KB-N].
  2. When the supplied sources do not cover the question (this is
     common for general curriculum topics where a KB chunk wasn't
     authored), use your own well-established knowledge of the
     subject to judge correctness. Set the citation field empty in
     that case and use the ``confidence`` field to reflect that the
     judgement is from general knowledge rather than a source.
  3. When neither path yields a confident judgement, return
     verdict = "unverified".

The student's answer should be judged on the substance of what they
said about the question, not on whether their wording matches a
specific phrasing. A correct mechanism explanation expressed in the
student's own words is still correct.

Output a JSON object with these keys:

  verdict             — one of: "correct", "partial", "wrong", "unverified".
  private_canonical   — the correct answer in your own words (one short sentence).
                         This is private to the tutoring system; the student
                         never sees this field directly.
  what_right          — short phrase the tutor can use to credit what
                         the student got right (empty if wrong / unverified).
  what_missing        — short phrase the tutor can use to surface
                         what's still missing (empty if fully correct).
  first_misconception — short, redacted hint at the first conceptual slip,
                         without revealing the canonical (empty if not wrong).
  citation            — verbatim quote (≤30 words) from one of the
                         sources that supports your judgement, with the
                         source label in brackets (e.g. "[KB-3]"). Empty
                         when no source applies (general-knowledge route
                         or genuine "unverified").
  confidence          — a number between 0 and 1.
                         ≥0.8 = strong direct support (sources or
                                unambiguous general knowledge).
                         0.5–0.79 = supported but with some hedging
                                (general knowledge with minor wording
                                ambiguity, or partial source coverage).
                         <0.5 = genuinely unsure — the runtime maps
                                this band to "unverified".

Return JSON only — no prose, no markdown fences.
"""


def render_non_math_grounded_user_prompt(
    *,
    question_stem: str,
    student_input: str,
    sources: list[str],
) -> str:
    """Render the long-context user prompt for grounded adjudication.

    Sources are placed FIRST, the decision question LAST — per the
    long-context query-at-end rule. Each source is labelled "[KB-N]"
    so the citation field can reference it cleanly.
    """
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
        f"Question: {question_stem.strip()}\n"
        f"Student answer: {student_input.strip()}\n\n"
        f"Based on the grounding sources above, judge the student answer "
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
