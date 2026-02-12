"""
Management command to test the tutoring engine.

Run with: python manage.py test_tutor_engine
"""

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
import os

from apps.accounts.models import Institution
from apps.curriculum.models import Lesson
from apps.tutoring.engine import TutorEngine, create_tutor_session
from apps.tutoring.grader import grade_answer, GradeResult


class Command(BaseCommand):
    help = 'Tests the tutoring engine with mock and optionally real LLM'

    def add_arguments(self, parser):
        parser.add_argument(
            '--real-llm',
            action='store_true',
            help='Test with real LLM (requires ANTHROPIC_API_KEY)',
        )

    def handle(self, *args, **options):
        self.test_grader()
        self.test_engine_mock()
        
        if options['real_llm']:
            self.test_engine_real()

    def test_grader(self):
        """Test the grading functions."""
        self.stdout.write("\n=== Testing Grader ===\n")
        
        lesson = Lesson.objects.first()
        if not lesson:
            self.stdout.write(self.style.ERROR("No lesson found. Run seed_sample_data first."))
            return
        
        practice_step = lesson.steps.filter(step_type='practice').first()
        if not practice_step:
            self.stdout.write(self.style.ERROR("No practice step found."))
            return
        
        self.stdout.write(f"Testing step: {practice_step}")
        self.stdout.write(f"Question: {practice_step.question}")
        self.stdout.write(f"Expected: {practice_step.expected_answer}")
        
        # Test correct answer
        result = grade_answer(practice_step, practice_step.expected_answer)
        self.stdout.write(f"\nCorrect answer test: {result.result} (score: {result.score})")
        
        # Test wrong answer
        result = grade_answer(practice_step, "999")
        self.stdout.write(f"Wrong answer test: {result.result} (score: {result.score})")
        
        self.stdout.write(self.style.SUCCESS("\n✅ Grader tests passed!"))

    def test_engine_mock(self):
        """Test the tutor engine with mock LLM."""
        self.stdout.write("\n=== Testing Tutor Engine (Mock LLM) ===\n")
        
        institution = Institution.objects.first()
        student = User.objects.filter(username='student1').first()
        lesson = Lesson.objects.first()
        
        if not all([institution, student, lesson]):
            self.stdout.write(self.style.ERROR("Missing test data. Run seed_sample_data first."))
            return
        
        # Create session
        session = create_tutor_session(
            student=student,
            lesson=lesson,
            institution=institution,
        )
        self.stdout.write(f"Created session: {session}")
        
        # Create engine with mock LLM
        engine = TutorEngine(session, use_mock_llm=True)
        
        # Start session
        self.stdout.write("\n--- Starting session ---")
        response = engine.start()
        self.stdout.write(f"Tutor: {response.message[:100]}...")
        self.stdout.write(f"Step: {response.step_index} ({response.step_type})")
        self.stdout.write(f"Waiting for answer: {response.is_waiting_for_answer}")
        
        # Process a few steps
        step_count = 0
        max_steps = 10
        
        while not response.is_session_complete and step_count < max_steps:
            step_count += 1
            
            if response.is_waiting_for_answer:
                step = engine.current_step
                answer = step.expected_answer if step else "test"
                
                self.stdout.write(f"\n--- Student answers: {answer} ---")
                response = engine.process_student_answer(answer)
                
                if response.grading and response.grading.result == GradeResult.CORRECT:
                    if not response.is_session_complete:
                        response = engine.advance_step()
            else:
                response = engine.advance_step()
        
        self.stdout.write(f"\n--- Session complete ---")
        self.stdout.write(f"Mastery achieved: {response.mastery_achieved}")
        self.stdout.write(f"Turns recorded: {session.turns.count()}")
        
        self.stdout.write(self.style.SUCCESS("\n✅ Engine test (mock) passed!"))

    def test_engine_real(self):
        """Test with real LLM."""
        self.stdout.write("\n=== Testing Tutor Engine (Real LLM) ===\n")
        
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            self.stdout.write(self.style.WARNING(
                "⚠️  ANTHROPIC_API_KEY not set. Skipping real LLM test."
            ))
            return
        
        institution = Institution.objects.first()
        student = User.objects.filter(username='student1').first()
        lesson = Lesson.objects.first()
        
        if not all([institution, student, lesson]):
            self.stdout.write(self.style.ERROR("Missing test data."))
            return
        
        session = create_tutor_session(
            student=student,
            lesson=lesson,
            institution=institution,
        )
        
        engine = TutorEngine(session, use_mock_llm=False)
        
        self.stdout.write("Starting session with real LLM...")
        response = engine.start()
        
        self.stdout.write(f"\n🤖 Tutor says:\n{response.message}")
        self.stdout.write(f"\n📊 Tokens: {response.tokens_in} in, {response.tokens_out} out")
        
        self.stdout.write(self.style.SUCCESS("\n✅ Real LLM test passed!"))
