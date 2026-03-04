# curriculum

Course hierarchy, RAG knowledge base, and automated content generation.

This is the largest app in the project. It defines the four-level content hierarchy (`Course > Unit > Lesson > LessonStep`), manages a ChromaDB vector store for retrieval-augmented generation, and orchestrates LLM-powered lesson content creation with media, educational materials, and localized context.

---

## Models

### Course

Top-level curriculum container.

| Field | Type | Description |
|-------|------|-------------|
| `institution` | ForeignKey(Institution, nullable) | `null` = platform-wide course visible to all schools |
| `title` | CharField(200) | e.g., "S1 Geography" |
| `description` | TextField | Course overview |
| `grade_level` | CharField(50) | e.g., "Grade 3", "S1" |
| `is_published` | BooleanField | Visibility to students |

### Unit

Groups related lessons within a course.

| Field | Type | Description |
|-------|------|-------------|
| `course` | ForeignKey(Course) | Parent course |
| `title` | CharField(200) | Unit name |
| `order_index` | PositiveIntegerField | Display/processing order |

### Lesson

A single teaching unit with a clear learning objective.

| Field | Type | Description |
|-------|------|-------------|
| `unit` | ForeignKey(Unit) | Parent unit |
| `title` | CharField(200) | Lesson name |
| `objective` | TextField | What the student will learn |
| `estimated_minutes` | PositiveIntegerField | Default: 15 |
| `mastery_rule` | CharField | `streak_3`, `streak_5`, `pass_quiz`, `complete_all` |
| `content_status` | CharField | `empty` > `generating` > `ready` / `failed` |
| `is_published` | BooleanField | Visibility to students |
| `metadata` | JSONField | Key concepts, skills, image suggestions |

### LessonStep

The atomic unit of instruction. Each step has a type, content, and optional rich JSON fields.

| Field | Type | Description |
|-------|------|-------------|
| `lesson` | ForeignKey(Lesson) | Parent lesson |
| `order_index` | PositiveIntegerField | Step sequence |
| `step_type` | CharField | `teach`, `worked_example`, `practice`, `quiz`, `summary` |
| `phase` | CharField | 5E phase: `engage`, `explore`, `explain`, `practice`, `evaluate` |
| `teacher_script` | TextField | What the AI tutor should say |
| `question` | TextField | Question text (for practice/quiz) |
| `answer_type` | CharField | `none`, `free_text`, `multiple_choice`, `short_numeric`, `true_false` |
| `choices` | JSONField | MCQ options list |
| `expected_answer` | TextField | Correct answer |
| `rubric` | TextField | Grading rubric for LLM evaluation |
| `hint_1/2/3` | TextField | Progressive hint ladder |
| `max_attempts` | PositiveIntegerField | Default: 3 |
| `concept_tag` | CharField(200) | Groups steps by concept for evaluation |

#### Rich JSON Fields

**`media`** -- Images, videos, and audio attachments:
```json
{
  "images": [
    {
      "url": "/media/lessons/diagram.png",
      "alt": "Cross-section of Earth's crust",
      "caption": "Figure 1: Tectonic plates",
      "type": "diagram",
      "source": "generated",
      "description": "Prompt used for generation"
    }
  ],
  "videos": [{"url": "https://...", "title": "...", "duration_seconds": 120}],
  "audio": [{"url": "/media/audio/...", "title": "Pronunciation guide"}]
}
```

**`educational_content`** -- Vocabulary, worked examples, formulas, local context:
```json
{
  "key_vocabulary": [{"term": "tectonic", "definition": "...", "example": "..."}],
  "worked_example": {
    "problem": "Calculate the distance...",
    "steps": [{"step": 1, "action": "...", "explanation": "..."}],
    "final_answer": "42 km"
  },
  "formulas": [{"name": "Distance", "formula": "d = s × t", "variables": {"d": "distance"}}],
  "key_points": ["Point 1", "Point 2"],
  "common_mistakes": ["Confusing X with Y"],
  "real_world_connections": ["Used in GPS navigation"],
  "seychelles_context": "The granite islands of Seychelles were formed by..."
}
```

**`curriculum_context`** -- Teaching strategies from the knowledge base:
```json
{
  "teaching_strategies": ["Use concrete examples before abstract concepts"],
  "learning_objectives": ["Identify three types of plate boundaries"],
  "assessment_criteria": ["Can label diagram correctly"],
  "differentiation": {
    "support": "Provide vocabulary glossary",
    "extension": "Research local geological features"
  }
}
```

---

## Knowledge Base (RAG)

### `CurriculumKnowledgeBase` class (`knowledge_base.py`)

A ChromaDB-backed vector store that indexes curriculum documents and teaching materials for semantic retrieval.

**Architecture:**
- **Embedding model**: `sentence-transformers/all-MiniLM-L6-v2` (local, offline, no API cost)
- **Storage**: `settings.VECTORDB_ROOT` (defaults to `media/vectordb`, overridable to `/tmp/vectordb`)
- **Collections**: Scoped per institution using `institution_id` prefix
- **Vector count**: ~470 vectors (175 curriculum + 295 teaching materials) in production

**Initialization:**
```python
kb = CurriculumKnowledgeBase(institution_id=5)
```
- Normalizes `None` institution to `GLOBAL_INSTITUTION_ID` (0)
- Creates separate ChromaDB `persist_directory` per institution
- Initializes embedding function (local sentence-transformers or OpenAI based on `EMBEDDING_BACKEND`)

**Query Methods:**

| Method | Purpose | Used By |
|--------|---------|---------|
| `query_for_content_generation()` | Lesson context for step generation | `content_generator.py` |
| `query_for_tutoring()` | Teaching strategies during live sessions | `conversational_tutor.py` |
| `query_for_exit_ticket_generation()` | Exam questions for MCQ grounding | `background_tasks.py` |
| `query_for_figure_descriptions()` | Textbook diagram descriptions | `skill_extraction.py`, `image_service.py` |
| `format_exam_questions_for_prompt()` | Formats retrieved exam questions for LLM | Exit ticket generator |

**Indexing:**
- `index_curriculum_text(text, metadata)` -- Chunks text and adds to ChromaDB
- `index_teaching_material(text, metadata)` -- Indexes textbook/reference content
- Called during curriculum upload processing and teaching material upload

---

## Content Generator

### `LessonContentGenerator` class (`content_generator.py`)

Generates 8-12 lesson steps per lesson using LLM with structured output (Instructor library).

**Pipeline per lesson:**
1. Query knowledge base for curriculum context and figure descriptions
2. Build prompt with course/unit/lesson info, KB context, and existing step patterns
3. Call LLM with `instructor` for structured output (Pydantic `LessonContentResult` schema)
4. Validate and save steps to DB with media descriptions, educational content, and curriculum context
5. Retry loop (3 attempts) with correction prompts for JSON parse failures

**Structured Output Schemas (Pydantic):**

| Schema | Fields |
|--------|--------|
| `TutoringStep` | phase, step_type, content, question, expected_answer, hints, media_suggestion |
| `LessonContentResult` | steps, exit_ticket questions, vocabulary, teaching_tips |
| `LessonSchema` | title, objective, key_concepts |
| `UnitSchema` | title, description, lessons |
| `LessonStructureResult` | 4-8 units for a course |

---

## Curriculum Parser

### `CurriculumParser` class (`curriculum_parser.py`)

Extracts text from uploaded documents and parses the course > unit > lesson hierarchy.

**Supported formats:** PDF (pdfplumber, PyMuPDF), DOCX (python-docx), TXT, Markdown

**Pipeline:**
1. Extract raw text from document
2. Detect section boundaries (units, lessons) using heading patterns
3. Parse lesson titles and objectives
4. Return structured hierarchy for teacher review

---

## Processing Pipeline

### `pipeline.py`

Orchestrates the 5-step upload-to-content pipeline:

| Step | Name | Description |
|------|------|-------------|
| 1 | PARSE | Extract text from PDF/DOCX |
| 2 | VECTORIZE | Chunk and embed into ChromaDB |
| 3 | GENERATE LESSONS | Query KB to structure course > unit > lesson hierarchy |
| 4 | GENERATE CONTENT | LLM creates tutoring steps, media, exit tickets per lesson |
| 5 | TUTORING | Live context delivery (not part of upload pipeline) |

---

## Signals

### `pre_delete` -- `cleanup_course_on_delete()`

When a Course is deleted:
1. Deletes ChromaDB vectors indexed from course uploads
2. Cleans teaching material upload files from disk
3. Removes curriculum upload files
4. Deletes exit ticket question images (FileField cleanup)
5. Removes orphaned MediaAssets only used by the deleted course

---

## Management Commands

| Command | Description |
|---------|-------------|
| `seed_seychelles` | Seeds 7 geography units (20 lessons) + 8 math units (27 lessons) for S1 |
| `seed_sample_data` | Creates sample curriculum data for development |
| `generate_content --course <id>` | Generates lesson content for a course via LLM |
| `generate_lesson_content --lesson <id>` | Generates content for a single lesson |
| `generate_media --course <id>` | Generates images via Gemini for lesson steps |
| `index_openstax` | Indexes OpenStax open-source textbook resources into KB |

---

## Architecture Decisions

- **Rich JSON fields** on LessonStep (`media`, `educational_content`, `curriculum_context`) rather than normalized tables -- enables flexible schema evolution and simpler content generation.
- **Local embeddings** (`sentence-transformers/all-MiniLM-L6-v2`) to avoid API costs and work offline. Configurable via `EMBEDDING_BACKEND`.
- **ChromaDB per institution** with separate `persist_directory` for data isolation.
- **Instructor library** for structured LLM output -- Pydantic validation with automatic retries instead of manual JSON parsing.
- **VectorDB on fast disk** -- `VECTORDB_ROOT` setting allows overriding to `/tmp/vectordb` in production where the default media mount (Azure SMB) is too slow for SQLite-backed ChromaDB.
