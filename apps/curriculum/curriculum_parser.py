"""
Seychelles Curriculum Parser

Extracts structured curriculum data from:
1. Mathematics Curriculum (text/markdown format)
2. Geography Syllabus (requires OCR - images)

This parser does NOT rely on AI for structure extraction.
It uses pattern matching to extract curriculum data directly.
"""

import re
import os
import json
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from django.utils import timezone

logger = logging.getLogger(__name__)


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class ParsedCurriculum:
    """Complete parsed curriculum."""
    subject: str
    grade_level: str
    cycle: str
    description: str
    units: List[Dict]
    teaching_strategies: List[str]
    assessment_methods: List[str]


# ============================================================================
# TEXT EXTRACTION
# ============================================================================

def extract_text_from_file(file_path: str) -> Tuple[str, str]:
    """
    Extract text from curriculum file.
    
    Returns: (text, file_type)
    """
    ext = os.path.splitext(file_path)[1].lower()
    
    if ext in ['.txt', '.md']:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            text = f.read()
        return text, 'text'
    
    elif ext == '.docx':
        return extract_from_docx(file_path), 'docx'
    
    elif ext == '.pdf':
        return extract_from_pdf(file_path), 'pdf'
    
    else:
        # Try reading as text anyway
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
            return text, 'text'
        except:
            raise ValueError(f"Unsupported file type: {ext}")


def extract_from_docx(file_path: str) -> str:
    """Extract text from DOCX file."""
    try:
        from docx import Document
        doc = Document(file_path)
        
        text_parts = []
        for para in doc.paragraphs:
            text_parts.append(para.text)
        
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text for cell in row.cells)
                text_parts.append(row_text)
        
        return "\n".join(text_parts)
    except Exception as e:
        logger.warning(f"python-docx failed: {e}, trying as text")
        # File might already be text (exported from Google Docs)
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()


def extract_from_pdf(file_path: str) -> str:
    """Extract text from PDF file."""
    try:
        import fitz
        doc = fitz.open(file_path)
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        return text
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        return ""


# ============================================================================
# DETECT SUBJECT TYPE
# ============================================================================

def detect_subject(text: str, provided_subject: str = "") -> str:
    """Detect the subject from text content."""
    text_lower = text.lower()
    
    if provided_subject:
        return provided_subject
    
    if 'mathematics' in text_lower or 'algebra' in text_lower or 'arithmetic' in text_lower:
        return 'Mathematics'
    elif 'geography' in text_lower or 'map skills' in text_lower or 'settlement' in text_lower:
        return 'Geography'
    elif 'biology' in text_lower or 'organism' in text_lower:
        return 'Biology'
    elif 'physics' in text_lower or 'mechanics' in text_lower:
        return 'Physics'
    elif 'chemistry' in text_lower or 'elements' in text_lower:
        return 'Chemistry'
    else:
        return 'General'


# ============================================================================
# MATHEMATICS CURRICULUM PARSER
# ============================================================================

def parse_mathematics_curriculum(text: str, grade_level: str = "S1") -> ParsedCurriculum:
    """
    Parse Seychelles Mathematics curriculum text.
    
    The curriculum is organized by:
    - Strands: Number, Measures, Shape & Space, Algebra, Handling Data
    - Each strand has Terminal Objectives
    """
    cycle = "4" if grade_level in ["S1", "S2"] else "5"
    
    # Define the strands
    strands = {
        "NUMBER": {
            "title": "Number",
            "terminal_objectives": [],
        },
        "MEASURES": {
            "title": "Measures", 
            "terminal_objectives": [],
        },
        "SHAPE AND SPACE": {
            "title": "Shape and Space",
            "terminal_objectives": [],
        },
        "ALGEBRA": {
            "title": "Algebra",
            "terminal_objectives": [],
        },
        "HANDLING DATA": {
            "title": "Handling Data",
            "terminal_objectives": [],
        }
    }
    
    # Extract terminal objectives from text
    current_strand = None
    lines = text.split('\n')
    
    for line in lines:
        line_clean = line.strip()
        line_upper = line_clean.upper()
        
        # Detect strand headers
        for strand_key in strands.keys():
            if strand_key in line_upper and len(line_clean) < 80:
                current_strand = strand_key
                break
        
        # Extract objectives (bullet points)
        if current_strand and line_clean.startswith('-'):
            objective = line_clean[1:].strip()
            # Filter out garbage (table borders, short text, etc.)
            if (objective and 
                len(objective) > 20 and 
                not objective.startswith('---') and
                not objective.startswith('===') and
                not all(c in '-=|+' for c in objective.replace(' ', ''))):
                strands[current_strand]["terminal_objectives"].append(objective)
    
    # Build units from strands
    units = []
    for strand_key, strand_data in strands.items():
        if strand_data["terminal_objectives"]:
            units.append({
                "number": len(units) + 1,
                "title": strand_data["title"],
                "duration": "Multiple periods",
                "introduction": f"{strand_data['title']} strand for Cycle {cycle}",
                "terminal_objectives": strand_data["terminal_objectives"],
                "lessons": create_lessons_from_objectives(
                    strand_data["terminal_objectives"],
                    strand_data["title"]
                )
            })
    
    # Teaching strategies
    teaching_strategies = [
        "Worked examples with step-by-step solutions",
        "Mental computation practice",
        "Problem solving in real-world contexts",
        "Group work and collaborative learning",
        "Use of manipulatives and visual aids",
        "Practice exercises with graduated difficulty",
        "Application to Seychelles context (SCR, local measurements)"
    ]
    
    assessment_methods = [
        "Written exercises",
        "Problem-solving tasks",
        "Mental math tests",
        "Practical investigations"
    ]
    
    return ParsedCurriculum(
        subject="Mathematics",
        grade_level=grade_level,
        cycle=cycle,
        description=f"Mathematics curriculum for Seychelles secondary schools (Cycle {cycle})",
        units=units,
        teaching_strategies=teaching_strategies,
        assessment_methods=assessment_methods
    )


def create_lessons_from_objectives(objectives: List[str], unit_title: str) -> List[Dict]:
    """Create lesson structures from terminal objectives."""
    lessons = []
    
    for i, objective in enumerate(objectives):
        # Create lesson title from objective
        title = create_lesson_title(objective)
        
        lessons.append({
            "title": title,
            "objective": objective,
            "enabling_objectives": create_enabling_objectives(objective),
            "teaching_strategies": ["Worked examples", "Practice exercises", "Discussion"],
            "resources": get_resources_for_topic(unit_title),
            "assessment_methods": ["Written exercises", "Oral questioning"],
            "estimated_minutes": 40,
            "order": i + 1
        })
    
    return lessons


def create_lesson_title(objective: str) -> str:
    """Create a student-friendly lesson title from an objective."""
    # Remove common prefixes
    prefixes = [
        "demonstrate the understanding of",
        "demonstrate understanding of",
        "understand and use",
        "use with confidence",
        "appreciate, discuss and express ideas about",
        "work out",
        "apply",
        "solve problems involving",
        "distinguish between",
        "recognise and name",
        "draw and measure",
        "use the",
        "form",
        "determine",
        "construct and solve",
        "develop",
        "know",
        "make",
        "choose",
    ]
    
    title = objective
    obj_lower = objective.lower()
    
    for prefix in prefixes:
        if obj_lower.startswith(prefix):
            title = objective[len(prefix):].strip()
            break
    
    # Capitalize first letter
    if title:
        title = title[0].upper() + title[1:]
    
    # Limit length
    if len(title) > 60:
        title = title[:57] + "..."
    
    return title or objective[:60]


def create_enabling_objectives(terminal_objective: str) -> List[str]:
    """Break a terminal objective into smaller enabling objectives."""
    parts = []
    
    # If objective mentions multiple skills, split them
    if " and " in terminal_objective.lower():
        segments = re.split(r'\s+and\s+', terminal_objective, flags=re.IGNORECASE)
        for seg in segments[:3]:  # Max 3 segments
            seg = seg.strip()
            if seg and len(seg) > 10:
                parts.append(seg)
    
    if not parts:
        parts.append(terminal_objective)
    
    # Add standard enabling objectives
    return parts[:5]  # Limit to 5


def get_resources_for_topic(unit_name: str) -> List[str]:
    """Get appropriate resources for a topic."""
    base = ["Textbook", "Workbook", "Whiteboard"]
    
    extras = {
        "Number": ["Calculator", "Number line"],
        "Measures": ["Rulers", "Measuring tape", "Scales"],
        "Shape and Space": ["Protractor", "Compass", "Graph paper"],
        "Algebra": ["Algebra tiles", "Graphing calculator"],
        "Handling Data": ["Graph paper", "Dice", "Survey forms"],
    }
    
    return base + extras.get(unit_name, [])


# ============================================================================
# LLM-BASED CURRICULUM PARSER (Robust)
# ============================================================================

def parse_curriculum_with_llm(text: str, subject: str, grade_level: str, institution_id: int = None) -> ParsedCurriculum:
    """
    Use LLM to parse curriculum structure from text.
    This is more robust than regex-based parsing.

    If institution_id is provided, queries the knowledge base for teaching
    material context to help align unit/lesson structure with textbooks.
    """
    from apps.llm.models import ModelConfig
    from apps.llm.client import get_llm_client

    # Get LLM client
    model_config = ModelConfig.objects.filter(is_active=True).first()
    if not model_config:
        logger.warning("No LLM configured, falling back to regex parser")
        return parse_generic_curriculum(text, subject, grade_level)

    llm_client = get_llm_client(model_config)

    # Query knowledge base for teaching material context if available
    kb_context_str = ""
    if institution_id:
        try:
            from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
            kb = CurriculumKnowledgeBase(institution_id=institution_id)
            kb_result = kb.query_for_content_generation(
                lesson_title=subject,
                lesson_objective=f"{subject} curriculum structure",
                unit_title="",
                subject=subject,
                grade_level=grade_level,
                n_results=8,
            )
            if kb_result and kb_result.chunks:
                excerpts = "\n\n".join(
                    f"--- From {c.get('metadata', {}).get('material_title', 'teaching material')} ---\n{c.get('content', '')[:400]}"
                    for c in kb_result.chunks[:6]
                    if c.get('content', '').strip()
                )
                if excerpts:
                    kb_context_str = f"""
REFERENCE MATERIAL FROM UPLOADED TEXTBOOKS/TEACHING RESOURCES:
The following excerpts are from textbooks and materials used at this school.
Align unit and lesson names with the terminology and structure used in these materials where appropriate.

{excerpts}
"""
        except Exception as e:
            logger.warning(f"KB query for curriculum parsing failed: {e}")

    # Truncate text if too long (keep first and last parts for context)
    max_chars = 30000
    if len(text) > max_chars:
        # Keep first 20k and last 10k
        text = text[:20000] + "\n\n[...middle section truncated...]\n\n" + text[-10000:]

    cycle = "4" if grade_level in ["S1", "S2"] else "5"

    prompt = f"""Analyze this curriculum document and extract its structure.

DOCUMENT TEXT:
{text}

CONTEXT:
- Subject: {subject}
- Grade Level: {grade_level} (Cycle {cycle})
- This is a Seychelles secondary school curriculum
{kb_context_str}
TASK:
Extract the curriculum structure as JSON with this format:
{{
    "units": [
        {{
            "title": "Unit title",
            "lessons": [
                {{
                    "title": "Lesson title (short, clear name)",
                    "objective": "What students will learn/be able to do"
                }}
            ]
        }}
    ]
}}

GUIDELINES:
1. Look for natural divisions in the curriculum (chapters, units, topics, strands, themes)
2. Each unit should have 3-15 lessons
3. Each lesson should cover ONE main concept or skill
4. Lesson titles should be clear and student-friendly (not "Objective 1.2")
5. If you find terminal objectives or learning outcomes, convert each into a lesson
6. If the document has numbered sections, use those as units
7. Extract as many lessons as you can find - don't skip content

Return ONLY valid JSON, no explanation or markdown."""

    try:
        response = llm_client.generate(
            prompt=prompt,
            system_prompt="You are a curriculum parsing assistant. Extract structured curriculum data from documents. Return only valid JSON.",
            max_tokens=8000,
            temperature=0.1,
        )
        
        # Parse the JSON response
        content = response.get('content', '').strip()
        
        # Clean up common issues
        if content.startswith('```'):
            content = content.split('```')[1]
            if content.startswith('json'):
                content = content[4:]
        content = content.strip()
        
        parsed = json.loads(content)
        
        # Convert to our format
        units = []
        for i, unit_data in enumerate(parsed.get('units', [])):
            lessons = []
            for j, lesson_data in enumerate(unit_data.get('lessons', [])):
                lessons.append({
                    "title": lesson_data.get('title', f'Lesson {j+1}'),
                    "objective": lesson_data.get('objective', ''),
                    "enabling_objectives": [],
                    "teaching_strategies": ["Discussion", "Practice", "Examples"],
                    "resources": ["Textbook", "Whiteboard"],
                    "assessment_methods": ["Written work", "Oral questioning"],
                    "estimated_minutes": 40,
                    "order": j + 1
                })
            
            if lessons:  # Only add units that have lessons
                units.append({
                    "number": i + 1,
                    "title": unit_data.get('title', f'Unit {i+1}'),
                    "duration": "Multiple periods",
                    "introduction": f"{unit_data.get('title', '')} for {subject}",
                    "terminal_objectives": [l['objective'] for l in lessons if l['objective']],
                    "lessons": lessons
                })
        
        return ParsedCurriculum(
            subject=subject,
            grade_level=grade_level,
            cycle=cycle,
            description=f"{subject} curriculum for {grade_level}",
            units=units,
            teaching_strategies=["Discussion", "Practical work", "Group activities", "Problem solving"],
            assessment_methods=["Written tests", "Projects", "Oral questioning", "Practical tasks"]
        )
        
    except json.JSONDecodeError as e:
        logger.error(f"LLM returned invalid JSON: {e}")
        logger.error(f"Response was: {content[:500]}...")
        # Fall back to regex parser
        return parse_generic_curriculum(text, subject, grade_level)
    except Exception as e:
        logger.error(f"LLM parsing failed: {e}")
        return parse_generic_curriculum(text, subject, grade_level)


# ============================================================================
# FALLBACK: REGEX-BASED PARSER
# ============================================================================

def parse_generic_curriculum(text: str, subject: str, grade_level: str) -> ParsedCurriculum:
    """
    Generic curriculum parser using flexible text extraction.
    Handles various document formats.
    """
    cycle = "4" if grade_level in ["S1", "S2"] else "5"
    
    units = []
    current_unit = None
    objectives = []
    
    lines = text.split('\n')
    
    # Patterns that indicate a section/unit header
    def is_header(line):
        line = line.strip()
        if not line or len(line) < 5:
            return False
        # **Bold headers**
        if line.startswith('**') and line.endswith('**'):
            return True
        # ALL CAPS headers
        if line.isupper() and 5 < len(line) < 60:
            return True
        # Numbered sections like "1. Introduction" or "Unit 1:"
        if re.match(r'^(Unit\s+)?\d+[\.\:\)]\s+\w', line, re.IGNORECASE):
            return True
        # Headers ending with colon
        if line.endswith(':') and len(line) < 50 and not line.startswith('-'):
            return True
        # Markdown headers
        if line.startswith('#'):
            return True
        return False
    
    # Patterns that indicate an objective/learning point
    def is_objective(line):
        line = line.strip()
        if not line or len(line) < 10:
            return False
        # Bullet points
        if line.startswith('-') or line.startswith('•') or line.startswith('*'):
            return True
        # Numbered lists
        if re.match(r'^\d+[\.\)]\s+\w', line):
            return True
        # Lettered lists
        if re.match(r'^[a-zA-Z][\.\)]\s+\w', line):
            return True
        return False
    
    def clean_header(line):
        """Clean up header text."""
        line = line.strip()
        line = line.strip('*#:')
        line = re.sub(r'^(Unit\s+)?\d+[\.\:\)]\s*', '', line, flags=re.IGNORECASE)
        return line.strip()
    
    def clean_objective(line):
        """Clean up objective text."""
        line = line.strip()
        # Remove bullet/number prefix
        line = re.sub(r'^[-•*]\s*', '', line)
        line = re.sub(r'^\d+[\.\)]\s*', '', line)
        line = re.sub(r'^[a-zA-Z][\.\)]\s*', '', line)
        return line.strip()
    
    for line in lines:
        line_clean = line.strip()
        
        if is_header(line_clean):
            # Save previous unit if it has objectives
            if current_unit and objectives:
                units.append({
                    "number": len(units) + 1,
                    "title": current_unit,
                    "duration": "Multiple periods",
                    "introduction": f"{current_unit} for {subject}",
                    "terminal_objectives": objectives[:30],  # Limit per unit
                    "lessons": create_lessons_from_objectives(objectives[:30], current_unit)
                })
            
            # Start new unit
            current_unit = clean_header(line_clean)
            objectives = []
        
        elif is_objective(line_clean):
            obj = clean_objective(line_clean)
            # Filter garbage
            if (obj and 
                len(obj) > 10 and 
                not obj.startswith('---') and
                not all(c in '-=|+_' for c in obj.replace(' ', ''))):
                objectives.append(obj)
    
    # Save last unit
    if current_unit and objectives:
        units.append({
            "number": len(units) + 1,
            "title": current_unit,
            "duration": "Multiple periods",
            "introduction": f"{current_unit} for {subject}",
            "terminal_objectives": objectives[:30],
            "lessons": create_lessons_from_objectives(objectives[:30], current_unit)
        })
    
    # If no units found, try to extract ANY content as lessons
    if not units:
        # Look for sentences that could be objectives
        all_objectives = []
        
        for line in lines:
            line_clean = line.strip()
            
            # Skip very short or very long lines
            if len(line_clean) < 15 or len(line_clean) > 300:
                continue
            
            # Skip lines that look like metadata
            if any(skip in line_clean.lower() for skip in 
                   ['page', 'copyright', 'table of contents', 'index', 'chapter']):
                continue
            
            # Check if it looks like an objective (contains action verbs)
            action_verbs = ['understand', 'explain', 'describe', 'identify', 'analyze',
                           'apply', 'evaluate', 'create', 'define', 'list', 'compare',
                           'demonstrate', 'develop', 'recognize', 'use', 'know', 'learn']
            
            line_lower = line_clean.lower()
            if any(verb in line_lower for verb in action_verbs):
                # Clean it up
                obj = clean_objective(line_clean)
                if obj and len(obj) > 15:
                    all_objectives.append(obj)
        
        # Also try bullet points one more time with looser criteria
        if not all_objectives:
            for line in lines:
                line_clean = line.strip()
                if is_objective(line_clean):
                    obj = clean_objective(line_clean)
                    if obj and len(obj) > 10:
                        all_objectives.append(obj)
        
        # Create a single unit if we found anything
        if all_objectives:
            # Remove duplicates while preserving order
            seen = set()
            unique_objectives = []
            for obj in all_objectives:
                if obj.lower() not in seen:
                    seen.add(obj.lower())
                    unique_objectives.append(obj)
            
            units.append({
                "number": 1,
                "title": f"{subject} Fundamentals",
                "duration": "Multiple periods",
                "introduction": f"Core concepts for {subject}",
                "terminal_objectives": unique_objectives[:30],
                "lessons": create_lessons_from_objectives(unique_objectives[:30], subject)
            })
    
    return ParsedCurriculum(
        subject=subject,
        grade_level=grade_level,
        cycle=cycle,
        description=f"{subject} curriculum for {grade_level}",
        units=units,
        teaching_strategies=["Discussion", "Practical work", "Group activities", "Field observation"],
        assessment_methods=["Written tests", "Projects", "Oral questioning", "Practical assessment"]
    )


# ============================================================================
# MAIN PARSING FUNCTION
# ============================================================================

def parse_curriculum_file(file_path: str, subject: str, grade_level: str) -> Dict:
    """
    Main entry point for parsing curriculum files.
    
    This does NOT use AI - it extracts structure directly from text.
    """
    text, file_type = extract_text_from_file(file_path)
    
    if not text or len(text) < 100:
        raise ValueError("Could not extract meaningful text from file")
    
    # Detect subject if not provided
    detected_subject = detect_subject(text, subject)
    
    # Parse based on subject
    if 'math' in detected_subject.lower():
        curriculum = parse_mathematics_curriculum(text, grade_level)
    else:
        curriculum = parse_generic_curriculum(text, detected_subject, grade_level)
    
    return {
        "curriculum": asdict(curriculum),
        "source_file": file_path,
        "extraction_method": file_type,
    }


# ============================================================================
# DATABASE INTEGRATION
# ============================================================================

def create_curriculum_from_structure(structure: Dict, institution, upload=None) -> Dict:
    """
    Create Course, Units, and Lessons from parsed structure.
    """
    from apps.curriculum.models import Course, Unit, Lesson, LessonStep
    
    subject_name = structure.get('subject', 'General')
    grade = structure.get('grade_level', 'S1')
    
    # Create course
    course_name = f"{subject_name} {grade}"
    
    course, created = Course.objects.update_or_create(
        institution=institution,
        title=course_name,
        defaults={
            'grade_level': grade,
            'description': structure.get('description', ''),
            'is_published': False,
        }
    )
    
    if upload:
        upload.created_course = course
        upload.add_log(f"{'Created' if created else 'Updated'} course: {course.title}")
    
    lessons_created = 0
    units_created = 0
    
    # Create Units and Lessons
    for unit_data in structure.get('units', []):
        unit, u_created = Unit.objects.update_or_create(
            course=course,
            title=unit_data.get('title', 'Unnamed Unit'),
            defaults={
                'description': unit_data.get('introduction', ''),
                'order_index': unit_data.get('number', 0),
            }
        )
        
        if u_created:
            units_created += 1
        
        if upload:
            upload.add_log(f"  {'Created' if u_created else 'Updated'} unit: {unit.title}")
        
        # Create lessons
        for lesson_data in unit_data.get('lessons', []):
            lesson_metadata = {
                'enabling_objectives': lesson_data.get('enabling_objectives', []),
                'teaching_strategies': lesson_data.get('teaching_strategies', []),
                'resources': lesson_data.get('resources', []),
                'assessment_methods': lesson_data.get('assessment_methods', []),
            }
            
            lesson, l_created = Lesson.objects.update_or_create(
                unit=unit,
                title=lesson_data.get('title', 'Unnamed Lesson'),
                defaults={
                    'objective': lesson_data.get('objective', ''),
                    'estimated_minutes': lesson_data.get('estimated_minutes', 40),
                    'order_index': lesson_data.get('order', 0),
                    'is_published': False,
                    'metadata': lesson_metadata,
                }
            )
            
            if l_created:
                lessons_created += 1
                
                # Create a basic teach step
                LessonStep.objects.get_or_create(
                    lesson=lesson,
                    order_index=0,
                    defaults={
                        'step_type': 'teach',
                        'teacher_script': f"Today we will learn about: {lesson.objective}",
                    }
                )
                
                if upload:
                    upload.add_log(f"    Created lesson: {lesson.title}")
    
    if upload:
        upload.lessons_created = lessons_created
        upload.save()
    
    return {
        'course_id': course.id,
        'course_name': course.title,
        'units_created': units_created,
        'lessons_created': lessons_created,
    }


# ============================================================================
# MAIN PROCESSING FUNCTION (called by dashboard)
# ============================================================================

def process_curriculum_upload(upload_id: int, skip_review: bool = False) -> Dict:
    """
    Process a curriculum upload with optional teacher review.
    
    Flow:
    1. Extract text from document
    2. Parse curriculum structure
    3. (If not skip_review) Set status to 'review' and wait for approval
    4. Create database records
    
    Args:
        upload_id: ID of the CurriculumUpload record
        skip_review: If True, skip the review step and create records immediately
    """
    from apps.dashboard.models import CurriculumUpload
    
    upload = CurriculumUpload.objects.get(id=upload_id)
    
    try:
        upload.status = 'processing'
        upload.current_step = 1
        upload.processing_log = ""
        upload.add_log("🚀 Starting curriculum processing...")
        upload.save()
        
        # Step 1: Extract text
        upload.add_log("📄 Step 1: Extracting text from document...")
        upload.add_log(f"   File: {upload.file_path}")
        
        text, file_type = extract_text_from_file(upload.file_path)
        upload.extracted_text_length = len(text)
        upload.add_log(f"   ✓ Extracted {len(text):,} characters ({file_type})")
        
        # Show preview of extracted text for debugging
        if text:
            preview = text[:500].replace('\n', ' ')[:200]
            upload.add_log(f"   Preview: {preview}...")
        
        upload.save()
        
        if len(text) < 100:
            raise ValueError("Could not extract meaningful text from file. The document may be scanned images or in an unsupported format.")
        
        # Step 2: Parse curriculum structure
        upload.current_step = 2
        upload.add_log("📚 Step 2: Parsing curriculum structure...")
        upload.save()
        
        detected_subject = detect_subject(text, upload.subject_name)
        upload.add_log(f"   Subject detected: {detected_subject}")
        
        # Try LLM-based parsing first (more robust)
        try:
            upload.add_log("   Using AI to analyze document structure...")
            curriculum = parse_curriculum_with_llm(
                text, detected_subject, upload.grade_level or 'S1',
                institution_id=upload.institution_id,
            )
            upload.add_log("   ✓ AI parsing complete")
        except Exception as e:
            upload.add_log(f"   ⚠️ AI parsing failed: {e}")
            upload.add_log("   Falling back to pattern-based parsing...")
            # Fall back to regex-based parsing
            if 'math' in detected_subject.lower():
                curriculum = parse_mathematics_curriculum(text, upload.grade_level or 'S1')
            else:
                curriculum = parse_generic_curriculum(text, detected_subject, upload.grade_level or 'S1')
        
        structure = asdict(curriculum)
        
        units_count = len(structure.get('units', []))
        lessons_count = sum(len(u.get('lessons', [])) for u in structure.get('units', []))
        
        upload.add_log(f"   ✓ Found {units_count} units with {lessons_count} lessons")
        
        # Log some details about what was found
        for unit in structure.get('units', [])[:3]:  # Show first 3 units
            upload.add_log(f"      📁 {unit.get('title')}: {len(unit.get('lessons', []))} lessons")
        
        upload.parsed_data = structure
        upload.save()
        
        # If no content found, show warning but still allow proceeding
        if lessons_count == 0:
            upload.add_log("⚠️ No lessons extracted. The document format may not be recognized.")
            upload.add_log(f"   Document had {len(text):,} characters of text.")
            upload.add_log("   Try uploading a document with clear sections and bullet points.")
            # Still go to review so teacher can see what happened
            upload.status = 'review'
            upload.add_log("⏸️ Please review - no content was extracted.")
            upload.save()
            
            return {
                'success': True,
                'status': 'review',
                'units_count': 0,
                'lessons_count': 0,
                'message': 'No lessons extracted. Please check document format.',
            }
        
        # If review is required, stop here and wait for teacher approval
        if not skip_review:
            upload.status = 'review'
            upload.add_log("⏸️ Waiting for teacher review...")
            upload.save()
            
            return {
                'success': True,
                'status': 'review',
                'units_count': units_count,
                'lessons_count': lessons_count,
                'message': 'Please review the parsed curriculum structure.',
            }
        
        # Step 3: Create database records
        return complete_curriculum_upload(upload_id)
        
    except Exception as e:
        logger.exception(f"Curriculum processing failed: {e}")
        upload.status = 'failed'
        upload.error_message = str(e)
        upload.add_log(f"❌ Error: {e}")
        upload.save()
        raise


def complete_curriculum_upload(upload_id: int, feedback: str = "") -> Dict:
    """
    Complete the curriculum upload by creating database records.
    Called after teacher approves the parsed structure.
    """
    from apps.dashboard.models import CurriculumUpload
    from django.utils import timezone
    
    upload = CurriculumUpload.objects.get(id=upload_id)
    
    try:
        upload.status = 'processing'
        upload.current_step = 3
        
        if feedback:
            upload.teacher_feedback = feedback
        
        upload.add_log("💾 Step 3: Creating curriculum records...")
        upload.save()
        
        structure = upload.parsed_data
        if not structure:
            raise ValueError("No parsed data available. Please re-process the document.")
        
        # Create database records
        result = create_curriculum_from_structure(
            structure=structure,
            institution=upload.institution,
            upload=upload
        )
        
        upload.units_created = result.get('units_created', 0)
        upload.lessons_created = result.get('lessons_created', 0)
        upload.add_log(f"   ✓ Created {result['units_created']} units, {result['lessons_created']} lessons")
        
        # Mark complete
        upload.status = 'completed'
        upload.completed_at = timezone.now()
        upload.add_log(f"✅ Complete! Course '{result['course_name']}' is ready.")
        upload.save()
        
        return {
            'success': True,
            'status': 'completed',
            'course_id': result['course_id'],
            'course_name': result['course_name'],
            'units_created': result['units_created'],
            'lessons_created': result['lessons_created'],
        }
        
    except Exception as e:
        logger.exception(f"Curriculum completion failed: {e}")
        upload.status = 'failed'
        upload.error_message = str(e)
        upload.add_log(f"❌ Error: {e}")
        upload.save()
        raise