# Pilot launch execution plan — Seychelles 2026-05-11

Living plan for the final 14-day stretch. Reflects decisions from the
Vaani × Edward meeting on 2026-04-27 (full notes in
`~/.claude/projects/.../memory/project_pilot_meeting_2026-04-27.md`).

## Hard deadlines

- **Wed 2026-04-29** — training content lock. Anything teachers will see
  during training must be functional & demoable by EOD Tuesday so we
  rehearse it Wed AM.
- **Mon 2026-05-11** — pilot launch in Seychelles.

## Implementation model (decided)

Hybrid across 4 schools:
- **2 schools**: individual accounts + individual devices. Full
  personalization. No "Add student" button.
- **2 schools**: shared devices, paired sessions. Pairs are
  pre-approved by the teacher and frozen for the pilot.

This is configurable at the **school level** (V1) and possibly
**class level** (V2 if needed). Drives whether the Add-student button
appears and who can be added.

## Pairs (not groups)

Pairs typically = 2 students, but the data model should support N (some
schools have 3+ students per device). Pairs are formed from **baseline
assessment scores** so paired students are at similar learning levels.
Frozen for the duration of the pilot — no mid-pilot reshuffles unless a
teacher overrides.

## Owned tasks (Edward)

### 1. Pair-based group sessions + school session-mode config — DUE WED 2026-04-29

**Why**: training demos this; the wrong behavior in front of teachers
poisons the rollout. Replaces the dropped "group approval" workflow
with a stronger gate (pre-approved pair).

**Scope**:
- New `StudentGroup` model on `apps/accounts/`. Per institution. M2M
  to `User` via membership table (so a student is in exactly one group
  at a time, with history if they're moved).
- Teacher dashboard page: `/dashboard/groups/` — create/edit groups,
  drag students in. Persistent for the pilot.
- New `Institution.session_mode = 'individual' | 'shared_device'`
  (default `'shared_device'` — safest for unknown schools).
- Student chat UI: hide Add-student button when school is `'individual'`.
- When `'shared_device'`: replace username/password modal with a
  groupmate picker (only students in the host's group are listed; tap to
  join, no password required).
- Server-side enforcement on `session_participants` POST: 403 if the
  student isn't in the host's group OR if the school is `'individual'`.
- Drop: lesson-level `allow_group_mode` checkbox (the school-level
  config supersedes it). Keep `max_group_size` per lesson.

**Status**: not started.

### 2. Bug report / feedback button — DUE WED 2026-04-29

**Why**: needed during training so teachers can flag issues in real
time without breaking the rehearsal flow. Vaani referenced the BR
platform's pattern.

**Scope**:
- Floating "Report a bug / feedback" button on every authenticated
  page (small, bottom-right, non-intrusive).
- Modal: short text + screenshot (capture current viewport via
  html2canvas), auto-attach URL + user agent + user id.
- Backend: new `FeedbackReport` model in `apps/dashboard/`, list view
  for superadmins.
- For students: simpler "thumbs down + free text" pattern, since
  Vaani flagged tech-literacy concerns for younger users.

**Status**: not started.

### 3. FAQ / docs page + short instructional videos — DUE WED 2026-04-29 (shell), videos rolling

**Why**: training references this. Teachers need a place to land for
self-serve troubleshooting after training.

**Scope**:
- New public-ish page at `/help/` (auth required for app-specific
  walkthroughs; static general FAQ open).
- Sections: "Getting started", "Lessons", "Sessions & pairs",
  "Reports", "Troubleshooting".
- Each section has short text + 1–2 minute embedded videos. Videos
  recorded separately (Loom / OBS); host on Azure Storage or YouTube
  unlisted; embed via iframe.
- Chatbot (later): defer to post-pilot. The static FAQ is enough for
  Wed.

**Status**: not started.

### 4. Data privacy / consent in sign-up — DUE BEFORE 2026-05-11

**Why**: country-level compliance per Vaani; legal-team input
expected. Not a training blocker.

**Scope**:
- New `accounts/PrivacyTerms` model (versioned) + admin to edit.
- Sign-up flow: required checkbox "I agree to the terms & privacy
  policy" linking to a static `/terms/` page.
- `User`/`Membership` records the version they accepted + timestamp.
- Existing users get a one-time interstitial on next login.

**Status**: not started. Waiting on legal-team text — Edward to draft
placeholder, swap content when text arrives.

## Other in-flight work (deferred until 1–3 are done)

- **Summative assessment system** — model migration shipped
  (`tutoring/0022_summative_assessments.py`). Generator, selection,
  teacher view, student take page still TODO. Plugs into the
  **baseline pre/post assessment** Vaani described — needed for pilot
  results, not for training. Plan: `memory/summative_assessments_plan.md`.

## Items NOT on Edward's plate (tracking only)

- Implementation-model confirmation with Martin — Vaani.
- Teacher input on timetable slots — Vaani.
- Training agenda + AI-literacy adaptation — Vaani drafting, Edward
  reviews.
- Transcript sharing — Vaani to send.

## Execution order I'll follow

1. Group sessions + school mode config (full day Mon–Tue).
2. Bug report button (half day Tue).
3. FAQ shell + 2–3 videos (half day Tue + Wed AM).
4. Wednesday: rehearse training; fix anything that breaks.
5. Wed–Thu: privacy/consent draft (waits on legal text).
6. Resume summative assessment work for baseline (Thu onward).

Will check in with user after step 1 and after step 3, since those are
the highest-blast-radius changes.
