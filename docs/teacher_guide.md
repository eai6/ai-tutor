# Teacher Guide — AI Tutor Platform

Comprehensive, action-oriented walkthrough for teachers and admins
using the AI Tutor dashboard. The help-assistant indexer reads this
file, so every common "how do I..." should be answerable here.

## Creating a course

**There is no "New Course" button.** Courses on this platform are
created by uploading a syllabus PDF — the parser creates the course,
its units, and one lesson per teaching objective in a single flow.

Step by step:
1. Go to **Curriculum → Upload Curriculum** in the left sidebar.
2. Upload the syllabus PDF.
3. Set subject (e.g. "Mathematics", "Geography") and grade level.
4. Set a default lesson duration (15 / 20 / 25 / 30 / 40 / 50 min).
   You can change this later without re-uploading — duration is a
   runtime knob, not a generation parameter.
5. Optionally attach teaching materials: textbooks, worksheets,
   past exam papers. Each gets a type tag that drives where it
   shows up in the AI tutoring.
6. Submit. Refresh the course list in 1–2 minutes. The course
   appears with units + lessons skeleton populated.
7. Lessons start with empty content. Click **⚡ Generate All
   Content** on the new course page to fill them in (~30–40 min
   for a 50-lesson course, runs in parallel).
8. Review and publish lessons (each lesson has a Publish button).

If you need to re-upload a different PDF for the same course later,
that's also supported via Curriculum → Upload — the system creates
a fresh course rather than overwriting.

## Re-parsing an existing course

Use this when the syllabus PDF was updated and you want to refresh
the unit / lesson skeleton WITHOUT regenerating all the content.

1. Open the course detail page.
2. Expand "Re-parse & Re-plan Lessons".
3. Click the red "Re-parse & Re-plan" button.

Re-parsing is **non-destructive**. Existing lessons with the same
title are updated in place — student mastery, session transcripts,
exit ticket questions and attempt history all survive. New lessons
in the new parse are added; lessons no longer in the parse are
left in place (orphans) so no student data is silently lost.
You can manually delete orphans if needed.

## Inviting teachers / staff

1. Go to **Settings → Invite Staff** in the left sidebar.
2. Enter the email of the teacher you want to add.
3. Optional: pick the role (teacher, admin) and the institution
   they should be added to.
4. Send the invite. The recipient gets an email with a registration
   link and creates their own account.

You can also see existing staff and pending invites under
**Settings → Staff**.

## Adding students

Two paths:

### Bulk upload (for many students at once)
1. Go to **Students → Bulk Upload** in the left sidebar.
2. Upload a CSV with columns: username, password, first_name,
   last_name, email (email optional). Template is on the bulk
   upload page.
3. Submit. The system creates accounts and adds them as students
   to your institution.

### Per-student
1. Go to **Students → All Students**.
2. Use the "Add student" form (when present) or invite individually
   with the staff invite flow set to role=student.

## Managing students

- **Students → All Students** lists every active student in your
  institution. Click a student to open their detail page.
- **Students → Student Groups** lets you set up pairs / groups
  for shared-device sessions. Pick "Shared device" mode to enable
  the "Add groupmate" button in student lessons; "Individual"
  mode hides it.
- **Settings → Classes → Promote students** advances students to
  the next grade level at the end of a school year.

## Generating lesson content

The course detail page has three buttons that run the LLM content
pipeline:

- **⚡ on a single lesson row** — wipe and regenerate just that
  lesson's steps. Exit-ticket questions are preserved.
- **⚡ Generate All Content** — yellow banner that appears when
  one or more lessons have no content yet. Fills empty lessons in
  parallel.
- **🔁 Regenerate all lessons** — purple banner. Replaces every
  lesson's steps across the whole course. Use after curriculum
  updates or to roll out new prompt changes. Takes ~30–40 min for
  a 50-lesson course running 3 in parallel.

Lesson regeneration preserves: exit-ticket questions and attempt
history, student mastery levels, SM-2 skill mastery state, all
session transcripts, and the permanent competency transcript.
What gets replaced: lesson steps (the 5E content + figures + hints).

## Setting default lesson duration

Lessons are generated at full depth (10 steps). The tutor engine
selects the right subset at session start based on the configured
duration — so changing duration is **instant, no regeneration**.

1. Open the course detail page.
2. Find the green "⏱️ Default lesson duration" form.
3. Pick the duration (15 / 20 / 25 / 30 / 40 / 50 min).
4. Optional: tick "Allow students to change" if you want students
   to pick their own session length. Untick to lock everyone to
   the default.
5. Click Apply.

Duration → step count mapping:
- 15 min → 3 steps (engage + teach + quiz)
- 20 min → 4 steps
- 25 min → 5 steps (adds worked example)
- 30 min → 6 steps (adds a second practice)
- 40 min → 8 steps (adds enrichment)
- 50 min → all 10 steps

## Editing exit-ticket questions

1. Open a lesson detail page.
2. Scroll to the exit-ticket question list (35 questions across
   5 formats: MCQ, fill-in-blank, matching, short-answer, data-
   interpretation).
3. Click any question to open the editor — change wording, options,
   accepted alternatives, explanation, embedded chart, or figure.
4. Data-interpretation questions render server-side from a
   structured spec (figure_spec / chart) — you edit the spec, not
   the rendered image.

## Generating + reviewing the summative exam

1. Open **Course → Summative**.
2. Click **Generate** if no bank exists. The system samples ≥5
   questions per teaching objective from each lesson's exit-ticket
   bank — for a 56-lesson course that's ~280 questions in the bank.
3. Each student sees 30 randomised questions per attempt. Retakes
   resample to feel genuinely different.
4. New summatives default to **unpublished**. Click **Publish**
   to make it active.

Publishing the summative triggers the "baseline gate": students
take the baseline summative before any lesson unlocks. (You can
make baseline optional via the soft-prompt banner if needed for
training sessions.)

## Weekly assignments

Set which lessons students work on this week:

1. Open the course detail page.
2. Expand the 📅 "Weekly assignments" card.
3. For each upcoming week, tick the lessons to assign and add an
   optional teacher note.
4. Save.

Students see assigned lessons on their home page; a Monday email
reminder summarises the week.

## Live monitoring

Watch sessions in real time:
1. From a course or lesson page, click **Live Monitor**.
2. See active sessions with current step + time on task.
3. Refreshes automatically.

## Reading reports

Three main report surfaces:

### Class Competency Map
The headline class-wide report. Each row is one teaching
objective; columns are baseline (first exit-ticket attempt) →
latest → final (best attempt) → Δ growth → mastered ≥70%. Plus
class readiness %, struggling objectives, and students with the
most gaps. Click a student to drill down to their per-objective
breakdown.

Source: per-lesson exit ticket attempts only (10 questions per
lesson, much richer than the summative's ~5 sampled per objective).

### Lesson session report
Per-lesson breakdown of who passed / failed / didn't take the
exit ticket. Click a row to read the full chat transcript.

### Class Readiness Report
A complementary roll-up of how ready each student is for the
course as a whole.

## Permanent competency transcript

Every time a student transitions to "mastered" on a lesson or
skill, the platform writes a durable record on their account.
These records survive course re-parses, lesson regenerations, and
even course deletion — they're the platform-of-record for what
each student has earned, regardless of whether the source course
still exists in its original form.

## Switching schools (super admin)

If you're a super admin with access to multiple schools, the
top-right header has a school picker. Selecting a school filters
every page on the dashboard to that school's data — including the
class competency map's roster.

## Flagged chats / safety

Students can flag tutor messages they find inappropriate. Flagged
chats appear under **Safety → Flagged Chats**. Each entry shows
the offending message, a snippet of context, and a link to the
full transcript. Mark resolved when reviewed.

## In-app help + AI assistant

The "Help / Feedback" button (bottom-right of every page) opens a
modal with two tabs:

- **💬 Ask the AI** — a chatbot trained on these docs. Answers
  how-to questions and can take navigational + assignment actions.
  No destructive actions; writes need explicit confirmation.
- **📩 Send to support** — text + optional screenshot capture
  forwarded to platform admins.

Things the AI assistant can help with:
- Answer "how do I..." questions about any feature
- Take you to specific pages (class readiness, summative review,
  student detail, etc.)
- Recommend the next lesson for a student
- Summarise course / student progress stats
- Assign a lesson to a specific week
- Set the default lesson duration for a course

Things the assistant cannot do (intentional safety): delete
courses / lessons / students / attempts / transcripts; modify
permissions or roles; edit prompts or model settings; touch
authentication.

## Common problems

### "My lesson is stuck on Generating"
Background generation jobs occasionally stall. The course detail
page auto-recovers stuck lessons after 10 min — refresh and try
the ⚡ button again.

### "The competency map shows 0/49 for everything"
This usually means one of:
- Students haven't taken the baseline yet — check the "Students
  attempted" stat.
- The summative was just regenerated, wiping past attempts.
  Students need to re-take the baseline.

### "A student says the tutor is confused"
Open the lesson session report → click their session → read the
transcript. The most common cause is a misread input. Use the
Help / Feedback button to flag the specific exchange.

### "The math grader marked a correct answer wrong"
The grader is forgiving with math notation — "38" and "38°" count
as the same answer. If you see a clearly-correct answer marked
wrong, send it via Send to support so we can tighten the rubric.

## Data integrity guarantees

Things you don't have to worry about:
- Re-parsing a curriculum doesn't destroy student data.
- Regenerating a lesson doesn't destroy mastery records or
  exit-ticket attempts.
- The permanent competency transcript survives any course-level
  destructive action.
- Course-level deletes (when implemented) will have a 30-day
  soft-delete grace period before hard-deletion.

## Architecture notes (for the curious)

- 5E pedagogy (Engage → Explore → Explain → Practice → Evaluate)
- Webb's DOK framework for cognitive demand
- Mastery learning (no advancement until mastery on the exit
  ticket)
- Local sentence-transformers for retrieval (offline-friendly)
- Anthropic Claude / OpenAI GPT / Google Gemini for tutoring,
  generation, and image gen — picked per purpose via the
  ModelConfig admin
- Django 5 + PostgreSQL on Azure Container Apps
