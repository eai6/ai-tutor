# Tool surface reduction — 5 tools → 2 (3 with figures) — Plan (2026-08-05)

## Problem

The tutor exposes 5 tools. Two of them carry 99.4% of all traffic; two others
have been called **once each in 1,443 production turns**. Every tool in the
schema is another option a 4B model must weigh on every turn, and the three
low-traffic tools cost 1,675 of the 6,999-byte schema block — **24% of the tool
budget for 0.6% of the calls**.

Measured across every `SessionTurn` carrying `metadata.tool_calls`:

| tool | calls | turns | % of tool-bearing turns |
|---|---|---|---|
| `record_answer` | 1,144 | 1,141 | 79.1% |
| `pose_question` | 1,138 | 1,132 | 78.4% |
| `auto_pose_fallback` *(server)* | 36 | 36 | 2.5% |
| `request_figure` | 12 | 12 | 0.8% |
| `auto_pivot` *(server)* | 3 | 3 | 0.2% |
| `auto_grade_fallback` *(server)* | 2 | 2 | 0.1% |
| `redirect_off_topic` | **1** | 1 | 0.1% |
| `advance_step` | **1** | 1 | 0.1% |

## Current state (from audit)

- `handle_redirect_off_topic` (`tools.py:1061`) writes `off_topic_count` and
  `last_off_topic_reason` to `engine_state`. **Nothing reads either.** Its own
  docstring: *"Purely a signal for analytics; does not block the conversation."*
  Off-topic is already handled by `intent.classify_student_message` and the
  CONVERSATIONAL branch of Block-0, neither of which needs the tool.
- `handle_advance_step` (`tools.py:1095`) does two things:
  1. moves `current_step_index` — fully redundant with `maybe_advance_step`,
     which advanced every measured session through all 5 steps with
     `forced_advances: 0` and zero `advance_step` calls;
  2. sets `es['remediation_complete']` when a failed `ExitTicketAttempt` exists
     (`tools.py:1142`), read at `engine.py:2785` to re-open the exit ticket for
     a retake.
- **(2) is a latent bug, not a reason to keep the tool.** The remediation-retake
  path fires only when the model calls a tool it uses in 1 turn in 1,443.
- `request_figure` is **already conditional**: `prompts.py:809` removes it from
  the tool list when `figures_enabled` is False. 5 of 8 courses have
  `tutoring_images_enabled=False`, so those sessions already see 4 tools.

## Target design

**2 tools by default, 3 when the course enables figures.**

| tool | keep? | why |
|---|---|---|
| `pose_question` | keep | 78.4% of turns |
| `record_answer` | keep | 79.1% of turns |
| `request_figure` | **keep, conditional** | dormant today but wanted for future lessons; already gated on `figures_enabled` so it costs nothing on the 5 courses with figures off |
| `redirect_off_topic` | **delete** | dead — nothing reads its output |
| `advance_step` | **delete** | redundant; its one real job moves server-side |

Figures stay because they are a real future feature (user direction). The point
is that a tutor on a figures-off course should carry exactly the two tools it
needs — which this achieves.

## Backend changes

### 1. Delete `redirect_off_topic`

- `prompts.py::TOOL_SCHEMAS` — remove the entry.
- `tools.py` — delete `handle_redirect_off_topic`; drop from the module docstring.
- `engine.py` — remove from `_KNOWN_TOOLS` (line 49), the import (2562) and the
  dispatch branch (2657).
- Leave existing `off_topic_count` values in `engine_state` alone. They are
  inert JSON keys on ≤1 session; a migration to strip them costs more than it
  saves. Do NOT add new writes.

### 2. Move `remediation_complete` server-side, then delete `advance_step`

The server already knows everything needed. `_build_exit_ticket_review`
(`engine.py:3009`) returns `missed_objectives[]`, each with an
`enabling_objective` — the documented step↔question linkage
(`auto-memory/feedback_step_question_linkage.md`).

New helper in `tools.py`, called from the same place `maybe_advance_step` is
called (`engine.py:775`):

```
maybe_complete_remediation(session):
    review = latest failed ExitTicketAttempt review, else return False
    missed = {eo for eo in review['missed_objectives']}
    covered = {eo for eo in missed
               if a post-attempt turn has a CORRECT verdict on a question
                  whose enabling_objective == eo}
    if missed and covered == missed:
        engine_state['remediation_complete'] = True
```

Criterion: **every objective the student failed has since been answered
correctly during remediation.** Deterministic, verdict-based, and mirrors
`maybe_advance_step` rather than inventing a new mechanism.

Then:
- `prompts.py::TOOL_SCHEMAS` — remove `advance_step`.
- `tools.py` — delete `handle_advance_step`.
- `engine.py` — remove from `_KNOWN_TOOLS`, import, dispatch branch, and the
  reference at line 304.
- `engine.py:2785` consumer is **unchanged** — it reads the flag, not the tool.

### 3. Docstring corrections (same change, they are now wrong)

- `engine.py:9` — step 1 claims the server picks the question via
  `pick_current_question`; it is never called. Either delete
  `pick_current_question` (`tools.py:371`) as dead code or correct the
  docstring. **Recommend deleting** — it has been dead since `2afc4e5`.
- `engine.py:16` — "4 tool schemas" → 2, or 3 with figures.
- `engine.py:31` — "Target: ≤ 600 lines" against an actual 3,358. Remove it.
- `simple_tutor/__init__.py:13-14` — lists the deleted tools.

## Out of scope

- Any change to `pose_question` / `record_answer` schemas. Their descriptions
  are long, but the compaction experiment measured **87% → 62% compliance** and
  was reverted (`~/.claude/plans/atomic-giggling-sutherland.md`). Leave alone.
- `family_tools.py` — unwired; its fate is a separate decision.
- The MCQ-only default (`TUTORING_QUESTION_TYPES`) — real and significant
  (`memory/simple_tutor_audit.md` §5b) but independent of tool count.
- Server-side pseudo-tools (`auto_pose_fallback` etc.) — these are salvage, not
  model-facing, and do not appear in the schema.

## Phased delivery

| phase | work | est. |
|---|---|---|
| 0 | Baseline: 3 replicates, `--persona capable`, record per-tool rates | 0.5 d |
| 1 | Delete `redirect_off_topic` (pure removal, no behaviour to preserve) | 0.25 d |
| 2 | `maybe_complete_remediation` + tests, THEN delete `advance_step` | 0.5 d |
| 3 | Docstrings + `pick_current_question` deletion | 0.25 d |
| 4 | Re-measure, 3 replicates per arm | 0.5 d |

Phase 2 lands the replacement **before** the removal so the retake path is never
broken in between.

## Risks

- **Remediation retake is currently near-dead**, so there is little production
  behaviour to regress — but also little evidence of what "working" looks like.
  Phase 2 needs a written test of the full fail → remediate → retake cycle; do
  not rely on the compliance harness, which never reaches the exit ticket.
- **Fewer tools ≠ better compliance, automatically.** Plausible, unproven. The
  honest justification here is a 24% smaller schema block and a simpler design,
  not a predicted compliance number. Phase 4 measures; it does not promise.
- 7 test files reference the removed tools and will need updating.

## Open questions

1. **Delete `pick_current_question` too?** Recommend yes — dead since `2afc4e5`,
   and its stale docstring actively misleads.
2. **Keep `advance_step_hints` analytics?** Recommend dropping with the tool —
   1 record ever.

## Next step

Phase 0: baseline 3 replicates on `--persona capable` so Phase 4 has something
to compare against.
