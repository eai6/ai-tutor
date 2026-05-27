# Simple-tutor prompt audit — 2026-05-27

**Branch**: `simple-tutor-systematic-eval`
**File audited**: `apps/tutoring/simple_tutor/prompts.py`
**Skills consulted**: `prompting-fundamentals-expert`, `claude-prompting-expert` (per CLAUDE.md).

## What I'm looking for

Per `claude-prompting-expert` guidance on Opus 4.7:

1. **Conflicting rules** — pairs that can't both be satisfied on a real turn. Claude 4.7 follows instructions literally; conflicts resolve by recency, not by judgment.
2. **Negative-only rules** with no positive counterpart — weaker than positive framing.
3. **Vague qualifiers** ("focused", "responsive") with no quantification.
4. **Caps shouting** (`CRITICAL`, `MUST`, all-caps NEVER) — over-triggers on Claude 4.5+.
5. **Rules buried far from the recency window** (end of `<rules>`).
6. **Tool-use guidance that lives in the main prompt instead of the tool description** — Anthropic's "writing tools for agents" guide says tool descriptions deserve equal rigor.

## Rules inventory

| ID | Rule (paraphrased) | Polarity | Length | Notes |
|---|---|---|---|---|
| R01 | REMEDIATION mode procedure | positive | ~25 lines | Mode-specific. Mostly clean. |
| R02 | Mode-switching (GRADE vs POSE) | positive | ~18 lines | The core dispatcher. |
| R03 | Keep each turn focused — 2-4 sentences for Q, ~150 words for examples | positive | 4 lines | **VAGUE QUALIFIER** — eval shows Opus 4.7 ignores "2-4 sentences". No hard cap. |
| R04 | Adapt to the 5E phase | positive | ~12 lines | Mostly informational. |
| R05 | Deliver content, not just questions | positive | 3 lines | **CONFLICTS with R03 on Explain phase.** Pulls toward longer output. |
| R06 | Responsive pacing | positive | 3 lines | Vague. "Slow down with smaller pieces" undercuts R03. |
| R07 | Tutor-driven and actionable + banned endings | positive + neg-list | ~18 lines | Strongest rule in the file. Good shape. |
| R08 | Question-type allowlist (MCQ / short_numeric / short_answer) | positive | 7 lines | Belongs in tool description for `pose_question`, not main prompt. |
| R09 | Reason carefully about reference_answer | positive | ~13 lines | Belongs in tool description for `pose_question`. |
| R10 | Always extract student's literal answer | positive | 6 lines | Belongs in `record_answer` description. |
| R11 | When step content delivered → advance_step | positive | 2 lines | Tool-call rule, belongs in `advance_step` description. |
| R12 | Figure rule | positive | 2 lines | Templated. |
| R13 | After 2 off-topic turns → redirect_off_topic | positive | 1 line | Belongs in `redirect_off_topic` description. |
| R14 | Do not reveal reference answers + banned phrases | positive + neg-list | 8 lines | Clean. |
| R15 | Speak to student, not about them + banned phrases | positive + neg-list | 9 lines | New in 2026-05-27. Clean. |
| R16 | Wrong-answer hint ladder | positive | ~25 lines | Clean. |
| R17 | Trust grader's verdict (anti-sycophancy) | positive | 2 lines | Clean. |

17 rules. Total `<rules>` block: ~190 lines (≈4 KB of system prompt).

## Conflicts detected

### Conflict 1 — R03 (length cap) vs R05 (deliver content) vs R16 (hint ladder)

**Symptom**: 16 of 17 single-turn eval failures cite `max_paragraphs` violation. The LLM picks R05 ("deliver content") on Explain-phase turns and R16 ("deeper hint…progressively deeper") on wrong-attempt turns, producing 3-5 paragraph responses that violate R03's "2-4 sentences".

**Decision (2026-05-27 user direction)**: **Drop the length cap entirely.** The tutor is free to explain at whatever length serves the explanation. The 16 max_paragraphs failures had rubric scores at-or-above threshold (often 1.00/0.70) — the LLM-judge thought the responses were pedagogically good, and we trust that signal over an arbitrary paragraph count.

<yes drop the length cap entirely for now. we already dropped it from the evaluation.>

**Fix**:
- Remove R03 entirely from the prompt.
- Remove the `max_paragraphs` deterministic assertion from eval scenarios that have it (or mark non-blocking).
- Do NOT add an `<output_format>` length cap.
- Trust the LLM-judge rubric (pedagogical quality) and the new `meta_reasoning_leak` / `passive_ending` / `narrates_tool_call` dimensions to police what actually matters.

### Conflict 2 — R07 (tutor-driven, immediately pose_question) vs R02 GRADE mode ("don't pose a new question in the same turn unless pivoting")

**Symptom**: On a correct verdict, R07 says "immediately call pose_question for the next question". R02's GRADE-mode rule says "Do NOT pose a new question in the same turn unless you're pivoting after several wrong attempts". These are compatible — pose AFTER record_answer is fine — but the wording overlaps and a literal reader could see them as conflicting.

**Fix**: Tighten R02's GRADE-mode rule to say "Do NOT pose a NEW question in the same turn as a WRONG verdict" — the existing rule's intent is to prevent piling pose on top of an incorrect attempt, not to prevent pose-after-correct.

### Conflict 3 — R09 ("reason carefully about reference_answer") vs R15 ("don't narrate your reasoning")

**Symptom**: R09 says "Before the tool call, mentally walk through the question". R15 bans visible narration. The conflict is purely about WHERE the reasoning happens — mental (allowed) vs prose in text reply (banned). Most reads will get this right, but a defensive rewrite makes it bulletproof.

**Fix**: R09 should explicitly state "this reasoning happens INTERNALLY, not in the visible text reply." Also: R09 belongs in the `pose_question` tool description, where reasoning-before-call is naturally scoped.

### Conflict 4 — R05 (deliver content during Explain) vs R02 POSE mode rule (always include question stem in text)

**Symptom**: On an Explain step, R05 says deliver content. R02 says when you call pose_question, include the stem in the text. Combined, the tutor writes an explanation paragraph AND a question stem AND options — easy to blow past any length cap.

**Fix (per 2026-05-27 user direction — no length cap)**: Add a tie-breaker: **"On Explain turns, deliver the content AND end with ONE check-for-understanding question. Both, in the same turn. The explanation can be as long as it needs to be — no word/paragraph cap."** This keeps R02's "always include question stem" and R05's "deliver content" both true without forcing the model to pick. The explanation-plus-question pattern is what we explicitly want.

## Negative-only rules with no positive counterpart

All current rules have a positive imperative. No purely-negative rules. ✅

## Vague qualifiers without quantification

- R03: "focused", "2-4 sentences", "~150 words" → **drop entirely**. The tutor explains at whatever length serves the lesson; quality is judged by the rubric dimensions, not by paragraph count. <again don't worry about word limit. let it explain.>
- R06: "slow down with smaller pieces", "advance faster" → drop entirely; pace adapts via prompt structure, not via prose instruction. <looks like we can drop this one. if it is really not nneeded>

## CAPS shouting

- `MUST`: 1 occurrence (in R01 REMEDIATION mode). Borderline — leave as `must` lowercase.
- `NEVER`: 5 occurrences in R10 / R15. Per claude-prompting-expert anti-pattern, these can over-trigger. **Replace with neutral imperatives**: "Auto-correcting destroys the grading signal" instead of "Never auto-correct…".
- `NOT`: many. Mixed. Mostly fine since they're inline (`do NOT just re-read`) rather than CAPS-shouting whole lines.

## Tool-call rules that should migrate to tool descriptions

Per `claude-prompting-expert` ("tool descriptions matter as much as the main prompt"):

| Rule | Move to |
|---|---|
| R08 (question-type allowlist) | `pose_question.description` |
| R09 (reason carefully) | `pose_question.description` |
| R10 (literal extracted_answer) | `record_answer.description` |
| R11 (call advance_step when ready) | `advance_step.description` |
| R13 (off-topic → redirect) | `redirect_off_topic.description` |

Result: `<rules>` shrinks from ~190 lines to ~110 lines. Tool descriptions grow from terse one-liners to substantive paragraphs — better cached (tools are in the static prefix). <good!>

## Recommended new structure

```
<role> ... </role>
<mode_block>
  REMEDIATION mode (R01)
  GRADE / POSE mode dispatcher (R02)
</mode_block>
<teaching>
  5E phase guidance (R04)
  Deliver content on Explain (R05)
  Wrong-answer hint ladder (R16)
  Tutor-driven and actionable (R07)
</teaching>
<student_voice>
  Do not reveal reference answers (R14)
  Speak to the student, not about them (R15)
  Trust grader's verdict (R17)
</student_voice>
<safety> ... </safety>
```

Drops R03 (length cap — tutor free to explain), R06 (vague pacing). Migrates R08–R11, R13 to tool descriptions. No `<output_format>` block — quality lives in the rubric dimensions, not in a paragraph count.

<<yes do it all>>

## Open questions

1. Should "REMEDIATION mode" stay in `<rules>` or move to a conditional block that only appears when an `<exit_ticket_review>` is present? Conditional rendering would let us drop ~25 lines from non-remediation turns and reduce conflict surface. **Decision (2026-05-27 user direction)**: Yes — make REMEDIATION mode conditionally rendered. Block 0 (cache-static) carries only TUTORING/GRADE/POSE rules. The REMEDIATION block appears in Block 2 (dynamic) when `exit_ticket_review` is populated. Trade-off: a small cache-miss on the first remediation turn, but cleaner static prefix and zero conflict surface on the 95% non-remediation path.
2. Length cap value: ≤120 words or ≤80 words? Need eval data to tune. Start at ≤120, tighten if `max_paragraphs` continues failing. <remove it. we are no longer doing length dimension.>
3. Do we add a single `<good_turn>` example (≤40 words) showing voice + tool-call pairing? Skill says skip on Opus 4.7 (reasoning model). **Decision (2026-05-27 user direction)**: include a small `<examples>` block with one good turn + one bad turn. Reason: we may switch to Sonnet (non-reasoning) in the future where few-shot demonstrably helps. Opus 4.7 tolerates short well-chosen examples; the cross-model portability is worth more than the small marginal cost on Opus. Keep it minimal — 2 short examples, no more.

## Next step

Phase 2: rewrite the prompt with the new structure. Each rule edit pairs with at least one new eval assertion (deterministic regex or rubric dimension) so we have a closed loop.
