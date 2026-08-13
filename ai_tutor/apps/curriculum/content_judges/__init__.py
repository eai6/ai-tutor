"""Per-domain quality judges for GENERATED content (lesson steps,
exit-ticket questions, images, image prompts).

Mirrors the shape of `apps/tutoring/judges/` — focused single-task
judges, fail-soft results, concurrent orchestration. The tutoring
judges run on real-time tutor turns; these run on static generated
content (post-gen for steps/exit-tickets/images, PRE-gen for image
prompts).

Three architectural mirrors of the tutoring judges:
  1. **Per-judge model config** — each judge has its own ModelConfig
     purpose (`content_judge_image_prompt`, `content_judge_factual`,
     etc.) so providers can be tuned per judge type.
  2. **Multi-provider fallback** — see `_providers.py`. If the primary
     provider (typically Gemini for grounding) fails, the chain tries
     the next configured provider before declaring failure.
  3. **Cross-provider review** — by design the judge provider is NEVER
     the same as the generator provider for the same artefact (e.g.
     OpenAI generates images → Gemini reviews). Avoids same-model
     self-confirmation bias.

Public surface:
  - `JudgeResult` — common result dataclass (passed/failed + violations
    + recommended_fix + skipped + skip_reason)
  - `run_image_prompt_judge(prompt, lesson_context)` — PRE-gen check
  - (more judges land in subsequent commits per Q1.2 / Q4 of the plan)
  - `run_all_post_gen_judges(content, content_type, context)` — concurrent
    orchestrator (lands in Q4 once all judges exist)

See memory/content_quality_review_plan.md for the full architecture.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class JudgeResult:
    """Common result for any content judge.

    Mirrors the per-judge result shape in `apps/tutoring/judges/` but
    unifies the fields — content judges all share the same surface so
    the orchestrator + regen layer can consume them uniformly.
    """
    # Did the judge approve the content? False → regen ensemble fires.
    passed: bool = True
    # Stable violation codes the judge raised (e.g. PROMPT_VAGUE,
    # PROMPT_HALLUCINATION_TRIGGER). Empty list when passed.
    violations: List[str] = field(default_factory=list)
    # Human-readable explanation, capped at 300 chars. Surfaced in
    # regen prompt + UI badges.
    reasoning: str = ""
    # Concrete fix suggestion the regen layer can use as guidance —
    # e.g. for image_prompt: a rewritten prompt; for factual_step: the
    # specific claim to retract or correct.
    recommended_fix: str = ""
    # Provider that produced the verdict (for audit + cross-provider
    # enforcement). E.g. "google", "anthropic".
    provider: str = ""
    # Model name (for audit). E.g. "gemini-3.1-pro-preview".
    model_name: str = ""
    # When the judge couldn't run (no client / empty input / LLM
    # error after fallback chain), passed defaults to True (don't
    # block on infrastructure failures), but skip_reason names why.
    skipped: bool = False
    skip_reason: str = ""


__all__ = [
    "JudgeResult",
]
