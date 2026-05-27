# Overnight Run Summary — 2026-05-19

Followed the recommended order from the previous session:
1. Fix A + B (Gemini/OpenAI `tool_choice`)
2. Fix C (OpenAI `max_completion_tokens` for GPT-5+/o-series)
3. Re-validate 3-cell slice
4. (Conditional) Full 9×2×2 matrix
5. Fix D + E (retry-with-backoff for 503/529) in parallel
6. (Discovered + added) Fix G — response-shape adapter

Status per task below. Adapter unlocked Gemini + OpenAI end-to-end —
this was the load-bearing change for the full matrix to be meaningful.

## Shipped (local commits, NOT pushed)

| Commit  | Task | Title |
|---------|------|-------|
| `1b04960` | #239 + #240 | LLM clients: Gemini + OpenAI `generate_with_tools` accept `tool_choice` |
| `f198768` | #241 | OpenAI client: `max_completion_tokens` for GPT-5 + o-series |
| `ad0b2b4` | #243 | LLM clients: retry-with-backoff for transient 503/529 |
| `18601f6` | #245 | LLM clients: cross-provider response-shape adapter for `generate_with_tools` |

Five commits ahead of `origin/main` (the four above plus three earlier
prompt-system commits: `cd0fcb8`, `e6743a9`, `ddc9c36`).

**Per project rule "Don't push without my approval" — all 5 stay
local until you confirm.** Each commit is small, focused, with a clear
"why" body.

## Re-validation result (3-cell slice, after all fixes)

Command: `--models opus,gemini-3-pro,gpt-5 --lessons 540 --personas struggler --max-turns 10`

| Model | Pre-fix (v2) | Post-fix (v3) |
|-------|--------------|---------------|
| Opus  | 10 turns, max_turns, 75% tool-use, 543s | **7 turns, exit_ticket, 75% tool-use, 2/6 regen-clean, 222s** |
| Gemini 3.1 Pro | 10 turns, max_turns, **0%** tool-use, 0/11 regen-clean, 467s | **7 turns, exit_ticket, 62% tool-use, 3/6 regen-clean, 313s** |
| GPT-5 | error (BadRequestError), 0 turns | **7 turns, exit_ticket, 88% tool-use, 0/6 regen-clean, 506s** |

All 3 cells now reach `exit_ticket` in 7 turns with real SelfRetry
candidates — the headline outcome.

Note GPT-5 has the highest tool-use rate (88%) but the lowest
regen-clean-cycle-1 (0/6) — its candidates are getting flagged by
validators but never landing clean on the retry. Worth a follow-up
investigation but doesn't block the matrix run.

## Discovered + fixed: Fix G (response-shape adapter)

The validation slice surfaced a NEW bug class not in
`memory/provider_experiment_validation_errors.md`:

```
TypeError: generate_with_tools returned non-Message: GenerateContentResponse
TypeError: generate_with_tools returned non-Message: ChatCompletion
```

Engine + SelfRetry walk Anthropic Message contract via `getattr` on
blocks: `.type`, `.text`, `.name`, `.input`. Gemini returns
`GenerateContentResponse`; OpenAI returns `ChatCompletion`. Neither
passes the `isinstance(message.content, list)` shape-check at
`apps/tutoring/regen/self_retry.py:368-374`.

Fix: added `AdaptedMessage / AdaptedTextBlock / AdaptedToolUseBlock /
AdaptedUsage` dataclasses in `apps/llm/client.py` (lines 47-243), with
`_adapt_gemini_response()` and `_adapt_openai_response()` wrappers.
Both `generate_with_tools` methods wrap their return values. 8 unit
tests pass; live validation slice shipped clean (no more TypeError).

Commit: `18601f6`.

## Still pending (not done this session)

| Task | Why deferred |
|------|--------------|
| #242 Fix F: cell-level error recovery in harness | Lower priority once D+E retries cover transient errors. Worth adding before the next provider experiment so partial cell data isn't lost on an unrecoverable error. |
| Full 9×2×2 matrix run (36 cells) | **Launched in background** as `/tmp/exp_full_matrix.log` — see "In-flight" below. |

## In-flight: full matrix run

Started in background after writing this summary:

    python manage.py run_model_experiment --max-turns 10 --force

9 models × 2 lessons (L540 geography + L638 math) × 2 personas
(struggler + capable) = **36 cells**. Average cell time in the
validation slice was ~350s, so estimated wall time **~3.5 hours**.

Output: `/tmp/exp_full_matrix.log`. Results appended to
`memory/.deepmind_model_experiment_results.jsonl`. Final report
overwrites `memory/deepmind_model_experiment_results.md`.

If you wake and it's still running, `tail /tmp/exp_full_matrix.log`
to see cell progress. If it finished, the report file at the top of
the JSONL has the same data the previous benchmark used.

## Issues surfaced during the full matrix run (2026-05-19)

1. **`gemini-3.1-flash-preview` model name returns 404.** All 4
   `gemini-3-flash` cells deadlocked (~25-50s each, 0% tool-use, 0
   turns of usable output). Wrong model identifier in
   `apps/tutoring/management/commands/run_model_experiment.py:69`.
   The actual Gemini 3 Flash model id needs verification — the v2.5
   Flash spec (`gemini-2.5-flash`) works fine in the matrix. **Fix
   before next run**: list available models via
   `client.models.list()` and update MODELS spec.
2. **OpenAI `image` content type rejected.** Math lesson L638 has
   figures rendered into messages with Anthropic-shaped
   `{"type": "image", ...}` blocks. OpenAI expects
   `{"type": "image_url", "image_url": {"url": "..."}}`. GPT-5 L638
   cell errored on this. The image-block translation needs to live
   in `OpenAIClient` (parallel to the response adapter that ships
   Anthropic shape OUT — this one ships Anthropic-shaped multimodal
   content IN).

## Blockers I hit

1. **Gemini SelfRetry shape mismatch** — biggest one. Documented in
   the previous session as "to investigate"; surfaced live in the
   first re-validation attempt. Built Fix G after Cell 1 of v3
   completed; cells 2 + 3 of v3 then validated the fix.
2. **Pre-existing pose_question test failure** at
   `test_text_block_passes_through_verbatim` (line 220) — confirmed
   it fails on `main` without my changes (different code path,
   unrelated to the adapter). Not a blocker but worth flagging.
3. **Cell 2 (Gemini) and Cell 3 (GPT-5) of v3 had no `↳ err:` lines
   in the log** — the harness recorded them as `ok` because the
   adapter let regen run. The "broken regen ships dirty" path only
   matters when SelfRetry produces no clean candidate; with the
   adapter that's now a real engine decision, not an integration bug.

## Open question for you (no action needed unless you disagree)

The full matrix will run for ~3.5 hours. If you want me to:
- (A) Just let it finish and you review the report when ready, OR
- (B) Cancel mid-run, OR
- (C) Re-launch with a different scope (e.g. only `--lessons 540` or
  only `--personas struggler` to halve the time),

let me know — otherwise it'll be done in `memory/deepmind_model_experiment_results.md`
when you next look.

Commit: `18601f6` (Fix G), `ad0b2b4` (D+E), `f198768` (C), `1b04960` (A+B)
