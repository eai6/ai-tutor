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
    num_gpu: int | None = None         # Ollama layers to offload to GPU. None → Ollama's autofit, which sizes to FREE memory at load time and silently leaves layers on CPU (measured 2026-07-27: 17-31 of 34 depending on what else was running). Decode tracks this directly — 25/34 gave 6.7 tok/s, 34/34 gave 13.5. Set high (99) to force full offload on a box where the model is known to fit.
    extra_body: dict | None = None     # provider thinking toggle, e.g. {"chat_template_kwargs": {"thinking": False}}
    ollama_think: bool | None = None   # Ollama /api/chat top-level `think` flag (extra_body's chat_template_kwargs is vLLM-only; Ollama ignores it). None = don't send. False works on HYBRID templates that gate on Think (qwen3:8b, qwen3.5:4b/9b) but NOT where <think> opens unconditionally (qwen3:4b Thinking-2507) — there it only disables Ollama's parser and the monologue lands in content. Verify per model.
    # What the student can physically send back, when the in-flight question is
    # an MCQ with options. None = decide by provider, which is what every model
    # did before this field existed: local_ollama gets the A-D buttons, cloud
    # gets a text box.
    #
    # Every PROFILED local model now sets this explicitly, so in practice None
    # means "a local tag nobody has profiled yet" and the provider rule is a
    # safety net rather than the mechanism. test_every_local_profile_declares_
    # its_answer_surface keeps it that way: a new local model that forgets the
    # field would otherwise inherit a guess based on hosting, which is exactly
    # the inference this field exists to stop.
    #
    # The provider default encodes "small local model" because until now those
    # were the same thing. They are not: a local 27B reads "northing" as option
    # B perfectly well, which is the failure (device session 29) the picker was
    # built for on the 4B. Size, not hosting, is what the surface should track.
    answer_surface: str | None = None   # None | 'picker' | 'free_text'
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
        if self.num_gpu is not None:
            out["num_gpu"] = self.num_gpu
        if self.ollama_think is not None:
            out["think"] = self.ollama_think
        if self.extra_body:
            out["extra_body"] = dict(self.extra_body)
        return out


# Token budgets (framework §2.1 / §3): thinking models need room for the reasoning
# trace BEFORE the tool-call; instruct models are fine at the historical default.
_MT_INSTRUCT = 16000
_MT_OPENAI = 8192        # GPT-5.x bills reasoning against the SAME budget as the answer
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
    # --- OpenAI ---
    #
    # An exact key MATTERS here beyond sampling. Without one, get_model_profile
    # returns None, family is None, and two things follow: max_tokens falls back
    # to 1024 (engine.py: `profile.max_tokens if profile else 1024`) while
    # reasoning bills against that same budget — so a reasoning model can spend
    # the whole allowance thinking and return finish_reason=length with empty
    # content and no tool call, which reads as a near-zero tutoring score for a
    # purely harness reason. And every family-gated repair path
    # (_should_force_pose, polarity alignment, auto-grade fallback, stuck-slot
    # pivot, ensure-posed-question) early-returns on `not family`, so the arm
    # runs unscaffolded while every other family gets all six.
    #
    # SAMPLING IS DELIBERATELY ABSENT. OpenAIClient._build_completion_kwargs
    # routes every `gpt-5*` name through its new-generation branch, which sends
    # only max_completion_tokens and drops temperature/top_p/top_k. The API
    # enforces it too. Setting sampling here would be inert and misleading.
    "openai/gpt-5.4-mini": ModelProfile(
        family="openai", mode="thinking", max_tokens=_MT_OPENAI,
        notes="Reasons at the API default; budget shared with the answer.",
    ),
    "openai/gpt-5.4-nano": ModelProfile(
        family="openai", mode="thinking", max_tokens=_MT_OPENAI,
        notes="Reasons at the API default; budget shared with the answer.",
    ),

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

    # --- Local Ollama on NVIDIA Jetson Orin (8 GB shared CPU/GPU) ---
    # DISTINCT spec from the cloud qwen3 profiles above so the committed
    # Colab/cloud eval numbers (which run at num_ctx=24192) are untouched.
    # This entry is used ONLY when TUTOR_MODEL_OVERRIDE=local_ollama/qwen3-4b-jetson
    # (the Modelfile-tagged model in infra/ollama/). Two on-device constraints:
    #   1. Instruct only. Enforced by the BASE MODEL, not a runtime flag: the tag
    #      builds FROM qwen3:4b-instruct (Qwen3-4B-Instruct-2507). Plain `qwen3:4b`
    #      is the Thinking-2507 checkpoint whose template opens `<think>`
    #      unconditionally, and ollama_think=False does NOT suppress it — that
    #      flag disables Ollama's thinking *parser*, so the monologue lands in
    #      message.content and the answer gets truncated. Measured 2026-07-26 on
    #      Ollama 0.30.7. Hence no ollama_think here. (The flag DOES work on the
    #      hybrid qwen3:8b / qwen3.5 templates — this is a 4b-Thinking quirk.)
    #   2. num_ctx capped to 16384 so the KV cache fits: at 24192 the qwen3-4b
    #      KV (~3.6 GB f16) + 2.6 GB weights exceeds free memory and Ollama
    #      OOMs ("cannot allocate CUDA0 buffer"). Pair with the server flags
    #      OLLAMA_FLASH_ATTENTION=1 + OLLAMA_KV_CACHE_TYPE=q8_0 (halves KV).
    #   3. num_gpu=99 for the same reason as qwen3.5:4b below — autofit was
    #      silently leaving layers on CPU.
    #   4. max_tokens 3072 -> 1024 (2026-07-29), matching qwen3.5:4b below.
    #      This is the tag the kiosk and ./chat.py actually run, so it was the
    #      one profile still carrying the old ceiling. It is a RUNAWAY GUARD,
    #      not the length mechanism — measured replies are 27-193 tokens, so
    #      neither value binds on a well-behaved turn and this buys no latency
    #      on a normal reply. What it bounds is the bad case: a repetition loop
    #      at ~16 tok/s costs a student 192 s of staring at 3072, against 64 s
    #      at 1024. The actual shortening is done by the <reply_length> budget
    #      in simple_tutor/prompts.py::_render_length_budget (2-3 sentences).
    "local_ollama/qwen3-4b-jetson": ModelProfile(
        family="qwen", mode="instruct", max_tokens=1024,
        temperature=0.7, top_p=0.8, top_k=20,
        num_ctx=16384, num_gpu=99,
        notes="Jetson Orin 8GB: Qwen3-4B-Instruct-2507 base, context capped for KV fit.",
        answer_surface="picker",
    ),

    # Reasoning sibling of the tag above. Tag built from
    # infra/ollama/Modelfile.qwen3-4b-thinking-jetson.
    #
    #   ollama_think=True — and this is the ONE local model here that wants it.
    #   Measured 2026-08-05, same prompt both ways:
    #     think=false -> 1184 chars of monologue in message.content, no answer
    #     think=true  -> 93-char correct answer in content, reasoning in
    #                    message.thinking, cleanly separated by Ollama's parser
    #   The instruct tag's Modelfile says the Thinking checkpoint is unusable;
    #   that was measured with think=false only. Do not "fix" this to False for
    #   consistency with its sibling — that is what breaks it.
    #
    #   max_tokens=4096 vs the sibling's 1024. NOT a preference: reasoning is
    #   spent from the SAME budget as the answer, so a thinking model on 1024
    #   can exhaust it mid-deliberation and return done_reason=length with an
    #   empty content and no tool call. Measured at num_predict=500 the model
    #   produced 2340 chars of thinking, zero content, and 0/5 tool calls —
    #   which reads in production as "the tutor stopped responding" rather than
    #   as truncation. The cost is real: more tokens per turn, more latency, and
    #   more KV pressure on an 8 GB board.
    "local_ollama/qwen3-4b-thinking-jetson": ModelProfile(
        family="qwen", mode="instruct", max_tokens=4096,
        answer_surface="picker",
        temperature=0.6, top_p=0.95, top_k=20,
        num_ctx=16384, num_gpu=99, ollama_think=True,
        notes=(
            "Jetson Orin 8GB: Qwen3-4B-Thinking-2507. Needs think=True (see "
            "above) and a large max_tokens because reasoning shares the "
            "generation budget with the answer."
        ),
    ),

    # --- Local Ollama, Qwen3.5 small series (0.8B/2B/4B/9B, released 2026-03-02) ---
    # WHY THESE EXIST: without an exact key, get_model_profile() falls through to the
    # generic r"qwen3" FAMILY_PATTERNS entry below, which is a CLOUD profile —
    # max_tokens=_MT_INSTRUCT (16000) and no num_ctx. client.py then computes
    # num_ctx = max(8192, 16000+8192) = 24192, the exact value the qwen3-4b-jetson
    # comment above says OOMs an 8 GB Orin. That fallthrough is not hypothetical: it
    # is how the whole Eval-3 sweep measured qwen3.5:4b, and it is the leading
    # explanation for its 21/50 on mt50 against qwen3:4b's 44/50 while simultaneously
    # topping the single-turn board at 178/200. Per-turn skill, no protocol endurance
    # — the signature of a model run wrong, not a model that is bad. Tags are keyed
    # with Ollama's ':' separator because that is what TUTOR_MODEL_OVERRIDE carries.
    #
    # ollama_think=False is correct HERE and not on qwen3-4b-jetson above: the
    # Qwen3.5 templates are HYBRID and gate on `Think`, so Ollama pre-closes the
    # block as `<think>\n\n</think>` and the flag genuinely suppresses reasoning.
    # qwen3:4b (Thinking-2507) opens `<think>` unconditionally and ignores it.
    # Sending it matters twice over on the Ollama path: a thinking model that emits
    # its tool call into the reasoning channel has NO salvage there, because
    # _adapt_ollama_response never calls _recover_reasoning_tool_call.
    #
    # 9b is profiled for correctness on a larger box only — it CANNOT run on the
    # Jetson: 6.6 GB of Q4 weights against ~5.6 GB free, before any KV cache. That
    # is the OOM, and no num_ctx setting fixes it.
    # num_gpu=99 forces all 34 layers onto the iGPU. Ollama's autofit was
    # leaving 3-17 of them on CPU depending on free memory at load time, which
    # cost 3.4x on decode (4.0 -> 13.5 tok/s) and made turn latency vary for
    # reasons nothing in the app could see. 3.0 GB of weights + a q8_0 KV cache
    # at num_ctx=16384 fits the Orin's 7.4 GB shared pool with room to spare;
    # the 9b below does not, which is why it stays on autofit.
    # max_tokens 3072 -> 1024 matches the production default (engine.py:1750),
    # so the eval path stops running at 3x production's generation budget. It
    # is an outer bound, not the length mechanism: measured output is 27-193
    # tokens, so 1024 never binds on a well-behaved reply but still stops a
    # runaway. Deliberately NOT set near the observed p90 (175) — a cap that
    # bites truncates mid-sentence, which reads worse to a student than a reply
    # that was merely long. The <reply_length> budget in prompts.py does the
    # actual work.
    # ── Larger local models, laptop-class only ───────────────────────────────
    #
    # Pulled 2026-08-06 to measure how much of the tutoring failure is model
    # CAPACITY versus engine design. Neither fits the 8 GB Jetson that is the
    # actual offline target — these are a quality ceiling, not a deployment
    # candidate. Read any result from them that way.
    #
    # num_ctx=16384 is not copied from the 4b out of habit: the qwen Block-0 the
    # tutor ships is ~20.8K characters (~5.2K tokens) before the question pool,
    # KB chunks and recent-turn window are added, so a smaller window would
    # silently truncate the instructions rather than the reply. 16384 leaves
    # room for that plus num_predict.
    #
    # num_gpu=99 for the reason given on the 4b entries: unpinned, Ollama
    # autofits against free memory AT LOAD TIME, so the layer split — and
    # therefore decode speed — depends on whatever else happened to be running.
    "local_ollama/qwen3:8b": ModelProfile(
        family="qwen", mode="instruct", max_tokens=1024,
        answer_surface="picker",
        temperature=0.7, top_p=0.8, top_k=20,
        num_ctx=16384, num_gpu=99,
        notes="5.2 GB weights. Comfortable on a 19 GB laptop alongside the app.",
    ),

    # 9.3 GB of weights plus a 16K KV cache is roughly 12-13 GB resident. On a
    # 19 GB machine that leaves little for macOS, the Django server and a
    # browser, so expect it to be the point where turn latency starts tracking
    # memory pressure rather than model size. If turns suddenly get 4-5x slower
    # mid-run, that is the signature — check free memory before blaming the
    # model (the same confound that invalidated a whole eval arm on 2026-07-30).
    "local_ollama/qwen3:14b": ModelProfile(
        family="qwen", mode="instruct", max_tokens=1024,
        answer_surface="picker",
        temperature=0.7, top_p=0.8, top_k=20,
        num_ctx=16384, num_gpu=99,
        notes="9.3 GB weights. Largest that runs usefully on 19 GB; not Jetson-viable.",
    ),

    # Qwen3.6-27B in NON-THINKING mode. Tag built from
    # infra/ollama/Modelfile.qwen3.6-27b-instruct.
    #
    # Unlike the 2507 pair above, Qwen3.6 is ONE hybrid checkpoint that thinks
    # by default; instruct mode is a request-time switch, so this profile is
    # half the mechanism (the Modelfile's sampling + num_ctx is the other half).
    # ollama_think=False is correct HERE and wrong on qwen3-4b-jetson: this
    # template GATES on Think, so the flag genuinely suppresses the monologue,
    # whereas Thinking-2507 opens <think> unconditionally and the flag only
    # disables Ollama's parser.
    #
    # max_tokens 2048 (not the 4B's 1024): a 27B writes longer teaching turns,
    # and with thinking off the budget is all visible answer.
    #
    # Baseline: the same weights in DEFAULT (thinking) mode scored 29/30 rubric
    # 0.89 on the v1 board — best on record — at ~5 h for 30 scenarios. This
    # entry exists to measure the same model without the reasoning tax.
    "local_ollama/qwen3.6-27b-instruct": ModelProfile(
        family="qwen", mode="instruct", max_tokens=2048,
        temperature=0.7, top_p=0.80, top_k=20,
        num_ctx=32768, ollama_think=False,
        notes="Qwen3.6-27B hybrid forced non-thinking (card's instruct sampling).",
        answer_surface="free_text",
    ),

    # Successor to the tag above, and deliberately its twin: same max_tokens,
    # same sampling, same num_ctx, same ollama_think=False. Qwen3.8's card
    # recommends the identical non-thinking numbers 3.6 does, so holding every
    # knob equal makes the mt100 row a clean 3.6-vs-3.8 read on one variable.
    # Tag built from infra/ollama/Modelfile.qwen3.8-27b-instruct.
    #
    # ollama_think=False VERIFIED to reach 3.8's gate, 2026-08-23: a dry run on
    # a rented 3090 reported thinking_chars=0, "answers directly". It was in
    # doubt because 3.8 moved its primary reasoning control to
    # `reasoning_effort` and routes `enable_thinking` through
    # chat_template_kwargs (vLLM-only; Ollama drops it), so the 3.6 evidence did
    # not transfer. run_matrix.sh's identity probe still prints thinking_chars
    # before the first scenario is scored — cheap insurance against the tag
    # being rebuilt from a different base.
    "local_ollama/qwen3.8-27b-instruct": ModelProfile(
        family="qwen", mode="instruct", max_tokens=2048,
        temperature=0.7, top_p=0.80, top_k=20,
        num_ctx=32768, ollama_think=False,
        notes="Qwen3.8-27B hybrid forced non-thinking; mode unverified until identity.log.",
        answer_surface="free_text",
    ),

    "local_ollama/qwen3.5:4b": ModelProfile(
        family="qwen", mode="instruct", max_tokens=1024,
        answer_surface="picker",
        temperature=0.7, top_p=0.8, top_k=20,
        num_ctx=16384, ollama_think=False, num_gpu=99,
        notes="Jetson Orin 8GB. Hybrid template — think=False is honoured.",
    ),
    # num_gpu=99 for the same reason as the 4b entries: unpinned, Ollama autofits
    # against free memory AT LOAD TIME, so the layer split depends on whatever
    # else happened to be running. Measured 2026-07-28 serving from the kiosk —
    # autofit put this at 32%/68% CPU/GPU and a turn took 149 s, against 26/26
    # layers on GPU and ~79 s when loaded on an idle box. Same model, same
    # context, 2x on nothing but placement.
    # max_tokens 1024 for the same runaway-guard reason as the 4b entries.
    "local_ollama/qwen3.5:2b": ModelProfile(
        family="qwen", mode="instruct", max_tokens=1024,
        answer_surface="picker",
        temperature=0.7, top_p=0.8, top_k=20,
        num_ctx=16384, ollama_think=False, num_gpu=99,
        notes="Jetson Orin 8GB fallback if 4b is too slow (2.7 GB Q4 weights).",
    ),

    # Modelfile-tagged 2b, mirroring qwen3-4b-jetson. PREFER THIS over the bare
    # qwen3.5:2b above on this box.
    #
    # A profile can only pin num_ctx on requests our own client builds. The
    # grader's Tier-2 verifier goes through instructor's OpenAI-compatible path
    # (POST /v1/chat/completions), which has no num_ctx field, so on the bare tag
    # it landed at Ollama's 4096 default — a different runner from the tutor's
    # 16384, which under OLLAMA_MAX_LOADED_MODELS=1 evicts and reloads the model
    # 30-45 s each way, twice per graded turn. A Modelfile PARAMETER applies on
    # every endpoint and closes that gap; see infra/ollama/Modelfile.qwen3.5-2b-jetson.
    #
    # num_ctx here MUST equal the Modelfile's. If they disagree, the tutor path
    # sends this value while every other path inherits the Modelfile's, and the
    # two thrash — which is the exact bug this entry exists to prevent.
    # Modelfile-tagged Qwen3.5 4B. Same reasoning as the 2b entry below: a
    # profile cannot reach the grader's OpenAI-compatible verifier path, so the
    # window has to be baked into the tag. num_ctx MUST equal the Modelfile's.
    "local_ollama/qwen3.5-4b-jetson": ModelProfile(
        family="qwen", mode="instruct", max_tokens=1024,
        answer_surface="picker",
        temperature=0.7, top_p=0.8, top_k=20,
        num_ctx=16384, ollama_think=False, num_gpu=99,
        notes="Jetson Orin 8GB: Qwen3.5-4B with num_ctx pinned in the Modelfile.",
    ),

    # max_tokens 1024: runaway guard, same as every other Jetson tutoring tag.
    # (0.8b and 9b below keep 3072 — the first is an intent classifier, not a
    # tutor, and the second cannot run on this box at all.)
    "local_ollama/qwen3.5-2b-jetson": ModelProfile(
        family="qwen", mode="instruct", max_tokens=1024,
        answer_surface="picker",
        temperature=0.7, top_p=0.8, top_k=20,
        num_ctx=16384, ollama_think=False, num_gpu=99,
        notes="Jetson Orin 8GB: Qwen3.5-2B with num_ctx pinned in the Modelfile.",
    ),
    "local_ollama/qwen3.5:0.8b": ModelProfile(
        family="qwen", mode="instruct", max_tokens=3072,
        answer_surface="picker",
        temperature=0.7, top_p=0.8, top_k=20,
        num_ctx=16384, ollama_think=False,
        notes="1.0 GB Q4. Too small to tutor; profiled for intent-classifier use.",
    ),
    "local_ollama/qwen3.5:9b": ModelProfile(
        family="qwen", mode="instruct", max_tokens=3072,
        answer_surface="picker",
        temperature=0.7, top_p=0.8, top_k=20,
        num_ctx=16384, ollama_think=False,
        notes="Does NOT fit the Jetson (6.6 GB Q4 > free RAM). Larger boxes only.",
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
