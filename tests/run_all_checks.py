#!/usr/bin/env python
"""
Quick automated verification of all P1 features.
Run: ./venv/bin/python tests/run_all_checks.py
"""
import os, sys, django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
django.setup()

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} — {detail}")


print("\n=== 1. SCHEMA CHECKS ===")

from apps.curriculum.models import Lesson, Unit, LessonStep, SeychellesContext
check("Unit.terminal_objectives field exists", hasattr(Unit, 'terminal_objectives'))
check("Unit.enabling_objectives field exists", hasattr(Unit, 'enabling_objectives'))
check("Lesson.enabling_objectives field exists", hasattr(Lesson, 'enabling_objectives'))
check("Lesson.content_quality field exists", hasattr(Lesson, 'content_quality'))
check("Lesson.teacher_approved field exists", hasattr(Lesson, 'teacher_approved'))
check("LessonStep.enabling_objective field exists", hasattr(LessonStep, 'enabling_objective'))
check("SeychellesContext model exists", SeychellesContext.objects.model is not None)

from apps.tutoring.models import ExitTicketQuestion
check("ExitTicketQuestion.question_type field exists", hasattr(ExitTicketQuestion, 'question_type'))
check("ExitTicketQuestion.answer_data field exists", hasattr(ExitTicketQuestion, 'answer_data'))

from apps.tutoring.skills_models import Skill
check("Skill.enabling_objective_text field exists", hasattr(Skill, 'enabling_objective_text'))
check("Skill.is_enabling_objective field exists", hasattr(Skill, 'is_enabling_objective'))
check("Skill.source_code field exists", hasattr(Skill, 'source_code'))


print("\n=== 2. SEYCHELLES CONTEXT LIBRARY ===")

count = SeychellesContext.objects.filter(is_active=True).count()
check(f"SeychellesContext has entries ({count})", count >= 20, f"Only {count} entries")

categories = set(SeychellesContext.objects.values_list('category', flat=True).distinct())
for cat in ['economic', 'geographic', 'trade', 'climate', 'population']:
    check(f"  Category '{cat}' exists", cat in categories)


print("\n=== 3. COMPETENCY THRESHOLDS ===")

from apps.accounts.models import PlatformConfig
config = PlatformConfig.load()
check("PlatformConfig.threshold_be_max exists", hasattr(config, 'threshold_be_max'))
check(f"  BE threshold = {config.threshold_be_max}%", config.threshold_be_max == 50)
check(f"  AE threshold = {config.threshold_ae_max}%", config.threshold_ae_max == 80)
check(f"  ME threshold = {config.threshold_me_min}%", config.threshold_me_min == 80)
check(f"  EE time = {config.threshold_ee_time_minutes} min", config.threshold_ee_time_minutes == 5)
check(f"  Move-on = {config.threshold_move_on}%", config.threshold_move_on == 70)

# Category assignments
cat = config.categorize_student(30)
check("30% -> BE", cat['code'] == 'BE', f"Got {cat['code']}")
cat = config.categorize_student(60)
check("60% -> AE", cat['code'] == 'AE', f"Got {cat['code']}")
cat = config.categorize_student(90)
check("90% -> ME", cat['code'] == 'ME', f"Got {cat['code']}")
cat = config.categorize_student(100, exit_time_minutes=3)
check("100%+3min -> EE", cat['code'] == 'EE', f"Got {cat['code']}")
cat = config.categorize_student(100, exit_time_minutes=10)
check("100%+10min -> ME (not EE)", cat['code'] == 'ME', f"Got {cat['code']}")


print("\n=== 4. GEOGRAPHY PARSER ===")

try:
    import fitz
    from apps.curriculum.curriculum_parser import parse_geography_curriculum

    doc = fitz.open('seychelles_package/curriculum_materials/geography_document_pdf.pdf')
    text = ''.join(page.get_text() for page in doc)
    result = parse_geography_curriculum(text, grade_level='S1')

    check(f"Geography S1: {len(result.units)} units", len(result.units) >= 5, f"Only {len(result.units)}")
    total_lessons = sum(len(u.get('lessons', [])) for u in result.units)
    check(f"Geography S1: {total_lessons} lessons", total_lessons >= 15, f"Only {total_lessons}")
    total_eos = sum(len(u.get('enabling_objectives', [])) for u in result.units)
    check(f"Geography S1: {total_eos} enabling objectives", total_eos >= 30, f"Only {total_eos}")

    # Check Population unit specifically
    pop_unit = next((u for u in result.units if 'population' in u['title'].lower()), None)
    if pop_unit:
        pop_eos = len(pop_unit.get('enabling_objectives', []))
        check(f"Population unit: {pop_eos} EOs", pop_eos >= 10, f"Only {pop_eos}")
        first_eo = pop_unit['enabling_objectives'][0] if pop_unit['enabling_objectives'] else ''
        check("First EO starts with action verb", first_eo.lower().startswith(('define', 'describe', 'explain', 'list', 'state', 'identify')), f"Got: {first_eo[:50]}")
    else:
        check("Population unit found", False, "Not found")
except FileNotFoundError:
    print("  SKIP  Geography PDF not found")
except Exception as e:
    check("Geography parser runs", False, str(e))


print("\n=== 5. MATH PARSER ===")

try:
    from apps.curriculum.curriculum_parser import parse_mathematics_curriculum

    doc = fitz.open('seychelles_package/curriculum_materials/MATHEMATICS-in-the-National-Curriculum.pdf')
    text = ''.join(page.get_text() for page in doc)
    result = parse_mathematics_curriculum(text, grade_level='S1')

    check(f"Math S1: {len(result.units)} strands", len(result.units) == 5, f"Got {len(result.units)}")
    total_lessons = sum(len(u.get('lessons', [])) for u in result.units)
    check(f"Math S1: {total_lessons} sub-strand lessons", total_lessons >= 10, f"Only {total_lessons}")

    strand_names = [u['title'] for u in result.units]
    for expected in ['Number', 'Algebra', 'Shape', 'Measures', 'Handling Data']:
        found = any(expected.lower() in s.lower() for s in strand_names)
        check(f"  Strand '{expected}' found", found)
except FileNotFoundError:
    print("  SKIP  Math PDF not found")
except Exception as e:
    check("Math parser runs", False, str(e))


print("\n=== 6. SUBJECT DETECTION ===")

from apps.curriculum.curriculum_parser import detect_subject
check("Math detection", detect_subject('mathematics algebra fractions') == 'Mathematics')
check("Geography detection", detect_subject('geography map skills population settlement') == 'Geography')
check("Provided subject override", detect_subject('random text', provided_subject='Chemistry') == 'Chemistry')
check("Unknown -> General", detect_subject('some random document') == 'General')


print("\n=== 7. CONTENT QUALITY TIERS ===")

from apps.curriculum.content_generator import LessonContentGenerator
gen = LessonContentGenerator.__new__(LessonContentGenerator)

t1 = gen._determine_content_quality({'related_content': ['text'], 'figure_descriptions': [{'d': 'fig'}], 'objectives': ['obj']})
check("Tier 1 detection (KB content + figures)", t1 == 'tier_1', f"Got {t1}")

t3 = gen._determine_content_quality({'related_content': [], 'objectives': ['obj']})
check("Tier 3 detection (objectives only)", t3 == 'tier_3', f"Got {t3}")

t4 = gen._determine_content_quality({'related_content': [], 'objectives': []})
check("Tier 4 detection (no context)", t4 == 'tier_4', f"Got {t4}")


print("\n=== 8. EO SKILL EXTRACTION ===")

from apps.tutoring.skill_extraction import SkillExtractionService
service = SkillExtractionService.__new__(SkillExtractionService)

bloom_tests = [
    ('Define population density', 'remember'),
    ('Explain the factors', 'understand'),
    ('List the main areas', 'remember'),
    ('Calculate the area', 'apply'),
    ('Evaluate the impact', 'evaluate'),
    ('Describe the trend', 'understand'),
    ('Identify the features', 'remember'),
]
for obj, expected in bloom_tests:
    actual = service._infer_bloom_level(obj)
    check(f"Bloom: '{obj[:30]}' -> {expected}", actual == expected, f"Got {actual}")


print("\n=== 9. EXIT TICKET GRADING ===")

from unittest.mock import MagicMock
from apps.tutoring.conversational_tutor import ConversationalTutor

tutor = ConversationalTutor.__new__(ConversationalTutor)

# MCQ
q = MagicMock(question_type='mcq', correct_answer='B')
check("MCQ correct", tutor._grade_exit_question(q, 'B'))
check("MCQ wrong", not tutor._grade_exit_question(q, 'C'))
check("MCQ case-insensitive", tutor._grade_exit_question(q, 'b'))

# Fill-in-blank
q = MagicMock(question_type='fill_in_blank', answer_data={'blanks': ['GNP'], 'accept_alternatives': [['gross national product']]})
check("FIB correct", tutor._grade_exit_question(q, ['GNP']))
check("FIB alternative", tutor._grade_exit_question(q, ['gross national product']))
check("FIB wrong", not tutor._grade_exit_question(q, ['GDP']))

# Matching
q = MagicMock(question_type='matching', answer_data={'pairs': [{'left': 'A', 'right': '1'}]})
check("Match correct", tutor._grade_exit_question(q, {'A': '1'}))
check("Match wrong", not tutor._grade_exit_question(q, {'A': '2'}))

# Short answer
q = MagicMock(question_type='short_answer', answer_data={'keywords': ['health', 'education'], 'min_keywords': 2})
check("Short answer correct", tutor._grade_exit_question(q, 'includes health and education'))
check("Short answer wrong", not tutor._grade_exit_question(q, 'it is a number'))


print("\n=== 10. ARTIFACT PARSING ===")

tutor2 = ConversationalTutor.__new__(ConversationalTutor)
text = "Here is a table: |||ARTIFACT:html|||<table><tr><td>Data</td></tr></table>|||/ARTIFACT||| And more text."
clean, html = tutor2._parse_artifact_signal(text)
check("Artifact parsed from text", html is not None)
check("Artifact HTML extracted", '<table>' in (html or ''))
check("Text cleaned of artifact signal", '|||ARTIFACT' not in clean)

# Sanitization
dirty = '<script>alert("xss")</script><table><tr><td>safe</td></tr></table>'
safe = tutor2._sanitize_artifact_html(dirty)
check("Script tags stripped", '<script>' not in safe)
check("Table preserved", '<table>' in safe)


print(f"\n{'='*50}")
print(f"RESULTS: {PASS} passed, {FAIL} failed out of {PASS+FAIL} checks")
if FAIL == 0:
    print("ALL CHECKS PASSED")
else:
    print(f"FIX {FAIL} FAILING CHECK(S)")
print()
