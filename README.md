# AI Tutor

A customizable, open-source AI tutoring platform built with Django.

## Features

- **Multi-tenant architecture**: Multiple institutions, each with their own content
- **Customizable AI behavior**: PromptPacks define tutor persona, teaching style, safety rules
- **Structured curriculum**: Course → Unit → Lesson → Steps (teaching, practice, quiz)
- **Intelligent grading**: Automatic grading for MCQ, numeric, true/false; LLM grading for free-text
- **Hint ladder**: Progressive hints when students struggle
- **Progress tracking**: Mastery-based advancement with streaks and completion tracking
- **Media support**: Attach images, videos, PDFs to any lesson step

## Quick Start

### 1. Clone and setup

```bash
cd ai_tutor
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file:

```env
SECRET_KEY=your-secret-key-here
DEBUG=True
ANTHROPIC_API_KEY=your-anthropic-api-key
```

### 3. Initialize database

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_sample_data
```

### 4. Run the server

```bash
python manage.py runserver
```

Visit:
- **Admin**: http://localhost:8000/admin/ (manage curriculum, prompts, users)
- **Tutor**: http://localhost:8000/tutor/ (student interface)

## Project Structure

```
ai_tutor/
├── apps/
│   ├── accounts/      # Institution, Membership (multi-tenancy)
│   ├── curriculum/    # Course, Unit, Lesson, LessonStep
│   ├── llm/           # PromptPack, ModelConfig, LLM client
│   ├── media_library/ # MediaAsset, StepMedia
│   └── tutoring/      # TutorSession, engine, grading, views
├── config/            # Django settings, URLs
├── templates/         # HTML templates
└── manage.py
```

## Key Components

### Tutor Engine (`apps/tutoring/engine.py`)

The heart of the system. It:
1. Loads lesson steps in order
2. Assembles prompts from PromptPack + step content
3. Calls the LLM to generate tutor responses
4. Grades student answers
5. Applies hint ladder when wrong
6. Tracks progress toward mastery

### Prompt Assembly (`apps/llm/prompts.py`)

Two-layer prompting:
- **PromptPack** (institution-controlled): Persona, teaching style, safety
- **Step instruction** (content-controlled): What to teach/ask right now

### Grading (`apps/tutoring/grader.py`)

- **Exact match**: MCQ, true/false
- **Numeric tolerance**: Math answers
- **LLM-based**: Free-text with rubrics

## Customization

### Creating a PromptPack

In Django Admin, create a PromptPack with:
- `system_prompt`: Core persona ("You are a friendly tutor named Sage...")
- `teaching_style_prompt`: Methodology ("Use Socratic questioning...")
- `safety_prompt`: Boundaries ("Keep content age-appropriate...")
- `format_rules_prompt`: Output rules ("Keep responses under 3 sentences...")

### Creating Lessons

1. Create a **Course** (e.g., "Grade 3 Math")
2. Create **Units** within the course (e.g., "Addition")
3. Create **Lessons** within units (e.g., "Two-Digit Addition")
4. Create **LessonSteps**:
   - `teach`: Explanation with no response needed
   - `worked_example`: Walk through an example, check understanding
   - `practice`: Problems with hints
   - `quiz`: Assessment questions
   - `summary`: Wrap-up

### Step Types & Answer Types

| Step Type | Best Answer Type | Use Case |
|-----------|------------------|----------|
| teach | none | Pure instruction |
| worked_example | short_numeric, multiple_choice | Check they followed |
| practice | any | Drill with hints |
| quiz | any | Assessment |
| summary | none | Celebrate completion |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/tutor/api/lessons/` | GET | List available lessons |
| `/tutor/api/session/start/<lesson_id>/` | POST | Start tutoring session |
| `/tutor/api/session/<id>/answer/` | POST | Submit student answer |
| `/tutor/api/session/<id>/advance/` | POST | Advance to next step |
| `/tutor/api/session/<id>/status/` | GET | Get session status |

## Testing

```bash
# Test with mock LLM (no API key needed)
python manage.py test_tutor_engine

# Test with real LLM
python manage.py test_tutor_engine --real-llm
```

## Next Steps

- [ ] Add WebSocket support for streaming responses
- [ ] Build React frontend for richer interactions
- [ ] Add analytics dashboard
- [ ] Implement OpenAI/Ollama providers
- [ ] Add Docker Compose for deployment

## License

MIT
