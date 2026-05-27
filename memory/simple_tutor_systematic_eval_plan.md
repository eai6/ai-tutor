# Simple-tutor systematic eval + prompt cleanup plan

**Branch**: `simple-tutor-systematic-eval`
**Started**: 2026-05-27
**Goal**: Audit + clean up the simple-tutor system prompt, then wire a closed-loop "prompt rule ↔ eval check" mechanism so future regressions surface in eval runs instead of in production chat.

## Why now

The first systematic eval run (`evals/runs/2026-05-27T01-56-58_b03997a3956d.json`) revealed:

| Pattern | Count | Diagnosis |
|---|---|---|
| `max_paragraphs` failures | 16/17 single-turn | Prompt says "2-4 sentences"; Opus 4.7 ignores it |
| Rubric < threshold | 4 | banned opener, math pushback, false-accept-numeric, struggler IDK |
| Meta-reasoning prose leak | observed in prod | No prompt rule against narration |
| Passive endings ("Take your time") | observed in prod | Rule existed but banned list missed it |
| Multi-turn end-to-end | 6/6 ✅ | Architecture works; problem is single-turn discipline |

Both `prompting-fundamentals-expert` and `claude-prompting-expert` skills consulted before any prompt change (per CLAUDE.md).

## Pedagogical rubric dimensions to track

From the Tutor Feedback rubric the user shared (one binary per dimension per scenario):

| Dimension | Desirable | Question |
|---|---|---|
| Mistake identification | Yes | Did the tutor recognize a student mistake? |
| Mistake location | Yes | Did it point to the genuine mistake, not a fake one? |
| Revealing the answer | No | Did the tutor give away the final answer? |
| Providing guidance | Yes | Did it offer correct guidance / hint / explanation? |
| Actionability | Yes | Is it clear what the student should do next? |
| Coherence | Yes | Is the response logically consistent with prior turns? |
| Tutor tone | Encouraging | Encouraging / neutral / offensive? |
| Human-likeness | Yes | Natural vs robotic / artificial? |

Plus **simple-tutor-specific** dimensions (rule-violation flags):

| Dimension | Yes/No | Source rule |
|---|---|---|
| meta_reasoning_leak | No (desirable) | "Speak to the student, not about them" |
| passive_ending | No | "Tutor-driven and actionable" |
| reveals_reference_answer | No | "Do not reveal reference answers" |
| banned_opener | No | (TBD: enumerated banned openers) |
| narrates_tool_call | No | "Tool calls do the bookkeeping silently" |

**No `over_length` dimension** — the tutor is free to explain at whatever length serves the lesson. Pedagogical quality is judged by the rubric (mistake identification, actionability, etc.), not by paragraph count. The 16 max_paragraphs single-turn failures in the baseline run had rubric scores at or above threshold; the deterministic length check was vetoing pedagogically good responses.

## Phases

### Phase 1 — Audit existing prompt for conflicts (no edits yet)

**Output**: `evals/reports/prompt_audit_2026-05-27.md` enumerating every `(positive imperative, negative constraint)` pair in `apps/tutoring/simple_tutor/prompts.py` and flagging any that could conflict on a real scenario. Specifically check:

- "Deliver content" (positive) vs the new length cap
- "Tutor-driven" (positive) vs anti-narration (negative) — could the model interpret narrating "next I'll pose…" as being tutor-driven?
- Mode-switching (GRADE vs POSE vs REMEDIATION) — does any rule apply outside its mode?
- Tool-call rules in main prompt vs in tool descriptions — duplications or contradictions

### Phase 2 — Rewrite the prompt with disciplined structure

Per claude-prompting-expert guidance:

1. **One rule per concern**, clustered by topic (mode, teaching, hint ladder, tools, safety).
2. **Positive imperative first** in each cluster, banned/guardrail list after.
3. **No length cap** — tutor explains at whatever length serves the lesson. Quality enforced by the rubric, not by paragraph count.
4. **Small few-shot block** — one good turn + one bad turn. Cross-model portability: Sonnet (when we use it in the future) benefits from few-shot; Opus 4.7 tolerates a tight, well-chosen pair. Keep to 2 examples max.
5. **Drop ALL CAPS shouting** — already removed but verify no remnants.
6. **Strengthen tool descriptions** — each tool's `description` carries the same rigor as the main prompt. Pre-empt the "do work in prose instead of calling the tool" failure mode.
7. **REMEDIATION mode conditionally rendered** — Block 0 (cache-static) holds only TUTORING/GRADE/POSE rules. The REMEDIATION block appears in Block 2 (dynamic) when `exit_ticket_review` is populated. Reduces non-remediation conflict surface to zero.
8. **Conflict 4 tie-breaker (no-length-cap version)**: "On Explain turns, deliver the content AND end with ONE check-for-understanding question. Both, in the same turn. The explanation can be as long as it needs to be." Explanation-plus-question is the desired pattern, not a problem to constrain.

### Phase 3 — Eval rubric expansion

`evals/scorers/llm_rubric.py` currently uses a single per-scenario rubric string. Expand to a structured multi-dimensional rubric where each dimension returns yes/no/score:

```yaml
rubric:
  dimensions:
    - mistake_identification: {desirable: yes}
    - mistake_location:        {desirable: yes}
    - reveals_answer:          {desirable: no}
    - actionability:           {desirable: yes}
    - tutor_tone:              {desirable: encouraging, allowed: [encouraging, neutral]}
    - human_likeness:          {desirable: yes}
    - meta_reasoning_leak:     {desirable: no}
    - passive_ending:          {desirable: no}
    - over_length:             {desirable: no}
```

Judge prompt asks for a JSON object keyed by dimension. Scoring: a scenario passes when ALL desirable dimensions are at the desired value AND any deterministic regex assertions pass.

Per-rule deterministic checks (cheap, no LLM call) for hot rules:

| Rule | Regex / check |
|---|---|
| `meta_reasoning_leak` | `r"(?im)^(the student|i'll|let me prompt|i shouldn't|i'm going to|now i need)"` |
| `passive_ending` | `r"(?i)(ready for the next|want to try another|let me know when you're ready|take your time|whenever you're ready)\\s*[.?!]?\\s*$"` |
| `banned_opener` | matches a stop-phrase list (`certainly`, `great question`, etc.) |

### Phase 4 — Wire prompt-rule-coverage tracking

Each prompt rule gets a unique ID (e.g. `R-OUTPUT-001` for the length cap). Maintain a small registry mapping rule → eval assertion(s) → judge dimension(s). The report renderer (`evals/report.py`) gains a "rule coverage" section showing which rules have at least one check.

A rule without a check is a process bug — the registry surfaces that.

### Phase 5 — Iterate

1. Run eval suite on baseline (current prompt).
2. Apply prompt change in Phase 2.
3. Re-run eval. Diff via `python -m evals.report --diff <prev>`.
4. Keep changes that improve pass rate; revert ones that don't.
5. New report committed at `evals/reports/simple_tutor_<date>.md` so we have a track record.

## Out of scope (deferred to next branch)

- Multi-agent decomposition (`agent-orchestration-expert` warns against premature)
- Touching the legacy `ConversationalTutor` — still serves prod, separate effort
- Frontend changes (no UI changes in this branch; staying on the prompt + eval layer)

## Conventions

- Every prompt edit and matching eval assertion ship in the SAME commit. A rule without a check is rejected.
- Commit messages prefix `prompt-audit:` for audit-only changes, `prompt-fix:` for content changes, `eval-rubric:` for rubric/scorer changes.
- Report markdown reused from the M12.9 format (Pass/Fail/Error overall + by mode + by persona + assertion frequency + analysis).

## Done criteria

- [ ] Phase 1 audit report committed to `evals/reports/prompt_audit_*.md`.
- [ ] Phase 2 rewritten prompt passes existing 378-test suite.
- [ ] Phase 3 rubric expanded; per-dimension judge prompt + parser shipped.
- [ ] Phase 4 rule registry with ≥1 check per rule.
- [ ] Phase 5 eval re-run posts ≥ baseline pass rate. The 16 `max_paragraphs` single-turn failures should flip to PASS because the assertion itself is dropped (the responses were already pedagogically good per the rubric).
