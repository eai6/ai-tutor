---
name: plan-feature
description: Plan a new feature or architectural change for this project. Use when the user asks to plan something, says "let's think about", "how could we", "plan for X", or asks for a design before implementation. Produces a markdown plan in memory/ grounded in the actual codebase, with open questions flagged. Don't use for trivial changes that fit in one function.
disable-model-invocation: false
---

# plan-feature

The user's standard workflow: plan first, implement second. Plans live in `memory/*.md` and are dense, reality-grounded, actionable documents — not vague brainstorms.

A good plan reads like something another engineer could pick up and execute. A bad plan is full of "consider X" without commitments.

## The workflow

### 1. Understand the ask

Restate the goal in one sentence. If the ask is ambiguous, ask ONE clarifying question before investigating — don't ask a list. Common axes of ambiguity:
- Scope (MVP vs full vision)
- Users (student vs teacher vs admin)
- Online vs offline constraints
- Group vs individual

### 2. Audit the relevant code

Before proposing anything, understand what exists. For anything beyond a single function, spawn an `Explore` subagent with a specific prompt. Expected output: file:line references, model fields, existing endpoints, data flow. Not prose.

Template prompt for the Explore agent:
```
I'm planning [FEATURE]. Audit the relevant code and report under 1000 words
with concrete file:line references. Do NOT propose architecture — just describe
what's there. Specifically: [3-5 specific questions]
```

Read the directly-relevant files yourself too (typically 2-3 files max). Don't re-read what the Explore agent already reported.

### 3. Check existing plans and memory

Before writing, search `memory/` for related plans (e.g., `ls memory/` and read any that look related). Avoid duplicating or contradicting them. If a related plan exists, UPDATE or CROSS-REFERENCE it rather than writing a competing plan.

Also check the auto-memory folder `~/.claude/projects/-Users-edwardamoah-Documents-GitHub-ai-tutor/memory/` for historical context that might be relevant (incidents, decisions).

### 4. Research externally only when needed

Use `WebSearch` / `WebFetch` ONLY when:
- User references an external tool/model/library (they've mentioned Gemma, Qwen, llama.rn, etc.)
- You're choosing between options and can't decide from first principles
- Current-year docs would change the recommendation

Don't research JS/Python basics or Django patterns you already know.

### 5. Write the plan

Save to `memory/<descriptive_name>_plan.md` in the **project root's memory folder** (`/Users/edwardamoah/Documents/GitHub/ai-tutor/memory/`), NOT the auto-memory folder.

Structure:

```markdown
# <Feature Name> — Plan (YYYY-MM-DD)

## Problem
[1-2 paragraphs: what are we changing and why]

## Current state (from audit)
[Brief: key facts from the audit that matter for the plan. File:line refs.]

## Target design
[The approach. Concrete. Specific. No "consider" language.]

## Data model changes
[Exact fields to add/remove with field types. Migration strategy.]

## Backend changes
[Specific files, specific functions, specific changes.]

## Frontend/mobile changes
[Concrete UX + state flow.]

## Out of scope
[Explicit list of things NOT being built in this iteration.
Prevents scope creep later.]

## Phased delivery
[Table with phases, work items, time estimates.
Estimates are days of focused work, solo.]

## Open questions
[Real questions with recommended defaults. Format: question + recommend +
reason. User confirms or redirects these before implementation starts.]

## Next step
[ONE concrete first action.]
```

### 6. Surface the plan in chat (short)

After saving, tell the user:
- What was saved and where
- 3-5 key decisions the plan commits to
- The open questions that need their input before implementation
- The proposed next step

Keep this summary under 200 words. The plan file is detailed; the chat is the elevator pitch.

## Rules for the plan itself

**Be concrete.** "Use Axios with interceptors for auth" beats "use an HTTP client." "Add `SessionParticipant` table with fields X, Y, Z" beats "support multiple students."

**Commit, don't hedge.** "Recommend: X because Y" beats "we could do X or Y or Z." The user wants decisions, not menus. When presenting options is genuinely necessary (user asked for it, or legitimately close call), frame with a clear recommendation.

**Call out scope cuts.** Every plan has an "Out of scope" section listing v2+ work explicitly. This is how you prevent scope creep during implementation.

**Estimate in solo-dev days.** The user is solo. 10 weeks = ~10 focused work-weeks. Running in parallel with their existing Django work = more calendar time. Be honest about both.

**Ground references to real files.** `apps/tutoring/conversational_tutor.py:1182` beats "the tutoring engine." Use the file:line format throughout.

**Don't duplicate CLAUDE.md content.** If a rule is in `CLAUDE.md`, reference it instead of repeating.

**Flag real risks.** Every plan has a "Risks" or "Open questions" section. Don't soft-pedal blockers.

## Anti-patterns to avoid

❌ Proposing code before auditing the existing code
❌ Writing prose plans without file:line refs
❌ Listing 10 options without recommending one
❌ Hiding complexity in "TBD" bullet points
❌ Forgetting the "Out of scope" section — user will scope-cut verbally, then lose track
❌ Saving the plan to the wrong memory folder (auto-memory vs project memory)
❌ Writing the same plan twice — check `memory/` first
❌ Starting implementation without surfacing open questions first

## Reference: plans in this project's memory

Read these as examples of the style the user expects:
- `memory/mobile_rn_plan.md` — detailed execution plan with phases and open questions
- `memory/group_lessons_plan.md` — minimal-scope feature plan
- `memory/lesson_competency_plan.md` — cleanup/migration plan with phased delivery
- `memory/offline_mobile_architecture.md` — architecture decisions with why+how lines

Each has: problem statement, audit-grounded current state, concrete target, phased delivery, open questions. Match this pattern.
