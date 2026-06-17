# Evaluate Vertex Model Garden models (DeepSeek, Kimi) — design

**Date:** 2026-06-17
**Status:** Design — approved approach, pending spec review
**Scope:** Benchmark-only. Add DeepSeek and Kimi K2 (and other versions) as
tutor-under-test rows in the existing `offline_eval` cloud sweep, reached via
Google Vertex AI **Model Garden** Model-as-a-Service (MaaS). No production
provider wiring beyond what the benchmark harness needs.

## Goal

Extend the proprietary "ceiling" benchmark (currently Claude + Gemini in
`offline_eval/cloud_models.txt`) to include large open-weight models served by
Vertex Model Garden MaaS, so they are scored on the **same 60-scenario harness,
same Anthropic judge + student-sim**, and rank directly against the existing
30-model leaderboard (`offline_eval/FINDINGS_offline_model_eval.md`).

Trying multiple model versions must be a **one-line change** (a row in
`cloud_models.txt` + a probe entry), not a code change per version.

## Key facts grounding the design

- DeepSeek and Kimi K2 in Vertex Model Garden are served over an
  **OpenAI-compatible Chat Completions endpoint**, not the Gemini
  `generate_content` surface. So the existing `OpenAIClient` provides ~90% of
  the integration (message translation, tool→function conversion, `tool_choice`
  mapping, `_adapt_openai_response`, token/temp kwargs). The only genuinely new
  behavior is **endpoint + auth**.
- The harness swaps only the tutor via `TUTOR_MODEL_OVERRIDE="provider/model"`
  (`ModelConfig.get_for('tutoring')` → split on first `/` → `resolve_runtime` →
  `get_llm_client` factory). Judge + student-sim stay Anthropic, preserving the
  cross-family grader. Nothing in the harness flow changes.
- `openai`, `google-auth`, and `google.auth.transport.requests` are already in
  the venv (`requirements.txt`: `openai==2.20.0`, `google-auth==2.48.0`). **No
  new dependency.**
- Vertex bearer tokens (OAuth) expire ~hourly; a 60-scenario sweep can outlast
  one token, so the client must refresh.

## Approach (chosen): subclass `OpenAIClient`

`VertexModelGardenClient(OpenAIClient)` reuses the OpenAI message/tool
translation and tool-schema conversion, and overrides endpoint + auth **plus the
two generate methods**. The live smoke test (2026-06-17, see "Smoke-test
findings") showed the OpenAI SDK's *parsed* `ChatCompletion.choices` is
intermittently `None` for DeepSeek MaaS even though the raw HTTP body is valid —
so the overrides call `chat.completions.with_raw_response.create()`, parse the
raw JSON dict, adapt it (mirroring the existing `_adapt_ollama_response` dict
adapter), and retry on empty `choices`. We still inherit
`_translate_messages_for_openai`, the tool-schema conversion, and the
`tool_choice` mapping.

Rejected alternatives:
- **Standalone client from `BaseLLMClient`** — duplicates ~200 lines of OpenAI
  message/tool translation for no gain.
- **Extend `OpenAIClient` in place** with an optional `base_url` + auth callable
  — mixes GCP auth concerns into the vanilla OpenAI path; violates the
  one-purpose-per-unit guidance in CLAUDE.md.

## Components

### 1. `VertexModelGardenClient` — `apps/llm/client.py`

Subclass of `OpenAIClient`.

- **`__init__`**:
  - Read project from `GOOGLE_CLOUD_PROJECT`, location from
    `GOOGLE_CLOUD_LOCATION` (default `us-central1`).
  - Obtain ADC credentials:
    `google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])`.
  - Build the OpenAI base URL:
    - regional: `https://{loc}-aiplatform.googleapis.com/v1/projects/{proj}/locations/{loc}/endpoints/openapi`
    - global (`GOOGLE_CLOUD_LOCATION=global`): `https://aiplatform.googleapis.com/v1/projects/{proj}/locations/global/endpoints/openapi`
  - Raise a clear `ValueError` if `GOOGLE_CLOUD_PROJECT` is unset (no silent
    fallback — a misconfigured benchmark must fail loudly).
  - Call `BaseLLMClient.__init__` (grandparent), not `OpenAIClient.__init__`,
    because the parent assigns `self.client` and we expose `client` as a
    property (below).
- **`client` property**: returns `openai.OpenAI(base_url=…, api_key=<fresh
  token>)`, refreshing the OAuth token when `creds.valid` is false
  (`creds.refresh(google.auth.transport.requests.Request())`). Solves hourly
  token expiry across a long sweep.
- **`_get_api_key`** → `''` — the credential is the OAuth token, not a static
  env key. (Parent `OpenAIClient.__init__` raises if api_key is empty; the
  custom `__init__` avoids that path.)
- **Override `_generate_impl` + `generate_with_tools`**: call
  `self.client.chat.completions.with_raw_response.create(...)`, `json.loads` the
  body, and adapt the dict → `LLMResponse` / `AdaptedMessage` (a small
  `_adapt_openai_dict` helper, sibling to `_adapt_ollama_response`). **Retry on
  empty/None `choices`** (the intermittent DeepSeek failure mode — finding #4).
  Ignore `message.reasoning_content` (thinking models populate it). Reuse the
  inherited `_translate_messages_for_openai` + tool-schema/`tool_choice` mapping.
- Inherited unchanged: `_is_new_generation` (DeepSeek/Kimi names don't match the
  GPT-5/o-series prefixes → "legacy" path → `max_tokens` + `temperature`, which
  the Vertex OpenAI-compat endpoint accepts), `_translate_messages_for_openai`,
  the tool-schema + `tool_choice` conversion inside `generate_with_tools`.

### 2. Provider wiring — `apps/llm/models.py`

- Add `VERTEX_MODEL_GARDEN = 'vertex_model_garden', 'Vertex Model Garden (MaaS)'`
  to `ModelConfig.Provider`. Generates one trivial choices-only migration
  (`AlterField`) — the only DB touch. (Approved by user 2026-06-17.)
- Add `'vertex_model_garden': 'GOOGLE_CLOUD_PROJECT'` to
  `_PROVIDER_API_KEY_ENV` so `resolve_runtime` builds an in-memory config. The
  value is used as a presence/identity hint only; the real credential is ADC.
- Factory branch in `get_llm_client`:
  `elif config.provider == ModelConfig.Provider.VERTEX_MODEL_GARDEN: return VertexModelGardenClient(config)`.

### 3. Harness data — `offline_eval/`

Regions differ per model (finding #3), so `cloud_models.txt` gains a **3rd
column = region**, and `run_cloud.sh` exports `GOOGLE_CLOUD_LOCATION=<region>`
per model before invoking `run_eval`. `run_cloud.sh` already reads a 3rd
`_rest` field (currently discarded) — wire it to the region. Because
`load_dotenv()` is non-override, an inline `GOOGLE_CLOUD_LOCATION` from the
runner wins over the `.env` default.

- **`cloud_models.txt`** — `# --- Vertex Model Garden (MaaS) ---` section,
  rows `provider/model  safe_name  region` (confirmed live 2026-06-17):
  - `vertex_model_garden/deepseek-ai/deepseek-v3.2-maas      deepseek-v3.2    global`
  - `vertex_model_garden/deepseek-ai/deepseek-v3.1-maas      deepseek-v3.1    us-west2`
  - `vertex_model_garden/moonshotai/kimi-k2-thinking-maas    kimi-k2-thinking global`
  - `vertex_model_garden/deepseek-ai/deepseek-r1-0528-maas   deepseek-r1      us-central1` (follow-up — thinking)

  The file is whitespace-split, and `TUTOR_MODEL_OVERRIDE` splits on the
  **first** `/`, so the embedded `/` in the vendor-prefixed id resolves to
  `provider='vertex_model_garden'`, `model='deepseek-ai/deepseek-v3.2-maas'`.
- **`_probe_cloud_models.py`**: add the Vertex candidates (with their regions) to
  `CANDIDATES` so a 1-token call validates each ID + region before a wrong one
  wastes a full 60-scenario run. The probe must set `GOOGLE_CLOUD_LOCATION` per
  candidate (regions differ).

## Data flow (unchanged)

```
run_cloud.sh
  → TUTOR_MODEL_OVERRIDE="vertex_model_garden/deepseek-ai/deepseek-v3.1-maas"
  → manage.py run_eval --single-turn
  → ModelConfig.get_for('tutoring')   # splits on first '/'
  → resolve_runtime('vertex_model_garden', 'deepseek-ai/deepseek-v3.1-maas')
  → get_llm_client(cfg)               # factory
  → VertexModelGardenClient           # OpenAI-compat call to Vertex MaaS
Judge + student-sim remain Anthropic Haiku 4.5 (cross-family grader preserved).
```

## Prerequisites — GCP setup (one-time)

**Status: DONE (2026-06-17)** for project `ai-tutor-499714` under
`pixeldesignlabs.dev@gmail.com`. Auth is isolated in a dedicated
`CLOUDSDK_CONFIG` dir (`~/.config/gcloud-pixeldesignlabs`) so the personal
`~/.config/gcloud` is untouched; `source pdl-gcloud.sh` enters work mode for
ad-hoc gcloud, and `.env` carries `CLOUDSDK_CONFIG` + `GOOGLE_CLOUD_PROJECT` for
the harness. Steps, for the record:

1. `gcloud auth application-default login` — creates ADC (separate from
   `gcloud auth login`; `google.auth.default()` reads it). ✅
2. `GOOGLE_CLOUD_PROJECT=ai-tutor-499714` in `.env`. **Region is per-model**
   (finding #3), set by the runner from `cloud_models.txt`; the `.env`
   `GOOGLE_CLOUD_LOCATION` is only a default for ad-hoc/probe use. ✅
3. `gcloud services enable aiplatform.googleapis.com`. ✅
4. **Enable each MaaS model** in Model Garden — under **Agent platform**:
   <https://console.cloud.google.com/agent-platform/model-garden> (accept terms
   per model; this is where the exact model ID + price are read). The console
   rebranded to "Agent platform → Model Garden" but the backing API is still
   **Vertex AI** (`aiplatform.googleapis.com`). Pick only **"… API Service"**
   (MaaS) cards — *not* "Serve with …" / "Explore and build" (those deploy
   GPU endpoints, billed per GPU-hour — out of scope). ✅ (DeepSeek V3.2/V3.1/R1,
   Kimi K2 Thinking)
5. IAM `roles/aiplatform.user` + active billing on the project. ✅ (owner)

## Cost (estimates — confirm per model card)

Vertex MaaS is pay-per-token through GCP billing; no per-endpoint hourly charge
(we are not deploying self-hosted Model Garden endpoints, which bill GPU-hours).

| Model | ~input /1M | ~output /1M | Est. per single-turn sweep (60 scenarios) |
|---|---|---|---|
| DeepSeek V3.x | ~$0.30 | ~$1.10 | ~$0.50–$1 |
| Kimi K2 | ~$0.60 | ~$2.50 | ~$1–$2 |

Per-sweep token math: ~3 tutor calls/scenario × ~8K-token system prompt (no
prompt caching on MaaS → input paid fresh each call) × 60 ≈ ~1.4M input + ~90K
output tokens per model. Trying 4–5 versions ≈ a few dollars total for the
single-turn set; multi-turn (20-scenario finalist set) costs more per scenario.
Anthropic scoring cost (judge + student-sim on Haiku 4.5) is constant per sweep
and small — identical to existing sweeps.

## Smoke-test findings (2026-06-17, live against the real endpoint)

Validated with `google.auth` + the OpenAI SDK directly (no client code yet),
against `ai-tutor-499714`:

1. **Auth + endpoints work.** ADC from the isolated dir resolves; the global
   (`aiplatform.googleapis.com`) and regional (`{region}-aiplatform.googleapis.com`)
   base URLs both serve; OpenAI SDK `base_url` + bearer token is correct.
2. **Structured `tool_calls` work — risk "tool-leak" RETIRED.** Both
   `deepseek-ai/deepseek-v3.2-maas` and `moonshotai/kimi-k2-thinking-maas`
   returned a proper OpenAI `tool_calls` (`pose_question` with valid JSON args,
   `finish_reason=tool_calls`). The MaaS endpoint translates DeepSeek's native
   `<｜tool▁calls｜>` format into OpenAI tool calls server-side, so **no
   DeepSeek-specific text-leak parser is needed**.
3. **Regions differ per model:** v3.2 → `global`, v3.1 → `us-west2`,
   r1-0528 → `us-central1`, kimi-k2-thinking → `global`. Drives the per-model
   region design above.
4. **DeepSeek non-streaming intermittently returns `choices=None`.** The OpenAI
   SDK's *parsed* `ChatCompletion.choices` came back `None` on repeated
   non-streaming calls, while `with_raw_response` showed a **valid JSON body**
   every time. Conclusion: parse from the raw JSON dict and **retry on empty
   `choices`**. (Kimi parsed fine via the SDK model — DeepSeek-specific.)
5. **`reasoning_content` + thinking.** Kimi-Thinking emitted ~1.3K chars of
   `reasoning_content`; DeepSeek ran non-thinking by default. The adapter ignores
   `reasoning_content`; **`max_tokens` must be generous** so thinking models
   don't truncate before the answer/tool-call.

## Remaining risks — validate during the eval smoke run

1. **Thinking-mode truncation / empty answers.** Kimi here is the *Thinking*
   variant and DeepSeek can think; with the tutor's large prompt + tool loop, a
   tight `max_tokens` could yield reasoning-only turns. Mitigation: generous
   `max_tokens` for these configs; watch for empty content with
   `finish_reason=length`. (Echoes the unresolved Gemini-Pro anomaly in
   FINDINGS — measure, don't assume capability from a harness artifact.)
2. **Intermittent empty `choices` under load.** Even with retry, a sustained
   empty-response spell on a model would inflate errors; the retry budget +
   per-model error logging in the sweep must surface it rather than silently
   pass/fail.
3. **DeepSeek thinking-vs-non-thinking control.** Tool calls are documented as
   non-thinking-only for DeepSeek; if the `-maas` default ever flips to thinking,
   tool use could break. Confirm the default at smoke run; if needed, pass a
   thinking-off knob via `extra_body`.

## Verification ladder (per CLAUDE.md bug-fix workflow)

1. ADC + project env set. ✅ (done; auth verified live)
2. `_probe_cloud_models.py` — 1-token reachability per ID + region.
3. `--single-turn` smoke on 1–2 scenarios per model — confirm tool calls land
   structurally, no reasoning-only/empty turns, tokens counted, empty-`choices`
   retry behaves.
4. Full 60-scenario `run_cloud.sh` sweep per model (instruct/non-thinking first,
   then R1 / thinking).
5. `aggregate.py` leaderboard; fold results into
   `FINDINGS_offline_model_eval.md`.

## Out of scope

- Production provider selection / dashboard wiring (benchmark-only).
- Self-deployed Model Garden GPU endpoints (cost model differs; not needed).
- Prompt/fine-tuning of these models (separate follow-up, as with the OSS
  candidates).
