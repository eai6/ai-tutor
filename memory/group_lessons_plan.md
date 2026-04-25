# Group Lessons — Implementation Plan (2026-04-23)

## Problem

Students often learn better in small groups. Today, a `TutorSession` belongs to exactly one student. We want: **multiple students complete a lesson together on one device, answer as a single collective, and all receive credit** — including the exit ticket score. No per-turn attribution, no real-time sync across devices, no turn-taking logic.

Scope: **same-device, answer-as-one**. Cheaper, matches Seychelles lab reality (shared devices). Multi-device real-time collaboration is explicitly out of scope for this iteration.

## Core design

One session, many students. Chat happens once. Exit ticket happens once. Credit flows to everyone.

```
┌─ TutorSession ────────────────────────┐
│  lesson: Lesson                       │
│  participants: [Sarah, Tom, Alex]     │  ← NEW (was single `student` FK)
│  primary_student: Sarah               │  ← still one (session "owner")
│  engine_state, turns, exit_ticket...  │
└───────────────────────────────────────┘
       │
       ├─→ Sarah   gets StudentLessonProgress row updated + ExitTicketAttempt
       ├─→ Tom     gets StudentLessonProgress row updated + ExitTicketAttempt
       └─→ Alex    gets StudentLessonProgress row updated + ExitTicketAttempt
```

Because students answer as a collective, **the tutor engine needs almost no changes** — it still sees a single stream of student input. The only engine change is the system prompt: address the group by name, not "you" singular.

## Data model changes

### `apps/tutoring/tutoring_models.py`

**Keep `TutorSession.student` as-is** (backwards-compatible; this becomes the "primary student" / session owner).

**Add `SessionParticipant`**:
```python
class SessionParticipant(models.Model):
    session = models.ForeignKey(TutorSession, on_delete=models.CASCADE, related_name='participants')
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='group_sessions')
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [('session', 'student')]
        indexes = [models.Index(fields=['session', 'is_active'])]
```

**Invariant**: `TutorSession.student` (primary) always has a corresponding `SessionParticipant` row. When a solo session is created, a single participant row is written. When group members join, additional rows are added.

**Convenience property on `TutorSession`**:
```python
@property
def active_students(self):
    return User.objects.filter(
        group_sessions__session=self,
        group_sessions__is_active=True,
    )

@property
def is_group(self):
    return self.participants.filter(is_active=True).count() > 1
```

### Migration strategy

Backfill: for every existing `TutorSession`, create one `SessionParticipant` row (the current `student`). One-off data migration. Zero behavior change for solo sessions.

### Progress + exit ticket

- `StudentLessonProgress`: no schema change. Session-end code updates one row per participant.
- `ExitTicketAttempt`: no schema change. Submission creates one row per participant with identical answers + score.

**Optional: `ExitTicketAttempt.group_session` (FK, nullable)** to let the teacher dashboard show "these three attempts are from the same group session" without cross-referencing.

### Teacher controls

Add to `Lesson` (or `Course` if you want course-wide policy):
```python
allow_group_mode = models.BooleanField(default=True)
max_group_size = models.PositiveIntegerField(default=4)
```

Later (optional): `ExitTicket.allow_group_submission = BooleanField(default=True)` — so teachers can force individual submission for summative assessments. **Defer this until a teacher asks for it.**

## Backend changes

### New REST endpoints

Under `/api/v1/` (from the RN plan):

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/sessions/<id>/participants/` | Add a participant. Body: `{username, password}` (second student authenticates on the primary device). Returns participant row. |
| DELETE | `/api/v1/sessions/<id>/participants/<user_id>/` | Mark participant inactive (`left_at` set, `is_active=False`). |
| GET | `/api/v1/sessions/<id>/participants/` | List current participants. |

### Existing endpoints — behavior adjustments

- **POST `/api/v1/sessions/`** (start session): accept optional `initial_participants: [{username, password}]` in body to start a group session in one call (primary student already authenticated via JWT; additional students supply credentials).
- **POST `/api/v1/sessions/<id>/respond/`**: no changes. Chat input is collective.
- **POST `/api/v1/sessions/<id>/exit-ticket/`**: server now iterates active participants and creates an `ExitTicketAttempt` for each with identical answers + score. Returns one response with per-participant status.

### Authentication model for joining

A second student on the first student's device must prove identity. Simplest approach:
1. Second student enters username + password on the primary device.
2. Backend validates credentials; does NOT issue them a new JWT (they never "log in" on this device).
3. Backend verifies they're in the same institution as the primary student (reject cross-school groups).
4. `SessionParticipant` row created.

No persistent auth token for the secondary student. If they want their own session later, they log in normally on their own device.

**Rejection cases** (return 400 with clear message):
- Not in same institution
- Already a participant in this session
- `SessionParticipant` count at `lesson.max_group_size`
- Lesson has `allow_group_mode=False`

### Rate limiting / abuse

Add participant count guardrail: max 4 participant adds per session (matches typical `max_group_size`). Rate-limit participant-add endpoint: 10 adds per minute per session to stop credential-stuffing attacks.

## Tutor engine changes

### `apps/tutoring/conversational_tutor.py`

Minimal: the engine treats collective input as-is. Only the system prompt adapts.

**In system prompt construction** (around line 2500):
- If `session.is_group`:
  - Inject `{"group_mode": True, "student_names": [...]}` into prompt variables
  - Add one block to the system prompt: `"You are tutoring a group of students: {names}. Address them as a group ('you all', 'everyone') or by name when appropriate. They are working together and will answer as one collective."`
- If not group: behavior unchanged.

**No changes to**:
- `respond()` flow
- Step evaluation (answer is still one string)
- Exit ticket handling (answers are still one set)
- Remediation

### Completion + progress update

In the session-end code path (wherever `StudentLessonProgress` gets updated today):
- Iterate `session.active_students`, update progress row for each.
- Same mastery logic per student. If Sarah had already mastered this lesson and Tom hadn't, both get the same session contribution; Sarah's record may already be at max, Tom advances.

## Mobile (RN) UI changes

### New flows in `mobile/app/(app)/lessons/[id].tsx`

Before starting a session, show two options:
- "Start solo" (default)
- "Start with a group"

If group: after "Start," go to the tutor screen with an empty participants list. Primary student can tap a "+ Add student" button anytime during the session to add more.

### Add-student modal

```
┌─ Add a student ────────────────┐
│ Username: [                  ] │
│ Password: [                  ] │
│                                │
│  [ Cancel ]     [ Add to group ]│
└────────────────────────────────┘
```

On success: participant appears in the top bar. On failure: inline error ("Wrong password" / "Different school" / "Group full").

### Participants bar (top of chat screen)

```
┌─ Chat: Newton's Second Law ────────────────────┐
│ 👥 Sarah · Tom · Alex       [ + Add ] [ × Tom] │
│────────────────────────────────────────────────│
│ Tutor: Hi everyone! Let's explore...           │
│ You (group): We think F = ma                   │
│ ...                                            │
```

"×" next to a name removes them (`left_at` set). Primary student cannot be removed (they end the session instead).

### Chat attribution

Because students answer as one: no per-speaker labels on student bubbles. Just "You" or "Your group." Tutor bubbles unchanged.

### Offline mode

- Group composition recorded locally in `session_participants` SQLite table.
- On sync, server reconciles: for each locally-recorded participant, POST to `/api/v1/sessions/<id>/participants/` with credentials. **Credential problem**: we can't store plaintext passwords in local SQLite. Two options:
  1. **Require online to add participants** (simplest). Joining group session needs connectivity; afterwards it can go offline. Credentials verified at join time, participant record syncs with the session.
  2. **Allow offline add** by storing a one-time-use hash and relying on server verification later. Complex.
- Recommend: **require online for participant add**, allow offline for everything else (chat, exit ticket).

### Local SQLite schema additions

```ts
export const session_participants = sqliteTable('session_participants', {
  id: text('id').primaryKey(),
  session_id: text('session_id').notNull(),
  student_id: integer('student_id').notNull(),
  student_username: text('student_username').notNull(),   // for display only
  student_display_name: text('student_display_name'),
  joined_at: integer('joined_at', { mode: 'timestamp' }).notNull(),
  left_at: integer('left_at', { mode: 'timestamp' }),
  is_active: integer('is_active', { mode: 'boolean' }).default(true),
  synced: integer('synced', { mode: 'boolean' }).default(false),
});
```

## Web UI changes (parity)

Lighter touch than mobile since web is less pilot-focused, but keep behavior consistent:
- `tutoring/catalog.html`: "Start with a group" toggle on lesson start
- `tutoring/lesson.html`: participants bar at top with add/remove
- `accounts/views.py`: reuse existing authentication for add-participant check (no new endpoint if using server-rendered form; or use the `/api/v1/` endpoint via JS).

Priority: mobile first, web second. Web can ship in the same backend release with UI following later.

## Teacher dashboard changes

### Minimal (v1)

- `dashboard/session_list`: show a "group" badge on sessions with >1 participant; click expands to list names.
- `dashboard/lesson/<id>/session-report/`: group the session view — when a session is shared, show one row with multiple student names + shared exit ticket score.
- `dashboard/session/<id>/chat-history/`: no UI change; chat is already collective.
- `dashboard/student/<id>/`: each student's session history includes group sessions they participated in, labeled as group.

### Later (optional)

- Per-student contribution metric (who typed most? might be naive without turn attribution — skip for v1)
- Teacher can disable group mode for a specific lesson via lesson edit UI

## Out of scope for this iteration

1. Multi-device real-time sync (different students on different phones in one session)
2. QR-code or join-code invitation flow
3. Persistent `StudyGroup` entities that teachers pre-configure
4. Per-speaker attribution of chat turns
5. Turn-taking enforcement
6. Per-student scoring divergence (e.g., individual remediation for one student mid-group-session)
7. Forcing individual exit ticket submission (defer `ExitTicket.allow_group_submission` until asked)

Each of these is a reasonable v2+ addition, but none are required for the core value.

## Phased build

| Phase | Work | Est. |
|---|---|---|
| **G1 — Backend data model** | `SessionParticipant` model + migration + backfill, update `TutorSession` properties, update exit-ticket submission to iterate participants, add `Lesson.allow_group_mode` + `max_group_size`. | 2 days |
| **G2 — Backend API** | Participant add/remove/list endpoints, update `/sessions/` create to accept initial_participants, update `/exit-ticket/` response. Tests. | 2 days |
| **G3 — Tutor engine prompt adjust** | Detect `is_group` in prompt builder; inject names + group instruction. Smoke-test one group session end-to-end. | 1 day |
| **G4 — Mobile UI** | Lesson-start picker, participants bar in chat screen, add/remove modal, local SQLite `session_participants` table, sync integration. | 3–4 days |
| **G5 — Web UI parity** | Same flows on existing Django templates. | 2 days |
| **G6 — Teacher dashboard** | Group badge on sessions + expanded participant list on reports. | 1 day |

**Total: ~10 working days.** Can run in parallel with mobile Phase A if backend work is sequenced ahead of mobile UI work. Could land in a pilot build within 2 weeks if prioritized.

## Open questions (decide before G1)

1. **Secondary-student auth**: is password-on-primary-device acceptable? Alternative is a 6-digit code the secondary student gets from their own logged-in device. More secure but needs both students to have devices — which defeats the same-device scenario. **Recommend: password-on-primary.** Flag as a trust/UX concern to revisit post-pilot.

2. **Group exit ticket recording**: is it acceptable for all group members to receive identical scores (including identical wrong-answer marks)? Pedagogically this is the simplest model, but it does mean a strong student can be dragged down by the group. **Recommend: yes, identical scores; teacher can manually override per-student afterwards via existing exit-ticket review flow.**

3. **Participant adds after exit ticket starts**: lock participants at exit-ticket start? Or allow mid-ticket join? **Recommend: lock at exit-ticket start.** Whoever is in the group when "Submit exit ticket" is tapped gets credit.

4. **Abandoned group sessions**: if a group session is abandoned (timeout, never submitted), how is that tracked for each participant? **Recommend: same as solo — session marked `abandoned`, no progress update, no exit-ticket row.** Simplest.

5. **Students across different grade levels**: should a Grade 8 student be allowed in a Grade 9 lesson's group session? Technically feasible but pedagogically questionable. **Recommend: allow it but log it; teacher dashboard flags cross-grade group sessions for review.**

## Next step

Approve open questions above (or redirect), then start G1: backend data model and migration. Pure Django, low risk, unblocks everything downstream.
