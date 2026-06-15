# RESOLVED — open-source models can run the production tutor engine

**(Supersedes the earlier "blocker" note, which was based on a flawed test.)**

## Goal
Evaluate open-source models running locally/offline (via Ollama) to find the
best one for low-connectivity deployments (Mozambique/Tanzania), especially on
phones/tablets.

## What was thought to be a blocker (and why it was wrong)
Our production tutor engine (`simple_tutor`) drives pedagogy through LLM
**tool-calls** (`pose_question`, `record_answer`, `advance_step`, …). Two earlier
issues looked like blockers:

1. **The engine hard-coded the Anthropic SDK** — it literally couldn't talk to a
   local model. → **Fixed.** The engine's LLM call now goes through our pluggable
   client factory; Anthropic keeps its existing path byte-for-byte, and any other
   provider (incl. local Ollama) is supported. (Added Ollama tool-calling support
   to the client layer.)

2. **A quick probe suggested small models "can't tool-call."** → **False alarm.**
   That was a weak test prompt. On a clear request, the small models tool-call
   fine (e.g. all of llama3.2:3b / qwen2.5:3b / a tool-tuned 8B correctly called a
   test function). The real nuance is *prompt-sensitivity*, not capability.

## Verified working (local, this machine)
- **`simple_tutor` + `llama3.2:3b` (3B, runs on a phone/tablet-class device):**
  full two-call tool loop fired — the model posed/advanced via tools and produced
  a real structured tutor turn. ✅
- **`simple_tutor` + Anthropic (production default):** unchanged, still works. ✅

## The real, nuanced finding
**Model choice matters more than model size.** Under the tutor's complex prompt:
- **Llama 3.x** follows the tool protocol reliably.
- **Qwen 2.5** tool-calls on simple prompts but tends to revert to plain text under
  the tutor prompt (so it loses the structured questioning/grading).

This is exactly what the evaluation is designed to measure — which offline model
best drives the real tutoring pedagogy, not just which one is smallest.

## Status / next step
The engine change is done and the evaluation harness is ready. Next is the full
offline-model sweep (each model scored across the lesson scenarios by our existing
Anthropic judges) to rank candidates. No production deploy yet — all local.

## Secondary constraints (hardware)
- The current test machine is 8 GB RAM / CPU-only, so it covers the
  phone/tablet (≤4B) and modest-laptop (≤9B) tiers. Bigger laptop models (14B+)
  need a ≥16–32 GB or GPU host (the harness is portable to one).
