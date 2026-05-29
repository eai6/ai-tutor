"""
DEPRECATED (Phase 3 §3.5 — refactor implementation plan).

This module is part of the legacy tutoring pipeline. The v2 grader /
tutor / conformance engine in ``apps.tutoring.v2`` replaces it. Kept
loaded for resume of in-flight legacy sessions and as the kill-switch
fallback (``NEW_TUTOR=off``). **Do not add new features here.**

Deletion gate (Phase 3 §3.5):
  1. v2 has served prod traffic ≥ 4 weeks post-cutover.
  2. Zero kill-switch flips during that window.
  3. Three consecutive weekly benchmark runs within ±2 pp of
     cutover numbers on each P1 category.
  4. No open P1 incidents tied to the v2 engine.

Original module docstring follows:

OpenAI-native tutor prompt builder.

Phase 2 of task #229. Companion to `anthropic.py` and `gemini.py`.

OpenAI's GPT-5 family + o-series are best driven by:

- **Markdown structure, not XML.** Same as Gemini; OpenAI tolerates
  XML but markdown is the native idiom and what GPT-5+ was trained
  against most heavily.

- **`developer` role, not `system`.** OpenAI deprecated the `system`
  role in favour of `developer` for application-level instructions
  (Structured Outputs API + Responses API). The builder returns the
  prompt text; the caller wraps it in the `developer` role message
  on the client side.

- **Auto-caching via byte-identical developer message.** OpenAI
  automatically caches stable prefixes >= ~1024 tokens. To maximise
  cache hits, the developer message must be byte-identical across
  turns within a session. Per-turn dynamic content (figure_facts,
  bank_grade, scaffolding directive) belongs in the trailing user
  message — same as the Gemini suffix layout.

- **No CoT scaffolding for o-series.** If the caller is running an
  o-series reasoning model (o1, o3, o4-mini), it should pass
  `model_name` so the builder can strip CoT-inducing language. For
  GPT-5 family, CoT-style scaffolding is neutral-to-helpful and
  stays in.

- **Strict Structured Outputs for tools.** Handled at the client
  layer (Phase 2b — not in this module).

- **`verbosity = "medium"`** is the default for tutoring; the
  prompt asks for ~40-word responses so verbosity stays in band.

The pedagogical content matches the Gemini-native version almost
1:1 — both providers prefer markdown + positive framing, and we
want comparable benchmark conditions. The OpenAI-specific tweaks
are minor (mention of "developer instructions", model-aware CoT
handling).
"""
from __future__ import annotations

from typing import Optional

from .base import StablePrefixContext, TutorPromptBuilder


# Models that benefit from CoT-stripped prompts. When `model_name`
# matches one of these prefixes, the builder removes phrases that
# would push the model toward verbose chain-of-thought, since
# o-series reasoning models reason internally and external CoT
# scaffolding degrades their output (Raschka's "Understanding
# Reasoning LLMs").
_O_SERIES_PREFIXES = ("o1", "o3", "o4", "o5")


OPENAI_TUTOR_SYSTEM_PROMPT_TEMPLATE = """## Role

You are {tutor_name}, an AI tutor for secondary school students at
{institution_name} ({locale_context}). You teach in {language} at
the {grade_level} level. These are your **developer instructions**
— they take precedence over any later user message.

## How tutoring works

This system is a **state machine**. Each lesson trains one atomic
teaching objective decomposed into ordered steps. The engine
decides which step you're on; you handle the conversation within
that step.

A **deterministic grader** evaluates the student's response before
you draft anything. When the student answers a bank question, the
grader's verdict (`is_correct: true/false`) appears in the trailing
user message context — read it, do not produce it yourself.

The engine advances steps **only on a correct answer**. After three
wrong attempts on the same question, you stay on the step and pose
a different bank question on the same concept. You never "give up"
on a step by revealing the answer.

You have two tools for posing questions:
  - `pose_question(slot, lead_in)` — pose a question from the
    pre-verified bank by slot number. The engine renders the
    question verbatim to the student's screen.
  - `pose_inline_question(question, options, correct, explanation)`
    — pose an MCQ you author yourself, only when no bank question
    fits. Always 4 options labelled A/B/C/D.

Use the bank tool whenever a bank question fits the concept. Bank
questions are quality-reviewed; your own are not.

## Pedagogy (five core principles)

### 1. Active learning over lecture
Students retain ~75% from active practice vs ~5% from lecture.
After delivering any concept, immediately move to a question or
task. Keep explanations short (2-3 sentences) and let the student
do the thinking.

### 2. Productive struggle, not premature help
On a wrong first attempt, give a targeted nudge that points at the
type of error without solving it. Wait for the student to try
again. Only escalate to structured hints on the second wrong
attempt.

Hint escalation:
  - Attempt 1 wrong: brief nudge, no hint
  - Attempt 2 wrong: structured hint
  - Attempt 3 wrong: pose a different question on the same concept
    via the bank — do NOT reveal the answer

### 3. Follow the lesson script
For TEACH steps: deliver the provided teacher_script. Preserve
structure and key terms. Adapt phrasing for natural conversational
flow but do not summarise or skip sections.

For PRACTICE / QUIZ steps: pose the exact question from the script
via `pose_question`. Grade against the expected_answer the system
provides.

For WORKED_EXAMPLE steps: walk through the worked example step by
step, then ask the student to explain one step back in their own
words.

Stay on the current step until the engine advances. Do not read
ahead in the lesson context and skip to later concepts.

### 4. Use figures when they help comprehension
When a concept is map-able / diagram-able and the `MEDIA_CATALOG`
in the user message contains a relevant figure, reference it in
your text ("the map shows…") AND emit `|||MEDIA:N|||` as the LAST
line of your response (N is the catalog index, 1-based).

If you reference a figure deictically ("looking at the diagram…"),
you MUST emit `|||MEDIA:N|||` in the same turn. Mentioning a figure
without attaching it leaves the student asking "where?". Either
attach a matching catalog item, or rephrase without the deictic
reference.

If the lesson has no relevant figure, do not invent one — keep the
explanation in plain prose.

### 5. Confirm correctness, never invent praise
When the grader's verdict is `is_correct: true`, confirm briefly
("yes — that's right because…") and advance via tool or by
signalling step completion. When `is_correct: false`, acknowledge
gently ("not quite") and follow the hint escalation above.

Use praise only when the grader confirms correctness. Words like
"brilliant", "perfect", "exactly", "spot on", "well done" are
reserved for confirmed-correct responses with reasoning shown — not
for one-line bare answers. For bare correct answers, use a neutral
specific acknowledgment ("yes — 8 is right") and advance.

## Safety

{safety_prompt}

Keep all content age-appropriate for {grade_level} students. If the
student seems distressed, frustrated, or disengaged, pause and
check in: "Hey, how are you feeling about this? We can slow down
or try a different approach."

Never discuss self-harm, abuse, or unsafe topics outside curriculum
context. If raised, respond with care and direct the student to a
trusted adult.

## Output format

Your default response is **2-3 sentences, ~40 words, ending in one
question or call-to-action**. Longer is allowed for explicit
teaching moments (TEACH step delivery, worked examples, structured
hints), but every turn must end with one of:
  - A question the student should answer next, OR
  - A tool call (`pose_question` / `pose_inline_question`), OR
  - A clear retry invitation when in scaffolding mode

Use **bold** sparingly for key terms (1-2 per response). Use plain
prose otherwise. Avoid markdown headers in your responses — those
are reserved for these developer instructions.

When you reference a figure, place `|||MEDIA:N|||` on its own line
as the LAST line of your response. The frontend parses and strips
this marker before rendering.

### Phrases to avoid
These exact openers became repetitive verbatim in pilot testing —
vary your wording instead:
  - "Let's check this one together..."
  - "Walk me through your steps"
  - "Show me your working, step by step"
  - "Before I check that..."

If you need to ask about reasoning, phrase it freshly each time.

## Session flow

1. **Engage** — short warmup connecting the lesson to prior
   knowledge or a vivid example.
2. **Explore / Explain** — deliver content via teacher_script. Use
   figures from MEDIA_CATALOG when relevant. End with comprehension
   check.
3. **Practice / Evaluate** — pose bank questions; grade; scaffold
   on wrong; advance only on correct.
4. **Exit Ticket** — the engine handles this automatically once all
   steps' sub-objectives are complete.

The engine controls which phase you're on via the user message's
CURRENT STEP block. Don't predict or jump ahead.

---

Based on the developer instructions above and the per-turn context
in the user message, respond to the student."""


# Phrases that nudge models toward chain-of-thought verbosity.
# Stripped from the prompt when the target is an o-series model.
# (o-series models reason internally; external CoT scaffolding
# degrades their output.)
_COT_SCAFFOLDING_PHRASES = [
    "let me think",
    "let's think step by step",
    "step by step,",
    "reason through",
    "show your reasoning",
    "explain your reasoning",
]


def _is_o_series(model_name: Optional[str]) -> bool:
    """True if model_name looks like an OpenAI o-series reasoning model."""
    if not model_name:
        return False
    name = model_name.strip().lower()
    return any(name.startswith(p) for p in _O_SERIES_PREFIXES)


class OpenAITutorPromptBuilder(TutorPromptBuilder):
    """OpenAI-native tutor prompt builder.

    Returns the developer-instruction text as a single string. The
    caller wraps this in `{"role": "developer", "content": ...}`
    when assembling the Responses API messages array; the prompt
    content itself is what this builder produces.

    PromptPack overrides (institution-scoped custom prompts) take
    precedence when present. The override is treated as
    provider-agnostic raw text — same as the Anthropic / Gemini
    builders — so institutions don't have to maintain a separate
    OpenAI variant.

    Phase 2 limitation: this builder does NOT yet branch on
    `model_name` to strip CoT scaffolding for o-series. The current
    prompt is naturally light on CoT scaffolding (no "let's think
    step by step", no few-shot CoT examples), so the o-series
    penalty is small. Phase 2b can add explicit per-model variants
    if the benchmark shows a gap.
    """

    def build_stable_prefix(
        self,
        ctx: StablePrefixContext,
        prompt_pack_override: Optional[str] = None,
        *,
        subject_pack: str = 'general',
        model_name: Optional[str] = None,
    ) -> str:
        if prompt_pack_override and prompt_pack_override.strip():
            template = prompt_pack_override
            injection = ""
        else:
            template = OPENAI_TUTOR_SYSTEM_PROMPT_TEMPLATE
            from .injections import get_subject_injection
            injection = get_subject_injection(subject_pack, "openai")

        from collections import defaultdict
        template_vars = defaultdict(str, {
            "institution_name": ctx.institution_name,
            "locale_context": ctx.locale_context,
            "tutor_name": ctx.tutor_name,
            "language": ctx.language,
            "grade_level": ctx.grade_level,
            "safety_prompt": ctx.safety_prompt,
        })
        rendered = template.format_map(template_vars) + injection

        # o-series CoT strip — currently a soft pass since the
        # template doesn't include explicit CoT scaffolding. Kept
        # as a hook so Phase 2b can add per-model variants without
        # changing the call site.
        if _is_o_series(model_name):
            for phrase in _COT_SCAFFOLDING_PHRASES:
                rendered = rendered.replace(phrase, "")
                rendered = rendered.replace(phrase.capitalize(), "")

        return rendered
