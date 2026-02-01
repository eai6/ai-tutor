# AI Tutor - TEVETA (Python + Claude)

Simple AI tutoring system using Claude.

## Setup

```bash
# 1. Create virtual environment
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create .env file with your API key
cp .env.example .env
# Edit .env and add your Anthropic API key
# Get one at: https://console.anthropic.com

# 4. Run
python app.py
```

Open http://localhost:8080

## .env Configuration

```
ANTHROPIC_API_KEY=sk-ant-api03-xxxxx
CLAUDE_MODEL=claude-3-5-sonnet-20241022  # Optional
```

## Files

- `app.py` - Flask server with Claude
- `templates/index.html` - Frontend UI
- `.env` - Your API key (create from .env.example)
- `ai_tutor.db` - SQLite database (auto-created)

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Main UI |
| `/api/curriculum` | GET | Full curriculum |
| `/api/chat` | POST | Send message |
| `/api/clear/<id>` | POST | Clear history |
