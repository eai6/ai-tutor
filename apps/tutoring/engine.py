"""
Step-Based Tutor Engine

A predictable, curriculum-driven tutoring engine that:
1. Follows pre-generated LessonSteps exactly
2. Presents content from the database
3. Evaluates answers against expected answers
4. Provides hints from the hint ladder
5. Tracks progress through steps

This replaces the conversational engine for production use.
"""

import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from enum import Enum

from django.utils import timezone

from apps.curriculum.models import Lesson, LessonStep
from apps.tutoring.models import (
    TutorSession, SessionTurn, StudentLessonProgress,
    ExitTicket, ExitTicketQuestion
)
from apps.tutoring.grader import grade_answer, GradeResult, GradingOutcome

logger = logging.getLogger(__name__)


class SessionPhase(Enum):
    """Phases of a tutoring session."""
    LESSON = "lesson"           # Going through lesson steps
    EXIT_TICKET = "exit_ticket" # Taking the exit assessment
    COMPLETED = "completed"     # Session finished


@dataclass
class TutorResponse:
    """Response from the tutor engine."""
    message: str
    step_index: int
    step_type: str
    phase: str
    
    # Question data for frontend
    question: Optional[Dict] = None
    
    # State flags
    is_waiting_for_answer: bool = False
    is_session_complete: bool = False
    mastery_achieved: bool = False
    
    # Grading info
    grading: Optional[GradingOutcome] = None
    attempts_remaining: Optional[int] = None
    hint: Optional[str] = None
    
    # For frontend rendering
    commands: Optional[List[Dict]] = None
    
    # Token usage
    tokens_in: int = 0
    tokens_out: int = 0


class TutorEngine:
    """
    Curriculum-driven tutoring engine.
    
    Follows the exact sequence of LessonSteps stored in the database.
    No AI generation during sessions - all content is pre-generated.
    """
    
    PASSING_SCORE = 8  # Out of 10 for exit ticket
    
    def __init__(self, session: TutorSession, llm_client=None):
        self.session = session
        self.lesson = session.lesson
        self.llm_client = llm_client  # Only used for free-text grading
        
        # Load lesson steps
        self.steps = list(
            LessonStep.objects.filter(lesson=self.lesson).order_by('order_index')
        )
        
        # Load exit ticket
        self.exit_ticket = ExitTicket.objects.filter(lesson=self.lesson).first()
        self.exit_questions = list(
            ExitTicketQuestion.objects.filter(exit_ticket=self.exit_ticket).order_by('order_index')
        ) if self.exit_ticket else []
        
        # Load or initialize state
        self._load_state()
    
    def _load_state(self):
        """Load session state from database."""
        state = self.session.engine_state or {}
        
        self.phase = SessionPhase(state.get('phase', 'lesson'))
        self.current_step_index = state.get('step_index', 0)
        self.current_attempt = state.get('current_attempt', 0)
        self.hints_given = state.get('hints_given', 0)
        
        # Exit ticket state
        self.exit_question_index = state.get('exit_question_index', 0)
        self.exit_correct_count = state.get('exit_correct_count', 0)
        self.exit_answers = state.get('exit_answers', [])
    
    def _save_state(self):
        """Save session state to database."""
        self.session.engine_state = {
            'phase': self.phase.value,
            'step_index': self.current_step_index,
            'current_attempt': self.current_attempt,
            'hints_given': self.hints_given,
            'exit_question_index': self.exit_question_index,
            'exit_correct_count': self.exit_correct_count,
            'exit_answers': self.exit_answers,
        }
        self.session.current_step_index = self.current_step_index
        self.session.save()
    
    @property
    def current_step(self) -> Optional[LessonStep]:
        """Get current lesson step."""
        if 0 <= self.current_step_index < len(self.steps):
            return self.steps[self.current_step_index]
        return None
    
    @property
    def current_exit_question(self) -> Optional[ExitTicketQuestion]:
        """Get current exit ticket question."""
        if 0 <= self.exit_question_index < len(self.exit_questions):
            return self.exit_questions[self.exit_question_index]
        return None
    
    # ========================================================================
    # PUBLIC API
    # ========================================================================
    
    def start(self) -> TutorResponse:
        """Start the tutoring session."""
        if not self.steps:
            return TutorResponse(
                message="This lesson has no content yet. Please check back later.",
                step_index=0,
                step_type="error",
                phase="error",
                is_session_complete=True,
            )
        
        # Present first step
        return self._present_current_step(is_start=True)
    
    def resume(self) -> TutorResponse:
        """Resume an existing session."""
        if self.phase == SessionPhase.COMPLETED:
            return TutorResponse(
                message="You've already completed this lesson!",
                step_index=self.current_step_index,
                step_type="completed",
                phase="completed",
                is_session_complete=True,
                mastery_achieved=self.session.mastery_achieved,
            )
        
        if self.phase == SessionPhase.EXIT_TICKET:
            return self._present_exit_question(is_resume=True)
        
        return self._present_current_step(is_resume=True)
    
    def process_answer(self, answer: str) -> TutorResponse:
        """Process student's answer."""
        if self.phase == SessionPhase.EXIT_TICKET:
            return self._process_exit_answer(answer)
        
        return self._process_step_answer(answer)
    
    def advance(self) -> TutorResponse:
        """Advance to next step (for non-question steps)."""
        if self.phase == SessionPhase.EXIT_TICKET:
            return self._present_exit_question()
        
        step = self.current_step
        if step and step.requires_response():
            return TutorResponse(
                message="Please answer the question first.",
                step_index=self.current_step_index,
                step_type=step.step_type,
                phase=self.phase.value,
                is_waiting_for_answer=True,
            )
        
        return self._advance_to_next_step()
    
    # ========================================================================
    # LESSON STEP HANDLING
    # ========================================================================
    
    def _present_current_step(self, is_start: bool = False, is_resume: bool = False) -> TutorResponse:
        """Present the current lesson step."""
        step = self.current_step
        
        if not step:
            # No more steps, move to exit ticket
            return self._start_exit_ticket()
        
        # Build message
        if is_start:
            message = f"Welcome to **{self.lesson.title}**! Let's begin.\n\n"
        elif is_resume:
            message = f"Welcome back! Let's continue with **{self.lesson.title}**.\n\n"
        else:
            message = ""
        
        # Add step content
        message += step.teacher_script
        
        # Build question data if applicable
        question_data = None
        commands = []
        
        if step.requires_response():
            question_data = self._build_question_data(step)
            commands.append({'type': 'show_question', 'data': question_data})
            
            if step.question:
                message += f"\n\n**Question:** {step.question}"
        
        # Save turn
        self._save_turn(
            role=SessionTurn.Role.TUTOR,
            content=message,
            step=step,
        )
        
        self._save_state()
        
        return TutorResponse(
            message=message,
            step_index=self.current_step_index,
            step_type=step.step_type,
            phase=self.phase.value,
            question=question_data,
            is_waiting_for_answer=step.requires_response(),
            commands=commands,
        )
    
    def _process_step_answer(self, answer: str) -> TutorResponse:
        """Process answer for a lesson step."""
        step = self.current_step
        
        if not step or not step.requires_response():
            return self._advance_to_next_step()
        
        # Save student answer
        self._save_turn(
            role=SessionTurn.Role.STUDENT,
            content=answer,
            step=step,
            metadata={'attempt': self.current_attempt + 1},
        )
        
        # Grade the answer
        grading = self._grade_step_answer(step, answer)
        self.current_attempt += 1
        
        if grading.result == GradeResult.CORRECT:
            # Correct! Move to next step
            message = f"✓ **Correct!** {grading.feedback}\n\n"
            
            self._save_turn(
                role=SessionTurn.Role.TUTOR,
                content=message,
                step=step,
            )
            
            # Reset attempt counter and advance
            self.current_attempt = 0
            self.hints_given = 0
            self._save_state()
            
            return self._advance_to_next_step(prefix_message=message)
        
        else:
            # Incorrect
            attempts_remaining = step.max_attempts - self.current_attempt
            
            if attempts_remaining <= 0:
                # Out of attempts, show answer and move on
                message = f"✗ Not quite. The correct answer is: **{step.expected_answer}**\n\n"
                if step.rubric:
                    message += f"{step.rubric}\n\n"
                
                self._save_turn(
                    role=SessionTurn.Role.TUTOR,
                    content=message,
                    step=step,
                )
                
                self.current_attempt = 0
                self.hints_given = 0
                self._save_state()
                
                return self._advance_to_next_step(prefix_message=message)
            
            else:
                # Give hint and let them try again
                hint = self._get_next_hint(step)
                self.hints_given += 1
                
                message = f"✗ {grading.feedback}\n\n"
                if hint:
                    message += f"💡 **Hint:** {hint}\n\n"
                message += f"Try again! ({attempts_remaining} attempt{'s' if attempts_remaining > 1 else ''} remaining)"
                
                self._save_turn(
                    role=SessionTurn.Role.TUTOR,
                    content=message,
                    step=step,
                )
                
                self._save_state()
                
                return TutorResponse(
                    message=message,
                    step_index=self.current_step_index,
                    step_type=step.step_type,
                    phase=self.phase.value,
                    question=self._build_question_data(step),
                    is_waiting_for_answer=True,
                    grading=grading,
                    attempts_remaining=attempts_remaining,
                    hint=hint,
                )
    
    def _advance_to_next_step(self, prefix_message: str = "") -> TutorResponse:
        """Move to the next step."""
        self.current_step_index += 1
        self._save_state()
        
        if self.current_step_index >= len(self.steps):
            # Finished all steps, start exit ticket
            return self._start_exit_ticket(prefix_message=prefix_message)
        
        # Present next step
        response = self._present_current_step()
        if prefix_message:
            response.message = prefix_message + response.message
        return response
    
    def _grade_step_answer(self, step: LessonStep, answer: str) -> GradingOutcome:
        """Grade a step answer."""
        # For multiple choice, simple string match
        if step.answer_type == 'multiple_choice':
            correct = answer.strip().upper() == step.expected_answer.strip().upper()
            return GradingOutcome(
                result=GradeResult.CORRECT if correct else GradeResult.INCORRECT,
                feedback="Well done!" if correct else "That's not quite right.",
                score=1.0 if correct else 0.0,
            )
        
        # For free text, use LLM if available
        if step.answer_type == 'free_text' and self.llm_client:
            return grade_answer(step, answer, self.llm_client)
        
        # Simple string match fallback
        correct = answer.strip().lower() == step.expected_answer.strip().lower()
        return GradingOutcome(
            result=GradeResult.CORRECT if correct else GradeResult.INCORRECT,
            feedback="Correct!" if correct else "Not quite right.",
            score=1.0 if correct else 0.0,
        )
    
    def _get_next_hint(self, step: LessonStep) -> Optional[str]:
        """Get the next hint based on hints given."""
        hints = step.hints
        if self.hints_given < len(hints):
            return hints[self.hints_given]
        return None
    
    def _build_question_data(self, step: LessonStep) -> Dict:
        """Build question data for frontend."""
        data = {
            'question': step.question,
            'type': step.answer_type,
            'label': f"Step {self.current_step_index + 1}",
        }
        
        if step.answer_type == 'multiple_choice' and step.choices:
            data['options'] = step.choices
            data['correct'] = step.expected_answer
        
        return data
    
    # ========================================================================
    # EXIT TICKET HANDLING
    # ========================================================================
    
    def _start_exit_ticket(self, prefix_message: str = "") -> TutorResponse:
        """Start the exit ticket assessment."""
        if not self.exit_questions:
            # No exit ticket, complete the session
            return self._complete_session(passed=True, prefix_message=prefix_message)
        
        self.phase = SessionPhase.EXIT_TICKET
        self.exit_question_index = 0
        self.exit_correct_count = 0
        self.exit_answers = []
        self._save_state()
        
        message = prefix_message
        message += "\n\n---\n\n"
        message += "## 🎯 Exit Ticket\n\n"
        message += f"Great job completing the lesson! Now let's check your understanding.\n\n"
        message += f"Answer **10 questions** about what you learned. You need **{self.PASSING_SCORE} correct** to pass.\n\n"
        
        return self._present_exit_question(prefix_message=message)
    
    def _present_exit_question(self, prefix_message: str = "", is_resume: bool = False) -> TutorResponse:
        """Present current exit ticket question."""
        question = self.current_exit_question
        
        if not question:
            # Finished all questions
            return self._complete_exit_ticket()
        
        message = prefix_message
        if is_resume:
            message += f"Welcome back! You're on question {self.exit_question_index + 1} of 10.\n\n"
        
        message += f"**Question {self.exit_question_index + 1} of 10**\n\n"
        message += question.question_text
        
        question_data = {
            'question': question.question_text,
            'type': 'multiple_choice',
            'label': f"Question {self.exit_question_index + 1} of 10",
            'options': [
                f"A) {question.option_a}",
                f"B) {question.option_b}",
                f"C) {question.option_c}",
                f"D) {question.option_d}",
            ],
            'correct': question.correct_answer,
        }
        
        commands = [{'type': 'show_question', 'data': question_data}]
        
        self._save_state()
        
        return TutorResponse(
            message=message,
            step_index=self.current_step_index,
            step_type="exit_ticket",
            phase=self.phase.value,
            question=question_data,
            is_waiting_for_answer=True,
            commands=commands,
        )
    
    def _process_exit_answer(self, answer: str) -> TutorResponse:
        """Process exit ticket answer."""
        question = self.current_exit_question
        
        if not question:
            return self._complete_exit_ticket()
        
        # Check answer
        user_answer = answer.strip().upper()
        correct = user_answer == question.correct_answer.strip().upper()
        
        # Record answer
        self.exit_answers.append({
            'question_index': self.exit_question_index,
            'user_answer': user_answer,
            'correct_answer': question.correct_answer,
            'is_correct': correct,
        })
        
        if correct:
            self.exit_correct_count += 1
            message = f"✓ **Correct!**\n\n"
        else:
            message = f"✗ The correct answer was **{question.correct_answer}**.\n\n"
            if question.explanation:
                message += f"_{question.explanation}_\n\n"
        
        # Save turn
        self._save_turn(
            role=SessionTurn.Role.STUDENT,
            content=answer,
            metadata={'exit_question': self.exit_question_index, 'correct': correct},
        )
        
        # Move to next question
        self.exit_question_index += 1
        self._save_state()
        
        if self.exit_question_index >= len(self.exit_questions):
            return self._complete_exit_ticket(prefix_message=message)
        
        # Show score progress
        message += f"**Score: {self.exit_correct_count}/{self.exit_question_index}**\n\n"
        
        return self._present_exit_question(prefix_message=message)
    
    def _complete_exit_ticket(self, prefix_message: str = "") -> TutorResponse:
        """Complete the exit ticket and check if passed."""
        passed = self.exit_correct_count >= self.PASSING_SCORE
        return self._complete_session(passed=passed, prefix_message=prefix_message)
    
    # ========================================================================
    # SESSION COMPLETION
    # ========================================================================
    
    def _complete_session(self, passed: bool, prefix_message: str = "") -> TutorResponse:
        """Complete the tutoring session."""
        self.phase = SessionPhase.COMPLETED
        
        # Update session
        self.session.status = TutorSession.Status.COMPLETED
        self.session.ended_at = timezone.now()
        self.session.mastery_achieved = passed
        self._save_state()
        
        # Update student progress
        progress, _ = StudentLessonProgress.objects.get_or_create(
            student=self.session.student,
            lesson=self.lesson,
            defaults={'institution': self.session.institution}
        )
        
        if passed:
            progress.mastery_level = 'mastered'
            progress.mastered_at = timezone.now()
        else:
            progress.mastery_level = 'in_progress'
        
        progress.last_session = self.session
        progress.save()
        
        # Build completion message
        message = prefix_message
        
        if self.exit_questions:
            message += f"\n\n---\n\n"
            message += f"## Final Score: {self.exit_correct_count}/{len(self.exit_questions)}\n\n"
        
        if passed:
            message += f"🎉 **Congratulations!** You've mastered **{self.lesson.title}**!\n\n"
            message += "You can now move on to the next lesson."
        else:
            message += f"📚 You scored {self.exit_correct_count}/{len(self.exit_questions)}. "
            message += f"You need {self.PASSING_SCORE} to pass.\n\n"
            message += "Review the material and try again!"
        
        commands = [{
            'type': 'show_result',
            'data': {
                'score': self.exit_correct_count,
                'total': len(self.exit_questions),
                'passed': passed,
                'message': "Mastery achieved!" if passed else "Keep practicing!",
            }
        }]
        
        return TutorResponse(
            message=message,
            step_index=self.current_step_index,
            step_type="completed",
            phase=self.phase.value,
            is_session_complete=True,
            mastery_achieved=passed,
            commands=commands,
        )
    
    # ========================================================================
    # HELPERS
    # ========================================================================
    
    def _save_turn(self, role: str, content: str, step: LessonStep = None,
                   tokens_in: int = 0, tokens_out: int = 0, metadata: Dict = None):
        """Save a conversation turn."""
        SessionTurn.objects.create(
            session=self.session,
            role=role,
            content=content,
            step=step,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            metadata=metadata or {},
        )


# ============================================================================
# FACTORY FUNCTION
# ============================================================================

def create_tutor_session(student, lesson, institution) -> TutorSession:
    """Create a new tutoring session."""
    session = TutorSession.objects.create(
        student=student,
        lesson=lesson,
        institution=institution,
        status=TutorSession.Status.ACTIVE,
        engine_state={
            'phase': 'lesson',
            'step_index': 0,
            'current_attempt': 0,
            'hints_given': 0,
        }
    )
    return session