"""Webb's Depth of Knowledge (DOK) framework — single source of truth.

Loaded into LLM system prompts for content generation, exit ticket
generation, and summative assessment generation. The same rubric is
used to:

  - Guide *teaching*: each tutoring step should be authored at an
    explicit DOK level so the lesson progresses students up the levels
    while drilling its single teaching objective.
  - Guide *assessment*: each question targets a stated DOK level so
    a paper has a balanced mix (recall → strategic thinking → extended).

Webb, Norman L. "Web Alignment Tool" (24 July 2005). Wisconsin Center
for Educational Research, University of Wisconsin–Madison.

Use `dok_guidance_for(purpose)` to embed in a system prompt; pass
`'content'` for lesson generation and `'assessment'` for exit ticket
or summative generation. The returned string is plain text and safe to
concatenate into any LLM prompt.
"""

from __future__ import annotations


# ---- Core rubric ------------------------------------------------------------

DOK_LEVELS = (
    {
        "level": 1,
        "name": "Recall",
        "verbs": [
            "draw", "identify", "list", "label", "illustrate", "define",
            "memorize", "calculate", "arrange", "repeat", "state", "tell",
            "tabulate", "name", "report", "design", "recall", "recite",
            "recognize", "use", "quote", "match",
            "who", "what", "when", "where", "why",
        ],
        "activities": [
            "Recall facts, definitions, terms, or simple procedures.",
            "Perform a routine calculation.",
            "Label parts of a diagram.",
            "Describe locations on a map.",
            "Apply a single rule or formula.",
        ],
    },
    {
        "level": 2,
        "name": "Skill / Concept",
        "verbs": [
            "infer", "categorize", "collect and display", "identify patterns",
            "organize", "construct", "modify", "predict", "interpret",
            "distinguish", "use context cues", "make observations",
            "summarize", "show", "graph", "classify", "separate",
            "cause/effect", "estimate", "compare", "relate",
        ],
        "activities": [
            "Identify and summarize the major events in a narrative.",
            "Use context cues to determine the meaning of unfamiliar words.",
            "Solve routine multi-step problems.",
            "Describe the cause/effect of a particular event.",
            "Identify patterns in events or behavior.",
            "Formulate a routine problem given data and conditions.",
            "Organize, represent, and interpret data.",
        ],
    },
    {
        "level": 3,
        "name": "Strategic Thinking",
        "verbs": [
            "revise", "develop a logical argument", "assess", "appraise",
            "construct", "use concepts to solve non-routine problems",
            "compare", "critique", "explain phenomena in terms of concepts",
            "investigate", "formulate", "draw conclusions", "differentiate",
            "hypothesize", "cite evidence",
        ],
        "activities": [
            "Support ideas with details and examples.",
            "Use voice appropriate to the purpose and audience.",
            "Identify research questions and design investigations for a scientific problem.",
            "Develop a scientific model for a complex situation.",
            "Determine the author's purpose and describe how it affects the interpretation of a reading selection.",
            "Apply a concept in other contexts.",
        ],
    },
    {
        "level": 4,
        "name": "Extended Thinking",
        "verbs": [
            "design", "connect", "synthesize", "apply concepts", "critique",
            "analyze", "create", "prove",
        ],
        "activities": [
            "Conduct a project that requires specifying a problem, designing and "
            "conducting an experiment, analyzing its data, and reporting results/solutions.",
            "Apply a mathematical model to illuminate a problem or situation.",
            "Analyze and synthesize information from multiple sources.",
            "Describe and illustrate how common themes are found across texts from different cultures.",
            "Design a mathematical model to inform and solve a practical or abstract situation.",
        ],
    },
)


# ---- Prompt-formatting helpers ---------------------------------------------

def _bullet(items, max_items=None):
    items = list(items)
    if max_items:
        items = items[:max_items]
    return "\n".join(f"  - {x}" for x in items)


def _level_block(level: dict, *, include_activities: bool = True) -> str:
    parts = [
        f"DOK Level {level['level']} — {level['name']}",
        f"  Signal verbs: {', '.join(level['verbs'][:14])}",
    ]
    if include_activities:
        parts.append("  Example activities:")
        parts.append(_bullet(level["activities"]))
    return "\n".join(parts)


_RUBRIC = "\n\n".join(_level_block(lvl) for lvl in DOK_LEVELS)


_CONTENT_GUIDANCE = """\
DEPTH OF KNOWLEDGE (DOK) — guide for authoring tutoring steps

A 20-minute lesson drills ONE teaching objective. Use Webb's DOK
framework to set the cognitive demand of each tutoring step inside
that lesson, and progress students up the levels:

  - Open with DOK 1 (recall) to anchor vocabulary and prerequisite facts.
  - Spend most steps at DOK 2 (skill/concept) — solve routine multi-step
    problems, interpret data, identify patterns.
  - Reach at least one DOK 3 (strategic thinking) step toward the end —
    compare strategies, justify a conclusion, apply the concept in a
    new context.
  - DOK 4 (extended thinking) is rare in a single 20-minute lesson;
    only include if the curriculum explicitly requires a project-style
    task.

When you author a tutoring step, pick a DOK level deliberately and use
verbs from that level's signal-verb list. Do not keep every step at
DOK 1 — students need transfer, not just recall.

{rubric}
"""


_ASSESSMENT_GUIDANCE = """\
DEPTH OF KNOWLEDGE (DOK) — guide for authoring questions

Every assessment question must target a specific DOK level. A balanced
paper looks like:

  - ~30% DOK 1 (recall): definitions, single-step calculations, naming.
  - ~45% DOK 2 (skill/concept): routine multi-step problems, data
    interpretation, comparing two options.
  - ~20% DOK 3 (strategic thinking): justify, critique, apply to a
    new context, draw conclusions from evidence.
  - ~5% DOK 4 (extended thinking): only for summative exams, only when
    the curriculum demands a project- or essay-style response.

When you write a question, pick a DOK level on purpose and use a verb
from that level's signal list. The cognitive level matters more than
question type — an MCQ can be DOK 3 if it asks the student to choose
the *best* justification; a short-answer can still be DOK 1 if it asks
for a definition.

{rubric}
"""


def dok_guidance_for(purpose: str) -> str:
    """Return the DOK guidance block to embed in a system prompt.

    `purpose`:
      - 'content'    — for lesson / teaching-step generation.
      - 'assessment' — for exit ticket and summative question generation.

    Anything else falls back to the assessment block (safest default).
    """
    template = _CONTENT_GUIDANCE if purpose == "content" else _ASSESSMENT_GUIDANCE
    return template.format(rubric=_RUBRIC)


__all__ = ["DOK_LEVELS", "dok_guidance_for"]
