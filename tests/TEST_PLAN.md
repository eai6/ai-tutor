# AI Tutor — Systematic Testing Plan

## 1. Automated Tests (run first)

```bash
# Full test suite (180 tests, 4 pre-existing audio mock failures expected)
./venv/bin/python manage.py test apps.tutoring.tests -v2

# P1 features only (34 tests)
./venv/bin/python manage.py test apps.tutoring.tests.test_p1_features -v2

# Quick smoke test
./venv/bin/python manage.py test apps.tutoring.tests -v0
```

Expected: 176 pass, 4 fail (audio mock issues — pre-existing, unrelated).

---

## 2. Parser Testing (no web app needed)

### 2a. Geography Parser
```bash
./venv/bin/python -c "
import fitz, django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
django.setup()
from ai_tutor.apps.curriculum.curriculum_parser import parse_geography_curriculum

doc = fitz.open('seychelles_package/curriculum_materials/geography_document_pdf.pdf')
text = ''.join(page.get_text() for page in doc)

for grade in ['S1', 'S2', 'S3']:
    result = parse_geography_curriculum(text, grade_level=grade)
    units = len(result.units)
    lessons = sum(len(u.get('lessons',[])) for u in result.units)
    eos = sum(len(u.get('enabling_objectives',[])) for u in result.units)
    print(f'{grade}: {units} units, {lessons} lessons, {eos} enabling objectives')
"
```

**Verify:** S1 should have 8 units, ~27 lessons, ~66 EOs. Each unit should have clean enabling objectives (action verbs, no teaching strategy noise).

### 2b. Math Parser
```bash
./venv/bin/python -c "
import fitz, django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
django.setup()
from ai_tutor.apps.curriculum.curriculum_parser import parse_mathematics_curriculum

doc = fitz.open('seychelles_package/curriculum_materials/MATHEMATICS-in-the-National-Curriculum.pdf')
text = ''.join(page.get_text() for page in doc)

for grade in ['S1', 'S3']:
    result = parse_mathematics_curriculum(text, grade_level=grade)
    units = len(result.units)
    lessons = sum(len(u.get('lessons',[])) for u in result.units)
    eos = sum(len(u.get('enabling_objectives',[])) for u in result.units)
    print(f'{grade}: {units} strands, {lessons} sub-strand lessons, {eos} K/S/A objectives')
"
```

**Verify:** S1 should have 5 strands (Number, Algebra, Shape & Space, Measures, Handling Data), ~15 lessons, K/S/A coded objectives.

### 2c. Subject Detection
```bash
./venv/bin/python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
django.setup()
from ai_tutor.apps.curriculum.curriculum_parser import detect_subject

print(detect_subject('This is a mathematics curriculum with algebra and fractions'))
print(detect_subject('Geography and map skills for secondary students'))
print(detect_subject('General document about education'))
print(detect_subject('', provided_subject='Chemistry'))
"
```

**Verify:** Mathematics, Geography, General, Chemistry.

---

## 3. Seychelles Context Library

```bash
./venv/bin/python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
django.setup()
from ai_tutor.apps.curriculum.models import SeychellesContext

print(f'Total entries: {SeychellesContext.objects.count()}')
for cat in SeychellesContext.Category.values:
    count = SeychellesContext.objects.filter(category=cat, is_active=True).count()
    if count:
        print(f'  {cat}: {count}')
"
```

**Verify:** 25 entries across economic, geographic, trade, climate, population, sustainable_dev, os_map, industry categories.

---

## 4. Content Quality Tier Detection

```bash
./venv/bin/python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
django.setup()
from ai_tutor.apps.curriculum.content_generator import LessonContentGenerator

gen = LessonContentGenerator.__new__(LessonContentGenerator)

# Tier 1: has KB content + figures
print(gen._determine_content_quality({'related_content': ['text'], 'figure_descriptions': [{'d':'fig'}], 'objectives': ['obj']}))

# Tier 3: syllabus only
print(gen._determine_content_quality({'related_content': [], 'objectives': ['obj']}))

# Tier 4: framework only
print(gen._determine_content_quality({'related_content': [], 'objectives': []}))
"
```

**Verify:** tier_1, tier_3, tier_4.

---

## 5. EO Skill Extraction

```bash
./venv/bin/python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
django.setup()
from ai_tutor.apps.tutoring.skill_extraction import SkillExtractionService

service = SkillExtractionService.__new__(SkillExtractionService)

# Test Bloom level inference
for obj in ['Define population density', 'Explain the factors', 'List the main areas', 'Calculate the area', 'Evaluate the impact']:
    bloom = service._infer_bloom_level(obj)
    print(f'  {obj[:40]:40s} -> {bloom}')
"
```

**Verify:** define→remember, explain→understand, list→remember, calculate→apply, evaluate→evaluate.

---

## 6. Competency Categories

```bash
./venv/bin/python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
django.setup()
from ai_tutor.apps.accounts.models import PlatformConfig

config = PlatformConfig.load()
print(f'Thresholds: BE<{config.threshold_be_max}%, AE<{config.threshold_ae_max}%, ME>={config.threshold_me_min}%, EE time<{config.threshold_ee_time_minutes}min, Move-on>={config.threshold_move_on}%')

for pct, time in [(30, None), (60, None), (90, None), (100, 3), (100, 10)]:
    cat = config.categorize_student(pct, time)
    print(f'  {pct}% (time={time}min) -> {cat[\"code\"]} ({cat[\"label\"]})')
"
```

**Verify:** 30%→BE, 60%→AE, 90%→ME, 100%+3min→EE, 100%+10min→ME.

---

## 7. Exit Ticket Grading (multi-format)

```bash
./venv/bin/python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
django.setup()
from unittest.mock import MagicMock
from ai_tutor.apps.tutoring.conversational_tutor import ConversationalTutor

tutor = ConversationalTutor.__new__(ConversationalTutor)

# MCQ
q = MagicMock(question_type='mcq', correct_answer='B')
print(f'MCQ correct: {tutor._grade_exit_question(q, \"B\")}')
print(f'MCQ wrong:   {tutor._grade_exit_question(q, \"C\")}')

# Fill-in-blank
q = MagicMock(question_type='fill_in_blank', answer_data={'blanks': ['GNP', 'US dollars'], 'accept_alternatives': [['gross national product'], ['USD']]})
print(f'FIB correct: {tutor._grade_exit_question(q, [\"GNP\", \"US dollars\"])}')
print(f'FIB alt:     {tutor._grade_exit_question(q, [\"gross national product\", \"USD\"])}')
print(f'FIB wrong:   {tutor._grade_exit_question(q, [\"GDP\", \"euros\"])}')

# Matching
q = MagicMock(question_type='matching', answer_data={'pairs': [{'left': 'GNP', 'right': 'Total value'}]})
print(f'Match correct: {tutor._grade_exit_question(q, {\"GNP\": \"Total value\"})}')
print(f'Match wrong:   {tutor._grade_exit_question(q, {\"GNP\": \"Wrong\"})}')

# Short answer
q = MagicMock(question_type='short_answer', answer_data={'keywords': ['health', 'education', 'income'], 'min_keywords': 2})
print(f'Short correct: {tutor._grade_exit_question(q, \"HDI includes health and education\")}')
print(f'Short wrong:   {tutor._grade_exit_question(q, \"it is a number\")}')
"
```

**Verify:** All True/False results match expected.

---

## 8. Web App — Manual Testing Checklist

### 8a. Admin (http://localhost:8000/admin/)

- [ ] `/admin/curriculum/seychellescontext/` — 25 entries visible, can edit
- [ ] `/admin/curriculum/lesson/` — content_quality and teacher_approved columns visible
- [ ] `/admin/curriculum/unit/` — terminal_objectives and enabling_objectives in edit form
- [ ] `/admin/tutoring/skill/` — is_enabling_objective and source_code columns visible
- [ ] `/admin/tutoring/exitticketquestion/` — question_type column visible

### 8b. Dashboard Settings (http://localhost:8000/dashboard/settings/)

- [ ] Competency Thresholds section visible (super admin only)
- [ ] Can edit BE/AE/ME/EE thresholds and save
- [ ] Can edit move-on threshold and save

### 8c. Curriculum Upload (http://localhost:8000/dashboard/curriculum/upload/)

- [ ] Upload geography PDF → verify units/lessons created with enabling objectives
- [ ] Upload math PDF → verify strand-based units created
- [ ] Upload a worksheet PDF → verify material_type=worksheet option available
- [ ] Check that figures are extracted (look at processing log for figure count)

### 8d. Lesson Detail (http://localhost:8000/dashboard/curriculum/lesson/ID/)

- [ ] Quality tier badge visible (Tier 1/2/3/4)
- [ ] "Approve Content" button visible for Tier 3-4 unpublished lessons
- [ ] "Session Report" button visible
- [ ] Enabling objectives visible in edit form (collapsed section)

### 8e. Lesson Session Report (http://localhost:8000/dashboard/lesson/ID/session-report/)

- [ ] Summary cards: sessions completed, avg competency, students below threshold
- [ ] Category distribution: EE/ME/AE/BE count boxes
- [ ] Recommendation banner with correct color (green/yellow/red)
- [ ] Per-objective competency table with bars
- [ ] Students grouped by category (BE → AE → ME → EE)
- [ ] Each group has targeted instruction recommendation
- [ ] Each group shows common weak objectives as pill badges
- [ ] Individual students show X/Y objectives, exit quiz, weak areas

### 8f. Class Readiness Report (http://localhost:8000/dashboard/class/COURSE_ID/readiness/)

- [ ] Heat map of enabling objectives across course
- [ ] Class readiness score
- [ ] Student gap list

### 8g. Tutoring Session (http://localhost:8000/tutor/)

- [ ] Start a lesson → verify tutor greets with context
- [ ] Answer questions → verify step progression
- [ ] Trigger exit ticket → verify sectioned layout (A: MCQ, B: Fill-in-blank, etc.)
- [ ] Verify progress bar updates as you answer
- [ ] Verify mobile layout (use browser dev tools, 375px width)
- [ ] Complete exit ticket → verify results display per question type
- [ ] Check that "Too easy?" / "Too hard?" buttons work

### 8h. Artifact Rendering

- [ ] If tutor generates an artifact (table, data), verify it renders in sandboxed iframe
- [ ] Desktop: artifact panel on right side
- [ ] Mobile: artifact inline in chat bubble
- [ ] Exit ticket data_interpretation: verify HTML table renders (if generated with HTML)

---

## 9. End-to-End Pipeline Test

The full flow to test:

```
1. Upload geography PDF as curriculum
2. Upload a worksheet PDF as teaching material (type: worksheet)
3. Wait for processing to complete
4. Review parsed structure → approve
5. Generate content for one lesson
6. Verify: lesson steps have enabling_objectives
7. Verify: exit ticket generated with mixed formats
8. Verify: EO skills created (check /admin/tutoring/skill/)
9. Publish the lesson
10. Log in as a student
11. Start the lesson → complete tutor session
12. Complete exit ticket
13. Log back in as teacher
14. Open Session Report → verify competency data populates
15. Verify recommendation makes sense
16. Verify students grouped by category
```

---

## 10. Run Everything

```bash
# 1. Automated tests
./venv/bin/python manage.py test apps.tutoring.tests -v2

# 2. Parser tests (copy commands from sections 2a, 2b above)

# 3. Start dev server
./venv/bin/python manage.py runserver

# 4. Open browser and follow manual checklist (section 8)
```
