# AI Tutor - Seychelles Secondary Schools

An AI-powered tutoring platform designed for secondary school students in Seychelles, covering Geography and Mathematics. Built with Django and powered by LLMs (Claude, GPT, or local Ollama).

## 🎓 Features

- **Science of Learning**: Structured tutoring sessions following research-based pedagogy
- **Two Operating Modes**: Rich mode (with curriculum content) or Lightweight mode (AI-generated)
- **Multiple LLM Providers**: Anthropic Claude, OpenAI GPT, or local Ollama
- **Progress Tracking**: Track student mastery and completion
- **Local Context**: Examples and names adapted for Seychelles

## 🚀 Quick Start

### 1. Install Dependencies

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment

Create a `.env` file in the project root:

```bash
# For Anthropic Claude (recommended)
ANTHROPIC_API_KEY=sk-ant-xxxxx

# For OpenAI (optional)
OPENAI_API_KEY=sk-xxxxx

# Django settings
DEBUG=True
SECRET_KEY=your-secret-key-here
```

### 3. Initialize Database

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_seychelles  # Seed curriculum data
```

### 4. Run the Server

```bash
python manage.py runserver
```

Visit http://localhost:8000 to access the tutor.

## 📚 Curriculum Structure

### Subjects
- **Geography** (7 units, 20 lessons)
  - Map Skills, Weather & Climate, Plate Tectonics, Population, Settlements, Coastal Landforms, Industry & Fishing
  
- **Mathematics** (8 units, 27 lessons)
  - Number, Fractions & Decimals, Percentages, Ratio & Proportion, Algebra, Geometry, Measures, Statistics

### Seychelles Schools Supported
- Anse Boileau, Anse Royale, Belonie, Beau Vallon, English River
- La Digue, Mont Fleuri, Perseverance, Pointe Larue, Plaisance, Praslin

## 🧠 Science of Learning Principles

The AI tutor follows research-based pedagogy:

1. **Retrieval Practice** - Start sessions reviewing previous topics
2. **Explicit Instruction** - Clear explanations with worked examples
3. **Guided Practice** - Scaffolded hints, never giving direct answers
4. **Exit Ticket** - 5 MCQ quiz, 4/5 required to pass
5. **Spaced Repetition** - Review modules periodically
6. **Local Context** - Seychelles names, places, and examples

## 🔧 LLM Configuration

### Using Anthropic Claude (Default)
Set `ANTHROPIC_API_KEY` in your `.env` file.

### Using OpenAI GPT
1. Set `OPENAI_API_KEY` in your `.env` file
2. In Django Admin, create a new ModelConfig:
   - Provider: `openai`
   - Model name: `gpt-4o`
   - API key env var: `OPENAI_API_KEY`

### Using Local Ollama
1. Install Ollama: https://ollama.ai
2. Pull a model: `ollama pull llama3`
3. Start Ollama: `ollama serve`
4. In Django Admin, create a new ModelConfig:
   - Provider: `local_ollama`
   - Model name: `llama3`
   - API base: `http://localhost:11434`
   - API key env var: (leave empty)

## 📁 Project Structure

```
ai_tutor/
├── apps/
│   ├── accounts/      # User, Institution, StudentProfile
│   ├── curriculum/    # Course, Unit, Lesson, LessonStep
│   ├── llm/          # PromptPack, ModelConfig, LLM clients
│   ├── media_library/ # Media assets (future)
│   └── tutoring/     # TutorSession, Engine, Grader
├── config/           # Django settings
├── templates/        # HTML templates
└── manage.py
```

## 🛣️ URLs

| URL | Purpose |
|-----|---------|
| `/` | Redirects to `/tutor/` |
| `/accounts/register/` | Student registration |
| `/accounts/login/` | Sign in |
| `/tutor/` | Subject & lesson catalog |
| `/tutor/lesson/<id>/` | Tutoring session |
| `/admin/` | Django admin |

## 🧪 Test Accounts

After running `seed_seychelles`:

- **Student**: student1 / student123
- **Teacher**: teacher1 / teacher123

## 📝 Development

### Running Tests
```bash
python manage.py test
```

### Creating Migrations
```bash
python manage.py makemigrations
python manage.py migrate
```

### Adding New Curriculum
Use Django Admin or create a management command similar to `seed_seychelles.py`.

## 📄 License

MIT License

## 🤝 Contributors

- World Bank Education Team
- Roy & Edward (Development Lead)



# Next steps 
# 1. Extract Docs Materials
https://edu.gov.sc/
# 2. Use Google gemini for content generation/organization and tutoring
# 3. Optimize media usage in tutoring. Prioritize already generated media, only generate a limited number of new images when requested by the student. 
# 4. Add protection against the generation of harmful material (text/media) during tutoring