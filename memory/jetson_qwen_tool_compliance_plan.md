# Jetson small-Qwen tool-calling compliance — config fix + offline measurement

Status: **in flight** (started 2026-07-26). Phase 1 (H1/H1b + offline rungs) only.

## Why

The tutor is going offline onto an **NVIDIA Jetson Orin Nano Super** — 7.4 GB shared
CPU/GPU RAM, ~5.6 GB free. `simple_tutor` *requires* tool use, so "which model" is
really "which model complies with the tool protocol".

**9B crashes, and it is not a config problem.** `qwen3.5:9b` Q4 weights are 6.6 GB,
larger than the 5.6 GB free before a single KV byte is allocated. `qwen3:8b` at 5.2 GB
leaves ~400 MB for KV — unusable at any real `num_ctx`. The ceiling on this box is **4B**.
There is no Qwen3.5-8B to fall back to: the Qwen3.5 small series is 0.8B / 2B / 4B / 9B
(released 2026-03-02, Apache 2.0, natively multimodal, 262K native context).

## The confound this plan exists to resolve

Two committed multi-turn draws rank `qwen3:4b` far above `qwen3.5:4b`:

| Draw | `qwen3:4b` | `qwen3.5:4b` |
|---|---|---|
| mt50 (n=50, seed 5) | **44/50 (88%)** | 21/50 (42%), 23 died at `max_turns` |
| oss13_mt (n=20) | **20/20** | 5/20 |
| Call-2 repairs | 53 (4.7%) | 597 (35%) |
| "still declined" | 10 (0.9%) | 422 (25%) |
| `record_answer` with no question in flight | **0** | 275 |

Yet single-turn sweep3 (n=200) **inverts**: `qwen3.5:4b` 178/200 (89%) is best,
`qwen3:4b` 115/200 (58%). Per-turn skill without protocol endurance — the signature of a
model being run wrong, not a model being bad.

`apps/llm/model_profiles.py` has no exact key for the qwen3.5 tags, so
`get_model_profile()` (`model_profiles.py:349-371`) falls through to the generic
`r"qwen3"` `FAMILY_PATTERNS` entry (`:251`). Verified by execution:

```
get_model_profile('local_ollama/qwen3.5:4b')
  → family=qwen mode=instruct max_tokens=16000
    sampling={'temperature': 0.7, 'top_p': 0.8, 'top_k': 20}   # no num_ctx, no think
```

- `num_ctx` absent → `client.py:1326` computes `max(8192, 16000+8192) = 24192` —
  precisely the value the Jetson profile's own comment (`model_profiles.py:191-194`)
  says OOMs an 8 GB Orin.
- `think` absent → `qwen3.5:4b` is a **hybrid** thinking template
  (`client.py:1348-1350`), so it ran the entire sweep **with thinking on**.
- The Ollama adapter never calls `_recover_reasoning_tool_call` (`client.py:579-612`) —
  that is wired only into the OpenAI/Vertex/Gemini adapters (`:205,:280,:348`). A
  thinking model on Ollama has **zero** salvage path for a tool call emitted in the
  reasoning channel.

Symptom match is exact: 16.9 avg turns vs 10.6, 23/50 dying at the turn budget, 35%
repair rate. **The 42% is plausibly an artifact of an unprofiled thinking model with a
16k output budget, not a property of Qwen3.5-4B.** Nothing downstream — model choice,
harness work — is trustworthy until this is settled.

## Scope of this phase

- **Offline rungs only.** No `ANTHROPIC_API_KEY` on this box and the only `ModelConfig`
  row is `student_sim/claude-haiku-4-5`. Multi-turn eval drives the student simulator
  and judges through Anthropic, so the multi-turn rungs are gated follow-ups.
- **Config fix, then measure, then stop.** H1/H1b only. Do **not** bundle the harness
  changes into the same measurement — bundling is how the 42% got recorded as a model
  property in the first place.
- **Portability via env-var override**, not rewrites, so a collaborator's workflow on the
  hardcoded paths keeps working.

## Box state (verified 2026-07-26)

- `.venv/` is the live env (Django 5.2.16, anthropic, requests, PyYAML, pytest-django).
  `venv/` has only pip+setuptools — and is what every `offline_eval` script points at,
  and what `PATH` leads with.
- Weights cached (19 GB): `qwen3:{4b, 4b-instruct, 8b}`, `qwen3.5:{4b, 9b}`,
  `qwen3-4b-jetson`. `qwen3.5:{2b, 0.8b}` are not cached (~3.7 GB to add).
- **The `ollama` binary is not installed** — `/usr/local/bin` is empty, nothing on PATH,
  nothing listening on `:11434`, no systemd unit, no container. The weights are present
  without a runtime. Rungs 0–1 are blocked until it is installed.

## Steps

### H1 — exact-match profiles for the local qwen3.5 tags
`apps/llm/model_profiles.py`, beside `local_ollama/qwen3-4b-jetson` (`:195-201`).
Exact keys for `local_ollama/qwen3.5:{4b,2b,0.8b,9b}`:
`family="qwen", mode="instruct", max_tokens=3072, temperature=0.7, top_p=0.8, top_k=20,
num_ctx=16384, ollama_think=False`.

`ollama_think=False` is safe **here and only here** — `model_profiles.py:61` and
`client.py:1348-1350` record that hybrid `qwen3.5:4b/9b` templates honour it (pre-closing
`<think>\n\n</think>`), while `qwen3:4b`-Thinking does not: there it disables only
Ollama's *parser* and the monologue lands in `content`. `max_tokens=3072` replaces the
inherited 16000, which is what inflates the computed `num_ctx`.

**Do not touch** the generic `r"qwen3"` `FAMILY_PATTERNS` entry (`:251`) or
`_MT_INSTRUCT=16000` (`:92`) — committed Colab/cloud eval numbers depend on them.

Production inertness: `MODEL_PROFILES` is consulted only via `TUTOR_MODEL_OVERRIDE`;
`_call_llm` resolves `profile=None` in production and `_family=None` gates the entire
eval-net stack (`engine.py:410-420`).

### H1b — make the fallthrough loud
`apps/llm/client.py:1326`. When `sampling.get('num_ctx') is None`, `logger.warning` the
computed value and the resolved spec. CLAUDE.md's no-silent-skip rule: a model falling
through to a family pattern on a memory-constrained box must be visible.

### Rung 0 — tool-emission go/no-go (~3 min, zero cloud)
`offline_eval/_probe_ollama_tools.py <tag>` against `qwen3.5:4b`, `qwen3:4b-instruct`,
`qwen3-4b-jetson`, `qwen3:4b`. `tool_calls == 0` on 3/3 → the tag never enters rung 1.
Serve with `OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0`. Record tag digests
first: `infra/ollama/Modelfile.qwen3-4b-jetson` notes that plain `qwen3:4b` now resolves
to Qwen3-4B-**Thinking**-2507, but the 88% mt50 winner ran under that tag earlier — the
reference model may no longer be the checkpoint that was measured.

### Rung 1 — the decisive config A/B (~20–30 min, zero cloud)
`scripts/bench_tutor_quality.py` scenario A is already *"Tool compliance… Metric:
tool-call rate, valid-slot rate"* with `tool_called`/`tool_slot`/`valid_slot` on
`TrialResult` (`:167-181`). Needs: a `local_ollama` dispatch branch (`SCENARIO_A_RUNNER`
`:371-375` covers only anthropic/openai/google) routed through `get_llm_client` so it
exercises the real `OllamaClient`; a `--real-schemas` flag swapping the toy 2-property
tool for the real `TOOL_SCHEMAS` (`simple_tutor/prompts.py:53-297`); and an `arg_shape`
field (`ok` | `double_stringified` | `string_int` | `options_as_string`).

2×2, 30 trials/cell: {`num_ctx=24192`, think unset} × {`16384`, `think=false`} against
{toy schema} × {real `TOOL_SCHEMAS`}. Results as JSON to `offline_eval/jetson_rung1/`.

**Decision rules**
- Cell 3 OOMs/times out but cell 4 does not, **or** cell 4 ≥ 90% while cell 3 ≤ 60% →
  the 42% is an artifact. Re-baseline `qwen3.5:4b`; H2–H5 become worth doing.
- Cell 4 ≈ cell 3 and both low → `qwen3.5:4b` genuinely lacks protocol adherence. The
  mt50 winner stands (already pinned as `qwen3-4b-jetson` → `FROM qwen3:4b-instruct`)
  and the ladder stops.
- Cell 2 ≫ cell 4 → schema bulk is a real cost; promote per-family schema slimming.

### Portability (env-var override only)
`ROOT="${AI_TUTOR_ROOT:-<existing>}"` across `run_matrix.sh:18`, `run_cloud.sh:12`,
`aggregate.py:14`, `seed_ollama_configs.py:13`, `_validate_wiring.py:3`,
`_recon_configs.py:3`, `_probe_cloud_models.py:8`, `probe_tools_matrix.sh:4`;
`PY="${PY:-$ROOT/venv/bin/python}"` at `run_matrix.sh:20`; `seed_ollama_configs.py:34`
honours `MODELS_FILE`.

## Deferred — harness changes, gated on rung 1

One A/B per change, never bundled.

- **H2 — forward `tool_choice` on Ollama.** `client.py:1294-1303` discards it unless
  `OLLAMA_FORWARD_TOOL_CHOICE=1`; one mt50 log carries the "NOT forwarded" warning 151
  times, so `_plan_call1`/`_plan_call2`/`_adaptive_force_now` are all inert. Modern
  Ollama `/api/chat` accepts it. Invert the default; one-shot retry-without-it on HTTP
  400. Keep `SIMPLE_TUTOR_ADAPTIVE_FORCING=1` — blanket forcing is what made
  gemini-3.1-pro emit 139 poses in one turn.
- **H3 — recovery parity on the Ollama path.** `_adapt_ollama_response`
  (`client.py:662-669`) discards prose when it text-recovers a call (other adapters keep
  both); `_maybe_parse_text_tool_call` (`:385-415`) accepts only `{"name","arguments"}`
  and returns the first match only; `_recover_reasoning_tool_call` is never wired in.
- **H4 — argument coercion at dispatch.** `engine.py:2162-2163` replaces non-dict params
  with `{}`, so a double-stringified `arguments` blob becomes a silent argument-less
  call. Qwen3.5 stringifies union fields near-100% consistently. Gate on
  `family is not None` so production is byte-identical.
- **H5 — inline validation feedback on the single repair.** `handle_pose_question`
  silently coerces bad `question_type`/`source` (`tools.py:450-463`) and never tells the
  model; `_format_tool_result_for_call2` renders 4 cases and `json.dumps`-es the rest
  (`engine.py:815-817`). Typia's harness moved a 3B-active model from 6.75% → ~100%
  first-pass success with exactly this loop. **No third LLM call** — `Call2FoldTests`
  asserts one. CLAUDE.md makes consulting `prompting-fundamentals-expert` non-negotiable
  before touching the repair instructions.
- **Instrumentation** — `metadata['tool_protocol']` on `_persist_tutor_turn`
  (`engine.py:2842-2889`) recording *expected vs emitted*, a `TOOL_CALL_MISSED` label so
  the existing `no_label_anywhere` verb scores it, and a column in `aggregate.py`. Every
  number in the table above had to be grepped out of `.log` files.

## Rejected

- **Ollama `format: json` / grammar-constrained decoding.** External measurement shows
  tool-invocation rate collapsing **100% → 0%** when structured-output constraints are
  combined with tool definitions (arXiv 2606.25605, "Constraint Tax"). It also cannot
  express "choose between `pose_question` and `record_answer`", and would constrain
  student-visible prose. H2 is the supported forcing mechanism.
- **Fine-tuning / LoRA on tool-call traces.** No labelled corpus — the compliance metric
  does not exist yet. Infeasible in 5.6 GB; freezes `TOOL_SCHEMAS` against a changing prompt.
- **Multi-agent decomposition.** Forbidden by CLAUDE.md absent a measured bottleneck;
  Cemri et al. 2025 measured 17× error amplification; doubles calls on the slowest box.
- **A third repair call.** 25% of the unprofiled config's repairs already fail at Call 2
  — evidence the config, not the retry count, is the defect.
- **Switching off Ollama to llama.cpp/vLLM.** The factory and all 8 `offline_eval`
  scripts assume Ollama; no evidence the runtime is the problem.
- **8B/9B.** Rejected on arithmetic, not preference — see "Why".

## Verification

```bash
.venv/bin/pytest apps/tutoring/simple_tutor/tests/ apps/llm/tests.py evals/ -q
```
Must stay green — `ForcePoseGateTests::test_production_never_forces`,
`Call1PlanTests::test_unforced_call_is_unchanged`, `Call2FoldTests` (all 5),
`AutoPoseFallbackTest::test_production_family_untouched`.

```bash
.venv/bin/python -c "
from apps.llm.model_profiles import get_model_profile as g
for s in ['local_ollama/qwen3.5:4b','local_ollama/qwen3-4b-jetson','claude-opus-4-7']:
    p=g(s); print(s, p and (p.max_tokens, p.sampling_dict()))"
```
`qwen3.5:4b` must go `16000 / no num_ctx / no think` → `3072 / 16384 / think=False`.
The other two must be **unchanged**.

Must not change: `local_ollama/qwen3-4b-jetson` resolution; the generic `r"qwen3"` family
entry; any committed Colab/cloud eval config.

## Related

- `memory/eval_harness_plan.md` — the three scoring layers and verb contract this reuses.
- `memory/multi_turn_eval_v1_plan.md` — the Eval-3 sweep design that produced the mt50
  numbers above.
- `memory/simple_tutor_m12_pose_question_milestones.md` — the `pose_question` tool
  architecture under test.
- `memory/portuguese_mozambique_pilot_plan.md` — the offline-deployment motivation.
