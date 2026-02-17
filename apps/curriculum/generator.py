"""
Lesson Content Generator

Pre-generates all lesson content from curriculum:
1. Teaching content (explanations with Seychelles context)
2. Worked examples
3. Practice problems with hints
4. Exit ticket questions

This content is stored in LessonSteps and can be reviewed/edited
by teachers before publishing.
"""

import json
import logging
from typing import Dict, List, Optional
from django.db import transaction

logger = logging.getLogger(__name__)


# ============================================================================
# PROMPTS FOR CONTENT GENERATION
# ============================================================================

LESSON_CONTENT_PROMPT = """You are an expert curriculum designer creating lesson content for Seychelles secondary school students.

LESSON DETAILS:
- Title: {title}
- Objective: {objective}
- Subject: {subject}
- Grade Level: {grade_level}
- Unit: {unit_title}

Generate comprehensive lesson content in the following JSON structure:

{{
    "teaching_sections": [
        {{
            "title": "Introduction",
            "content": "Clear explanation introducing the concept...",
            "key_points": ["Point 1", "Point 2", "Point 3"],
            "seychelles_example": "A real-world example from Seychelles context..."
        }},
        {{
            "title": "Main Concept",
            "content": "Detailed explanation of the core concept...",
            "key_points": ["Point 1", "Point 2"],
            "seychelles_example": "Another local example..."
        }}
    ],
    "worked_examples": [
        {{
            "problem": "A clear problem statement...",
            "steps": [
                "Step 1: First, we...",
                "Step 2: Then, we...",
                "Step 3: Finally, we..."
            ],
            "answer": "The final answer with explanation",
            "seychelles_context": "How this relates to Seychelles..."
        }}
    ],
    "practice_problems": [
        {{
            "question": "Practice question text...",
            "type": "multiple_choice",
            "options": ["A) Option 1", "B) Option 2", "C) Option 3", "D) Option 4"],
            "correct_answer": "B",
            "explanation": "Why B is correct...",
            "hint_1": "First hint - gentle nudge",
            "hint_2": "Second hint - more specific",
            "hint_3": "Third hint - almost gives it away",
            "difficulty": "medium"
        }},
        {{
            "question": "Another practice question...",
            "type": "short_answer",
            "correct_answer": "Expected answer",
            "explanation": "Why this is correct...",
            "hint_1": "First hint",
            "hint_2": "Second hint",
            "hint_3": "Third hint",
            "difficulty": "easy"
        }}
    ],
    "summary": {{
        "key_takeaways": ["Takeaway 1", "Takeaway 2", "Takeaway 3"],
        "connection_to_next": "How this connects to what they'll learn next..."
    }},
    "image_suggestions": [
        {{
            "description": "Diagram showing...",
            "purpose": "Helps visualize...",
            "placement": "After introduction"
        }}
    ]
}}

REQUIREMENTS:
1. Generate 2-3 teaching sections with clear explanations
2. Include at least 1 worked example with step-by-step solution
3. Generate exactly 5 practice problems (mix of easy, medium, hard)
4. Use Seychelles context (places, culture, local examples)
5. Hints should progressively give more help
6. Keep language appropriate for secondary school students

Generate the content now:"""


EXIT_TICKET_PROMPT = """Generate exactly 10 multiple choice questions for an exit assessment on this lesson.

LESSON: {title}
OBJECTIVE: {objective}
SUBJECT: {subject}

Requirements:
1. Questions 1-3: Easy (recall facts)
2. Questions 4-7: Medium (apply concepts)  
3. Questions 8-10: Hard (analyze/evaluate)
4. Each question has exactly 4 options (A, B, C, D)
5. Use Seychelles context where possible
6. Clear, unambiguous questions

Output JSON array:
[
    {{
        "question": "Question text...",
        "option_a": "First option",
        "option_b": "Second option",
        "option_c": "Third option",
        "option_d": "Fourth option",
        "correct": "B",
        "explanation": "Why B is correct...",
        "difficulty": "easy"
    }},
    ...
]

Generate 10 questions now:"""


# ============================================================================
# CONTENT GENERATOR CLASS
# ============================================================================

class LessonContentGenerator:
    """Generates and saves all lesson content to database."""
    
    def __init__(self, llm_client):
        self.llm_client = llm_client
    
    def generate_for_lesson(self, lesson, save_to_db: bool = True) -> Dict:
        """Generate all content for a single lesson."""
        from apps.curriculum.models import LessonStep
        from apps.tutoring.exit_ticket_models import ExitTicket, ExitTicketQuestion
        
        logger.info(f"Generating content for lesson: {lesson.title}")
        
        # Get context
        unit = lesson.unit
        course = unit.course
        
        # Generate main lesson content
        content = self._generate_lesson_content(
            title=lesson.title,
            objective=lesson.objective,
            subject=course.title,
            grade_level=course.grade_level,
            unit_title=unit.title,
        )
        
        if not content:
            logger.error(f"Failed to generate content for lesson {lesson.id}")
            return {'success': False, 'error': 'Content generation failed'}
        
        # Generate exit ticket
        exit_questions = self._generate_exit_ticket(
            title=lesson.title,
            objective=lesson.objective,
            subject=course.title,
        )
        
        if save_to_db:
            with transaction.atomic():
                # Clear existing steps (if regenerating)
                LessonStep.objects.filter(lesson=lesson).delete()
                
                step_index = 0
                
                # Create teaching steps
                for section in content.get('teaching_sections', []):
                    LessonStep.objects.create(
                        lesson=lesson,
                        order_index=step_index,
                        step_type='teach',
                        teacher_script=self._format_teaching_script(section),
                        answer_type='none',
                    )
                    step_index += 1
                
                # Create worked example steps
                for example in content.get('worked_examples', []):
                    LessonStep.objects.create(
                        lesson=lesson,
                        order_index=step_index,
                        step_type='worked_example',
                        teacher_script=self._format_worked_example(example),
                        answer_type='none',
                    )
                    step_index += 1
                
                # Create practice problem steps
                for i, problem in enumerate(content.get('practice_problems', [])):
                    step = LessonStep.objects.create(
                        lesson=lesson,
                        order_index=step_index,
                        step_type='practice',
                        teacher_script=f"Practice Problem {i+1}",
                        question=problem.get('question', ''),
                        answer_type=self._map_answer_type(problem.get('type', 'multiple_choice')),
                        choices=problem.get('options'),
                        expected_answer=problem.get('correct_answer', ''),
                        rubric=problem.get('explanation', ''),
                        hint_1=problem.get('hint_1', ''),
                        hint_2=problem.get('hint_2', ''),
                        hint_3=problem.get('hint_3', ''),
                        max_attempts=3,
                    )
                    step_index += 1
                
                # Create summary step
                summary = content.get('summary', {})
                if summary:
                    LessonStep.objects.create(
                        lesson=lesson,
                        order_index=step_index,
                        step_type='summary',
                        teacher_script=self._format_summary(summary),
                        answer_type='none',
                    )
                
                # Save exit ticket
                if exit_questions:
                    # Delete existing
                    ExitTicket.objects.filter(lesson=lesson).delete()
                    
                    exit_ticket = ExitTicket.objects.create(
                        lesson=lesson,
                        passing_score=8,
                        time_limit_minutes=15,
                        instructions=f"Answer all 10 questions about {lesson.title}. You need 8 correct to pass."
                    )
                    
                    for i, q in enumerate(exit_questions[:10]):
                        ExitTicketQuestion.objects.create(
                            exit_ticket=exit_ticket,
                            question_text=q.get('question', ''),
                            option_a=q.get('option_a', ''),
                            option_b=q.get('option_b', ''),
                            option_c=q.get('option_c', ''),
                            option_d=q.get('option_d', ''),
                            correct_answer=q.get('correct', 'A'),
                            explanation=q.get('explanation', ''),
                            difficulty=q.get('difficulty', 'medium'),
                            order_index=i,
                        )
                
                # Store metadata
                lesson.metadata = {
                    'content_generated': True,
                    'image_suggestions': content.get('image_suggestions', []),
                    'generation_version': '2.0',
                }
                lesson.save()
        
        return {
            'success': True,
            'content': content,
            'exit_questions': exit_questions,
            'steps_created': step_index + 1,
        }
    
    def _generate_lesson_content(self, title: str, objective: str, subject: str, 
                                  grade_level: str, unit_title: str) -> Optional[Dict]:
        """Call LLM to generate lesson content."""
        prompt = LESSON_CONTENT_PROMPT.format(
            title=title,
            objective=objective,
            subject=subject,
            grade_level=grade_level,
            unit_title=unit_title,
        )
        
        try:
            response = self.llm_client.generate(
                messages=[{"role": "user", "content": prompt}],
                system_prompt="You are an expert curriculum designer. Output valid JSON only."
            )
            
            content = response.content.strip()
            
            # Extract JSON
            if '```json' in content:
                content = content.split('```json')[1].split('```')[0]
            elif '```' in content:
                content = content.split('```')[1].split('```')[0]
            
            return json.loads(content)
            
        except Exception as e:
            logger.error(f"Lesson content generation failed: {e}")
            return None
    
    def _generate_exit_ticket(self, title: str, objective: str, subject: str) -> List[Dict]:
        """Call LLM to generate exit ticket questions."""
        prompt = EXIT_TICKET_PROMPT.format(
            title=title,
            objective=objective,
            subject=subject,
        )
        
        try:
            response = self.llm_client.generate(
                messages=[{"role": "user", "content": prompt}],
                system_prompt="You are an expert assessment designer. Output valid JSON array only."
            )
            
            content = response.content.strip()
            
            # Extract JSON array
            start = content.find('[')
            end = content.rfind(']') + 1
            if start != -1 and end > start:
                return json.loads(content[start:end])
            
            return []
            
        except Exception as e:
            logger.error(f"Exit ticket generation failed: {e}")
            return []
    
    def _format_teaching_script(self, section: Dict) -> str:
        """Format a teaching section into a script."""
        script = f"## {section.get('title', 'Lesson')}\n\n"
        script += section.get('content', '') + "\n\n"
        
        key_points = section.get('key_points', [])
        if key_points:
            script += "**Key Points:**\n"
            for point in key_points:
                script += f"• {point}\n"
            script += "\n"
        
        example = section.get('seychelles_example')
        if example:
            script += f"**Example from Seychelles:** {example}\n"
        
        return script
    
    def _format_worked_example(self, example: Dict) -> str:
        """Format a worked example into a script."""
        script = "## Worked Example\n\n"
        script += f"**Problem:** {example.get('problem', '')}\n\n"
        script += "**Solution:**\n"
        
        for i, step in enumerate(example.get('steps', []), 1):
            script += f"{i}. {step}\n"
        
        script += f"\n**Answer:** {example.get('answer', '')}\n"
        
        context = example.get('seychelles_context')
        if context:
            script += f"\n*{context}*\n"
        
        return script
    
    def _format_summary(self, summary: Dict) -> str:
        """Format lesson summary."""
        script = "## Summary\n\n"
        script += "**What we learned today:**\n"
        
        for takeaway in summary.get('key_takeaways', []):
            script += f"✓ {takeaway}\n"
        
        connection = summary.get('connection_to_next')
        if connection:
            script += f"\n**Coming up next:** {connection}\n"
        
        return script
    
    def _map_answer_type(self, type_str: str) -> str:
        """Map question type to LessonStep answer type."""
        mapping = {
            'multiple_choice': 'multiple_choice',
            'short_answer': 'free_text',
            'numeric': 'short_numeric',
            'true_false': 'true_false',
        }
        return mapping.get(type_str, 'multiple_choice')


# ============================================================================
# BATCH GENERATION
# ============================================================================

def generate_content_for_course(course_id: int):
    """Generate content for all lessons in a course."""
    from apps.curriculum.models import Course, Lesson
    from apps.llm.models import ModelConfig
    from apps.llm.client import get_llm_client
    
    course = Course.objects.get(id=course_id)
    
    model_config = ModelConfig.objects.filter(is_active=True).first()
    if not model_config:
        raise ValueError("No active LLM model configured")
    
    llm_client = get_llm_client(model_config)
    generator = LessonContentGenerator(llm_client)
    
    lessons = Lesson.objects.filter(unit__course=course)
    results = []
    
    for lesson in lessons:
        result = generator.generate_for_lesson(lesson, save_to_db=True)
        results.append({
            'lesson_id': lesson.id,
            'lesson_title': lesson.title,
            'success': result.get('success', False),
            'steps_created': result.get('steps_created', 0),
        })
        logger.info(f"Generated content for: {lesson.title} - {result.get('steps_created', 0)} steps")
    
    return results


def generate_content_for_lesson(lesson_id: int):
    """Generate content for a single lesson."""
    from apps.curriculum.models import Lesson
    from apps.llm.models import ModelConfig
    from apps.llm.client import get_llm_client
    
    lesson = Lesson.objects.get(id=lesson_id)
    
    model_config = ModelConfig.objects.filter(is_active=True).first()
    if not model_config:
        raise ValueError("No active LLM model configured")
    
    llm_client = get_llm_client(model_config)
    generator = LessonContentGenerator(llm_client)
    
    return generator.generate_for_lesson(lesson, save_to_db=True)
