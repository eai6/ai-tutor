# Vision-Based Material Extraction — Test & Improvement Plan

## The 4 Curriculum Pillars

```
        Content (what to teach)
            |
Structure ← CURRICULUM → Pedagogy (how to teach)
            |
        Assessment (how to test)
```

Each pillar is informed by different document types:
- **Structure**: Curriculum syllabus → units, grade levels, objectives
- **Content**: Syllabus + textbooks → concepts, definitions, examples
- **Pedagogy**: Teacher notes + textbooks → strategies, activities, scaffolding
- **Assessment**: Worksheets + exam papers → question formats, mark schemes, rubrics

## Test Phase 1: Curriculum Vision Extraction (PRIORITY)

### Test 1.1: S3 Geography Syllabus
```bash
./venv/bin/python -c "
from apps.curriculum.curriculum_parser import extract_curriculum_with_vision
result = extract_curriculum_with_vision(
    'seychelles_package/curriculum_materials/geography_document_pdf.pdf',
    'Geography', 'S3'
)
# Verify:
# - Unit 16 (Development & Trade): 11 TOs, ~21 EOs
# - Unit 18 (Map Skills): 8 TOs
# - Unit 21 (Coastal Landforms): 5 TOs
# - All units have grade_level='S3'
"
```

**Expected**: 6 S3 units, ~40+ terminal objectives, ~80+ enabling objectives.
**Compare against**: Manual count from the PDF (already verified: Unit 16 has exactly 11 TOs)

### Test 1.2: S1 Geography Syllabus
Same PDF, different grade. Should extract only S1 units (Units 1-7).

### Test 1.3: Mathematics Curriculum
```bash
./venv/bin/python -c "
from apps.curriculum.curriculum_parser import extract_curriculum_with_vision
result = extract_curriculum_with_vision(
    'seychelles_package/curriculum_materials/MATHEMATICS-in-the-National-Curriculum.pdf',
    'Mathematics', 'S1'
)
# Math has different structure: K/S/A coded objectives, strands
# Verify vision handles the matrix table format
"
```

## Test Phase 2: Worksheet Vision Extraction

### Test 2.1: Geography Worksheet
```bash
./venv/bin/python -c "
from apps.dashboard.material_tasks import extract_material_with_vision
items = extract_material_with_vision(
    'seychelles_package/worksheet/geography/Development-Trade-worksheet-2.pdf',
    material_type='worksheet',
    subject='Geography',
    grade_level='S3'
)
for item in items:
    print(f'Q{item.get(\"question_number\",\"?\")}: [{item.get(\"question_type\",\"?\")}] {item.get(\"question_text\",\"\")[:80]}')
    print(f'  Command: {item.get(\"command_word\",\"\")} | Marks: {item.get(\"marks\",\"\")} | Answer: {item.get(\"answer\",\"\")[:50]}')
"
```

**Verify per question**:
- [ ] question_number matches the worksheet
- [ ] question_type is correct (MCQ, short_answer, data_analysis, etc.)
- [ ] question_text is the full text (not truncated)
- [ ] command_word extracted (define, describe, explain, etc.)
- [ ] marks correct if shown
- [ ] figure_description present if question has a diagram
- [ ] vocabulary_terms extracted

### Test 2.2: Multiple Geography Worksheets
Run on all worksheets in `seychelles_package/worksheet/geography/` and compare:
- Total questions extracted vs manual count
- Question type distribution
- Any questions missed

### Test 2.3: Math Worksheet
```bash
./venv/bin/python -c "
from apps.dashboard.material_tasks import extract_material_with_vision
items = extract_material_with_vision(
    'seychelles_package/worksheet/mathematics/BIDMAS WORKSHEET_260318_152040.pdf',
    material_type='worksheet',
    subject='Mathematics',
    grade_level='S1'
)
# Math worksheets have different structure: calculations, word problems
# Verify: question types, answer extraction, difficulty detection
"
```

## Test Phase 3: Exam Paper Vision Extraction

### Test 3.1: S3 Geography Exam
```bash
./venv/bin/python -c "
from apps.dashboard.material_tasks import extract_material_with_vision
items = extract_material_with_vision(
    'path/to/S3_Geography_Examination_2021.pdf',
    material_type='question_bank',
    subject='Geography',
    grade_level='S3'
)
for item in items:
    print(f'Q{item.get(\"question_number\",\"?\")}: [{item.get(\"question_type\",\"?\")}] marks={item.get(\"marks\",\"?\")}')
    print(f'  {item.get(\"question_text\",\"\")[:100]}')
    print(f'  Source: {item.get(\"source_description\",\"none\")}')
    print(f'  Command: {item.get(\"command_word\",\"\")}')
"
```

**Verify**:
- [ ] Every question extracted (including sub-parts a, b, c, i, ii)
- [ ] Mark allocations correct (1, 2, 3, 4, 6 marks)
- [ ] Command words captured (State, Describe, Explain, Suggest, With the aid of...)
- [ ] Source-based questions have source_description (map, table, photograph)
- [ ] Section A (map reading) vs Section B (topics) distinguished

### Test 3.2: Compare 2021 vs 2023 Exam
Run on both exam papers. Verify:
- Question format patterns are consistent
- Mark allocation norms are captured
- Different source types identified

## Test Phase 4: Exit Ticket Format Alignment

### Test 4.1: Generate Exit Ticket WITH Worksheet Context
1. Upload S3 worksheets for Development & Trade
2. Process with Rich mode
3. Generate exit ticket for a Development & Trade lesson
4. Compare generated questions against actual worksheet questions:
   - [ ] Same command words used (State, Define, Describe, Explain)
   - [ ] Same question types (MCQ, short answer, source-based, data interpretation)
   - [ ] Similar mark allocations
   - [ ] Seychelles-specific context present

### Test 4.2: Generate Exit Ticket WITHOUT Worksheet Context
Generate for a lesson with no uploaded materials. Compare quality.
The difference shows the value of the material extraction.

## Improvement Criteria

For each material type, extraction quality is measured by:
1. **Completeness**: % of questions/items extracted vs total in document
2. **Accuracy**: Are question types correctly classified?
3. **Richness**: Are marks, command words, figures, answers captured?
4. **Alignment**: Do generated exit tickets match the extracted formats?

Target: >90% completeness, >85% type accuracy for worksheets and exams.

## How to Run Tests

```bash
# All vision extraction tests
./venv/bin/python tests/test_vision_extraction.py

# Quick check on one worksheet
./venv/bin/python -c "
from apps.dashboard.material_tasks import extract_material_with_vision
items = extract_material_with_vision('path/to/file.pdf', 'worksheet', 'Geography', 'S3')
print(f'Extracted {len(items)} items')
for i in items[:5]:
    print(f'  {i.get(\"question_type\",\"?\")}: {i.get(\"question_text\",\"\")[:60]}')
"
```
