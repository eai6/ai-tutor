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
    # Bumped from 1024 → 4096 on 2026-05-27. The 8 BEA-aligned standard
    # rubric items + 3-5 scenario-specific items × {item, score,
    # reasoning} fields per entry consistently truncated at exactly
    # 1024 tokens in the phase-5 run — 42 of 80 scenarios returned
    # malformed JSON because the closing markdown fence got cut off.
    # 4096 gives ~3x headroom for the largest scenarios.
    'max_tokens': 4096,
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RubricItemScore:
    item: str
    score: float
    reasoning: str = ''
    # False when the judge returned "n/a" — a conditional item whose
    # precondition doesn't hold (e.g. an "if the student made a mistake…" item
    # on a no-mistake turn). N/A items are EXCLUDED from the mean (B3), so a
    # spurious 0.0 on an inapplicable item no longer sinks an otherwise-passing
    # scenario. ``score`` is 0.0 for n/a items but is not counted.
    applicable: bool = True


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


# ----------------------------------------------------------------------
# Pedagogical dimensions — structured multi-dimensional rubric
# ----------------------------------------------------------------------
#
# Added 2026-05-27 per `memory/simple_tutor_systematic_eval_plan.md`.
# Eight binary pedagogical dimensions evaluated in one judge call per
# single-turn scenario. Output is per-dimension yes/no, so a scenario
# passes iff every "desirable" dimension is at its desired value.
#
# The simple_tutor-specific rule-violation flags (meta_reasoning_leak,
# passive_ending) are NOT here — they live in the deterministic scorer
# (cheaper, regex-based, no LLM call needed). The judge call is reserved
# for the dimensions that genuinely need a model in the loop.


@dataclass
class DimensionScore:
    """One judged dimension. ``value`` is the judge's verdict
    (typically "yes" / "no", or "encouraging" / "neutral" / "offensive"
    for ``tutor_tone``). ``desirable`` is the value(s) that pass the
    dimension. ``passed`` = value is in the desirable set.
    """
    name: str
    value: str
    desirable: tuple[str, ...]
    reasoning: str = ''
    passed: bool = False


@dataclass
class DimensionsResult:
    dimensions: list[DimensionScore] = field(default_factory=list)
    passed: bool = False
    provider: str = ''
    model: str = ''
    tokens_in: int = 0
    tokens_out: int = 0
    error: str = ''


# Each entry: (name, prompt-facing question, desirable values).
# Desirable values are case-sensitive lowercase. The judge is asked to
# return a single value per dimension matching this set.
PEDAGOGICAL_DIMENSIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        'mistake_identification',
        'STEP 1: Did the student make a mistake on their last turn? '
        'STEP 2: If NO mistake was made, answer "yes" (recognition was correctly not needed). '
        'If a mistake WAS made, answer "yes" when the tutor acknowledged it or asked the '
        'student to reconsider, "no" when the tutor missed the mistake or affirmed the '
        'wrong answer.',
        ('yes',),
    ),
    (
        'mistake_location',
        'STEP 1: Did the tutor identify a mistake in their reply? '
        'STEP 2: If the tutor did NOT identify a mistake, answer "yes" (vacuously — '
        'nothing to misattribute). If the tutor DID identify a mistake, answer "yes" '
        'when the identified mistake matches the genuine error the student made, "no" '
        'when the tutor critiqued the wrong thing or invented a mistake the student '
        'did not make.',
        ('yes',),
    ),
    (
        'reveals_answer',
        'Did the tutor reveal the answer to a question that the student has NOT YET '
        'answered correctly? '
        'Answer "yes" if the tutor explicitly stated, named, or paraphrased the '
        'correct option/value for a question still being worked on (a hint turn '
        'after a wrong attempt, or before any attempt). '
        'Answer "no" if (a) the tutor only scaffolded with hints, (b) the student '
        'just answered correctly and the tutor is now affirming + advancing — '
        'affirming a correct answer is NOT revealing.',
        ('no',),
    ),
    (
        'providing_guidance',
        'Did the tutor offer correct, useful guidance — a hint, explanation, worked '
        'example, or framing — that moves the student forward? Answer "yes" or "no".',
        ('yes',),
    ),
    (
        'actionability',
        'After reading the tutor\'s reply, is it clear what the student should DO next '
        '(answer a specific question, compute a value, try an example)? Answer "yes" if '
        'the reply ends with a concrete action. Answer "no" if it ends passively '
        '("take your time", "let me know when ready", or generally trails off).',
        ('yes',),
    ),
    (
        'coherence',
        'Is the tutor\'s reply logically consistent with the prior conversation and '
        'the student\'s last turn? Answer "no" if it contradicts itself, ignores the '
        'student\'s actual question, or repeats prior content without acknowledging it.',
        ('yes',),
    ),
    (
        'tutor_tone',
        'What is the dominant tone of the tutor\'s reply? Answer one of: '
        '"encouraging" (warm, supportive), "neutral" (factual, no warmth/coldness), or '
        '"offensive" (dismissive, sarcastic, condescending).',
        ('encouraging', 'neutral'),
    ),
    (
        'human_likeness',
        'Does the reply read as natural human teaching — or robotic/templated? '
        'Answer "yes" for natural, "no" for robotic.',
        ('yes',),
    ),
)


_DIMENSIONS_SYSTEM_PROMPT = """\
You are an evaluator of AI tutor responses for a secondary-school tutoring \
platform. For each dimension listed in the user message, judge the TUTOR \
RESPONSE TO EVALUATE against the prior CONVERSATION and the student's last turn.

Each dimension is a discrete categorical judgement (typically yes/no, sometimes \
yes/no/encouraging/neutral). Do NOT score on a 0-1 scale — pick the single \
value that best describes the response.

Output ONLY a JSON object with EXACTLY this shape:
{"dimensions": [{"name": "<dimension name>", "value": "<one value>", "reasoning": "<one sentence>"}, ...]}

Use the exact dimension name as the `name` key. Return one entry per dimension, \
in the same order. Do not wrap in markdown code fences. Do not include any text \
outside the JSON.\
"""


def _build_dimensions_user_prompt(
    conversation: list[dict],
    student_turn: str,
    tutor_text: str,
) -> str:
    conv_str = _format_conversation(conversation)
    dim_str = '\n'.join(
        f"- {name}: {question} (allowed values: {', '.join(desirable + (_negate(desirable),))})"
        for name, question, desirable in PEDAGOGICAL_DIMENSIONS
    )
    return (
        f"CONVERSATION:\n{conv_str}\n"
        f"Student: {student_turn}\n\n"
        f"TUTOR RESPONSE TO EVALUATE:\n{tutor_text}\n\n"
        f"DIMENSIONS:\n{dim_str}\n\n"
        f"Return JSON."
    )


def _negate(desirable: tuple[str, ...]) -> str:
    """The plausible "undesirable" answer to list alongside the desired
    one, so the judge knows the full enum. For tutor_tone the negate is
    "offensive"; for the rest it's the opposite of yes/no.
    """
    if 'yes' in desirable and 'no' not in desirable:
        return 'no'
    if 'no' in desirable and 'yes' not in desirable:
        return 'yes'
    if 'encouraging' in desirable or 'neutral' in desirable:
        return 'offensive'
    return ''


def score_pedagogical_dimensions(
    *,
    conversation: list[dict],
    student_turn: str,
    tutor_text: str,
    judge_config: dict[str, Any] | None = None,
) -> DimensionsResult:
    """Score a tutor response against the eight pedagogical dimensions.

    Single judge call, structured JSON output. Returns a populated
    ``DimensionsResult`` where ``passed`` is True iff every dimension's
    value is in its desirable set.

    On any failure (LLM error, malformed JSON), returns a result with
    ``error`` set and ``passed=False``.
    """
    cfg = {**DEFAULT_RUBRIC_JUDGE, **(judge_config or {})}
    provider = str(cfg['provider'])
    model = str(cfg['model'])
    temperature = float(cfg.get('temperature', 0.0))
    max_tokens = int(cfg.get('max_tokens', 1024))

    result = DimensionsResult(provider=provider, model=model)

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

    user_prompt = _build_dimensions_user_prompt(
        conversation, student_turn, tutor_text,
    )

    try:
        resp = client.generate(
            messages=[{'role': 'user', 'content': user_prompt}],
            system_prompt=_DIMENSIONS_SYSTEM_PROMPT,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as exc:
        result.error = f"judge call failed: {type(exc).__name__}: {exc}"
        return result

    result.tokens_in = resp.tokens_in
    result.tokens_out = resp.tokens_out

    parsed = _extract_json(resp.content)
    if parsed is None or 'dimensions' not in parsed:
        result.error = f"could not parse JSON response: {resp.content[:200]!r}"
        return result

    raw = parsed.get('dimensions') or []
    if not isinstance(raw, list):
        result.error = f"`dimensions` is not a list: {raw!r}"
        return result

    # Index by name (case-insensitive) so the judge can return them
    # in any order without breaking the mapping.
    by_name: dict[str, dict] = {}
    for entry in raw:
        if isinstance(entry, dict):
            key = str(entry.get('name', '')).strip().lower()
            if key:
                by_name[key] = entry

    for name, _question, desirable in PEDAGOGICAL_DIMENSIONS:
        entry = by_name.get(name.lower(), {})
        value = str(entry.get('value', '')).strip().lower() or '?'
        reasoning = str(entry.get('reasoning', '')).strip()
        passed = value in desirable
        result.dimensions.append(DimensionScore(
            name=name,
            value=value,
            desirable=desirable,
            reasoning=reasoning,
            passed=passed,
        ))

    result.passed = all(d.passed for d in result.dimensions)
    return result


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

Some items are CONDITIONAL — they start with "If …" (e.g. "If the student made \
a mistake, …"). When such an item's condition does NOT hold for this turn (e.g. \
the student made no mistake and gave no answer — an off-topic, clarification, or \
distress turn), the item does not apply: return the string "n/a" as its score \
instead of a number. An "n/a" item is excluded from scoring — it neither helps \
nor hurts. Use "n/a" ONLY for genuinely inapplicable conditional items; when in \
doubt whether an item applies, score it normally.

Output ONLY a JSON object with EXACTLY this shape:
{"scores": [{"item": "<verbatim rubric item text>", "score": <float or "n/a">, "reasoning": "<one sentence>"}, ...]}

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
        f"RUBRIC ITEMS — judge each across the WHOLE session:\n{rubric_str}\n\n"
        "For each item: consider every tutor turn where the item is relevant "
        "(e.g. an 'if the student made a mistake' item applies only on turns "
        "where the student actually erred; a 'reveals the answer' item applies "
        "only while a question is unresolved). Score how well the tutor "
        "satisfied the item on those relevant turns overall — 1.0 if it held on "
        "every relevant turn, lower if it lapsed on some. If the item never "
        "became relevant anywhere in the session, return \"n/a\" for it (n/a is "
        "excluded from scoring — it neither helps nor hurts). Do not judge a "
        "single turn in isolation.\n\n"
        "Return JSON."
    )


# ---------------------------------------------------------------------------
# JSON parsing — robust to small fence/quote drift
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r'```(?:json)?\s*(.*?)\s*```', re.DOTALL)
_OPEN_FENCE_RE = re.compile(r'```(?:json)?\s*\n?', re.IGNORECASE)


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    # Try direct parse first.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip markdown fences if present (closed pair).
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Missing closing fence (truncation at max_tokens). Strip the
    # leading ```json fence and try repair-then-parse on the tail.
    om = _OPEN_FENCE_RE.search(text)
    if om:
        candidate = text[om.end():].rstrip('` \n')
        repaired = _repair_truncated_json(candidate)
        if repaired is not None:
            return repaired
    # Fall back to "find the largest {...} block".
    first = text.find('{')
    last = text.rfind('}')
    if first != -1 and last > first:
        try:
            return json.loads(text[first:last + 1])
        except json.JSONDecodeError:
            pass
    # Last resort: try repair on the whole thing.
    return _repair_truncated_json(text)


def _repair_truncated_json(text: str) -> dict | None:
    """Best-effort repair on a JSON string truncated mid-output.

    The truncation pattern we see from the rubric judge is: it returns
    a valid JSON prefix that hits max_tokens partway through one of the
    items. The closing brackets are missing. We close them.

    Returns the parsed dict on success, None on failure. Whatever
    items were complete at truncation time come through; the partial
    final item is discarded.
    """
    s = text.strip().rstrip('`').strip()
    if not s.startswith('{'):
        return None
    # Drop trailing partial entry (anything after the last complete
    # closing brace of an array element).
    # Strategy: walk forward tracking brace/bracket depth, recording
    # the position of the last balanced point inside the top-level
    # `scores` / `dimensions` array; then close the array + object.
    depth_curly = 0
    depth_square = 0
    in_string = False
    escape = False
    last_safe = -1
    for i, ch in enumerate(s):
        if escape:
            escape = False
            continue
        if ch == '\\' and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth_curly += 1
        elif ch == '}':
            depth_curly -= 1
            if depth_square >= 1 and depth_curly == 1:
                # Just closed an array element at top level.
                last_safe = i
        elif ch == '[':
            depth_square += 1
        elif ch == ']':
            depth_square -= 1
    if last_safe < 0:
        # Never closed any array element; can't repair.
        return None
    head = s[: last_safe + 1]
    # Close: end the array, end the outer object.
    repaired = head + ']}'
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
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
    # A score of "n/a" marks an inapplicable conditional item (B3): it is
    # recorded but excluded from the mean.
    for i, item in enumerate(rubric_items):
        if i < len(raw_scores) and isinstance(raw_scores[i], dict):
            raw = raw_scores[i]
            raw_score = raw.get('score', 0.0)
            reasoning = str(raw.get('reasoning', '')).strip()
            if isinstance(raw_score, str) and raw_score.strip().lower() in ('n/a', 'na'):
                result.items.append(RubricItemScore(
                    item=item, score=0.0, reasoning=reasoning, applicable=False,
                ))
            else:
                try:
                    score_val = max(0.0, min(1.0, float(raw_score)))
                except (TypeError, ValueError):
                    score_val = 0.0
                result.items.append(RubricItemScore(
                    item=item, score=score_val, reasoning=reasoning,
                ))
        else:
            result.items.append(RubricItemScore(
                item=item, score=0.0,
                reasoning='judge did not score this item',
            ))

    # Mean over APPLICABLE items only — n/a (inapplicable conditional) items are
    # excluded so a spurious 0.0 can't sink an otherwise-passing scenario. If
    # every item is n/a (no applicable rubric to judge), treat the layer as a
    # vacuous pass.
    scored = [s for s in result.items if s.applicable]
    if scored:
        result.mean_score = sum(s.score for s in scored) / len(scored)
    elif result.items:
        result.mean_score = 1.0
    result.passed = result.mean_score >= result.pass_threshold
    return result
