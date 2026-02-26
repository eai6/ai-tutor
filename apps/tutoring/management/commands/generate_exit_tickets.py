"""
Generate standardized exit tickets for lessons.

Usage:
    python manage.py generate_exit_tickets --lesson 5
    python manage.py generate_exit_tickets --course "Geography"
    python manage.py generate_exit_tickets --all
"""

import json
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.curriculum.models import Lesson, Course
from apps.tutoring.models import ExitTicket, ExitTicketQuestion
from apps.llm.client import get_llm_client
from apps.llm.models import ModelConfig


EXIT_TICKET_PROMPT = """Generate exactly 10 multiple choice questions for a summative assessment (exit ticket) on this lesson.

LESSON: {lesson_title}
OBJECTIVE: {lesson_objective}
SUBJECT: {subject}
{exam_context}
REQUIREMENTS:
1. Generate EXACTLY 10 questions
2. Questions should progress from easy (recall) to hard (analysis)
3. Each question must have exactly 4 options (A, B, C, D)
4. Include one correct answer per question
5. Questions should directly assess the lesson objective
6. Use context relevant to Seychelles secondary school students

OUTPUT FORMAT (JSON array):
[
    {{
        "question": "What is...?",
        "option_a": "First option",
        "option_b": "Second option", 
        "option_c": "Third option",
        "option_d": "Fourth option",
        "correct": "B",
        "explanation": "B is correct because...",
        "difficulty": "easy"
    }},
    ...
]

DIFFICULTY DISTRIBUTION:
- Questions 1-3: easy (recall facts)
- Questions 4-7: medium (apply concepts)
- Questions 8-10: hard (analyze/evaluate)

Generate the 10 questions now:"""


class Command(BaseCommand):
    help = 'Generate standardized exit tickets for lessons using AI'

    def add_arguments(self, parser):
        parser.add_argument(
            '--lesson',
            type=int,
            help='Generate exit ticket for a specific lesson ID',
        )
        parser.add_argument(
            '--course',
            type=str,
            help='Generate exit tickets for all lessons in a course',
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Generate exit tickets for all lessons without one',
        )
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='Overwrite existing exit tickets',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be generated without saving',
        )

    def handle(self, *args, **options):
        # Get LLM client - use first active model config
        model_config = ModelConfig.get_for('tutoring')
        if not model_config:
            raise CommandError("No active model config found. Create one in admin.")
        
        llm_client = get_llm_client(model_config)
        
        # Determine which lessons to process
        if options['lesson']:
            lessons = Lesson.objects.filter(id=options['lesson'])
            if not lessons.exists():
                raise CommandError(f"Lesson {options['lesson']} not found")
        elif options['course']:
            lessons = Lesson.objects.filter(
                unit__course__title__icontains=options['course'],
                is_published=True
            )
        elif options['all']:
            # All published lessons without exit tickets
            existing_ids = ExitTicket.objects.values_list('lesson_id', flat=True)
            lessons = Lesson.objects.filter(is_published=True).exclude(id__in=existing_ids)
        else:
            raise CommandError("Specify --lesson, --course, or --all")
        
        self.stdout.write(f"Found {lessons.count()} lessons to process")
        
        for lesson in lessons:
            self.stdout.write(f"\nProcessing: {lesson.title}")
            
            # Check if exit ticket exists
            existing = ExitTicket.objects.filter(lesson=lesson).first()
            if existing and not options['overwrite']:
                self.stdout.write(self.style.WARNING(f"  ⏭️  Skipped (already has exit ticket)"))
                continue
            
            try:
                questions = self._generate_questions(llm_client, lesson)
                
                if options['dry_run']:
                    self.stdout.write(self.style.SUCCESS(f"  ✓ Would generate {len(questions)} questions"))
                    for i, q in enumerate(questions[:3]):
                        self.stdout.write(f"    Q{i+1}: {q['question'][:60]}...")
                    continue
                
                # Save to database
                with transaction.atomic():
                    # Delete existing if overwriting
                    if existing:
                        existing.delete()
                    
                    # Create exit ticket
                    exit_ticket = ExitTicket.objects.create(
                        lesson=lesson,
                        passing_score=8,
                        time_limit_minutes=15,
                        instructions=f"Answer all 10 questions about {lesson.title}. You need 8 correct to pass."
                    )
                    
                    # Create questions
                    for i, q in enumerate(questions):
                        ExitTicketQuestion.objects.create(
                            exit_ticket=exit_ticket,
                            question_text=q['question'],
                            option_a=q['option_a'],
                            option_b=q['option_b'],
                            option_c=q['option_c'],
                            option_d=q['option_d'],
                            correct_answer=q['correct'],
                            explanation=q.get('explanation', ''),
                            difficulty=q.get('difficulty', 'medium'),
                            order_index=i,
                        )
                    
                    self.stdout.write(self.style.SUCCESS(f"  ✓ Created exit ticket with {len(questions)} questions"))
                    
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  ✗ Error: {e}"))
        
        self.stdout.write(self.style.SUCCESS("\nDone!"))

    def _generate_questions(self, llm_client, lesson) -> list:
        """Generate 10 MCQ questions using AI, grounded in real exam questions when available."""
        subject = lesson.unit.course.title if lesson.unit and lesson.unit.course else "General"

        # Query KB for real exam questions to ground the generation
        exam_context = ""
        try:
            from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
            course = lesson.unit.course
            kb = CurriculumKnowledgeBase(institution_id=course.institution_id)
            exam_questions = kb.query_for_exit_ticket_generation(
                lesson_title=lesson.title,
                lesson_objective=lesson.objective or '',
                subject=subject,
                grade_level=course.grade_level or '',
                n_results=5,
            )
            exam_context = kb.format_exam_questions_for_prompt(exam_questions)
            if exam_context:
                exam_context = "\n\n" + exam_context + "\n"
        except Exception as e:
            self.stderr.write(f"  KB query failed (continuing without): {e}")

        prompt = EXIT_TICKET_PROMPT.format(
            lesson_title=lesson.title,
            lesson_objective=lesson.objective,
            subject=subject,
            exam_context=exam_context,
        )
        
        from apps.llm.prompts import get_prompt_or_default
        institution_id = lesson.unit.course.institution_id if lesson.unit and lesson.unit.course else None
        exit_sys_prompt = get_prompt_or_default(
            institution_id, 'exit_ticket_prompt',
            "You are an expert educational assessment designer.",
            json_required=True,
        )
        messages = [{"role": "user", "content": prompt}]
        response = llm_client.generate(messages, system_prompt=exit_sys_prompt)
        
        # Parse JSON from response
        content = response.content
        
        # Try to extract JSON array
        try:
            # Find JSON array in response
            start = content.find('[')
            end = content.rfind(']') + 1
            if start != -1 and end > start:
                json_str = content[start:end]
                questions = json.loads(json_str)
                
                if len(questions) < 10:
                    raise ValueError(f"Only {len(questions)} questions generated, need 10")
                
                return questions[:10]  # Take first 10
            else:
                raise ValueError("No JSON array found in response")
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse questions: {e}")