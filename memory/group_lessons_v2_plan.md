# Group Lessons v2 — Plan (2026-04-25)

Three iterations on the v1 group lessons feature (`group_lessons_plan.md`):

1. **Track completion mode per student** so the teacher knows whether each
   lesson was completed solo or in a group, and with whom.
2. **Lock participants at lesson start** — students can no longer add
   another student mid-lesson. Once the first student message is sent,
   the group is fixed.
3. **Teacher-approval gate for group sessions** (configurable per lesson)
   to prevent students adding many friends just to coast through. Lessons
   default to auto-approve; teachers can flip to require-approval per
   lesson.

## Data model changes

### `StudentLessonProgress`
- `last_completion_session = FK(TutorSession, null=True)` — points at the
  session that earned mastery (or the most recent attempt session).
- `last_completion_was_group = BooleanField(default=False)` — derived
  from the participant count at completion time. Cheap to filter on.

### `TutorSession`
- `group_approval_status = CharField(choices=['not_required', 'pending',
  'approved', 'denied'], default='not_required')`
- `group_approval_decided_by = FK(User, null=True)` — the teacher who
  approved/denied.
- `group_approval_decided_at = DateTimeField(null=True)`

### `Lesson`
- `group_requires_approval = BooleanField(default=False)` — when True,
  any group session for this lesson is gated until a teacher approves.

## Behavior

### Lock at lesson start
- `_try_add_participant` rejects with `error: 'lesson_already_started'`
  when `session.engine_state.get('exchange_count', 0) > 0`.
- The `chat_start_session` `initial_participants` body still works
  because that runs BEFORE the first turn — that's the only sanctioned
  way to form a group.
- Frontend hides "+ Add student" button once the first message is sent.

### Approval gate
- When a session would become a group (the second participant is added)
  AND `lesson.group_requires_approval=True` AND the current status is
  `not_required`: set status to `pending`.
- `respond()` short-circuits when `group_approval_status == 'pending'` —
  returns a `TutorMessage` with `phase='awaiting_approval'` and a
  message instructing the group to wait for the teacher.
- Frontend: chat input is disabled with an "Awaiting teacher approval"
  banner when status is `pending`.
- Teachers see a "Group Sessions Awaiting Approval" widget on the
  dashboard with a count badge in the nav. One-click approve/deny.
- Approve → status = `approved`, session unblocks.
- Deny → status = `denied`, secondary participants are marked inactive
  (they can rejoin or start their own session), session continues solo.

### Tracking completion mode
- `_update_competency` already iterates all active participants. Extend
  it to set:
  - `progress.last_completion_session = self.session`
  - `progress.last_completion_was_group = (len(participants) > 1)`
- Teacher dashboard student-detail row shows a 👥 badge for group-
  completed lessons.

## API additions

| Method | Path | Purpose |
|---|---|---|
| GET | `/dashboard/api/group-approvals/` | List pending sessions for teacher's institution |
| POST | `/dashboard/api/group-approvals/<session_id>/approve/` | Approve |
| POST | `/dashboard/api/group-approvals/<session_id>/deny/` | Deny + deactivate non-primary participants |

The participant-add endpoint returns `409` with
`error: 'lesson_already_started'` for mid-lesson attempts and
`200 + { ok: true, requires_approval: true }` when approval is needed.

## Phased delivery

| Phase | Work | Est |
|---|---|---|
| H1 | Models + migration. New fields on Lesson, TutorSession, StudentLessonProgress. | 0.5d |
| H2 | Lock at start in `_try_add_participant`. Frontend hides Add button once messaged. | 0.25d |
| H3 | Approval gate in tutor engine: short-circuit `respond()` when pending. | 0.5d |
| H4 | Teacher dashboard: pending-approvals widget + approve/deny endpoints. | 0.75d |
| H5 | Track completion mode: `_update_competency` writes new fields. Dashboard student-detail badge. | 0.5d |
| H6 | Tests covering all paths. | 0.5d |

Total: ~3 days.

## Open questions (committed defaults)

1. **Default approval mode**: opt-in (require_approval defaults False).
2. **Mid-lesson approval flow**: secondary participants kicked on deny;
   primary student continues solo. No silent demotion.
3. **What if a participant joins via `initial_participants` and then the
   primary tries to add another mid-lesson?**: blocked by the lock-at-
   start rule. Initial form is final.
4. **Teacher visibility**: pending approvals shown in dashboard nav with
   a badge count; auto-refresh every 30s on the approvals page.
