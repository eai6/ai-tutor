"""
Skill Extraction Service

Automatically extracts skills from lessons during content generation.
Also handles prerequisite detection and skill linking.

This runs as part of the content generation pipeline to populate
the skills knowledge graph.
"""

import json
import logging
import re
from typing import List, Dict, Optional, Tuple

from django.db import transaction

from apps.curriculum.models import Course, Unit, Lesson, LessonStep
from apps.tutoring.skills_models import Skill, LessonPrerequisite
from apps.llm.models import ModelConfig
from apps.llm.client import get_llm_client

logger = logging.getLogger(__name__)


class SkillExtractionService:
    """
    Extracts skills from lessons using LLM analysis.
    
    For each lesson, this service:
    1. Analyzes the lesson content
    2. Extracts 2-5 atomic skills
    3. Identifies prerequisite skills
    4. Links skills to lessons and steps
    """
    
    EXTRACTION_PROMPT = """Analyze this lesson and extract the key skills/concepts that students will learn.

LESSON INFORMATION:
- Course: {course_title}
- Unit: {unit_title}
- Lesson Title: {lesson_title}
- Lesson Objective: {lesson_objective}

LESSON CONTENT:
{lesson_content}

PREVIOUSLY DEFINED SKILLS IN THIS COURSE:
{existing_skills}

INSTRUCTIONS:
1. Extract 2-5 atomic skills that students will learn from this lesson
2. Each skill should be a specific, measurable ability
3. Use snake_case for skill codes (e.g., "identify_fault_types")
4. Prefix codes with subject abbreviation (e.g., "geo_", "math_", "bio_")
5. Identify prerequisites from the existing skills list
6. Assign appropriate Bloom's taxonomy level
7. Rate difficulty from 0.0 (easy) to 1.0 (hard)

Return ONLY valid JSON in this exact format:
{{
    "skills": [
        {{
            "code": "geo_identify_fault_types",
            "name": "Identify types of geological faults",
            "description": "Distinguish between normal, reverse, and strike-slip faults based on their characteristics",
            "difficulty": 0.6,
            "bloom_level": "understand",
            "importance": 0.8,
            "prerequisites": ["geo_explain_plate_movement", "geo_identify_plate_boundaries"],
            "tags": ["geology", "tectonics", "faults"]
        }}
    ],
    "lesson_prerequisites": ["Previous Lesson Title 1", "Previous Lesson Title 2"]
}}

BLOOM'S TAXONOMY LEVELS: remember, understand, apply, analyze, evaluate, create
"""

    PREREQUISITE_DETECTION_PROMPT = """Analyze these two lessons and determine if the first lesson is a prerequisite for the second.

POTENTIAL PREREQUISITE LESSON:
Title: {prereq_title}
Objective: {prereq_objective}
Skills: {prereq_skills}

TARGET LESSON:
Title: {target_title}
Objective: {target_objective}
Skills: {target_skills}

Does the first lesson teach concepts that are required to understand the second lesson?

Return JSON:
{{
    "is_prerequisite": true/false,
    "strength": 0.0-1.0,
    "reason": "Brief explanation"
}}
"""

    def __init__(self, institution_id: int):
        self.institution_id = institution_id
        self._llm_client = None
    
    @property
    def llm_client(self):
        """Lazy load LLM client."""
        if self._llm_client is None:
            config = ModelConfig.objects.filter(is_active=True).first()
            if config:
                self._llm_client = get_llm_client(config)
        return self._llm_client
    
    def extract_skills_for_lesson(self, lesson: Lesson) -> List[Skill]:
        """
        Extract skills from a lesson and save them to the database.
        
        Args:
            lesson: The lesson to analyze
        
        Returns:
            List of created/updated Skill objects
        """
        if not self.llm_client:
            logger.error("No LLM client available for skill extraction")
            return []
        
        # Get lesson content
        lesson_content = self._get_lesson_content(lesson)
        
        # Get existing skills in this course
        existing_skills = self._get_existing_skills(lesson.unit.course)
        
        # Build prompt
        prompt = self.EXTRACTION_PROMPT.format(
            course_title=lesson.unit.course.title,
            unit_title=lesson.unit.title,
            lesson_title=lesson.title,
            lesson_objective=lesson.objective,
            lesson_content=lesson_content,
            existing_skills=existing_skills,
        )
        
        # Call LLM
        try:
            response = self.llm_client.generate(
                messages=[{"role": "user", "content": prompt}],
                system_prompt="You are an expert curriculum analyst. Extract skills precisely and return valid JSON only."
            )
            
            # Parse response
            result = self._parse_llm_response(response.content)
            
            if not result or 'skills' not in result:
                logger.warning(f"No skills extracted for lesson {lesson.id}")
                return []
            
            # Create skills
            skills = self._create_skills(lesson, result['skills'])
            
            # Create lesson prerequisites
            if 'lesson_prerequisites' in result:
                self._create_lesson_prerequisites(lesson, result['lesson_prerequisites'])
            
            logger.info(f"Extracted {len(skills)} skills for lesson '{lesson.title}'")
            return skills
            
        except Exception as e:
            logger.error(f"Error extracting skills for lesson {lesson.id}: {e}")
            return []
    
    def extract_skills_for_course(self, course: Course) -> Dict:
        """
        Extract skills for all lessons in a course.
        
        Returns:
            Dict with extraction statistics
        """
        stats = {
            'lessons_processed': 0,
            'skills_created': 0,
            'prerequisites_created': 0,
            'errors': [],
        }
        
        lessons = Lesson.objects.filter(
            unit__course=course
        ).order_by('unit__order_index', 'order_index')
        
        for lesson in lessons:
            try:
                skills = self.extract_skills_for_lesson(lesson)
                stats['lessons_processed'] += 1
                stats['skills_created'] += len(skills)
            except Exception as e:
                stats['errors'].append(f"{lesson.title}: {str(e)}")
        
        # After all lessons, detect inter-lesson prerequisites
        prereqs_created = self._detect_lesson_prerequisites(course)
        stats['prerequisites_created'] = prereqs_created
        
        return stats
    
    def _get_lesson_content(self, lesson: Lesson) -> str:
        """Get lesson content as text for analysis."""
        steps = LessonStep.objects.filter(lesson=lesson).order_by('order_index')
        
        content_parts = []
        for step in steps:
            content_parts.append(f"[{step.step_type.upper()}] {step.teacher_script}")
            if step.question:
                content_parts.append(f"Question: {step.question}")
        
        return "\n\n".join(content_parts)
    
    def _get_existing_skills(self, course: Course) -> str:
        """Get existing skills in this course as formatted text."""
        skills = Skill.objects.filter(course=course).order_by('code')
        
        if not skills:
            return "No skills defined yet."
        
        skill_list = []
        for skill in skills:
            skill_list.append(f"- {skill.code}: {skill.name}")
        
        return "\n".join(skill_list)
    
    def _parse_llm_response(self, response: str) -> Optional[Dict]:
        """Parse LLM response to extract JSON."""
        # Try to find JSON in the response
        response = response.strip()
        
        # Remove markdown code blocks if present
        if response.startswith("```"):
            response = re.sub(r'^```\w*\n?', '', response)
            response = re.sub(r'\n?```$', '', response)
        
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            # Try to extract JSON from the response
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except json.JSONDecodeError:
                    pass
        
        logger.warning(f"Could not parse LLM response as JSON: {response[:200]}...")
        return None
    
    @transaction.atomic
    def _create_skills(self, lesson: Lesson, skills_data: List[Dict]) -> List[Skill]:
        """Create Skill objects from extracted data."""
        created_skills = []
        
        for skill_data in skills_data:
            code = skill_data.get('code', '').lower().strip()
            if not code:
                continue
            
            # Get or create skill
            skill, created = Skill.objects.get_or_create(
                institution_id=self.institution_id,
                code=code,
                defaults={
                    'name': skill_data.get('name', code.replace('_', ' ').title()),
                    'description': skill_data.get('description', ''),
                    'course': lesson.unit.course,
                    'unit': lesson.unit,
                    'primary_lesson': lesson,
                    'difficulty_score': skill_data.get('difficulty', 0.5),
                    'importance': skill_data.get('importance', 0.5),
                    'bloom_level': skill_data.get('bloom_level', 'understand'),
                    'tags': skill_data.get('tags', []),
                }
            )
            
            # Update if exists
            if not created:
                skill.name = skill_data.get('name', skill.name)
                skill.description = skill_data.get('description', skill.description)
                skill.difficulty_score = skill_data.get('difficulty', skill.difficulty_score)
                skill.importance = skill_data.get('importance', skill.importance)
                skill.bloom_level = skill_data.get('bloom_level', skill.bloom_level)
                skill.tags = skill_data.get('tags', skill.tags)
                skill.save()
            
            # Add lesson relationship
            skill.lessons.add(lesson)
            
            # Handle prerequisites
            prereq_codes = skill_data.get('prerequisites', [])
            for prereq_code in prereq_codes:
                prereq_skill = Skill.objects.filter(
                    institution_id=self.institution_id,
                    code=prereq_code.lower().strip()
                ).first()
                
                if prereq_skill:
                    skill.prerequisites.add(prereq_skill)
            
            created_skills.append(skill)
        
        return created_skills
    
    def _create_lesson_prerequisites(self, lesson: Lesson, prereq_titles: List[str]):
        """Create LessonPrerequisite relationships."""
        for title in prereq_titles:
            prereq_lesson = Lesson.objects.filter(
                unit__course=lesson.unit.course,
                title__icontains=title
            ).first()
            
            if prereq_lesson and prereq_lesson.id != lesson.id:
                LessonPrerequisite.objects.get_or_create(
                    lesson=lesson,
                    prerequisite=prereq_lesson,
                    defaults={'strength': 1.0, 'is_direct': True}
                )
    
    def _detect_lesson_prerequisites(self, course: Course) -> int:
        """
        Detect prerequisite relationships between lessons based on skills.
        
        A lesson is a prerequisite if it teaches skills that are prerequisites
        for skills in another lesson.
        """
        created = 0
        
        lessons = Lesson.objects.filter(
            unit__course=course
        ).prefetch_related('primary_skills')
        
        for target_lesson in lessons:
            target_skills = target_lesson.primary_skills.all()
            
            for target_skill in target_skills:
                prereq_skills = target_skill.prerequisites.all()
                
                for prereq_skill in prereq_skills:
                    if prereq_skill.primary_lesson and prereq_skill.primary_lesson != target_lesson:
                        _, was_created = LessonPrerequisite.objects.get_or_create(
                            lesson=target_lesson,
                            prerequisite=prereq_skill.primary_lesson,
                            defaults={'strength': 0.8, 'is_direct': True}
                        )
                        if was_created:
                            created += 1
        
        return created


class SkillLinkingService:
    """
    Links skills to lesson steps based on content analysis.
    
    This allows us to know which skills are tested by which questions,
    enabling precise skill assessment.
    """
    
    def link_skills_to_steps(self, lesson: Lesson):
        """
        Link skills to lesson steps based on content matching.
        
        For each practice/quiz step, determines which skill it tests.
        """
        skills = Skill.objects.filter(lessons=lesson)
        steps = LessonStep.objects.filter(
            lesson=lesson,
            step_type__in=['practice', 'quiz']
        )
        
        # Simple keyword matching (can be enhanced with LLM)
        for step in steps:
            step_text = f"{step.teacher_script} {step.question}".lower()
            
            for skill in skills:
                # Check if skill keywords appear in step
                skill_keywords = skill.name.lower().split()
                skill_keywords += skill.description.lower().split()[:10]
                
                matches = sum(1 for kw in skill_keywords if kw in step_text)
                
                if matches >= 2:  # Threshold for matching
                    # This step tests this skill
                    # Add to skills_tested M2M (if we add that field to LessonStep)
                    pass


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def extract_skills_for_new_lesson(lesson: Lesson):
    """
    Utility function to extract skills for a newly generated lesson.
    
    Call this after content generation completes.
    """
    service = SkillExtractionService(lesson.institution.id)
    return service.extract_skills_for_lesson(lesson)


def extract_skills_for_course(course: Course):
    """
    Utility function to extract skills for all lessons in a course.
    
    Useful for batch processing existing content.
    """
    service = SkillExtractionService(course.institution.id)
    return service.extract_skills_for_course(course)


def rebuild_skill_graph(course: Course):
    """
    Rebuild the entire skill graph for a course.
    
    This will:
    1. Clear existing skills
    2. Re-extract all skills
    3. Rebuild prerequisites
    """
    # Delete existing skills for this course
    Skill.objects.filter(course=course).delete()
    LessonPrerequisite.objects.filter(lesson__unit__course=course).delete()
    
    # Re-extract
    return extract_skills_for_course(course)
