# Contributing to AI Tutor

Thank you for your interest in contributing to AI Tutor! This project aims to make quality vocational education accessible worldwide through AI-powered personalized learning.

## Ways to Contribute

### 1. 🌍 Add Country Configurations

Help expand AI Tutor to new countries by creating configuration files:

1. Copy `config/settings.example.json` to `config/examples/your-country.json`
2. Customize for your country's:
   - TVET institution name and mission
   - Local companies and industries
   - Safety standards organizations
   - Language and currency
3. Submit a pull request

### 2. 📚 Add Curriculum Content

Create curriculum files for vocational programs:

1. Create a file in `curricula/your-country-program.json`
2. Follow the curriculum schema:
   ```json
   {
     "programs": [{
       "code": "XX",
       "name": "Program Name",
       "modules": [{
         "code": "XX-101",
         "name": "Module Name",
         "learning_objectives": ["..."],
         "topics": [{"name": "...", "subtopics": ["..."]}]
       }]
     }]
   }
   ```
3. Ensure content aligns with your national qualifications framework

### 3. 🌐 Translate the Interface

Help make AI Tutor accessible in more languages:

1. Create translation files in `translations/`
2. Translate UI strings
3. Update the language configuration

### 4. 🐛 Report Issues

Found a bug? Please report it:

1. Check if the issue already exists
2. Create a new issue with:
   - Clear description
   - Steps to reproduce
   - Expected vs actual behavior
   - Screenshots if applicable
   - Your environment (OS, Python version)

### 5. 💡 Suggest Features

Have an idea? We'd love to hear it:

1. Open a discussion or issue
2. Describe the feature and its benefits
3. Explain the use case

### 6. 🔧 Submit Code

#### Setting Up Development Environment

```bash
# Fork and clone the repo
git clone https://github.com/YOUR_USERNAME/ai-tutor.git
cd ai-tutor

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Set up pre-commit hooks
pre-commit install
```

#### Code Style

- Follow PEP 8 for Python code
- Use meaningful variable and function names
- Add docstrings to functions and classes
- Write tests for new features

#### Submitting a Pull Request

1. Create a feature branch: `git checkout -b feature/your-feature`
2. Make your changes
3. Run tests: `pytest`
4. Commit with clear messages
5. Push and create a pull request
6. Fill in the PR template

## Code of Conduct

### Our Standards

- Be respectful and inclusive
- Welcome newcomers
- Focus on constructive feedback
- Respect differing viewpoints

### Our Responsibilities

Maintainers will:
- Review PRs in a timely manner
- Provide constructive feedback
- Maintain the project's quality standards

## Questions?

- Open a GitHub Discussion
- Email: [your-email@example.com]

## Recognition

All contributors will be recognized in:
- The README contributors section
- Release notes

Thank you for helping make vocational education accessible worldwide! 🎓
