"""Per-family model profiles for the offline tutor evaluation.

Single source of truth for the *sampling / mode* knobs each model family needs
to be benchmarked fairly. Derived from ``offline_eval/PROMPT_ENGINEERING_FRAMEWORK.md``
(§3 per-family deep dives + Appendix A quick-reference). Companion to the prompt
TEXT, which stays single-sourced in ``apps/tutoring/simple_tutor/prompts.py``
(see ``family_prompt_delta``): WORDS live there, KNOBS live here.

Why this exists
---------------
The eval drives every tutor model through ``simple_tutor`` with a hardcoded
``max_tokens=1024`` and no per-family sampling, so:
  - *thinking* models truncate their ``<think>`` block before emitting the
    tool-call (deepseek-r1 22%, qwen3-next-80b-thinking 2% — harness artifacts,
    not capability); and
  - every cloud model ran at temp ≈ 0 (``resolve_runtime`` builds a synthetic
    config with ``purpose=JUDGE``), ignoring the framework's mode-specific
    sampling (Qwen 0.6-0.7 + top_p/top_k, Gemini-3 leave 1.0, Mistral Nemo 0.3,
    never-greedy on thinking models).

This registry is consumed ONLY by ``apps/tutoring/simple_tutor/engine.py::_call_llm``
when ``TUTOR_MODEL_OVERRIDE`` is set (i.e. during a sweep). When the override is
unset (production), ``get_model_profile`` returns ``None`` and behaviour is
unchanged — the ``[0.1,0.3]`` tutoring clamp and ``JUDGE=0`` invariants in
``apps/llm/models.py::effective_temperature`` are left fully intact. The eval
bypasses the clamp by passing explicit sampling at the call site — the same
mechanism the regen ensemble already uses legitimately.

Placing this in ``apps/llm/`` (not ``offline_eval/``) makes it AVAILABLE to
production should we later choose per-family sampling for the live tutor; this
task does not wire it there.

Adding a model = one entry in ``MODEL_PROFILES`` (exact spec) or a
``FAMILY_PATTERNS`` row (regex fallback for new point-versions / local tags).
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProfile:
    """Eval-time sampling + mode knobs for one model (or family).

    ``temperature is None`` means "do not send temperature" — let the provider
    use its own default (e.g. Gemini-3, where the framework says leave 1.0).
    All optional fields default to None so each provider client picks the keys
    it supports and ignores the rest.
    """
    family: str            # qwen|grok|gemini|deepseek|glm|kimi|mistral|anthropic|granite|llama
    mode: str              # 'instruct' | 'thinking' | 'non_thinking'
    max_tokens: int        # output budget; thinking ≥16-32K so the trace + tool-call fit
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    presence_penalty: float | None = None
    num_ctx: int | None = None         # Ollama context window (prompt+generation). None → client sizes it to num_predict+headroom (Ollama's 4096 default truncates reasoning models mid-<think>).
    extra_body: dict | None = None     # provider thinking toggle, e.g. {"chat_template_kwargs": {"thinking": False}}
    prompt_strategy: str = "default"   # 'default' | 'persona_suppress' | 'no_system_no_fewshot'
    prompt_format: str = "xml"         # 'xml' (default, Claude/Grok/GLM/Kimi/DeepSeek) | 'markdown' (Gemini/Qwen)
    notes: str = ""

    def sampling_dict(self) -> dict:
        """The non-None sampling fields, as a dict for the client layer.

        Empty dict is meaningful: it signals 'a profile applies, but send no
        sampling overrides' (e.g. Gemini left at its default temperature).
        """
        out: dict = {}
        if self.temperature is not None:
            out["temperature"] = self.temperature
        if self.top_p is not None:
            out["top_p"] = self.top_p
        if self.top_k is not None:
            out["top_k"] = self.top_k
        if self.presence_penalty is not None:
            out["presence_penalty"] = self.presence_penalty
        if self.num_ctx is not None:
            out["num_ctx"] = self.num_ctx
        if self.extra_body:
            out["extra_body"] = dict(self.extra_body)
        return out


# Token budgets (framework §2.1 / §3): thinking models need room for the reasoning
# trace BEFORE the tool-call; instruct models are fine at the historical default.
_MT_INSTRUCT = 16000
_MT_THINKING = 32768
_MT_ANTHROPIC = 2048     # raise the eval ceiling above the legacy 1024 (Claude rarely hits it)
_MT_GEMINI_THINK = 8192
_MT_GROK_THINK = 8192

# DeepSeek-chat (non-thinking) toggle on the OpenAI-compatible Vertex endpoint.
_DEEPSEEK_NO_THINK = {"chat_template_kwargs": {"thinking": False}}


# ── Exact-spec profiles — every row in offline_eval/cloud_models.txt ──────────
# Keyed by the FULL TUTOR_MODEL_OVERRIDE spec string ("provider/model[/...]").
MODEL_PROFILES: dict[str, ModelProfile] = {
    # --- Anthropic (prod-owned sampling; eval just raises the max_tokens ceiling) ---
    "anthropic/claude-opus-4-7": ModelProfile(
        family="anthropic", mode="thinking", max_tokens=_MT_ANTHROPIC,
        notes="Adaptive thinking; temperature owned by prod path / _supports_temperature.",
    ),
    "anthropic/claude-sonnet-4-6": ModelProfile(
        family="anthropic", mode="instruct", max_tokens=_MT_ANTHROPIC,
    ),
    "anthropic/claude-haiku-4-5-20251001": ModelProfile(
        family="anthropic", mode="instruct", max_tokens=_MT_ANTHROPIC,
    ),

    # --- Gemini (framework §3.5: leave temperature at provider default = 1.0) ---
    "google/gemini-3.1-pro-preview": ModelProfile(
        family="gemini", mode="thinking", max_tokens=_MT_GEMINI_THINK,
        notes="Gemini-3: leave temp default (omit). thinkingLevel not thinkingBudget.",
    ),
    "google/gemini-3.5-flash": ModelProfile(
        family="gemini", mode="thinking", max_tokens=_MT_GEMINI_THINK,
        notes="Gemini-3: leave temp default (omit).",
    ),
    "google/gemini-2.5-pro": ModelProfile(
        family="gemini", mode="thinking", max_tokens=_MT_GEMINI_THINK,
        notes="Suspect leaderboard row (Pro<Flash) — thinking-mode harness artifact.",
    ),
    "google/gemini-2.5-flash": ModelProfile(
        family="gemini", mode="instruct", max_tokens=4096,
    ),

    # --- DeepSeek (framework §3.6) ---
    "vertex_model_garden/deepseek-ai/deepseek-v3.2-maas": ModelProfile(
        family="deepseek", mode="non_thinking", max_tokens=4096,
        temperature=0.0, presence_penalty=0.0, extra_body=_DEEPSEEK_NO_THINK,
        notes="deepseek-chat (non-thinking); coding/math temp 0.0.",
    ),
    "vertex_model_garden/deepseek-ai/deepseek-v3.1-maas": ModelProfile(
        family="deepseek", mode="non_thinking", max_tokens=4096,
        temperature=0.0, presence_penalty=0.0, extra_body=_DEEPSEEK_NO_THINK,
    ),
    "vertex_model_garden/deepseek-ai/deepseek-r1-0528-maas": ModelProfile(
        family="deepseek", mode="thinking", max_tokens=_MT_THINKING,
        temperature=0.6, top_p=0.95, prompt_strategy="no_system_no_fewshot",
        notes=("R1: never temp 0; function-calling unsupported on deepseek-reasoner, "
               "so a tool-loop is a structural mis-fit. Raised max_tokens records its "
               "TRUE behaviour rather than a truncation artifact (was 22%)."),
    ),

    # --- Moonshot Kimi (framework §3.7: thinking, temp 1.0 — do NOT lower) ---
    "vertex_model_garden/moonshotai/kimi-k2-thinking-maas": ModelProfile(
        family="kimi", mode="thinking", max_tokens=16000,
        temperature=1.0, top_p=0.95,
        notes="Tuned for temp 1.0; lowering degrades the reasoning trace. max_tokens>=16k.",
    ),

    # --- Qwen3 (framework §3.3: instruct 0.7/0.8/20, thinking 0.6/0.95/20, never greedy) ---
    "vertex_model_garden/qwen/qwen3-next-80b-a3b-instruct-maas": ModelProfile(
        family="qwen", mode="instruct", max_tokens=_MT_INSTRUCT,
        temperature=0.7, top_p=0.8, top_k=20,
    ),
    "vertex_model_garden/qwen/qwen3-next-80b-a3b-thinking-maas": ModelProfile(
        family="qwen", mode="thinking", max_tokens=_MT_THINKING,
        temperature=0.6, top_p=0.95, top_k=20,
        notes="Was 2% — reasoning truncated at 1024 tokens. Never greedy.",
    ),
    "vertex_model_garden/qwen/qwen3-coder-480b-a35b-instruct-maas": ModelProfile(
        family="qwen", mode="instruct", max_tokens=_MT_INSTRUCT,
        temperature=0.7, top_p=0.8, top_k=20,
    ),
    "vertex_model_garden/qwen/qwen3-235b-a22b-instruct-2507-maas": ModelProfile(
        family="qwen", mode="instruct", max_tokens=_MT_INSTRUCT,
        temperature=0.7, top_p=0.8, top_k=20,
    ),

    # --- xAI Grok (framework §3.2: suppress persona; penalties REJECTED on reasoning) ---
    "vertex_model_garden/xai/grok-4.1-fast-reasoning": ModelProfile(
        family="grok", mode="thinking", max_tokens=_MT_GROK_THINK,
        temperature=0.6, prompt_strategy="persona_suppress",
        notes="Reasoning: omit presence/frequency_penalty + stop (xAI API errors on them).",
    ),
    "vertex_model_garden/xai/grok-4.1-fast-non-reasoning": ModelProfile(
        family="grok", mode="non_thinking", max_tokens=4096,
        temperature=0.5, presence_penalty=0.0, prompt_strategy="persona_suppress",
    ),
    "vertex_model_garden/xai/grok-4.20-reasoning": ModelProfile(
        family="grok", mode="thinking", max_tokens=_MT_GROK_THINK,
        temperature=0.6, prompt_strategy="persona_suppress",
        notes="Reasoning: omit penalties.",
    ),
    "vertex_model_garden/xai/grok-4.20-non-reasoning": ModelProfile(
        family="grok", mode="non_thinking", max_tokens=4096,
        temperature=0.5, presence_penalty=0.0, prompt_strategy="persona_suppress",
    ),

    # --- Zhipu GLM (framework §3.4: temperature OR top_p, not both → set top_p only) ---
    "vertex_model_garden/zai-org/glm-5-maas": ModelProfile(
        family="glm", mode="thinking", max_tokens=_MT_GEMINI_THINK,
        top_p=0.95,
        notes="Temp OR top_p, not both — omit temp, set top_p.",
    ),
    "vertex_model_garden/zai-org/glm-4.7-maas": ModelProfile(
        family="glm", mode="thinking", max_tokens=_MT_GEMINI_THINK,
        top_p=0.95,
    ),
}


# ── Family-pattern fallback — new point-versions + local Ollama tags ──────────
# Ordered: MOST SPECIFIC FIRST. Matched (regex search, case-insensitive) against
# the full spec string. First hit wins. Covers run_matrix.sh local models too
# (e.g. mistral-nemo:12b → temp 0.3) so the local sweep also benefits.
_FAMILY_PATTERNS_RAW: list[tuple[str, ModelProfile]] = [
    # Mistral Nemo is unusually temperature-sensitive (framework §3.8: use 0.3).
    (r"mistral-?nemo", ModelProfile(
        family="mistral", mode="instruct", max_tokens=1024, temperature=0.3,
        notes="Nemo officially 'requires smaller temperatures' — 0.3.")),
    (r"mistral|mixtral", ModelProfile(
        family="mistral", mode="instruct", max_tokens=1024, temperature=0.3)),

    # Qwen: thinking before instruct; qwen2.5/qwen2 dense are instruct.
    (r"qwen3.*think", ModelProfile(
        family="qwen", mode="thinking", max_tokens=_MT_THINKING,
        temperature=0.6, top_p=0.95, top_k=20)),
    (r"qwen3", ModelProfile(
        family="qwen", mode="instruct", max_tokens=_MT_INSTRUCT,
        temperature=0.7, top_p=0.8, top_k=20)),
    # qwen2/2.5 are non-thinking dense instruct models. In the Eval-3 sweep
    # they never exceeded 497 output tokens (p99 = 351), so the old 1024 cap
    # never truncated them. It DID starve their context: num_predict feeds
    # Ollama's num_ctx = max(8192, num_predict + 8192), giving qwen2.5 a
    # 9216-token window against qwen3's 24192 while its prompt peaked at 7126.
    # 4096 restores comfortable headroom (8x the observed maximum, and a
    # 12288-token window) WITHOUT the 2.6x KV-cache blow-up that _MT_INSTRUCT
    # would cause: on qwen2.5:72b a 24192 window costs ~7.9 GB of KV on top of
    # ~40 GB of q4 weights, which risks OOM on anything below an 80 GB card.
    (r"qwen2|qwen2\.5|qwen", ModelProfile(
        family="qwen", mode="instruct", max_tokens=4096,
        temperature=0.7, top_p=0.8, top_k=20)),

    # DeepSeek: r1 before the chat models.
    (r"deepseek-?r1", ModelProfile(
        family="deepseek", mode="thinking", max_tokens=_MT_THINKING,
        temperature=0.6, top_p=0.95, prompt_strategy="no_system_no_fewshot")),
    (r"deepseek", ModelProfile(
        family="deepseek", mode="non_thinking", max_tokens=4096,
        temperature=0.0, extra_body=_DEEPSEEK_NO_THINK)),

    # Kimi thinking.
    (r"kimi.*think|kimi-k2-think", ModelProfile(
        family="kimi", mode="thinking", max_tokens=16000,
        temperature=1.0, top_p=0.95)),

    # Grok: non-reasoning before reasoning (else 'reasoning' substring matches both).
    (r"grok.*non-?reasoning", ModelProfile(
        family="grok", mode="non_thinking", max_tokens=4096,
        temperature=0.5, presence_penalty=0.0, prompt_strategy="persona_suppress")),
    (r"grok.*reasoning", ModelProfile(
        family="grok", mode="thinking", max_tokens=_MT_GROK_THINK,
        temperature=0.6, prompt_strategy="persona_suppress")),
    (r"grok", ModelProfile(
        family="grok", mode="non_thinking", max_tokens=4096,
        temperature=0.5, presence_penalty=0.0, prompt_strategy="persona_suppress")),

    # GLM.
    (r"glm", ModelProfile(
        family="glm", mode="thinking", max_tokens=_MT_GEMINI_THINK, top_p=0.95)),

    # Gemini: leave temperature at the provider default.
    (r"gemini-3|gemini.*3\.", ModelProfile(
        family="gemini", mode="thinking", max_tokens=_MT_GEMINI_THINK)),
    (r"gemini", ModelProfile(
        family="gemini", mode="instruct", max_tokens=4096)),

    # Anthropic.
    (r"claude|anthropic", ModelProfile(
        family="anthropic", mode="instruct", max_tokens=_MT_ANTHROPIC)),

    # Gemma 3 (Google OSS): not a thinking model. Use Gemma's documented sampling
    # (temp 1.0 / top_p 0.95 / top_k 64) — "family-recommended", consistent with
    # leaving the Gemini family at its provider default. Gets the XML Block-0 +
    # targeted rules (Google lineage favours XML here; see build_family_block_0),
    # which also gives it the "never emit tool syntax" rule its weaker Ollama
    # tool-calling needs. max_tokens 2048: instruct reply + one tool call.
    (r"gemma", ModelProfile(
        family="gemma", mode="instruct", max_tokens=2048,
        temperature=1.0, top_p=0.95, top_k=64,
        notes="Gemma 3 default sampling; XML+targeted-rules prompt.")),

    # Other local OSS families — standard instruct defaults.
    (r"granite", ModelProfile(family="granite", mode="instruct", max_tokens=1024, temperature=0.3)),
    (r"llama", ModelProfile(family="llama", mode="instruct", max_tokens=1024, temperature=0.3)),
]

FAMILY_PATTERNS: list[tuple[re.Pattern, ModelProfile]] = [
    (re.compile(pat, re.IGNORECASE), prof) for pat, prof in _FAMILY_PATTERNS_RAW
]


# ── Per-family prompt FORMAT ──────────────────────────────────────────────────
# The framework predicts Gemini AND Qwen favour Markdown over XML (§3.3 / §3.5).
# We TESTED both against the real tutor harness (results2 XML vs results2_md
# Markdown, 60-scenario each):
#   - Qwen: Markdown helped (instruct +8, coder +2, 235b +3; thinking −2 noise) → markdown
#   - Gemini: Markdown HURT (2.5-pro −17, 2.5-flash −5, 3.1-pro 0, 3.5-flash +5) → keep XML
# So only Qwen flips. Gemini stays on XML — on this complex multi-section tutoring
# prompt, XML's explicit delimiters serve Gemini better than Google's general
# "prefer Markdown" guidance (a task-specific, evidence-over-doc decision). The
# Markdown Block-0 text lives in apps/tutoring/simple_tutor/family_prompts.py.
_MARKDOWN_FAMILIES = {"qwen"}
MODEL_PROFILES = {
    k: (dataclasses.replace(v, prompt_format="markdown")
        if v.family in _MARKDOWN_FAMILIES and v.prompt_format == "xml" else v)
    for k, v in MODEL_PROFILES.items()
}
FAMILY_PATTERNS = [
    (pat, dataclasses.replace(prof, prompt_format="markdown")
     if prof.family in _MARKDOWN_FAMILIES and prof.prompt_format == "xml" else prof)
    for pat, prof in FAMILY_PATTERNS
]


def get_model_profile(spec_or_model_name: str | None) -> ModelProfile | None:
    """Resolve a ModelProfile for a TUTOR_MODEL_OVERRIDE spec (or model name).

    Lookup order:
      1. Exact-spec match in ``MODEL_PROFILES``.
      2. Family-pattern (regex) fallback in ``FAMILY_PATTERNS``, first hit wins.
      3. ``None`` — caller keeps current behaviour (production no-op).
    """
    if not spec_or_model_name:
        return None
    spec = spec_or_model_name.strip()
    exact = MODEL_PROFILES.get(spec)
    if exact is not None:
        return exact
    # Family-pattern fallback matches the MODEL-NAME part only (after the last
    # '/'), never the provider prefix — otherwise "local_ollama" (which contains
    # the substring "llama") false-matches the llama pattern and mis-tags every
    # Ollama model. e.g. local_ollama/phi4 -> "phi4" -> no match -> None.
    tail = spec.rsplit('/', 1)[-1]
    for pattern, profile in FAMILY_PATTERNS:
        if pattern.search(tail):
            return profile
    return None
