# Page Directory — AI Tutor Platform

`[STAFF]` Authoritative reference for every navigable URL on the
platform. The help assistant reads this file to direct users to the
right page. Each entry: URL pattern, who can access it, what the
page does, common tasks, and any recent gotchas.

URLs use `<param>` placeholders. The dashboard is rooted at
`/dashboard/`; the student-facing tutor is rooted at `/tutor/`.

---

## Student-facing pages (audience: all)

### `/tutor/` — Lesson catalog
The student's home. Lists courses available for their grade level
(based on their `StudentProfile.grade_level`). Each course expands
to its lessons; tapping a lesson opens the chat tutor.

Common tasks:
- "Where do I start?" → catalog filters by their grade automatically.
- "I don't see my course" → check the student's grade is set correctly
  (Settings → Profile, or ask their teacher).

### `/tutor/lesson/<lesson_id>/` — Chat tutor
The conversational tutoring session for one lesson. Walks the student
through 4–10 steps of the 5E flow (Engage / Explore / Explain /
Practice / Evaluate), then transitions to the exit ticket.

UI affordances:
- Chat header → **Help / report issue button** (opens the same modal
  as the floating button on other pages).
- Header overflow menu (mobile): "Too hard", "Too easy", "Toggle audio",
  "Restart lesson", "Help / report issue".
- Math toolbar above the input: `°  π  √  ²  ³  ±  ≤  ≥  ≠  ×  ÷  ½  ¼  ¾  θ`.

### `/tutor/lesson/<lesson_id>/exit-ticket/` — Exit ticket
10 questions sampled from the lesson's bank (~32 questions total).
Pass threshold = 70% (configurable per lesson via
`ExitTicket.passing_score`). Failure auto-routes to remediation.

### `/privacy/` — Privacy dashboard (GDPR)
Self-serve consent + data export. Visible to authenticated students
and teachers.

### `/help/` — Public help / FAQ
Audience-tagged FAQ sections. Public — no login required. Indexed
into the help-assistant KB.

---

## Teacher pages (audience: teacher)

All under `/dashboard/`. Teachers see content scoped to their
institution; super-admins see everything.

### `/dashboard/` — Home
Per-school overview: total sessions, completion rate, average
competency, flagged-session count badge. Top of the nav for daily
sanity checks.

### `/dashboard/classes/` — Class list
Roster grouped by grade (S1–S5). Each grade shows headcount.

### `/dashboard/classes/<grade>/` — Class detail
Roster for one grade, plus the courses generated for that grade.
Shows class-mastery average per course. Includes both the
school's institution-scoped courses AND platform-wide (global)
courses — competency stats still scope to this school's students.

Common tasks:
- "Which course is the class on?" → see lesson count + students
  attempted per course.
- "Promote the class to next grade" → select students, click
  promote.

### `/dashboard/competency/class/<course_id>/` — Class competency map
The longitudinal competency matrix. One row per teaching objective
(= one row per lesson, since the platform follows
"1 lesson = 1 teaching objective"). Columns: Students, Average
competency, Mastered ≥70%. "Class readiness" at the top is the
average of the Average competency column across lessons that have
attempts.

Common tasks:
- "Where is the class struggling?" → bottom of the table.
- "Who's behind?" → "Students with the most gaps" panel below.

### `/dashboard/competency/student/<student_id>/<course_id>/` — Per-student competency
Same matrix, scoped to one student. Status pill per row
(mastered / developing / weak / not assessed) plus competency %.

### `/dashboard/students/` — Student list
Roster across all grades.

### `/dashboard/students/<student_id>/` — Student detail
Per-student view. Sections:
- Stats: Total Sessions, Lessons Mastered, In Progress.
  ("Completed Sessions" was removed because session-status diverged
  from lesson-mastery — they measure different things.)
- Course Progress: per-course lesson list with mastery / weak-concepts
  per lesson. Includes drafts, not just published lessons.
- Recent Sessions: most recent 10 sessions with status badge.

Common tasks:
- Promote/demote between grades (form at top).
- "Why hasn't this student completed?" → check Recent Sessions for
  abandoned states.

### `/dashboard/students/bulk-upload/` — Bulk student CSV upload
Upload `username,password,first_name,last_name,email` to mass-create
student accounts in the teacher's institution.

### `/dashboard/curriculum/` — Curriculum hub
Lists courses. Drill into a course to see units → lessons.

### `/dashboard/curriculum/upload/` — Upload curriculum
The single way to create a course on the platform. Upload a syllabus
PDF + subject + grade level. Parser creates the course, units, and
one lesson per teaching objective. (See teacher_guide.md "Creating
a course" for the full flow.)

### `/dashboard/curriculum/course/<course_id>/` — Course detail
Course-level view: units, lessons (published + draft), prerequisites,
generation status, summative bank link.

### `/dashboard/curriculum/lesson/<lesson_id>/` — Lesson detail
Single-lesson view. **Read-only for teachers** (post-2026-05-07).
Super-admins see edit affordances:
- Add / remove prerequisites
- Edit Step / Generate Content buttons
- Edit / Delete buttons on each exit-ticket question
- Approve / Publish / Unpublish / Regenerate
Teachers see the structure (steps, exit-ticket bank, prerequisites)
without edit buttons.

### `/dashboard/curriculum/course/<course_id>/summative/` — Summative review
Course summative bank + per-student score table.
- Top: # attempted, # passed, class average best, pass threshold.
- Roster table: Student, Best (e.g. `24/30 · 80%`), Latest, Attempts,
  Last attempted, Status (Passed / Below pass / Not taken).
- Below: every question in the bank with options + answer.
Common tasks: see who needs help (sort puts not-taken at the bottom).

### `/dashboard/curriculum/course/<course_id>/summative/generate/` — Generate summative
Triggers summative bank generation.

### `/dashboard/lesson/<lesson_id>/monitor/` — Live monitor
Real-time view of active student sessions on this lesson. Shows
status, idle time, current step, exchange count, exit-ticket score.

Columns:
- Exchanges = engine-tracked counter (one per student↔tutor exchange).
  Same number shown on the chat history page.

### `/dashboard/lesson/<lesson_id>/report/` — Session report
Class snapshot for one lesson:
- Avg competency, completion count
- Students grouped by competency category (UN / BE / AE / ME / EE)
  with per-bucket targeted-instruction recommendations
- Each student shows: name (with exit-ticket time subtitle), competency %
  Students appear in exactly one bucket (deduped by display name when
  test accounts share a name).

### `/dashboard/sessions/<session_id>/chat/` — Chat history
Full transcript of one session. Header shows Active Duration +
Exchanges (same number as the live monitor). Internal eval/judge
metadata is intentionally hidden from this view — teachers shouldn't
see "via combined_judge / authoring_violation / why?" chips.

### `/dashboard/flagged/` — Flagged chats
Sessions where a student message tripped the safety judge
(harmful / inappropriate / manipulation). Safety-only — validator
flags (curriculum-contradicted etc.) do NOT surface here.

Columns: Student, Lesson, Flag Reason, Flagged At, Status, View.
Stats at top: Safety Flags, Unreviewed.

### `/dashboard/flagged/<session_id>/` — Flagged session detail
Same chat transcript as `/dashboard/sessions/<id>/chat/` with the
flagged turns highlighted + a "Mark reviewed" button.

### `/dashboard/feedback/` — Feedback inbox (super-admin only)
All feedback / bug reports submitted via the Help button. Filter
by status (open / closed) and severity.

---

## Super-admin pages (audience: super_admin)

In addition to all teacher pages:

### `/admin/` — Django admin
Full DB admin. Use only when the dashboard UI doesn't expose what
you need (e.g. directly editing ModelConfig rows, fixing institution
membership).

### `/admin/llm/modelconfig/` — LLM model configurations
Per-purpose model selection: tutoring / judge / regen / generation /
exit_tickets / image_generation / help_assistant / skill_extraction.
Set `is_active=True` on the row you want live; multiple active per
purpose makes the engine pick the first.
- The regen ensemble fans out to ALL active `purpose=regen` rows
  concurrently. Add a second/third row to ensemble across providers.

### `/admin/safety/safetyauditlog/` — Safety audit log
Every flagged event. Read-only view for compliance.

---

## Common task → page

| User wants to… | Go to |
|---|---|
| Generate course content | curriculum/upload (super-admin only) |
| See a class's overall progress | classes/<grade>/ |
| See where a class is struggling | competency/class/<course_id>/ |
| See one student's mastery | students/<student_id>/ |
| Review a flagged chat | flagged/ |
| See who passed the summative | curriculum/course/<id>/summative/ |
| Review one chat transcript | sessions/<id>/chat/ |
| Watch students live | lesson/<id>/monitor/ |
| Bulk-add students | students/bulk-upload/ |
| Promote/demote grade | students/<id>/ (form at top) |
| Add a regen model | /admin/llm/modelconfig/ |

---

## Recently changed (Edward, 2026-05-07/08)

- Teachers can no longer edit lessons (publish, regenerate, edit step,
  edit/delete exit-ticket questions, image regen). Super-admin only.
- The flagged dashboard is safety-only (was: safety + validator).
- Class competency matrix shows one row per lesson (was: one row per
  enabling-objective which produced 394-row matrices).
- Student detail dropped "Completed Sessions" stat + Competency
  Breakdown widget. Course Progress denominator counts ALL lessons
  (not just published).
- Summative review page now shows per-student score table.
- Chat history "Messages" → "Exchanges" (aligns with monitor).
- Help button moved into chat header (was floating bottom-right
  overlapping the input on phones).
- Container app autoscales 1→4 replicas on HTTP concurrency.

These are the answers that should override anything older the
indexer pulls in.
