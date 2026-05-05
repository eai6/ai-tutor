"""Quality + compliance bench for candidate tutor models.

Two scenarios:

  A — Tool compliance. Student is mid-lesson and ready for the next
      question. The system prompt has a question_bank with 5 slots
      plus the pose_question tool. The model SHOULD call the tool
      with a valid slot. Metric: tool-call rate, valid-slot rate.

  B — Math correctness under self-generation. The model is given a
      plain word-problem with NO tool and NO bank — just a math
      tutor system prompt. The model has to do the arithmetic
      itself. Metric: did the response contain the correct numerical
      answer(s)?

This complements the latency-only bench by showing how each model
behaves when (1) the structure is in place — does it use the tool —
and (2) when it has to compute by itself — is its math right.

Usage:
    DJANGO_SETTINGS_MODULE=config.settings venv/bin/python scripts/bench_tutor_quality.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import statistics
from dataclasses import dataclass
from typing import Callable, Optional

# Add repo root to sys.path so we can import config / apps
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Bootstrap Django so we can use the existing client classes
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402
django.setup()

from apps.llm.client import (  # noqa: E402
    AnthropicClient, OpenAIClient, GeminiClient,
)


# ---------------------------------------------------------------------------
# Candidates — keep models that completed the latency bench. gpt-5 too slow
# for real-time tutoring; deprecated/503 google models excluded.
# ---------------------------------------------------------------------------

CANDIDATES = [
    ("anthropic", "claude-haiku-4-5-20251001"),
    ("anthropic", "claude-sonnet-4-20250514"),
    ("anthropic", "claude-opus-4-7"),
    ("openai", "gpt-4o-mini"),
    ("openai", "gpt-4o"),
    ("openai", "gpt-4.1"),
    ("openai", "o3-mini"),
    ("google", "gemini-3-pro-preview"),
]

TRIALS = 3


# ---------------------------------------------------------------------------
# Scenario A — Tool compliance (ready-for-next-question)
# ---------------------------------------------------------------------------

SCENARIO_A_SYSTEM = """You are a Socratic math tutor for a Grade 8/9 student in Seychelles.
Your job is to scaffold the student through a single lesson on
"angles around a point" — never to author or solve.

<socratic_rules>
1. ALWAYS END WITH A QUESTION. The tutor leads — every turn ends with a
   question that moves the student forward.
2. ONE NEW IDEA AT A TIME.
</socratic_rules>

<math_teaching>
=== R1: BANK IS THE SOURCE OF TRUTH ===
For any question with numerical values:
  • POSE — call the pose_question tool with a slot from the
    <question_bank> below. The tool is the ONLY way to ask a
    numerical question. Do NOT type questions in your text response.
</math_teaching>

<question_bank>
  HARD RULE — questions you pose MUST come from this bank.
  To ask any numerical question, call the pose_question tool with a
  slot index from the list below.
  [0] (current step) Three angles around a point are 95°, 70°, and x°. Find x.
      expected_answer: 195°
  [1] Two angles around a point are 130° and 90°. Find the third angle.   (concept=angles_around_point, answer=140°)
  [2] Four angles around a point are 80°, 100°, 60°, and x°. Find x.   (concept=angles_around_point, answer=120°)
  [3] In a circle, what is the sum of all angles meeting at the centre?   (concept=angles_around_point, answer=360°)
  [4] Five angles around a point are 75°, 60°, 75°, 65°, and x°. Find x.   (concept=angles_around_point, answer=85°)
</question_bank>

<final_reminder>
MATH RULE — to ask any numerical question, call pose_question. Do NOT
type questions in your text response. The bank is the only source of truth.
</final_reminder>
"""

SCENARIO_A_USER = "Got that one right! Give me another question to try."


# ---------------------------------------------------------------------------
# Scenario B — Math correctness under self-generation (no tool)
# ---------------------------------------------------------------------------

SCENARIO_B_SYSTEM = """You are a math tutor for a Grade 8/9 student.
Solve any math problem the student asks, showing the working clearly."""

SCENARIO_B_USER = (
    "Five angles meet at a single point. Four of them measure 75°, 60°, "
    "75°, and 65°. What is the sum of those four, and what is the size "
    "of the fifth angle?"
)

# Expected answers for grading
SCENARIO_B_EXPECTED_SUM = 275  # 75 + 60 + 75 + 65
SCENARIO_B_EXPECTED_FIFTH = 85  # 360 - 275


# ---------------------------------------------------------------------------
# pose_question tool definition (Scenario A only)
# ---------------------------------------------------------------------------

POSE_QUESTION_TOOL = {
    "name": "pose_question",
    "description": (
        "Pose a verified bank question to the student. This is the "
        "ONLY way to ask any numerical question. The slot index "
        "refers to the <question_bank> in the system prompt."
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
                "description": "Optional one-sentence framing before the question.",
            },
        },
        "required": ["slot"],
    },
}

VALID_SLOTS = set(range(5))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class TrialResult:
    latency_ms: float
    tokens_in: int
    tokens_out: int
    text_excerpt: str
    # Scenario A
    tool_called: bool = False
    tool_slot: Optional[int] = None
    valid_slot: bool = False
    text_contains_question_mark: bool = False
    # Scenario B
    sum_correct: bool = False
    fifth_correct: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Client setup (same shape as bench_tutoring_models)
# ---------------------------------------------------------------------------


def _make_config(provider: str, model: str):
    class _Config:
        pass
    cfg = _Config()
    cfg.provider = provider
    cfg.model_name = model
    cfg.max_tokens = 1024
    cfg.temperature = 0.7
    cfg.api_key_env_var = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "google": "GOOGLE_API_KEY",
    }.get(provider, "")
    cfg.api_base = ""

    def _get_api_key():
        return os.environ.get(cfg.api_key_env_var, "")

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
    raise ValueError(provider)


# ---------------------------------------------------------------------------
# Scenario A runners — test tool compliance
# ---------------------------------------------------------------------------


def _run_a_anthropic(client) -> TrialResult:
    t0 = time.perf_counter()
    msg = client.generate_with_tools(
        messages=[{"role": "user", "content": SCENARIO_A_USER}],
        system_prompt=SCENARIO_A_SYSTEM,
        tools=[POSE_QUESTION_TOOL],
        max_tokens=512,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    tool_called = False
    tool_slot = None
    text_excerpt = ""
    for block in (msg.content or []):
        btype = getattr(block, "type", None)
        if btype == "tool_use" and getattr(block, "name", "") == "pose_question":
            tool_called = True
            tool_slot = (getattr(block, "input", {}) or {}).get("slot")
        elif btype == "text":
            text_excerpt += (getattr(block, "text", "") or "")
    return TrialResult(
        latency_ms=elapsed_ms,
        tokens_in=msg.usage.input_tokens,
        tokens_out=msg.usage.output_tokens,
        text_excerpt=text_excerpt[:200],
        tool_called=tool_called,
        tool_slot=tool_slot,
        valid_slot=isinstance(tool_slot, int) and tool_slot in VALID_SLOTS,
        text_contains_question_mark="?" in text_excerpt,
    )


def _run_a_openai(client) -> TrialResult:
    t0 = time.perf_counter()
    response = client.generate_with_tools(
        messages=[{"role": "user", "content": SCENARIO_A_USER}],
        system_prompt=SCENARIO_A_SYSTEM,
        tools=[POSE_QUESTION_TOOL],
        max_tokens=512,
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
        text_excerpt=text_excerpt,
        tool_called=tool_called,
        tool_slot=tool_slot,
        valid_slot=isinstance(tool_slot, int) and tool_slot in VALID_SLOTS,
        text_contains_question_mark="?" in (text_excerpt or ""),
    )


def _run_a_google(client) -> TrialResult:
    t0 = time.perf_counter()
    response = client.generate_with_tools(
        messages=[{"role": "user", "content": SCENARIO_A_USER}],
        system_prompt=SCENARIO_A_SYSTEM,
        tools=[POSE_QUESTION_TOOL],
        max_tokens=512,
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
        text_excerpt=text_excerpt[:200],
        tool_called=tool_called,
        tool_slot=tool_slot,
        valid_slot=isinstance(tool_slot, int) and tool_slot in VALID_SLOTS,
        text_contains_question_mark="?" in (text_excerpt or ""),
    )


# ---------------------------------------------------------------------------
# Scenario B runners — test math correctness without tool
# ---------------------------------------------------------------------------


def _check_math_response(text: str) -> tuple[bool, bool]:
    """Return (sum_correct, fifth_correct) by string-searching the
    response for the expected numbers.

    Heuristic: match the digits with a degree marker or word boundary
    so we don't false-positive on "275 days" or "85 years".
    """
    text_l = text.lower()
    # Look for "275" near "sum" or "= 275"
    sum_re = re.compile(r"\b275\s*°?\b")
    fifth_re = re.compile(r"\b85\s*°?\b")
    sum_correct = bool(sum_re.search(text_l))
    fifth_correct = bool(fifth_re.search(text_l))
    return sum_correct, fifth_correct


def _run_b_generic(client, provider: str) -> TrialResult:
    """Plain text generation, no tool. Works for all providers via
    the standard `generate` API."""
    t0 = time.perf_counter()
    resp = client.generate(
        messages=[{"role": "user", "content": SCENARIO_B_USER}],
        system_prompt=SCENARIO_B_SYSTEM,
        max_tokens=512,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    text = resp.content or ""
    sum_ok, fifth_ok = _check_math_response(text)
    return TrialResult(
        latency_ms=elapsed_ms,
        tokens_in=resp.tokens_in,
        tokens_out=resp.tokens_out,
        text_excerpt=text[:240],
        sum_correct=sum_ok,
        fifth_correct=fifth_ok,
    )


SCENARIO_A_RUNNER = {
    "anthropic": _run_a_anthropic,
    "openai": _run_a_openai,
    "google": _run_a_google,
}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_scenario(label: str, runner_fn: Callable, trials: int):
    """Run a scenario across all candidate models.

    `runner_fn(client, provider) -> TrialResult` is the per-trial
    runner.
    """
    print(f"\n{'='*100}\nSCENARIO {label}\n{'='*100}")
    results_by_model: dict[tuple[str, str], list[TrialResult]] = {}
    for provider, model in CANDIDATES:
        print(f"\n→ {provider:9s} {model}")
        sys.stdout.flush()
        try:
            client = _make_client(provider, model)
        except Exception as e:
            print(f"   client init FAILED: {e}")
            results_by_model[(provider, model)] = [
                TrialResult(0, 0, 0, "", error=str(e))
            ]
            continue
        trial_results: list[TrialResult] = []
        for i in range(trials):
            try:
                r = runner_fn(client, provider)
                trial_results.append(r)
                print(
                    f"   trial {i+1}/{trials}: {r.latency_ms:6.0f}ms  "
                    f"in={r.tokens_in:5d} out={r.tokens_out:4d}  "
                    f"{_summarise_trial(r)}"
                )
            except Exception as e:
                err_short = (str(e) or type(e).__name__)[:120]
                print(f"   trial {i+1}/{trials}: ERROR: {err_short}")
                trial_results.append(TrialResult(0, 0, 0, "", error=err_short))
        results_by_model[(provider, model)] = trial_results
    return results_by_model


def _summarise_trial(r: TrialResult) -> str:
    if r.tool_called:
        slot_tag = f"slot={r.tool_slot}{'' if r.valid_slot else '!'}"
        return f"TOOL {slot_tag}"
    bits = []
    if r.sum_correct:
        bits.append("sum✓")
    if r.fifth_correct:
        bits.append("fifth✓")
    if not bits:
        bits.append("text-only")
    if r.text_contains_question_mark:
        bits.append("?-in-prose")
    return " ".join(bits)


def summarise_a(results_by_model):
    print(f"\n{'='*100}\nSUMMARY — Scenario A: Tool Compliance\n{'='*100}")
    print(f"{'PROVIDER':10s}  {'MODEL':35s}  {'TOOL':>6s}  {'VALID':>6s}  {'?-PROSE':>8s}  {'MEDIAN ms':>10s}")
    print("-"*100)
    rows = []
    for (provider, model), results in results_by_model.items():
        ok = [r for r in results if not r.error]
        if not ok:
            print(f"{provider:10s}  {model:35s}  ERROR")
            continue
        tool_rate = sum(1 for r in ok if r.tool_called) / len(ok) * 100
        valid_rate = sum(1 for r in ok if r.valid_slot) / len(ok) * 100
        q_in_prose = sum(1 for r in ok if r.text_contains_question_mark) / len(ok) * 100
        median = statistics.median([r.latency_ms for r in ok])
        rows.append((provider, model, tool_rate, valid_rate, q_in_prose, median))
        print(f"{provider:10s}  {model:35s}  {tool_rate:5.0f}%  {valid_rate:5.0f}%  {q_in_prose:7.0f}%  {median:10.0f}")


def summarise_b(results_by_model):
    print(f"\n{'='*100}\nSUMMARY — Scenario B: Math Correctness Without Tool\n{'='*100}")
    print(f"{'PROVIDER':10s}  {'MODEL':35s}  {'SUM (275°)':>10s}  {'FIFTH (85°)':>11s}  {'BOTH':>6s}  {'MEDIAN ms':>10s}")
    print("-"*100)
    for (provider, model), results in results_by_model.items():
        ok = [r for r in results if not r.error]
        if not ok:
            print(f"{provider:10s}  {model:35s}  ERROR")
            continue
        sum_rate = sum(1 for r in ok if r.sum_correct) / len(ok) * 100
        fifth_rate = sum(1 for r in ok if r.fifth_correct) / len(ok) * 100
        both_rate = sum(1 for r in ok if r.sum_correct and r.fifth_correct) / len(ok) * 100
        median = statistics.median([r.latency_ms for r in ok])
        print(f"{provider:10s}  {model:35s}  {sum_rate:9.0f}%  {fifth_rate:10.0f}%  {both_rate:5.0f}%  {median:10.0f}")


def print_excerpts(scenario: str, results_by_model):
    """Print one example response per model so we can eyeball quality."""
    print(f"\n{'='*100}\nSAMPLE RESPONSES — Scenario {scenario}\n{'='*100}")
    for (provider, model), results in results_by_model.items():
        ok = [r for r in results if not r.error]
        if not ok:
            continue
        sample = ok[0]
        if sample.tool_called:
            print(f"\n[{provider}/{model}] TOOL_CALLED slot={sample.tool_slot}")
            if sample.text_excerpt.strip():
                print(f"  text-block: {sample.text_excerpt!r}")
        else:
            print(f"\n[{provider}/{model}] text-only")
            print(f"  {sample.text_excerpt!r}")


def main():
    print(f"Quality bench — {len(CANDIDATES)} models × {TRIALS} trials × 2 scenarios")

    results_a = run_scenario(
        "A — Tool compliance (student asks for next question)",
        lambda client, provider: SCENARIO_A_RUNNER[provider](client),
        TRIALS,
    )

    results_b = run_scenario(
        "B — Math correctness without tool (75+60+75+65 + fifth angle)",
        _run_b_generic,
        TRIALS,
    )

    summarise_a(results_a)
    print_excerpts("A", results_a)
    summarise_b(results_b)
    print_excerpts("B", results_b)


if __name__ == "__main__":
    main()
