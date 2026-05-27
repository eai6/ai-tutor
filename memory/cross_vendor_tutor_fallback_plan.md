---
name: cross-vendor-tutor-fallback-plan
description: Plan for task #256 — wire Gemini 3.1 → Gemini 3.5 → Opus 4.7 fallback for the TUTORING path so single-vendor outages don't drop sessions.
metadata:
  type: project
---

# Cross-vendor tutoring fallback chain — Plan (2026-05-20)

## Problem

Migration `apps/llm/migrations/0028_swap_runtime_to_gemini_3_1_flash_lite.py` swapped the active tutoring `ModelConfig` to `google/gemini-3.1-flash-lite-preview` (2.8× faster end-to-end than Opus on browser e2e). But the engine binds a single `llm_client` per session and only the per-vendor retry-with-backoff defends against transient errors. If Google has a sustained outage (or hard-quota exhaustion), every active session drops to the static `_fallback_response()` HTML — no tutoring at all.

The judge stack already solved this for evaluation calls via `get_judge_provider_chain` + `call_judge_with_fallback` (`apps/curriculum/content_judges/_providers.py:76,175`). We need the analogous cascade for the tutoring path: when the active tutor client exhausts retries (or hits a hard non-retryable status), try the next-tier client before giving up.

## Current state (from audit)

**Tutor call sites that need fallback** — only three, all in `apps/tutoring/conversational_tutor.py`:

| File:line | Call | Notes |
|---|---|---|
| 5296 | `self.llm_client.generate_with_tools(...)` | Live: pose_question / pose_inline_question tool path |
| 5330 | `self.llm_client.generate(...)` | Live: plain-text path when tools unavailable or fail |
| 3423 | `self.llm_client.generate_stream(...)` | **Dead in prod** (CLAUDE.md: no SSE on Azure Container Apps) — skip |

Each is wrapped in `try/except` that on failure returns a static `_fallback_response()` (lines 5319, 5353). The cross-vendor cascade must sit *inside* those try blocks: try tier 2, then tier 3, **then** fall through to the static response.

**Existing per-vendor retry posture** (`apps/llm/client.py`):

- `AnthropicClient` (line 336): `MAX_RETRIES=4`, backoff `[15, 30, 60, 120]s`, catches `RateLimitError | InternalServerError | APIStatusError` for status ≥ 429.
- `GeminiClient` (line 985): `MAX_RETRIES=3`, backoff `[2, 5, 12]s`, catches `ServerError | ClientError` whitelisted to status ∈ `{429, 500, 502, 503, 504}`.
- `OpenAIClient` (line 735): **no retry-with-backoff**. Not currently in our intended chain (Gemini → Gemini → Anthropic), so deferred.

**What the per-vendor retry does NOT catch** — these are the failures the cross-vendor cascade exists for:

- Persistent 5xx after retries exhausted (e.g., regional Google outage that lasts > 19s)
- Hard 401/403 (auth) — won't recover on retry, but worth falling through in case the next-tier API key is healthy
- Quota/billing failures (often 403 or 429 with quota-exhaustion subcode)
- Network-level failures (DNS, TLS) — these surface as `httpx.*Error` etc. and aren't currently in any except clause

**Judge chain precedent** — `apps/curriculum/content_judges/_providers.py`:
- `get_judge_provider_chain(purpose, ...)` builds an ordered list of `JudgeProvider` from active `ModelConfig`s, deduping by **distinct provider** so a same-vendor outage doesn't kill the whole chain.
- `_FALLBACK_PURPOSES = ('judge', 'judge_fallback', 'generation', 'tutoring', 'exit_tickets')` — judges piggyback off other purposes to find distinct-provider tiers.
- `call_judge_with_fallback` iterates the chain, broadly catches `Exception` per provider, `continue`s on failure, returns the first success. Never raises out.

**ModelConfig.Purpose** (`apps/llm/models.py:120-157`): 15 purposes exist. `tutoring_fallback` / `tutoring_fallback_2` do **not** exist. `ModelConfig.get_for(purpose)` returns one row — not a chain.

## Target design

**Two pieces.**

### Piece 1: `FallbackLLMClient` wrapper (new file `apps/llm/fallback_client.py`)

A `BaseLLMClient` subclass that owns an ordered list of `(ModelConfig, llm_client)` tiers and delegates `generate` / `generate_with_tools` to the first healthy one. On a transient-or-hard provider failure, falls through to the next tier. Telemetry-rich (every failover logged with tier index, provider, error class, status).

```python
class FallbackLLMClient(BaseLLMClient):
    def __init__(self, tiers: list[tuple[ModelConfig, BaseLLMClient]], *,
                 transient_classes: tuple[type[Exception], ...]):
        ...

    def generate(self, messages, system_prompt, **kw) -> Message:
        return self._call_with_fallback("generate", messages=messages,
                                       system_prompt=system_prompt, **kw)

    def generate_with_tools(self, messages, system_prompt, tools, **kw) -> Message:
        return self._call_with_fallback("generate_with_tools",
                                        messages=messages, system_prompt=system_prompt,
                                        tools=tools, **kw)

    def _call_with_fallback(self, method_name, **kw):
        last_exc = None
        for tier_idx, (config, client) in enumerate(self._tiers):
            try:
                t0 = time.monotonic()
                result = getattr(client, method_name)(**kw)
                if tier_idx > 0:
                    logger.warning(
                        "[FallbackLLMClient] %s succeeded on tier=%d "
                        "provider=%s model=%s after %d failed tier(s)",
                        method_name, tier_idx, config.provider, config.model_name, tier_idx,
                    )
                return result
            except self._transient_classes as exc:
                last_exc = exc
                logger.warning(
                    "[FallbackLLMClient] %s tier=%d provider=%s model=%s FAILED "
                    "(%s: %s) — trying next tier",
                    method_name, tier_idx, config.provider, config.model_name,
                    type(exc).__name__, str(exc)[:200],
                )
                continue
        # All tiers exhausted — re-raise the last error so the engine's
        # outer except can fall back to _fallback_response().
        raise last_exc
```

**Key decisions in this shape:**

- **Delegates, doesn't subclass per-vendor logic.** Each tier's client still runs its own retry-with-backoff (the D+E fix). The wrapper only sees the final outcome (success or exhausted-retry exception). Composition over inheritance.
- **Broad `transient_classes` by default.** Mirrors `call_judge_with_fallback`'s "catch `Exception`, try next" posture. Justified because the engine's outer `except` already routes anything-unexpected to `_fallback_response()` — falling through more tiers can only improve recovery odds, never make things worse. Logic errors (e.g., `TypeError` from the response-shape adapter) will still surface because each tier will hit the same bug and the chain will exhaust quickly.
- **Re-raises on full exhaustion** so the engine's existing static-fallback path still triggers. The wrapper doesn't replace `_fallback_response()` — it just gives more chances to avoid reaching it.
- **No streaming method.** `generate_stream` is omitted entirely; the dead `respond_stream()` path can keep using a non-fallback client.

### Piece 2: chain builder + binding (new function in `apps/llm/fallback_client.py`)

```python
def build_tutoring_fallback_client() -> BaseLLMClient:
    """Returns FallbackLLMClient(tier1, tier2, tier3) or, if any tier
    is missing/unconfigured, a thinner chain (or even a single client).
    Order: tutoring → tutoring_fallback → tutoring_fallback_2.
    """
```

Plus update `conversational_tutor.py:1542-1547`:

```python
# OLD
config = ModelConfig.get_for('tutoring')
if config:
    self._llm_client = get_llm_client(config)

# NEW
self._llm_client = build_tutoring_fallback_client()
```

**Failure-class taxonomy** — what `transient_classes` includes:

| Class | Source | Why |
|---|---|---|
| `anthropic.APIError` and subclasses | anthropic SDK | Covers RateLimit, InternalServer, APIStatus, plus auth/quota |
| `google.genai.errors.APIError` and subclasses | google.genai | Same coverage on Google side |
| `openai.APIError` and subclasses | openai SDK | Future-proofing if we add OpenAI to the chain |
| `httpx.HTTPError`, `httpx.RequestError` | httpx | Network-layer failures all three SDKs use |
| `TimeoutError`, `ConnectionError` | stdlib | Catch-all |

Excluded: `TypeError`, `ValueError`, `AttributeError`, `NotImplementedError` — these are our-side bugs and should surface, not be swallowed by silent failover.

## Data model changes

**New `ModelConfig.Purpose` enum values** (additive, no migration of existing rows):

- `TUTORING_FALLBACK = 'tutoring_fallback'` — tier 2
- `TUTORING_FALLBACK_2 = 'tutoring_fallback_2'` — tier 3

**Migration 0029** seeds the two new rows (idempotent, `get_or_create`):

| Purpose | Provider | Model | Temp | Why this slot |
|---|---|---|---|---|
| `tutoring_fallback` | google | `gemini-3.5-flash` | 0.2 | Same vendor, different model — survives Lite-Preview-specific quota / 429 |
| `tutoring_fallback_2` | anthropic | `claude-opus-4-7` | 0.0 | Cross-vendor — survives Google-wide outage |

Reversible: backwards step deletes the two seeded rows (only ones with `purpose IN ('tutoring_fallback', 'tutoring_fallback_2')`). Doesn't touch the active `tutoring` row.

## Backend changes

| File | Change |
|---|---|
| `apps/llm/models.py:120` | Add `TUTORING_FALLBACK` + `TUTORING_FALLBACK_2` to `Purpose` enum + admin Meta if needed |
| `apps/llm/fallback_client.py` (new) | `FallbackLLMClient` class + `build_tutoring_fallback_client()` |
| `apps/llm/client.py` | No changes — per-vendor retry stays as-is |
| `apps/llm/migrations/0029_seed_tutoring_fallback_purposes.py` (new) | RunPython seed for the two new rows |
| `apps/tutoring/conversational_tutor.py:1542` | Replace `ModelConfig.get_for('tutoring')` + `get_llm_client(config)` with `build_tutoring_fallback_client()` |
| `apps/llm/tests/test_fallback_client.py` (new) | Unit tests: tier 0 succeeds → no failover; tier 0 raises transient → tier 1 returns; all tiers raise → final exception re-raised; logic error (TypeError) on tier 0 still tries tier 1 (per broad-catch policy) |

**Telemetry to add** (already in the snippet above, formalized):

```
[FallbackLLMClient] generate tier=0 provider=google model=gemini-3.1-flash-lite-preview FAILED (ServerError: 503 ...) — trying next tier
[FallbackLLMClient] generate succeeded on tier=2 provider=anthropic model=claude-opus-4-7 after 2 failed tier(s)
```

These lines let us grep production logs for `tier=2 .* succeeded` to see how often Opus is actually rescuing sessions.

## Frontend/mobile changes

**None.** The cascade is invisible to the chat UI. The student sees a (slightly slower) successful tutor turn instead of the static fallback HTML.

## Out of scope

- **OpenAI retry-with-backoff** parity with Anthropic/Gemini (separate gap, not blocking this chain since OpenAI isn't a chain tier).
- **Per-session sticky tier selection.** Each turn re-tries from tier 0 (so a recovered primary gets traffic back immediately). We do **not** maintain "this session is now on tier 2 for its lifetime" — a more conservative design but unnecessary given per-turn LLM calls are independent.
- **Circuit breaker** to skip a known-down tier without an attempt. The per-vendor retry already absorbs the latency cost of a quick-fail; circuit-breaker complexity isn't justified yet. Revisit if logs show repeated tier-0 failures within a short window.
- **Streaming path** (`respond_stream` / `generate_stream`). Dead in production.
- **Cross-vendor regen ensemble.** Regen already supports N concurrent configs (`apps/tutoring/conversational_tutor.py:1593`); fallback semantics there are different and out of scope.
- **Custom domain on Azure Container Apps** — unrelated; mentioned only because it was in another in-flight thread.

## Phased delivery

| Phase | Work | Estimate |
|---|---|---|
| **P1** | Add purpose enum values + migration 0029 (seed two rows) | 0.25d |
| **P2** | Build `FallbackLLMClient` + `build_tutoring_fallback_client()` in `apps/llm/fallback_client.py` | 0.5d |
| **P3** | Wire into `conversational_tutor.py:1542` (`build_tutoring_fallback_client()`) | 0.25d |
| **P4** | Unit tests in `apps/llm/tests/test_fallback_client.py` (4 cases: tier-0-OK, tier-0-fail-tier-1-OK, all-fail, broad-catch behaviour) | 0.5d |
| **P5** | **Fault-injection test** — Django shell script that monkeypatches the live tier-0 client to raise `ServerError(503)`, drives one tutor turn end-to-end, asserts a tier-1 response landed. Doubles as the "test" half of task #256. | 0.5d |
| **P6** | Browser e2e on L540 with a forced tier-0 failure (one turn) — confirm UI looks normal | 0.5d |
| **P7** | Ship — commit with `Refs: memory/cross_vendor_tutor_fallback_plan.md`, push, watch prod logs for the new `[FallbackLLMClient]` lines for 24h | 0.25d |

**Total: ~2.75 days of focused work.**

## Open questions

1. **Should the chain re-instantiate clients per-call, or cache them at session start?** Recommend: **cache at session start** (mirrors current `_llm_client` singleton-per-session). Re-instantiating per-call adds latency for no benefit; we'd only need to re-build if a `ModelConfig` row was edited mid-session.
2. **Should `_fallback_response()` (the static HTML) be removed once the chain is in place?** Recommend: **keep it**. It's the safety-net under the safety-net — covers the case where all three tiers fail simultaneously (e.g., bug in our adapter code that breaks every provider equally).
3. **Should the broad-catch policy filter out `TypeError`/`ValueError` to avoid masking our-side bugs?** Recommend: **yes, exclude logic-error classes** (codified in the failure-class taxonomy table above). The judge chain catches everything because judges are non-critical-path; the tutor is critical-path and a `TypeError` from our adapter masquerading as a transient failure would burn through three tiers silently.
4. **Do we need an `is_active` flag on tier 2 / tier 3 for per-institution opt-out?** Defer. Today every institution shares one tutoring config; if multi-tenant per-institution chains become a need, add an `institution`-scoped layer then.

## Risks

- **R1: Latency amplification on partial failure.** If tier 0 takes 19s to exhaust retries before falling through, then tier 1 takes another 12s, the student waits 30s+ for a turn. Mitigation: pin per-vendor `MAX_RETRIES` in `client.py` to lower values for the tutoring path specifically — currently shared with content-gen. Track in P7 production logs and tune if observed.
- **R2: Silent over-billing.** Every tier-0 failure now generates a tier-1 (and possibly tier-2) call that we pay for. Mitigation: telemetry (already in design) means we can quantify weekly. If tier-2 (Opus) is rescuing > 1% of turns sustainedly, that's a Google-side reliability concern to escalate before it becomes a cost concern.
- **R3: ModelConfig.get_for() side effect.** Line 289 has a fallback: "if no config for purpose, return any active config." If `tutoring_fallback` row is mistakenly missing, `get_for` will return *something*, which might be a content-gen config that we don't want as a tutor tier. Mitigation: `build_tutoring_fallback_client()` should use `ModelConfig.objects.filter(is_active=True, purpose=...).first()` directly, bypassing `get_for`'s fallback semantics.

## Next step

**P1 — add the two enum values + migration 0029.** I'll write the migration as a pure additive `get_or_create`, run `pytest -k models` to confirm no enum-value breakage, then proceed to P2 (the wrapper class).

Commit: TBD — will end commit body with `Refs: memory/cross_vendor_tutor_fallback_plan.md`.
