# DeepMind meeting — slide outline (2026-05-18)

Talking-points + structure for tomorrow's presentation. Populate the
data tables from `memory/deepmind_model_experiment_results.md` once
the sweep finishes.

## Story arc

**The hook:** building a real conversational tutor exposes the
**tool-use + instruction-following** axis of model quality far more
sharply than any closed benchmark we've seen.

**The data:** same lesson, same student persona, same judge stack,
same tool definitions. Only the tutor LLM changes. 9 models across 3
providers, 2 lessons (geography + math), 2 personas (struggler +
capable). 36 sessions, each ~10–30 turns through `respond()`.

**The finding:** [populate after sweep] — likely some variant of:
- Tool-use compliance varies wildly (Opus 95%+ vs others ~50-80%).
- Sonnet @ default temp leaked XML `<tool_use>` blocks as prose; @ 0.0 it doesn't.
- Gemini 3 Pro vs Opus on factual / leak detection — TBD.
- GPT-5 — TBD.

**The "why this matters for Gemini":**
- The bench is reusable. Every prompt change Google ships could be
  re-evaluated on real tutoring sessions in <3 hours.
- The judge stack is provider-agnostic — Gemini can be the judge,
  the regenerator, or the tutor independently.
- Existing public benchmarks don't surface what we measure
  (multi-turn tool discipline under remediation pressure).

## Slide order (proposed)

1. **Title** — "Tutor model quality is a tool-use story" (or similar).
2. **The product** — 30s overview: Seychelles pilot, 5E lessons, judge stack, regen ensemble. Screenshot.
3. **The bench** — what we measure per turn (one diagram):
   - tool-use rate
   - validator flags (no_question, answer_leak, repeated_question, …)
   - regen-cycle convergence (clean cycle-1 vs cycles-exhausted)
   - leak incidents
   - session terminate-reason
4. **Method** — synthetic student persona drives `respond()`; same judge stack runs; ModelConfig swapped per cell. Two personas × two subjects × nine models = 36 sessions.
5. **Headline chart** — bar chart: tool-use rate × model, grouped by provider. Colour by tier.
6. **Headline chart** — bar chart: regen cycle-1 clean rate × model.
7. **Headline chart** — heatmap or grouped bar: validator flag distribution per model.
8. **Geography vs math** — does the picture change by subject? Side-by-side.
9. **Struggler vs capable** — does the picture change by persona? Side-by-side.
10. **Cost slide (if we capture token data)** — quality vs $.
11. **What we built for Gemini** — explicit feature ask / call-to-action:
    - Tighter `tools=[…]` adherence with no XML leakage.
    - Stronger short-answer-judge prompts (Gemini already wins on grounded factuality with Search; we want that on tutor-side judges).
    - Long-context: the tutoring prompt is 30KB+; Gemini's window is an asset.
12. **What's next** — what we'd do with closer Gemini access:
    - Run Gemini Flash as the judge to halve eval cost.
    - Test Gemini 3 Pro as the regen rewriter (small focused prompt, latency-sensitive).
    - Try Gemini 3 Pro with `tool_config.function_calling_config.mode='ANY'` (force tool selection) — would solve the tool-use compliance gap if it works.
13. **Q&A — open mic.**

## Headline numbers to fill in (after sweep)

| Metric | Opus 4.7 | Sonnet 4 | Haiku 4.5 | Gemini 3 Pro | Gemini 3 Flash | Gemini 2.5 Flash | GPT-5 | GPT-4o | GPT-4o mini |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Tool-use rate (avg) |  |  |  |  |  |  |  |  |  |
| Clean cycle-1 regen (avg) |  |  |  |  |  |  |  |  |  |
| Sessions exhausted regen-cycles |  |  |  |  |  |  |  |  |  |
| Leak incidents (total) |  |  |  |  |  |  |  |  |  |
| Sessions completing exit-ticket transition |  |  |  |  |  |  |  |  |  |

## Open questions (for DeepMind themselves)

1. **Gemini 3 tool-use mode** — is there a "strict-tool" mode that guarantees tool selection over prose when the prompt has tools defined? (Roughly equivalent to OpenAI's `tool_choice: required` but for Gemini.)
2. **Long-context tutoring**: is there guidance on how to structure a 30KB+ system prompt that's read every turn? Caching docs help but we still cache-miss on schema changes.
3. **Vision judges** — we run a `figure_alignment` vision judge on every generated image. Gemini's multimodal-first design seems ideal here. What's the right cost-quality tier?
4. **On-device** — we paused mobile-RN inference because Gemma 3n / Qwen 3.5 at small sizes weren't pilot-grade. What's the realistic floor on-device for a 5-minute tutor turn?

## Files to bring to the meeting

- `memory/deepmind_model_experiment_results.md` — raw per-cell data, sortable tables.
- This outline (slide draft).
- A screenshot of the product (`screenshots/session65_exit_complete.png`).
- Optional: link to the open-source bench code (`apps/tutoring/student_sim/` + `apps/tutoring/management/commands/run_model_experiment.py`) — Google folk love to see the methodology repo, not just the result.
