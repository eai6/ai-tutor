# dashboard

Teacher-facing admin panel for curriculum management, content generation, student progress tracking, and safety review.

Teachers use the dashboard to upload curriculum documents, review and edit generated content (steps, exit questions, prerequisites), monitor student progress, manage classes, and review flagged conversations.

---

## Models

### CurriculumUpload

Tracks curriculum document uploads through the processing pipeline.

| Field | Type | Description |
|-------|------|-------------|
| `institution` | ForeignKey(Institution, nullable) | `null` = platform-wide upload |
| `uploaded_by` | ForeignKey(User) | Teacher who uploaded |
| `file_path` | CharField(500) | Path to uploaded file |
| `subject_name` | CharField(100) | Subject identifier |
| `grade_level` | CharField(20) | Target grade |
| `status` | CharField | See status workflow below |
| `current_step` | IntegerField | Pipeline step tracker |
| `parsed_data` | JSONField | Stored parsed curriculum for teacher review |
| `created_course` | ForeignKey(Course, nullable) | The course created from this upload |
| `units_created` / `lessons_created` / `steps_created` | IntegerField | Result counts |
| `processing_log` | TextField | Timestamped processing log |
| `is_cancelled` | BooleanField | Cancellation flag (checked by background workers) |

**Status workflow:**
```
pending → processing → review → media_processing → completed
                  ↘                                    ↗
                    → failed
```

### TeachingMaterialUpload

Tracks teaching material uploads (textbooks, references, worksheets, question banks).

| Field | Type | Description |
|-------|------|-------------|
| `institution` | ForeignKey(Institution, nullable) | `null` = platform-wide material |
| `course` | ForeignKey(Course, nullable) | Associated course |
| `material_type` | CharField | `textbook`, `reference`, `worksheet`, `notes`, `question_bank`, `other` |
| `file_path` | CharField(500) | Path to uploaded file |
| `chunks_created` | IntegerField | Number of ChromaDB vectors created |
| `figures_extracted` | IntegerField | Number of figures found in document |

Materials are indexed into the ChromaDB knowledge base for RAG during content generation and tutoring.

### TeacherClass

Groups students into classes for management.

| Field | Type | Description |
|-------|------|-------------|
| `institution` | ForeignKey(Institution) | Scoped to school |
| `name` | CharField(100) | Class name (e.g., "S1A") |
| `grade_level` | CharField(20) | Grade level |
| `teacher` | ForeignKey(User) | Assigned teacher |
| `students` | ManyToManyField(User) | Enrolled students |
| `courses` | ManyToManyField(Course) | Assigned courses |

---

## Views

All views are protected by the `@teacher_required` decorator (alias for `@staff_required`), which verifies the user has a `staff` role `Membership`.

### Dashboard Home

| View | URL | Description |
|------|-----|-------------|
| `dashboard_home` | `/dashboard/` | Key metrics: total students, sessions, completion rates |

### Curriculum Management

| View | URL | Description |
|------|-----|-------------|
| `curriculum_list` | `/dashboard/curriculum/` | All courses with status indicators |
| `course_detail` | `/dashboard/curriculum/course/<id>/` | Units and lessons with content status, generation controls |
| `course_edit` | `/dashboard/curriculum/course/<id>/edit/` | Edit course title, description, grade |
| `course_delete` | `/dashboard/curriculum/course/<id>/delete/` | Delete course (cascades) |
| `course_publish_all` | `/dashboard/curriculum/course/<id>/publish-all/` | Publish all lessons in a course |
| `unit_create` | `/dashboard/curriculum/course/<id>/unit/create/` | Create a new unit |
| `lesson_create` | `/dashboard/curriculum/unit/<id>/lesson/create/` | Create a new lesson |

### Lesson Detail & Editing

| View | URL | Description |
|------|-----|-------------|
| `lesson_detail` | `/dashboard/curriculum/lesson/<id>/` | Full lesson view: steps, media, exit questions, prerequisites |
| `step_edit` | `/dashboard/curriculum/step/<id>/edit/` | Edit step content, question, hints, media |
| `exit_question_edit` | `/dashboard/curriculum/exit-question/<id>/edit/` | AJAX: edit/delete exit ticket question |
| `lesson_prerequisite_edit` | `/dashboard/curriculum/lesson/<id>/prerequisites/` | AJAX: add/remove lesson prerequisites |
| `lesson_publish` | `/dashboard/curriculum/lesson/<id>/publish/` | Toggle lesson publish status |
| `lesson_regenerate` | `/dashboard/curriculum/lesson/<id>/regenerate/` | Regenerate full pipeline: steps + media + exit tickets + skills + prerequisites |
| `lesson_generate_content` | `/dashboard/curriculum/lesson/<id>/generate/` | Generate content for a single lesson (background) |

### Upload & Processing

| View | URL | Description |
|------|-----|-------------|
| `curriculum_upload` | `/dashboard/curriculum/upload/` | Upload curriculum PDF/DOCX |
| `curriculum_process` | `/dashboard/curriculum/process/<id>/` | Review parsed curriculum structure |
| `curriculum_approve` | `/dashboard/curriculum/process/<id>/approve/` | Approve and create course from parsed data |
| `curriculum_process_api` | `/dashboard/api/curriculum/<id>/process/` | Step-by-step processing API |

### Bulk Generation

| View | URL | Description |
|------|-----|-------------|
| `course_generate_all` | `/dashboard/curriculum/course/<id>/generate-all/` | Generate all content for a course (background, parallel) |
| `course_generate_media` | `/dashboard/curriculum/course/<id>/generate-media/` | Generate media only |
| `content_progress` | `/dashboard/curriculum/content-progress/<id>/` | Real-time generation progress page |
| `media_progress` | `/dashboard/curriculum/media-progress/<id>/` | Media generation progress page |
| `cancel_generation` | `/dashboard/curriculum/cancel-generation/<id>/` | Cancel ongoing generation |
| `cancel_lesson_generation` | `/dashboard/curriculum/lesson/<id>/cancel/` | Cancel single lesson generation |

### Teaching Materials

| View | URL | Description |
|------|-----|-------------|
| `course_upload_material` | `/dashboard/curriculum/course/<id>/upload-material/` | Upload textbook/reference material |
| `material_process` | `/dashboard/materials/process/<id>/` | Process uploaded material (index into KB) |

### Students & Classes

| View | URL | Description |
|------|-----|-------------|
| `student_list` | `/dashboard/students/` | Student list with progress metrics |
| `student_detail` | `/dashboard/students/<id>/` | Individual student progress report |
| `class_list` | `/dashboard/classes/` | Class management |

### Safety & Flagging

| View | URL | Description |
|------|-----|-------------|
| `flagged_sessions` | `/dashboard/flagged/` | Queue of flagged sessions for review |
| `flagged_session_detail` | `/dashboard/flagged/<id>/` | Detailed view of flagged conversation |
| `resolve_flag` | `/dashboard/flagged/<id>/resolve/` | Mark flag as reviewed |

### Settings & Admin

| View | URL | Description |
|------|-----|-------------|
| `settings_page` | `/dashboard/settings/` | Platform branding, school list, grade list |
| `switch_school` | `/dashboard/switch-school/` | School picker for multi-school staff |
| `reports_overview` | `/dashboard/reports/` | Reports overview |

---

## Background Tasks

### `background_tasks.py`

Background task execution using Python threading (can be replaced with Celery for production).

#### `run_async(func, *args, **kwargs)`

Runs any function in a daemon thread. Closes DB connections for thread safety.

#### `generate_all_content_async(course_id, upload_id, generate_media)`

The main bulk generation pipeline:

1. Identifies lessons needing generation (skips those with 5+ existing steps)
2. Processes lessons in parallel (`ThreadPoolExecutor`, 2 workers)
3. Each lesson runs: **steps** > **media** > **exit tickets** > **skills**
4. Checks for cancellation after each completed lesson
5. After all lessons: runs **course-level prerequisite detection** from skill graph
6. Logs progress to `CurriculumUpload.processing_log` for real-time UI polling

#### `generate_complete_lesson(lesson_id, institution_id, log_fn)`

Atomic function for one lesson's full pipeline:

| Step | Action | Description |
|------|--------|-------------|
| 1/4 | Generate steps | LLM creates 8-12 lesson steps via `LessonContentGenerator` |
| 2/4 | Generate media | Gemini creates educational diagrams for steps with `media.images` descriptions |
| 3/4 | Generate exit tickets | LLM creates 35 MCQ questions via `generate_exit_ticket_for_lesson()` |
| 4/4 | Extract skills | LLM analyzes content for skills via `SkillExtractionService` |

Each step has cancellation checks, timing instrumentation, and error isolation (one step failing doesn't block the others).

#### `generate_exit_ticket_for_lesson(lesson, institution)`

Generates 35 MCQs for a lesson:
- Builds prompt with lesson content, KB context, and exam question grounding
- Parses LLM JSON response with `parse_llm_json()`
- Creates `ExitTicket` + `ExitTicketQuestion` records
- Optionally generates figure images for questions with `figure_prompt`

---

## Health Check

### `views_health.py`

```python
def health_check(request):
    # Tests database connectivity
    # Returns {"status": "ok"} or 503 error
```

Used by Azure Container Apps for liveness and readiness probes:
- **Liveness**: every 60s, 5 failures = container restart
- **Readiness**: every 30s, 3 failures = stop routing traffic

---

## Template Tags

### `dashboard_extras.py`

| Filter | Usage | Description |
|--------|-------|-------------|
| `get_item` | `{{ mydict|get_item:key }}` | Dictionary key accessor |
| `percentage` | `{{ value|percentage:total }}` | Calculate percentage (0 if total=0) |

---

## Architecture Decisions

- **Threading over Celery** -- Simplicity for the current scale. Background tasks use `threading.Thread(daemon=True)`. DB connections are explicitly closed for thread safety.
- **2 parallel workers** for bulk generation -- Balances throughput with LLM rate limits and memory usage.
- **Progress via polling** -- The progress pages poll `CurriculumUpload.processing_log` for real-time updates rather than using WebSockets.
- **AJAX editing** -- Exit questions and prerequisites use inline AJAX (JSON POST) for editing, matching the pattern of `saveQuestion()`/`deleteQuestion()`.
- **Prerequisite detection after generation** -- Course-level prerequisite detection runs after bulk generation completes (uses skill graph, no additional LLM calls).
