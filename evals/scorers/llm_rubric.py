"""LLM-as-judge rubric scorer (Layer 3 from memory/eval_harness_plan.md).

Catches behaviors the deterministic + judge-derived assertions can't:
- "Did the tutor's explanation actually address the student's misconception?"
- "Was the tone appropriate for a struggling student?"
- "Did the tutor adapt its strategy after the student refused twice?"

Each rubric item is judged 0.0-1.0 by a small, pinned LLM (default
Claude Haiku 4.5 @ temperature=0). The scenario passes iff the weighted
mean meets ``pass_threshold``.

The judge prompt is small and the output is strict JSON — pin model +
temp so scores stay reproducible across runs. See the plan doc for the
rationale on Haiku vs heavier models (cost dominates over precision at
this verb).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from apps.llm.client import get_llm_client
from apps.llm.models import ModelConfig

logger = logging.getLogger(__name__)


# Default rubric-judge config. Scenarios can override per-file via the
# ``rubric_judge:`` block. Keep this in sync with the plan doc; bump
# when a better cheap reliable JSON-emitting model lands.
DEFAULT_RUBRIC_JUDGE = {
    'provider': 'anthropic',
    'model': 'claude-haiku-4-5-20251001',
    'temperature': 0.0,
    # Sized for ~12-item rubrics (the 8 BEA-aligned standard items plus
    # 3-5 scenario-specific items). Each scored item is roughly 250 chars
    # (item text + score + one-sentence reasoning), JSON overhead pushes
    # the realistic ceiling to ~3K tokens. 1024 was sized for the older
    # 3-4 item rubrics and caused truncation-induced JSON parse errors
    # on the BEA-expanded dataset.
    'max_tokens': 3072,
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RubricItemScore:
    item: str
    score: float
    reasoning: str = ''


@dataclass
class RubricResult:
    items: list[RubricItemScore] = field(default_factory=list)
    mean_score: float = 0.0
    pass_threshold: float = 0.0
    passed: bool = False
    provider: str = ''
    model: str = ''
    tokens_in: int = 0
    tokens_out: int = 0
    error: str = ''


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an evaluator of AI tutor responses for a secondary-school tutoring \
platform. For each rubric item listed in the user message, judge whether \
the TUTOR RESPONSE TO EVALUATE satisfies it, taking the prior CONVERSATION \
into account.

Score each item on a continuous scale from 0.0 to 1.0:
- 1.0 = fully satisfies the rubric item
- 0.7 = mostly satisfies, minor issue
- 0.5 = partially satisfies — some merit but clear gaps
- 0.3 = barely satisfies — significant issue
- 0.0 = does not satisfy

Output ONLY a JSON object with EXACTLY this shape:
{"scores": [{"item": "<verbatim rubric item text>", "score": <float>, "reasoning": "<one sentence>"}, ...]}

Do not include any text outside the JSON. Do not wrap in markdown code fences. \
Use the exact rubric item text as the `item` key — verbatim.\
"""


def _format_conversation(conversation: list[dict]) -> str:
    if not conversation:
        return "(no prior conversation)"
    lines = []
    for turn in conversation:
        role = turn.get('role', '?').capitalize()
        text = (turn.get('content') or turn.get('text') or '').strip()
        lines.append(f"{role}: {text}")
    return '\n'.join(lines)


def _build_user_prompt(
    conversation: list[dict], student_turn: str, tutor_text: str,
    rubric_items: list[str],
) -> str:
    conv_str = _format_conversation(conversation)
    rubric_str = '\n'.join(
        f"{i+1}. {item}" for i, item in enumerate(rubric_items)
    )
    return (
        f"CONVERSATION:\n{conv_str}\n"
        f"Student: {student_turn}\n\n"
        f"TUTOR RESPONSE TO EVALUATE:\n{tutor_text}\n\n"
        f"RUBRIC ITEMS:\n{rubric_str}\n\n"
        f"Return JSON."
    )


def _build_trajectory_prompt(
    transcript: list[dict], rubric_items: list[str],
) -> str:
    """Variant prompt for whole-session evaluation (multi-turn scenarios)."""
    conv_str = _format_conversation(transcript)
    rubric_str = '\n'.join(
        f"{i+1}. {item}" for i, item in enumerate(rubric_items)
    )
    return (
        f"FULL TUTORING SESSION TRANSCRIPT:\n{conv_str}\n\n"
        f"RUBRIC ITEMS (judge whether each was satisfied ACROSS the session):\n"
        f"{rubric_str}\n\n"
        f"Return JSON."
    )


# ---------------------------------------------------------------------------
# JSON parsing — robust to small fence/quote drift
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r'```(?:json)?\s*(.*?)\s*```', re.DOTALL)


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    # Try direct parse first.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip markdown fences if present.
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Fall back to "find the largest {...} block".
    first = text.find('{')
    last = text.rfind('}')
    if first != -1 and last > first:
        try:
            return json.loads(text[first:last + 1])
        except json.JSONDecodeError:
            pass
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score(
    rubric_items: list[str],
    *,
    conversation: list[dict],
    student_turn: str,
    tutor_text: str,
    pass_threshold: float,
    judge_config: dict[str, Any] | None = None,
) -> RubricResult:
    """Score a tutor response against a rubric using a pinned judge LLM.

    Returns a populated ``RubricResult``. On any failure (no rubric items,
    LLM error, malformed JSON), returns a result with ``error`` set and
    ``passed=False``. Callers should treat error as a hard fail for the
    scenario.
    """
    cfg = {**DEFAULT_RUBRIC_JUDGE, **(judge_config or {})}
    provider = str(cfg['provider'])
    model = str(cfg['model'])
    temperature = float(cfg.get('temperature', 0.0))
    max_tokens = int(cfg.get('max_tokens', 1024))

    result = RubricResult(
        pass_threshold=pass_threshold, provider=provider, model=model,
    )

    if not rubric_items:
        result.error = 'no rubric items'
        return result

    # Resolve a usable ModelConfig (existing or in-memory fallback).
    model_config = ModelConfig.resolve_runtime(provider, model)
    if model_config is None:
        result.error = (
            f"could not resolve ModelConfig for provider={provider!r}, "
            f"model={model!r}"
        )
        return result

    try:
        client = get_llm_client(model_config)
    except Exception as exc:
        result.error = f"client init failed: {type(exc).__name__}: {exc}"
        return result

    user_prompt = _build_user_prompt(
        conversation, student_turn, tutor_text, rubric_items,
    )
    return _call_and_parse(
        client, user_prompt, rubric_items, max_tokens, temperature, result,
    )


def score_trajectory(
    rubric_items: list[str],
    *,
    transcript: list[dict],
    pass_threshold: float,
    judge_config: dict[str, Any] | None = None,
) -> RubricResult:
    """Score an entire session transcript against a rubric.

    Same return shape as ``score()``, but the rubric items are evaluated
    against the whole tutor↔student exchange rather than one response.
    Used by multi-turn scenarios (Phase 4).
    """
    cfg = {**DEFAULT_RUBRIC_JUDGE, **(judge_config or {})}
    provider = str(cfg['provider'])
    model = str(cfg['model'])
    temperature = float(cfg.get('temperature', 0.0))
    max_tokens = int(cfg.get('max_tokens', 1024))

    result = RubricResult(
        pass_threshold=pass_threshold, provider=provider, model=model,
    )
    if not rubric_items:
        result.error = 'no rubric items'
        return result

    model_config = ModelConfig.resolve_runtime(provider, model)
    if model_config is None:
        result.error = (
            f"could not resolve ModelConfig for provider={provider!r}, "
            f"model={model!r}"
        )
        return result
    try:
        client = get_llm_client(model_config)
    except Exception as exc:
        result.error = f"client init failed: {type(exc).__name__}: {exc}"
        return result

    user_prompt = _build_trajectory_prompt(transcript, rubric_items)
    return _call_and_parse(
        client, user_prompt, rubric_items, max_tokens, temperature, result,
    )


def _call_and_parse(
    client, user_prompt: str, rubric_items: list[str],
    max_tokens: int, temperature: float, result: RubricResult,
) -> RubricResult:
    """Shared judge-call + JSON parsing path. Mutates ``result`` and returns it."""

    try:
        resp = client.generate(
            messages=[{'role': 'user', 'content': user_prompt}],
            system_prompt=_SYSTEM_PROMPT,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as exc:
        result.error = f"judge call failed: {type(exc).__name__}: {exc}"
        return result

    result.tokens_in = resp.tokens_in
    result.tokens_out = resp.tokens_out

    parsed = _extract_json(resp.content)
    if parsed is None or 'scores' not in parsed:
        result.error = f"could not parse JSON response: {resp.content[:200]!r}"
        return result

    raw_scores = parsed.get('scores') or []
    if not isinstance(raw_scores, list):
        result.error = f"`scores` is not a list: {raw_scores!r}"
        return result

    # Map judged scores back to the rubric items by index — the judge is
    # told to return items in the same order. Tolerate length mismatch by
    # padding missing items with 0.0 and a "judge did not score this" note.
    for i, item in enumerate(rubric_items):
        if i < len(raw_scores) and isinstance(raw_scores[i], dict):
            raw = raw_scores[i]
            score_val = float(raw.get('score', 0.0))
            score_val = max(0.0, min(1.0, score_val))
            result.items.append(RubricItemScore(
                item=item, score=score_val,
                reasoning=str(raw.get('reasoning', '')).strip(),
            ))
        else:
            result.items.append(RubricItemScore(
                item=item, score=0.0,
                reasoning='judge did not score this item',
            ))

    if result.items:
        result.mean_score = sum(s.score for s in result.items) / len(result.items)
    result.passed = result.mean_score >= result.pass_threshold
    return result
