"""Benchmark candidate tutor models on a representative math turn.

Runs the SAME prompt + tool against each candidate model, three times,
and reports median latency, token counts, and whether the model called
the pose_question tool.

Usage:
    DJANGO_SETTINGS_MODULE=ai_tutor.config.settings venv/bin/python scripts/bench_tutoring_models.py

The script needs API keys via env vars (or active ModelConfig rows):
    ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY

It does not modify any data — purely measurement.
"""
from __future__ import annotations

import json
import os
import sys
import time
import statistics
from dataclasses import dataclass
from typing import Optional

# Add repo root to sys.path so we can import config / apps
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Bootstrap Django so we can use the existing client classes
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ai_tutor.config.settings")
import django  # noqa: E402
django.setup()

from ai_tutor.apps.llm.client import (  # noqa: E402
    AnthropicClient, OpenAIClient, GeminiClient,
)


# ---------------------------------------------------------------------------
# Candidate models — adjust as needed before running
# ---------------------------------------------------------------------------

CANDIDATES = [
    # (provider, model_name)
    ("anthropic", "claude-haiku-4-5-20251001"),
    ("anthropic", "claude-sonnet-4-20250514"),
    ("anthropic", "claude-opus-4-7"),
    ("openai", "gpt-4o-mini"),
    ("openai", "gpt-4o"),
    ("openai", "gpt-4.1"),
    ("openai", "gpt-5"),
    ("openai", "o3-mini"),
    ("google", "gemini-2.0-flash"),
    ("google", "gemini-2.5-pro"),
    ("google", "gemini-3-pro-preview"),
]

# Number of trials per model (median is reported)
TRIALS = 3


# ---------------------------------------------------------------------------
# Representative tutor prompt (math, mid-turn)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a Socratic math tutor for a Grade 8/9 student in Seychelles. \
Your job is to scaffold the student through a single lesson on \
"angles around a point" — never to author or solve.

<socratic_rules>
1. NEVER praise an answer until you've seen the student's reasoning. \
Words like "perfect", "exactly right", "great job" are forbidden when \
the student gave a one-line answer with no explanation. Instead ask \
"What makes you think that?"
2. ALWAYS END WITH A QUESTION. The tutor leads — every turn ends with \
a question that moves the student forward.
3. ONE NEW IDEA AT A TIME. Don't list 5 facts. Introduce one concept, \
ask the student to engage, then layer the next.
4. NEVER NARRATE WHAT YOU ARE ABOUT TO DO.
</socratic_rules>

<math_teaching>
=== R1: BANK IS THE SOURCE OF TRUTH ===
For any question with numerical values:
  • POSE — call the pose_question tool with a slot from the
    <question_bank> below. The tool is the ONLY way to ask a
    numerical question. Do NOT type questions in your text response.
  • GRADE — read the verdict from <bank_evaluation_signal>. The
    server already checked against the bank's stored answer; do NOT
    recompute.
  • EXPLAIN — quote the canonical_working from the bank verbatim.

=== R2: WORKING BEFORE EVALUATION ===
NEVER confirm or deny a bare answer (e.g. '35', 'x=4'). Respond:
'Before I check that — show me your working.'

=== R3: SCAFFOLD, DON'T SOLVE ===
Never do the math FOR the student. Wrong answer → give a TARGETED
HINT, ask retry. Reveal answer only after 5+ wrong attempts on the
same step OR explicit 'I give up'.
</math_teaching>

<question_bank>
  HARD RULE — questions you pose MUST come from this bank.
  To ask any numerical question, call the pose_question tool with a
  slot index. Slot 0 = current step's canonical question.
  Slots 1+ = exit-ticket bank questions for this step's concept.
  [0] (current step) Three angles around a point are 95°, 70°, and x°. Find x.
      expected_answer: 195°
  [1] Two parallel lines are cut by a transversal. One corresponding angle measures 120°. What is the measure of the other corresponding angle?   (concept=corresponding_angles, answer=120°)
  [2] Two angles around a point are 130° and 90°. Find the third angle.   (concept=angles_around_point, answer=140°)
  [3] Four angles around a point are 80°, 100°, 60°, and x°. Find x.   (concept=angles_around_point, answer=120°)
  [4] In a circle, what is the sum of all angles meeting at the centre?   (concept=angles_around_point, answer=360°)
</question_bank>

<bank_evaluation_signal>
  The student's PREVIOUS answer was a bank question (slot 0).
  Verdict: INCORRECT
  Canonical answer: 195°
  Canonical working: 360 - (95 + 70) = 360 - 165 = 195, so x = 195°.
  Per math_teaching Rule 1, do NOT say "correct" or "wrong" — ask
  the student to walk through their working before any confirmation.
</bank_evaluation_signal>

<final_reminder>
MATH RULE — to ask any numerical question, call pose_question. Do
NOT type questions in your text response. The bank is the only
source of truth.
</final_reminder>
"""

USER_MESSAGE = (
    "I think x = 65 because 360 - 295 = 65"
)

POSE_QUESTION_TOOL = {
    "name": "pose_question",
    "description": (
        "Pose a verified bank question to the student. This is the "
        "ONLY way to ask any numerical question. The slot index "
        "refers to the <question_bank> in the system prompt. Slot 0 "
        "is the current step's canonical question; slots 1+ are "
        "exit-ticket bank questions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slot": {
                "type": "integer",
                "minimum": 0,
                "maximum": 4,
                "description": "Bank slot to pose. Must be 0..4.",
            },
            "lead_in": {
                "type": "string",
                "description": "Optional one-sentence framing (e.g. 'Try this:').",
            },
        },
        "required": ["slot"],
    },
}


# ---------------------------------------------------------------------------
# Adapter — uniform call across providers
# ---------------------------------------------------------------------------


@dataclass
class TrialResult:
    latency_ms: float
    tokens_in: int
    tokens_out: int
    tool_called: bool
    tool_slot: Optional[int]
    text_excerpt: str
    error: Optional[str] = None


def _make_config(provider: str, model: str):
    """Build a minimal stand-in for ModelConfig so the existing
    client classes can run. We don't need the DB."""
    class _Config:
        pass
    cfg = _Config()
    cfg.provider = provider
    cfg.model_name = model
    cfg.max_tokens = 1024
    cfg.temperature = 0.7
    if provider == "anthropic":
        cfg.api_key_env_var = "ANTHROPIC_API_KEY"
    elif provider == "openai":
        cfg.api_key_env_var = "OPENAI_API_KEY"
    elif provider == "google":
        cfg.api_key_env_var = "GOOGLE_API_KEY"
    cfg.api_base = ""

    def _get_api_key():
        env_var = getattr(cfg, "api_key_env_var", "")
        return os.environ.get(env_var, "") if env_var else ""

    cfg.get_api_key = _get_api_key
    return cfg


def _make_client(provider: str, model: str):
    cfg = _make_config(provider, model)
    if provider == "anthropic":
        return AnthropicClient(cfg)
    if provider == "openai":
        return OpenAIClient(cfg)
    if provider == "google":
        return GeminiClient(cfg)
    raise ValueError(f"Unknown provider: {provider}")


def _run_anthropic(client, model: str) -> TrialResult:
    t0 = time.perf_counter()
    msg = client.generate_with_tools(
        messages=[{"role": "user", "content": USER_MESSAGE}],
        system_prompt=SYSTEM_PROMPT,
        tools=[POSE_QUESTION_TOOL],
        max_tokens=1024,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    tool_called = False
    tool_slot = None
    text_excerpt = ""
    for block in (msg.content or []):
        btype = getattr(block, "type", None)
        if btype == "tool_use" and getattr(block, "name", "") == "pose_question":
            tool_called = True
            inp = getattr(block, "input", {}) or {}
            tool_slot = inp.get("slot")
        elif btype == "text":
            text_excerpt += (getattr(block, "text", "") or "")
    return TrialResult(
        latency_ms=elapsed_ms,
        tokens_in=msg.usage.input_tokens,
        tokens_out=msg.usage.output_tokens,
        tool_called=tool_called,
        tool_slot=tool_slot,
        text_excerpt=text_excerpt[:200],
    )


def _run_openai(client, model: str) -> TrialResult:
    t0 = time.perf_counter()
    response = client.generate_with_tools(
        messages=[{"role": "user", "content": USER_MESSAGE}],
        system_prompt=SYSTEM_PROMPT,
        tools=[POSE_QUESTION_TOOL],
        max_tokens=1024,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    msg = response.choices[0].message
    tool_called = False
    tool_slot = None
    if msg.tool_calls:
        for tc in msg.tool_calls:
            if tc.function.name == "pose_question":
                tool_called = True
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    tool_slot = args.get("slot")
                except Exception:
                    tool_slot = None
                break
    text_excerpt = (msg.content or "")[:200]
    return TrialResult(
        latency_ms=elapsed_ms,
        tokens_in=getattr(response.usage, "prompt_tokens", 0),
        tokens_out=getattr(response.usage, "completion_tokens", 0),
        tool_called=tool_called,
        tool_slot=tool_slot,
        text_excerpt=text_excerpt,
    )


def _run_google(client, model: str) -> TrialResult:
    t0 = time.perf_counter()
    response = client.generate_with_tools(
        messages=[{"role": "user", "content": USER_MESSAGE}],
        system_prompt=SYSTEM_PROMPT,
        tools=[POSE_QUESTION_TOOL],
        max_tokens=1024,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    tool_called = False
    tool_slot = None
    text_excerpt = ""
    try:
        for cand in (response.candidates or []):
            for part in (cand.content.parts or []):
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", "") == "pose_question":
                    tool_called = True
                    args = dict(fc.args or {})
                    tool_slot = args.get("slot")
                elif getattr(part, "text", None):
                    text_excerpt += (part.text or "")
    except Exception:
        pass
    return TrialResult(
        latency_ms=elapsed_ms,
        tokens_in=getattr(response.usage_metadata, "prompt_token_count", 0) or 0,
        tokens_out=getattr(response.usage_metadata, "candidates_token_count", 0) or 0,
        tool_called=tool_called,
        tool_slot=tool_slot,
        text_excerpt=text_excerpt[:200],
    )


PROVIDER_RUNNER = {
    "anthropic": _run_anthropic,
    "openai": _run_openai,
    "google": _run_google,
}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_one(provider: str, model: str, trials: int) -> list[TrialResult]:
    print(f"\n→ {provider:9s} {model}")
    sys.stdout.flush()
    try:
        client = _make_client(provider, model)
    except Exception as e:
        print(f"   client init FAILED: {e}")
        return [TrialResult(0, 0, 0, False, None, "", error=str(e))]

    runner = PROVIDER_RUNNER[provider]
    results = []
    for i in range(trials):
        try:
            r = runner(client, model)
            tag = "tool" if r.tool_called else "text"
            print(
                f"   trial {i+1}/{trials}: {r.latency_ms:7.0f}ms  "
                f"in={r.tokens_in:5d} out={r.tokens_out:4d}  "
                f"{tag} slot={r.tool_slot}"
            )
            results.append(r)
        except Exception as e:
            err_short = (str(e) or type(e).__name__)[:140]
            print(f"   trial {i+1}/{trials}: ERROR: {err_short}")
            results.append(TrialResult(0, 0, 0, False, None, "", error=err_short))
    return results


def summarise(results_by_model: dict[tuple[str, str], list[TrialResult]]):
    print("\n" + "=" * 96)
    print(
        f"{'PROVIDER':10s}  {'MODEL':35s}  "
        f"{'MEDIAN ms':>10s}  {'MIN ms':>8s}  {'MAX ms':>8s}  "
        f"{'IN':>6s}  {'OUT':>5s}  {'TOOL':>4s}"
    )
    print("-" * 96)
    rows = []
    for (provider, model), results in results_by_model.items():
        ok = [r for r in results if not r.error]
        if not ok:
            err = (results[0].error or "no result")[:50] if results else "no result"
            print(f"{provider:10s}  {model:35s}  ERROR: {err}")
            rows.append((provider, model, float("inf"), 0, False))
            continue
        latencies = [r.latency_ms for r in ok]
        median = statistics.median(latencies)
        mn = min(latencies)
        mx = max(latencies)
        in_med = int(statistics.median([r.tokens_in for r in ok]))
        out_med = int(statistics.median([r.tokens_out for r in ok]))
        tool_rate = sum(1 for r in ok if r.tool_called) / len(ok)
        rows.append((provider, model, median, tool_rate, True))
        print(
            f"{provider:10s}  {model:35s}  "
            f"{median:10.0f}  {mn:8.0f}  {mx:8.0f}  "
            f"{in_med:6d}  {out_med:5d}  {tool_rate*100:3.0f}%"
        )
    print("=" * 96)

    print("\nLatency leaderboard (median, lower is better):")
    rows.sort(key=lambda r: r[2])
    for i, (provider, model, median, tool_rate, ok) in enumerate(rows, 1):
        if not ok:
            continue
        marker = " ✓" if tool_rate >= 0.5 else " ✗ low tool-use"
        print(f"  {i:2d}. {provider:9s} {model:35s}  {median:7.0f}ms{marker}")


def main():
    print(f"Bench — {len(CANDIDATES)} models × {TRIALS} trials")
    results_by_model: dict[tuple[str, str], list[TrialResult]] = {}
    for provider, model in CANDIDATES:
        results_by_model[(provider, model)] = run_one(provider, model, TRIALS)
    summarise(results_by_model)


if __name__ == "__main__":
    main()
