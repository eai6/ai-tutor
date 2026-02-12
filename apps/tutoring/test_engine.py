"""
Test script for the tutoring engine.

Run with: python manage.py shell < apps/tutoring/test_engine.py
Or: python manage.py test_tutor_engine
"""

import os
import django

# Setup Django if running standalone
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.contrib.auth.models import User
from apps.accounts.models import Institution
from apps.curriculum.models import Lesson
from apps.llm.models import PromptPack, ModelConfig
from apps.tutoring.engine import TutorEngine, create_tutor_session
from apps.tutoring.grader import grade_answer, GradeResult


def test_grader():
    """Test the grading functions."""
    print("\n=== Testing Grader ===\n")
    
    # Get a practice step
    lesson = Lesson.objects.first()
    if not lesson:
        print("No lesson found. Run seed_sample_data first.")
        return
    
    practice_step = lesson.steps.filter(step_type='practice').first()
    if not practice_step:
        print("No practice step found.")
        return
    
    print(f"Testing step: {practice_step}")
    print(f"Question: {practice_step.question}")
    print(f"Expected: {practice_step.expected_answer}")
    
    # Test correct answer
    result = grade_answer(practice_step, practice_step.expected_answer)
    print(f"\nCorrect answer test: {result.result} (score: {result.score})")
    assert result.result == GradeResult.CORRECT, "Should be correct!"
    
    # Test wrong answer
    result = grade_answer(practice_step, "999")
    print(f"Wrong answer test: {result.result} (score: {result.score})")
    assert result.result == GradeResult.INCORRECT, "Should be incorrect!"
    
    # Test numeric tolerance
    if practice_step.expected_answer.isdigit():
        result = grade_answer(practice_step, practice_step.expected_answer + ".0")
        print(f"Numeric format test: {result.result}")
        assert result.result == GradeResult.CORRECT, "Should accept '55.0' for '55'!"
    
    print("\n✅ Grader tests passed!")


def test_engine_with_mock():
    """Test the tutor engine with mock LLM."""
    print("\n=== Testing Tutor Engine (Mock LLM) ===\n")
    
    # Get test data
    institution = Institution.objects.first()
    student = User.objects.filter(username='student1').first()
    lesson = Lesson.objects.first()
    
    if not all([institution, student, lesson]):
        print("Missing test data. Run seed_sample_data first.")
        return
    
    # Create session
    session = create_tutor_session(
        student=student,
        lesson=lesson,
        institution=institution,
    )
    print(f"Created session: {session}")
    
    # Create engine with mock LLM
    engine = TutorEngine(session, use_mock_llm=True)
    
    # Start session
    print("\n--- Starting session ---")
    response = engine.start()
    print(f"Tutor: {response.message[:100]}...")
    print(f"Step: {response.step_index} ({response.step_type})")
    print(f"Waiting for answer: {response.is_waiting_for_answer}")
    
    # Advance through steps
    step_count = 0
    max_steps = 10  # Safety limit
    
    while not response.is_session_complete and step_count < max_steps:
        step_count += 1
        
        if response.is_waiting_for_answer:
            # Get the expected answer for testing
            step = engine.current_step
            answer = step.expected_answer if step else "test"
            
            print(f"\n--- Student answers: {answer} ---")
            response = engine.process_student_answer(answer)
            print(f"Tutor: {response.message[:100]}...")
            print(f"Grading: {response.grading.result if response.grading else 'N/A'}")
            
            # If correct, advance
            if response.grading and response.grading.result == GradeResult.CORRECT:
                if not response.is_session_complete:
                    print("\n--- Advancing to next step ---")
                    response = engine.advance_step()
                    print(f"Tutor: {response.message[:100]}...")
                    print(f"Step: {response.step_index} ({response.step_type})")
        else:
            # No answer needed, advance
            print("\n--- Advancing (no answer needed) ---")
            response = engine.advance_step()
            print(f"Tutor: {response.message[:100]}...")
            print(f"Step: {response.step_index} ({response.step_type})")
    
    print(f"\n--- Session complete ---")
    print(f"Mastery achieved: {response.mastery_achieved}")
    print(f"Total steps processed: {step_count}")
    
    # Check session state
    session.refresh_from_db()
    print(f"Session status: {session.status}")
    print(f"Turns recorded: {session.turns.count()}")
    
    print("\n✅ Engine test passed!")


def test_engine_with_real_llm():
    """Test with real LLM (requires API key)."""
    print("\n=== Testing Tutor Engine (Real LLM) ===\n")
    
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        print("⚠️  ANTHROPIC_API_KEY not set. Skipping real LLM test.")
        print("   Set the environment variable to test with real API.")
        return
    
    # Get test data
    institution = Institution.objects.first()
    student = User.objects.filter(username='student1').first()
    lesson = Lesson.objects.first()
    
    if not all([institution, student, lesson]):
        print("Missing test data. Run seed_sample_data first.")
        return
    
    # Create session
    session = create_tutor_session(
        student=student,
        lesson=lesson,
        institution=institution,
    )
    
    # Create engine with real LLM
    engine = TutorEngine(session, use_mock_llm=False)
    
    # Start session
    print("Starting session with real LLM...")
    response = engine.start()
    
    print(f"\n🤖 Tutor says:\n{response.message}")
    print(f"\n📊 Tokens used: {response.tokens_in} in, {response.tokens_out} out")
    
    print("\n✅ Real LLM test passed!")


if __name__ == "__main__":
    test_grader()
    test_engine_with_mock()
    test_engine_with_real_llm()
