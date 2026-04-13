# AI Tutor — Manual Web App Testing Plan

## Prerequisites

```bash
# 1. Run automated checks first (must all pass)
./venv/bin/python tests/run_all_checks.py

# 2. Apply migrations
./venv/bin/python manage.py migrate

# 3. Start dev server
./venv/bin/python manage.py runserver
```

Ensure you have:
- A super admin account (staff user)
- At least one teacher account
- At least one student account
- The geography and math curriculum PDFs in `seychelles_package/curriculum_materials/`

---

## Test 1: Admin Panel Verification

**URL:** http://localhost:8000/admin/

### 1.1 Seychelles Context Library
- [ ] Navigate to `/admin/curriculum/seychellescontext/`
- [ ] Verify 25 entries are listed
- [ ] Verify columns: Title, Category, Subject tags, Is active, Updated at
- [ ] Click any entry — verify content field is populated with real Seychelles data
- [ ] Filter by category (e.g., "economic") — verify filter works
- [ ] Edit one entry (e.g., change content text) — save — verify it saves
- [ ] Toggle "Is active" on one entry — save — verify toggle works

### 1.2 Curriculum Models
- [ ] Navigate to `/admin/curriculum/unit/`
- [ ] Click any unit — expand "Objectives" section
- [ ] Verify `terminal_objectives` and `enabling_objectives` JSON fields are visible
- [ ] Navigate to `/admin/curriculum/lesson/`
- [ ] Verify columns include: Content quality, Teacher approved
- [ ] Click any lesson — verify "Content Quality", "Enabling Objectives" fieldsets exist
- [ ] Navigate to `/admin/curriculum/lessonstep/`
- [ ] Click any step — verify "enabling_objective" and "concept_tag" fields in "Lesson & Order" section

### 1.3 Exit Ticket Questions
- [ ] Navigate to `/admin/tutoring/exitticketquestion/`
- [ ] Verify "Question type" column is visible
- [ ] Click any question — verify `question_type` dropdown shows: MCQ, Fill in the Blank, Matching, Short Answer, Data Interpretation

### 1.4 Skills
- [ ] Navigate to `/admin/tutoring/skill/`
- [ ] Verify columns: Code, Name, Course, Bloom level, Is enabling objective, Difficulty
- [ ] Filter by "Is enabling objective" = Yes (may be empty if no content generated yet)

---

## Test 2: Dashboard Settings — Competency Thresholds

**URL:** http://localhost:8000/dashboard/settings/

**Login as:** Super admin

### 2.1 View Thresholds
- [ ] Scroll to "Competency Categories & Thresholds" section
- [ ] Verify all fields are visible with default values:
  - Below Expectation (BE): 50%
  - Approaching Expectation (AE): 80%
  - Meeting Expectation (ME): 80%
  - Exceeding Expectation (EE) time: 5 minutes
  - Move-on threshold: 70%

### 2.2 Edit Thresholds
- [ ] Change "Move-on threshold" to 75%
- [ ] Click "Save Thresholds"
- [ ] Verify success message appears
- [ ] Refresh page — verify value persisted as 75%
- [ ] Change back to 70% and save

### 2.3 Non-Superadmin
- [ ] Log in as a regular teacher
- [ ] Go to Settings
- [ ] Verify the "Competency Categories & Thresholds" section is NOT visible

---

## Test 3: Curriculum Upload — Geography

**URL:** http://localhost:8000/dashboard/curriculum/upload/

**Login as:** Teacher or super admin

### 3.1 Upload Geography Curriculum
- [ ] Click "Upload Curriculum"
- [ ] Select file: `seychelles_package/curriculum_materials/geography_document_pdf.pdf`
- [ ] Set Subject: "Geography"
- [ ] Set Grade Level: "S1"
- [ ] Submit upload
- [ ] Wait for processing to complete (watch the processing log)

### 3.2 Verify Parsing Results
- [ ] Verify the processing log shows unit count (should be ~8 units)
- [ ] Verify lesson count (should be ~27 lessons)
- [ ] If a review step appears, inspect the parsed structure:
  - [ ] Units should have titles like "Introduction to Geography", "The Earth in the Solar System", "Weather", "Introduction to Population Studies", etc.
  - [ ] Each unit should have lessons with enabling objectives listed
- [ ] Approve the structure

### 3.3 Verify Database Records
- [ ] Navigate to the created course in the dashboard
- [ ] Click into a unit (e.g., "Introduction to Population Studies")
- [ ] Verify lessons are listed with titles matching the syllabus
- [ ] Click a lesson — verify "Enabling Objectives" section shows objectives like "Define population and terms...", "Describe the trend..."

---

## Test 4: Curriculum Upload — Mathematics

### 4.1 Upload Math Curriculum
- [ ] Upload `seychelles_package/curriculum_materials/MATHEMATICS-in-the-National-Curriculum.pdf`
- [ ] Subject: "Mathematics"
- [ ] Grade Level: "S1"
- [ ] Submit and wait for processing

### 4.2 Verify Strand Structure
- [ ] Verify 5 units created (one per strand):
  - [ ] S1: Number
  - [ ] S1: Algebra
  - [ ] S1: Shape and Space
  - [ ] S1: Measures
  - [ ] S1: Handling Data
- [ ] Each unit should have sub-strand lessons (e.g., "Whole Numbers and Place Value", "Operations with Whole Numbers", "Fractions, Decimals and Percentages")
- [ ] Verify enabling objectives include K/S coded objectives (e.g., "K408: ...", "S401: ...")

---

## Test 5: Teaching Material Upload — Worksheet

### 5.1 Upload a Worksheet
- [ ] Navigate to a course detail page
- [ ] Click "Upload Teaching Material"
- [ ] Select a worksheet PDF from `seychelles_package/worksheet/mathematics/`
- [ ] Set Material Type to **"Worksheet"** (verify this option exists in dropdown)
- [ ] Submit and wait for processing

### 5.2 Verify Processing
- [ ] Check processing log shows:
  - Text extraction completed
  - Chunks indexed
  - Figures extracted (if the worksheet has diagrams/figures)
- [ ] If the worksheet matched to a course, verify "Matched worksheet to X lesson(s)" in the log

---

## Test 6: Content Generation

### 6.1 Generate Content for a Lesson
- [ ] Go to a lesson detail page (one that has enabling objectives from the curriculum upload)
- [ ] Click "Generate Content" (or "Regenerate Content")
- [ ] Wait for generation to complete

### 6.2 Verify Generated Content
- [ ] Verify lesson steps are created (12-18 steps, visible in the lesson detail page)
- [ ] Expand some steps — verify they have:
  - [ ] Phase labels (engage, explore, explain, practice, evaluate)
  - [ ] concept_tag grouping
  - [ ] teacher_script content
  - [ ] Questions for practice/quiz steps
- [ ] Verify the `enabling_objective` field on steps (check admin or expand step)

### 6.3 Verify Exit Ticket Generated
- [ ] Scroll to "Exit Ticket" section on the lesson detail page
- [ ] Verify questions were auto-generated (should see 35 questions)
- [ ] Check that question types are mixed (not all MCQ) — look for question_type labels in admin

### 6.4 Verify EO Skills Created
- [ ] Go to `/admin/tutoring/skill/`
- [ ] Filter by "Is enabling objective" = Yes
- [ ] Verify skills exist matching the lesson's enabling objectives
- [ ] Check that `bloom_level` is set correctly (e.g., "Define..." → remember)
- [ ] Check that `source_code` is set for math objectives (e.g., K408, S401)

### 6.5 Verify Content Quality Tier
- [ ] On the lesson detail page, verify the quality tier badge is shown
- [ ] If tier_3 or tier_4, verify the "Approve Content" button appears
- [ ] Click "Approve Content" — verify success message
- [ ] Verify "Approved" badge appears
- [ ] Now publish the lesson — verify it publishes (should not be blocked after approval)

---

## Test 7: Student Tutoring Session

**Login as:** Student

### 7.1 Start a Lesson
- [ ] Navigate to the tutor catalog: http://localhost:8000/tutor/
- [ ] Verify the published lesson appears
- [ ] If grade-level filtering is active, verify only appropriate lessons show
- [ ] Click the lesson to start

### 7.2 Tutor Interaction
- [ ] Verify the tutor greets with a warm opening
- [ ] Verify the opening references the lesson objective
- [ ] Answer a few questions — verify the tutor responds contextually
- [ ] Verify the step progress indicator shows (e.g., "Step 3/15")
- [ ] Verify the phase badge updates (Engage → Explore → Explain → Practice → Evaluate)

### 7.3 Difficulty Controls
- [ ] Verify "Too easy?" and "Too hard?" buttons are visible below the chat
- [ ] Click "Too easy?" — verify feedback message appears
- [ ] Verify the button highlights as active

### 7.4 Gamification
- [ ] Answer questions correctly in a row
- [ ] Verify correct-answer glow on tutor response bubble
- [ ] After 3 correct: verify streak celebration banner

### 7.5 Artifact Rendering (if triggered)
- [ ] If the tutor generates a data table or diagram, verify it renders:
  - [ ] Desktop: in the right-side artifact panel
  - [ ] Mobile (resize browser to 375px): inline in the chat bubble
- [ ] Verify the content is inside a bordered container (sandboxed iframe)

---

## Test 8: Exit Ticket — Multi-Format

### 8.1 Trigger Exit Ticket
- [ ] Complete all lesson steps (or wait for the tutor to trigger the exit ticket)
- [ ] Verify the exit ticket modal appears

### 8.2 Sectioned Layout
- [ ] Verify questions are grouped into sections:
  - [ ] Section A: Multiple Choice (with A1, A2, A3... numbering)
  - [ ] Section B: Fill in the Blanks (if any — with B1, B2... numbering)
  - [ ] Section C: Matching (if any)
  - [ ] Section D: Written Response (if any — short answer + data interpretation)
- [ ] Verify each section has a colored letter badge and instruction text
- [ ] Verify only sections with questions are shown

### 8.3 Question Types
- [ ] **MCQ**: Verify radio-button style options (A, B, C, D). Click to select.
- [ ] **Fill-in-blank**: Verify text input fields appear at blank positions in a sentence
- [ ] **Matching**: Verify two columns with dropdown selectors for each pair
- [ ] **Short answer**: Verify textarea appears
- [ ] **Data interpretation**: Verify data table/description appears above the textarea
  - [ ] If the data description contains HTML, verify it renders in a styled container

### 8.4 Progress Indicator
- [ ] Verify "0 of 10 answered" text and empty progress bar at the top
- [ ] Answer a question — verify count updates ("1 of 10 answered")
- [ ] Verify progress bar fills proportionally
- [ ] Verify submit button is disabled until all 10 questions are answered

### 8.5 Submit and Results
- [ ] Answer all 10 questions and submit
- [ ] Verify results display:
  - [ ] MCQ: correct answers highlighted green, wrong answers red
  - [ ] Non-MCQ: "Correct!" or "Incorrect. [explanation]" message below the question
- [ ] Verify the completion modal appears (if passed) or remediation message (if failed)

### 8.6 Mobile Testing
- [ ] Open Chrome DevTools → toggle device toolbar → select iPhone SE (375px)
- [ ] Trigger exit ticket
- [ ] Verify full-width modal (no border-radius, no wasted padding)
- [ ] Verify MCQ options have large touch targets (min 48px height)
- [ ] Verify fill-in-blank inputs are large enough (16px font, no iOS zoom)
- [ ] Verify matching dropdowns are full-width and stacked vertically
- [ ] Verify textareas are full-width with auto-grow

---

## Test 9: Session Report — Teacher View

**Login as:** Teacher

### 9.1 Access Session Report
- [ ] Navigate to the lesson detail page in the dashboard
- [ ] Click the "Session Report" button (purple button)
- [ ] Verify the session report page loads

### 9.2 Summary Cards
- [ ] Verify 4 summary cards:
  - Sessions Completed: X/Y
  - Avg Competency: X%
  - Below [threshold]%: count
  - Enabling Objectives: count

### 9.3 Category Distribution
- [ ] Verify 4 colored boxes showing EE, ME, AE, BE counts
- [ ] Verify the counts add up to total students

### 9.4 Recommendation Banner
- [ ] Verify the recommendation banner appears with correct color:
  - Green: "All students have achieved at least 70%..."
  - Yellow: "X student(s) are below the threshold: [names]..."
  - Red: "X/Y students are below the threshold..."
- [ ] Verify sub-text matches the recommendation action
- [ ] If green/yellow: verify "Next Lesson" link is present
- [ ] If red: verify "Review This Lesson" link is present

### 9.5 Per-Objective Competency Table
- [ ] Verify each enabling objective is listed
- [ ] Verify competency bar shows percentage
- [ ] Verify "X/Y achieved" column
- [ ] Verify color coding: green (>=70%), yellow (40-70%), red (<40%)
- [ ] Verify Bloom level badge (remember, understand, apply, etc.)

### 9.6 Students by Category
- [ ] Verify students are grouped into colored sections:
  - [ ] **BE (red)**: Students below 50%
  - [ ] **AE (yellow)**: Students 50-80%
  - [ ] **ME (green)**: Students 80-100%
  - [ ] **EE (purple)**: Students 100% + fast exit ticket
- [ ] Only categories with students should appear
- [ ] Verify each category has:
  - [ ] Category header with code, label, student count
  - [ ] **Targeted instruction recommendation** (bold "Instruction:" text)
  - [ ] **Focus objectives** as pill badges (common weak objectives for the group)
  - [ ] Student table with: Name, Objectives (X/Y), Exit Quiz score + time, Weak Areas

### 9.7 Targeted Instruction Quality
- [ ] **BE group instruction** should mention: intensive support, one-on-one, specific objectives
- [ ] **AE group instruction** should mention: brief review, close to threshold, AI tutor continues
- [ ] **ME group instruction** should mention: ready for next lesson, extension activities
- [ ] **EE group instruction** should mention: challenge problems, peer tutoring, leadership

---

## Test 10: Class Readiness Report

**URL:** http://localhost:8000/dashboard/class/COURSE_ID/readiness/

### 10.1 Course-Level View
- [ ] Verify header shows course name, student count, objective count
- [ ] Verify class readiness score circle (green/yellow/red)
- [ ] Verify recommendation banner
- [ ] Verify heat map table of all enabling objectives across the course
- [ ] Verify unit divider rows in the table
- [ ] Verify student gaps section at the bottom

---

## Test 11: End-to-End Pipeline

Complete this sequence without interruption:

1. [ ] **Super admin**: Configure competency thresholds in Settings (confirm defaults or adjust)
2. [ ] **Teacher**: Upload geography curriculum PDF → approve → generate content for 1 lesson
3. [ ] **Teacher**: Upload a worksheet as teaching material (type: Worksheet) for the same course
4. [ ] **Teacher**: Verify lesson has exit ticket, enabling objectives, quality tier badge
5. [ ] **Teacher**: Approve and publish the lesson
6. [ ] **Student 1**: Start and complete the tutor session for the lesson
7. [ ] **Student 1**: Complete the exit ticket (try to pass — score 8+/10)
8. [ ] **Student 2**: Start the same lesson, answer some questions wrong, complete exit ticket
9. [ ] **Teacher**: Open Session Report for the lesson
10. [ ] **Teacher**: Verify both students appear with their competency categories
11. [ ] **Teacher**: Verify the recommendation makes sense given the students' performance
12. [ ] **Teacher**: Verify targeted instruction recommendations per category group
13. [ ] **Teacher**: Make the move-on decision based on the report

---

## Test Results Template

| Test | Status | Notes |
|------|--------|-------|
| 1.1 Seychelles Context | | |
| 1.2 Curriculum Models | | |
| 1.3 Exit Ticket Questions | | |
| 1.4 Skills | | |
| 2.1 View Thresholds | | |
| 2.2 Edit Thresholds | | |
| 2.3 Non-Superadmin | | |
| 3.1 Upload Geography | | |
| 3.2 Verify Parsing | | |
| 3.3 Verify Records | | |
| 4.1 Upload Math | | |
| 4.2 Verify Strands | | |
| 5.1 Upload Worksheet | | |
| 5.2 Verify Processing | | |
| 6.1 Generate Content | | |
| 6.2 Verify Steps | | |
| 6.3 Verify Exit Ticket | | |
| 6.4 Verify EO Skills | | |
| 6.5 Verify Quality Tier | | |
| 7.1 Start Lesson | | |
| 7.2 Tutor Interaction | | |
| 7.3 Difficulty Controls | | |
| 7.4 Gamification | | |
| 7.5 Artifact Rendering | | |
| 8.1 Trigger Exit Ticket | | |
| 8.2 Sectioned Layout | | |
| 8.3 Question Types | | |
| 8.4 Progress Indicator | | |
| 8.5 Submit Results | | |
| 8.6 Mobile Testing | | |
| 9.1 Access Report | | |
| 9.2 Summary Cards | | |
| 9.3 Category Distribution | | |
| 9.4 Recommendation | | |
| 9.5 Per-Objective Table | | |
| 9.6 Students by Category | | |
| 9.7 Instruction Quality | | |
| 10.1 Class Readiness | | |
| 11. End-to-End | | |
