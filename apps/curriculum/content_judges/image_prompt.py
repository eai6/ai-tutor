"""PRE-generation judge for image-generation prompts.

Runs BEFORE the image generation API is called. Catches bad image prompts
cheap (one text-LLM call ~ $0.001) instead of paying for a wasted image
generation (~ $0.04 per image and 8-15s of latency) when the prompt is
malformed, vague, or guaranteed to hallucinate.

**Hooks at:** `apps/tutoring/image_service.py::get_or_generate_image`
**Generator-side providers:** OpenAI gpt-image-2 (primary) / Gemini Imagen
**Judge-side providers:** Gemini → Anthropic → OpenAI (cross-provider; the
provider that's about to generate the image is EXCLUDED so the judge can't
rubber-stamp its own generator's bias).

Six stable violation codes — chosen because each names a concrete failure
mode we observed in the 2026-04 / 2026-05 image-quality review traces:
  - PROMPT_VAGUE — under-specified; image gen will guess
  - PROMPT_HALLUCINATION_TRIGGER — asks for specific data/labels/numbers
    the prompt itself doesn't provide → model invents them
  - PROMPT_OFF_TOPIC — drifts from the lesson's actual learning objective
  - PROMPT_WRONG_VISUAL_TYPE — asks for a photo where a schematic / labeled
    diagram is needed (photos invent detail; schematics are intentional)
  - PROMPT_GRADE_MISMATCH — abstraction level wrong for the lesson grade
  - PROMPT_RELIES_ON_TEXT_IN_IMAGE — banks on the model rendering legible
    long text / equations / data tables (it's unreliable at this)

Verdict shape (`JudgeResult`):
  - passed=True → caller proceeds with the original prompt unchanged
  - passed=False → caller may use `recommended_fix` (a rewritten prompt)
    or skip image generation entirely. The decision belongs to the caller
    (`image_service.py`); this module only produces the verdict.

Returns `passed=True, skipped=True` on infrastructure failures so the
image pipeline never blocks on a judge outage. Skip reasons are surfaced
for telemetry.
"""

from __future__ import annotations

import logging
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from apps.curriculum.content_judges import JudgeResult
from apps.curriculum.content_judges._providers import (
    call_judge_structured_with_fallback,
    get_judge_provider_chain,
)

logger = logging.getLogger(__name__)


# ─── Stable violation codes ────────────────────────────────────────────
VIOLATION_CODES = (
    "PROMPT_VAGUE",
    "PROMPT_HALLUCINATION_TRIGGER",
    "PROMPT_OFF_TOPIC",
    "PROMPT_WRONG_VISUAL_TYPE",
    "PROMPT_GRADE_MISMATCH",
    "PROMPT_RELIES_ON_TEXT_IN_IMAGE",
)


# ─── System instruction (Gemini-style: direct task, no persona) ────────
# Why this shape:
#   - Gemini 3 docs: "State your goal clearly and concisely. Avoid
#     unnecessary or overly persuasive language." → no "expert reviewer"
#     persona priming.
#   - Positive framing throughout (Gemini over-indexes on "do not X").
#   - Violation codes are CLOSED (one of N). Closed enums beat free text.
#   - Output is reason-in-prose-inside-XML, then emit JSON. Per Tam et al.
#     2025, forced strict-JSON during generation drops reasoning accuracy
#     10-15%; the wrapper pattern recovers most of that while keeping the
#     downstream parser simple. Prompting-fundamentals "Output formatting"
#     section.
_SYSTEM_INSTRUCTION = """\
Review one candidate prompt that is about to be sent to an image-generation \
model. Decide whether the image gen will produce a usable teaching figure \
for the lesson.

Approve a prompt when it is:
  - specific enough to produce a recognisable figure
  - grounded in the lesson's subject and grade
  - matched to a visual type the gen model handles reliably (schematic, \
labelled diagram, simple illustration, photograph of a real-world scene)
  - free of requests for data, labels, equations, or text the prompt itself \
does not supply

Reject a prompt when it has any of these issues. Use ONLY these codes:

  PROMPT_VAGUE
    Under-specified. The gen model would have to guess most of the figure.
    Example trigger: "Show a diagram about geography."

  PROMPT_HALLUCINATION_TRIGGER
    Asks for specific numbers, labels, country names, dates, or data the \
prompt does not provide.
    Example trigger: "Bar chart showing each Seychelles island's exact \
population." (no figures supplied)

  PROMPT_OFF_TOPIC
    The prompt drifts from the lesson's stated learning objective.
    Example trigger: lesson is "rivers and erosion" but prompt asks for a \
volcano.

  PROMPT_WRONG_VISUAL_TYPE
    Asks for a photograph where a labelled schematic is needed (photos \
invent detail; labelled schematics are intentional). Or vice versa.
    Example trigger: "Photo of the water cycle." (should be a labelled \
schematic)

  PROMPT_GRADE_MISMATCH
    Abstraction level wrong for the lesson's grade band.
    Example trigger: Form 1 cell-biology lesson asking for "ribosome \
cryo-EM structure".

  PROMPT_RELIES_ON_TEXT_IN_IMAGE
    The figure depends on the gen model rendering legible long text — \
multi-line equations, paragraph captions, data tables.
    Example trigger: "Diagram with these five paragraphs of explanation \
written under each layer."

When rejecting, write a `recommended_fix` that is a complete rewritten \
prompt (≤80 words) the caller can send to the gen model directly. The \
fix must be specific and self-contained — no "make it better" comments.

In `reasoning`, write 2-4 short sentences walking through what the \
prompt asks for and whether each violation code applies. Cite the \
lesson context.
"""


# ─── Output schema (instructor / Pydantic) ─────────────────────────────
_ALLOWED_VIOLATIONS = Literal[
    "PROMPT_VAGUE",
    "PROMPT_HALLUCINATION_TRIGGER",
    "PROMPT_OFF_TOPIC",
    "PROMPT_WRONG_VISUAL_TYPE",
    "PROMPT_GRADE_MISMATCH",
    "PROMPT_RELIES_ON_TEXT_IN_IMAGE",
]


class ImagePromptVerdict(BaseModel):
    """Structured output for the image_prompt judge."""
    reasoning: str = Field(
        description=(
            "2-4 short sentences walking through what the prompt asks "
            "for and whether each violation code applies. Cite the "
            "lesson context."
        ),
        max_length=1500,
    )
    passed: bool = Field(
        description="True iff the candidate prompt is approved for use.",
    )
    violations: List[_ALLOWED_VIOLATIONS] = Field(
        default_factory=list,
        description="Zero or more violation codes from the enum.",
    )
    recommended_fix: str = Field(
        default="",
        description=(
            "When rejecting: complete rewritten prompt (≤80 words) the "
            "caller can send directly. Empty when passing."
        ),
        max_length=800,
    )


def _dedupe_violations(codes: List[str]) -> List[str]:
    out: List[str] = []
    for code in codes:
        s = str(code or "").strip().upper()
        if s in VIOLATION_CODES and s not in out:
            out.append(s)
    return out


# ─── Few-shot examples ──────────────────────────────────────────────────
# Why this shape:
#   - Gemini docs: "Well-constructed examples can even replace lengthy
#     instructions." Use 2-5 varied examples.
#   - Cover one PASS and one of each common rejection code so the model
#     sees the format AND the boundary cases.
#   - Examples kept terse — over-long examples bias output length.
_FEW_SHOT_EXAMPLES = """\
<example>
<input>
<lesson>
<subject>Geography</subject>
<grade>Form 2 (Year 8)</grade>
<title>The water cycle</title>
<objective>Students can label evaporation, condensation, precipitation, \
and collection on a diagram of the water cycle.</objective>
</lesson>
<category>diagram</category>
<candidate_prompt>
Schematic of the water cycle showing evaporation rising from a lake, \
condensation forming clouds, precipitation as rain, and collection back \
into the lake. Label the four stages with arrows between them.
</candidate_prompt>
</input>
<reasoning>
Prompt names all four objective stages, specifies a labelled schematic \
(right visual type for the goal), and gives concrete spatial layout. \
Grade-appropriate and on-topic. No data the prompt doesn't supply.
</reasoning>
<verdict>
{"passed": true, "violations": [], "recommended_fix": ""}
</verdict>
</example>

<example>
<input>
<lesson>
<subject>Geography</subject>
<grade>Form 1 (Year 7)</grade>
<title>The Seychelles archipelago</title>
<objective>Students can locate the three main island groups (Inner, \
Amirantes, Outer) on a map.</objective>
</lesson>
<category>diagram</category>
<candidate_prompt>
Bar chart showing the exact population of each island in the Seychelles.
</candidate_prompt>
</input>
<reasoning>
Lesson objective is map-based location of island groups. The prompt \
asks for a bar chart of populations, which (a) drifts from the location \
objective and (b) requires per-island figures the prompt itself does \
not supply, so the gen model would invent them.
</reasoning>
<verdict>
{"passed": false, "violations": ["PROMPT_OFF_TOPIC", \
"PROMPT_HALLUCINATION_TRIGGER"], "recommended_fix": "Schematic outline \
map of the Seychelles archipelago with the Inner Islands, Amirantes, \
and Outer Islands shaded in three different colours and labelled. \
Include a small compass rose."}
</verdict>
</example>

<example>
<input>
<lesson>
<subject>Mathematics</subject>
<grade>Form 1 (Year 7)</grade>
<title>Adding fractions with the same denominator</title>
<objective>Students can add two fractions sharing a denominator and \
write the sum.</objective>
</lesson>
<category>diagram</category>
<candidate_prompt>
A nice math picture about fractions.
</candidate_prompt>
</input>
<reasoning>
Prompt is under-specified — no shapes, no specific fractions, no \
indication of the operation. The gen model would guess everything.
</reasoning>
<verdict>
{"passed": false, "violations": ["PROMPT_VAGUE"], "recommended_fix": \
"Two pizza diagrams side by side. The first shows 2/8 shaded, the \
second shows 3/8 shaded. A plus sign between them. To the right, a \
third pizza shows 5/8 shaded with an equals sign before it. Use \
simple flat colours, no text labels inside the pizzas."}
</verdict>
</example>

<example>
<input>
<lesson>
<subject>Geography</subject>
<grade>Form 3 (Year 9)</grade>
<title>Tourism in Seychelles</title>
<objective>Students can describe how tourism employment varies by \
island.</objective>
</lesson>
<category>photo</category>
<candidate_prompt>
Infographic with three paragraphs of explanation under each island \
showing tourism employment statistics, historical trends, and \
government policy summary.
</candidate_prompt>
</input>
<reasoning>
Prompt asks the gen model to render multiple paragraphs of legible text \
inside the image, which is unreliable. It also asks for statistics not \
provided in the prompt. The category says "photo" but the request is \
infographic-style.
</reasoning>
<verdict>
{"passed": false, "violations": ["PROMPT_RELIES_ON_TEXT_IN_IMAGE", \
"PROMPT_HALLUCINATION_TRIGGER", "PROMPT_WRONG_VISUAL_TYPE"], \
"recommended_fix": "Photograph of a beachfront hotel scene in the \
Seychelles: staff in uniform welcoming arriving guests, tropical \
setting, soft daylight. No on-image text or statistics."}
</verdict>
</example>
"""


def _build_user_prompt(
    candidate_prompt: str,
    *,
    category: str,
    lesson_subject: str,
    lesson_grade: str,
    lesson_title: str,
    lesson_objective: str,
    textbook_context: str,
) -> str:
    """Compose the user message.

    Long-context query-last rule: examples first, then the candidate
    LAST with an anchor phrase. Gemini long-context docs: ~30% quality
    improvement when query/input goes after context.
    """
    return f"""\
{_FEW_SHOT_EXAMPLES}

Now review the next candidate using the same format and the same \
violation codes.

<input>
<lesson>
<subject>{(lesson_subject or "(unknown)").strip()[:120]}</subject>
<grade>{(lesson_grade or "(unknown)").strip()[:80]}</grade>
<title>{(lesson_title or "(unknown)").strip()[:200]}</title>
<objective>{(lesson_objective or "(none provided)").strip()[:400]}\
</objective>
<style_hint>{(textbook_context or "(none)").strip()[:200]}</style_hint>
</lesson>
<category>{(category or "general").strip()[:40]}</category>
<candidate_prompt>
{(candidate_prompt or "").strip()[:1500]}
</candidate_prompt>
</input>

Based on the lesson context and category above, judge whether this \
candidate prompt is approved. Use only the six violation codes. If \
the prompt is acceptable, return passed=true and an empty violations \
list.
"""


# ─── Public entry point ────────────────────────────────────────────────
def run_image_prompt_judge(
    candidate_prompt: str,
    *,
    category: str = "general",
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    textbook_context: str = "",
    exclude_provider: Optional[str] = None,
    judge_purpose: str = "content_judge_image_prompt",
    max_tokens: int = 3500,
) -> JudgeResult:
    """Review an image-generation prompt before it's sent to the gen model.

    Args:
        candidate_prompt: The prompt that's about to be sent to image gen.
        category: Image category as passed to image_service ("diagram",
            "photo", "illustration", etc.). The judge uses this to flag
            visual-type mismatches.
        lesson_subject / lesson_grade / lesson_title / lesson_objective:
            Lesson context for on-topic + grade-mismatch checks.
        textbook_context: Optional style hint forwarded from image_service
            (~ "schematic style, line art").
        exclude_provider: Provider that's about to GENERATE the image
            (e.g. 'openai' for gpt-image-2). The judge chain skips this
            provider so the judge can't be the same vendor that's about
            to produce the artefact. See `_providers.py` for rationale.
        judge_purpose: ModelConfig purpose to consult first. Defaults to
            the per-judge purpose; falls through to generation/judge/
            tutoring if not configured.
        max_tokens: Cap on judge output. 800 covers 4-sentence reasoning
            + verdict JSON + a rewritten fix prompt comfortably.

    Returns:
        JudgeResult. On infrastructure failure (no providers, all
        providers errored, malformed output): passed=True + skipped=True
        with a skip_reason — the image pipeline must never block on a
        judge outage.
    """
    result = JudgeResult()

    # Cheap input guards before any LLM call.
    if not candidate_prompt or not candidate_prompt.strip():
        result.skipped = True
        result.skip_reason = "empty_prompt"
        return result
    if len(candidate_prompt.strip()) < 10:
        # Too short to meaningfully review — let it through; image gen
        # will fail loudly enough on its own.
        result.skipped = True
        result.skip_reason = "prompt_below_min_length"
        return result

    providers = get_judge_provider_chain(
        judge_purpose, exclude_provider=exclude_provider,
    )
    if not providers:
        logger.warning(
            "[ImagePromptJudge] no providers available "
            f"(purpose={judge_purpose}, exclude={exclude_provider}) — skipping"
        )
        result.skipped = True
        result.skip_reason = "no_providers_available"
        return result

    user_prompt = _build_user_prompt(
        candidate_prompt,
        category=category,
        lesson_subject=lesson_subject,
        lesson_grade=lesson_grade,
        lesson_title=lesson_title,
        lesson_objective=lesson_objective,
        textbook_context=textbook_context,
    )

    call = call_judge_structured_with_fallback(
        user_prompt,
        providers,
        ImagePromptVerdict,
        system_prompt=_SYSTEM_INSTRUCTION,
        max_tokens=max_tokens,
    )
    if not call.success:
        logger.warning(
            "[ImagePromptJudge] all providers failed: "
            f"{call.error_class}: {call.error_detail}"
        )
        result.skipped = True
        result.skip_reason = (
            f"all_providers_failed: {call.error_class}"
        )
        return result

    result.provider = call.provider
    result.model_name = call.model_name

    verdict: ImagePromptVerdict = call.verdict
    result.reasoning = (verdict.reasoning or "").strip()[:300]

    passed = bool(verdict.passed)
    violations = _dedupe_violations(list(verdict.violations or []))
    fix = (verdict.recommended_fix or "").strip()[:600]

    # Internal consistency: passed=true with violations is contradictory.
    # Trust the violation list (it's the actionable signal) and downgrade
    # the verdict.
    if violations and passed:
        passed = False

    result.passed = passed
    result.violations = violations
    result.recommended_fix = fix if not passed else ""

    return result


__all__ = [
    "VIOLATION_CODES",
    "run_image_prompt_judge",
]
