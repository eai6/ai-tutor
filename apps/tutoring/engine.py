"""
Tutor Engine - Orchestrates the tutoring session flow.

This is the "brain" that:
1. Manages session state (current step, attempts, streaks)
2. Coordinates prompt assembly and LLM calls
3. Grades answers and applies hint ladder
4. Tracks progress toward mastery

Design: Stateless functions that operate on session objects.
The Django models hold all state; the engine just processes it.
"""

from dataclasses import dataclass
from typing import Optional
from datetime import datetime
from django.utils import timezone
from django.db import transaction

from apps.accounts.models import Institution
from apps.curriculum.models import Lesson, LessonStep
from apps.llm.models import PromptPack, ModelConfig
from apps.llm.client import get_llm_client, BaseLLMClient, LLMResponse
from apps.llm.prompts import build_tutor_message
from apps.tutoring.models import TutorSession, SessionTurn, StudentLessonProgress
from apps.tutoring.grader import grade_answer, GradingOutcome, GradeResult


@dataclass
class TutorResponse:
    """What the tutor says + metadata about the turn."""
    message: str
    step_index: int
    step_type: str
    is_waiting_for_answer: bool
    is_session_complete: bool
    mastery_achieved: bool
    attempts_remaining: Optional[int] = None
    grading: Optional[GradingOutcome] = None
    tokens_in: int = 0
    tokens_out: int = 0


class TutorEngine:
    """
    Manages a tutoring session.
    
    Usage:
        engine = TutorEngine(session)
        
        # Start the session (gets first step)
        response = engine.start()
        
        # Process student answer
        response = engine.process_student_answer("42")
        
        # Continue to next step
        response = engine.advance_step()
    """
    
    def __init__(self, session: TutorSession, use_mock_llm: bool = False):
        self.session = session
        self.lesson = session.lesson
        self.prompt_pack = session.prompt_pack
        self.model_config = session.model_config
        self.use_mock_llm = use_mock_llm
        
        # Load lesson steps (ordered)
        self.steps = list(self.lesson.steps.order_by('order_index'))
        
        # Get or create LLM client
        self._llm_client: Optional[BaseLLMClient] = None
    
    @property
    def llm_client(self) -> BaseLLMClient:
        """Lazy-load LLM client."""
        if self._llm_client is None:
            self._llm_client = get_llm_client(self.model_config, use_mock=self.use_mock_llm)
        return self._llm_client
    
    @property
    def current_step(self) -> Optional[LessonStep]:
        """Get the current step based on session state."""
        if self.session.current_step_index < len(self.steps):
            return self.steps[self.session.current_step_index]
        return None
    
    def get_conversation_history(self) -> list[dict]:
        """Build conversation history from session turns."""
        turns = self.session.turns.order_by('created_at')
        messages = []
        
        for turn in turns:
            if turn.role == SessionTurn.Role.TUTOR:
                messages.append({"role": "assistant", "content": turn.content})
            elif turn.role == SessionTurn.Role.STUDENT:
                messages.append({"role": "user", "content": turn.content})
            # Skip system turns
        
        return messages
    
    def _get_step_metadata(self) -> dict:
        """Get metadata for the current step from session turns."""
        # Find metadata from the most recent turn for this step
        step = self.current_step
        if not step:
            return {}
        
        recent_turns = self.session.turns.filter(step=step).order_by('-created_at')
        if recent_turns.exists():
            return recent_turns.first().metadata or {}
        return {}
    
    def _save_turn(
        self,
        role: str,
        content: str,
        step: Optional[LessonStep] = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        metadata: Optional[dict] = None,
    ) -> SessionTurn:
        """Save a turn to the session."""
        return SessionTurn.objects.create(
            session=self.session,
            role=role,
            content=content,
            step=step,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            metadata=metadata or {},
        )
    
    def _call_llm(self, attempt_number: int = 1, previous_answer: Optional[str] = None, hint_level: int = 0) -> LLMResponse:
        """Make an LLM call for the current step."""
        step = self.current_step
        history = self.get_conversation_history()
        
        system_prompt, messages = build_tutor_message(
            prompt_pack=self.prompt_pack,
            lesson=self.lesson,
            step=step,
            conversation_history=history,
            attempt_number=attempt_number,
            previous_answer=previous_answer,
            hint_level=hint_level,
        )
        
        return self.llm_client.generate(messages=messages, system_prompt=system_prompt)
    
    def start(self) -> TutorResponse:
        """
        Start the tutoring session.
        Returns the tutor's opening message for the first step.
        """
        step = self.current_step
        if not step:
            return TutorResponse(
                message="This lesson has no steps configured.",
                step_index=0,
                step_type="error",
                is_waiting_for_answer=False,
                is_session_complete=True,
                mastery_achieved=False,
            )
        
        # Call LLM to generate opening
        response = self._call_llm()
        
        # Save tutor turn
        self._save_turn(
            role=SessionTurn.Role.TUTOR,
            content=response.content,
            step=step,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            metadata={"attempt": 1, "hint_level": 0},
        )
        
        return TutorResponse(
            message=response.content,
            step_index=self.session.current_step_index,
            step_type=step.step_type,
            is_waiting_for_answer=step.requires_response(),
            is_session_complete=False,
            mastery_achieved=False,
            attempts_remaining=step.max_attempts if step.requires_response() else None,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
    
    def process_student_answer(self, answer: str) -> TutorResponse:
        """
        Process a student's answer to the current step.
        
        Grades the answer, applies hint ladder if wrong,
        updates progress tracking, and generates tutor response.
        """
        step = self.current_step
        if not step:
            return self._session_complete_response()
        
        if not step.requires_response():
            # This step doesn't need an answer - advance
            return self.advance_step()
        
        # Save student's answer
        metadata = self._get_step_metadata()
        attempt_number = metadata.get("attempt", 1)
        
        self._save_turn(
            role=SessionTurn.Role.STUDENT,
            content=answer,
            step=step,
            metadata={"attempt": attempt_number},
        )
        
        # Grade the answer
        grading = grade_answer(step, answer, self.llm_client)
        
        # Update progress tracking
        progress = self._get_or_create_progress()
        progress.total_attempts += 1
        
        if grading.result == GradeResult.CORRECT:
            # Correct! Update streak and potentially advance
            progress.correct_streak += 1
            progress.total_correct += 1
            progress.save()
            
            # Check for mastery
            mastery_achieved = self._check_mastery(progress)
            
            # Generate encouraging response and advance
            return self._generate_correct_response(grading, mastery_achieved)
        
        else:
            # Wrong - apply hint ladder
            progress.correct_streak = 0  # Reset streak
            progress.save()
            
            hint_level = metadata.get("hint_level", 0) + 1
            attempts_used = attempt_number
            attempts_remaining = step.max_attempts - attempts_used
            
            if attempts_remaining <= 0:
                # Out of attempts - show answer and move on
                return self._generate_reveal_answer_response(step, grading)
            
            # Generate response with hint
            return self._generate_retry_response(
                step=step,
                grading=grading,
                attempt_number=attempt_number + 1,
                hint_level=hint_level,
                attempts_remaining=attempts_remaining,
                previous_answer=answer,
            )
    
    def advance_step(self) -> TutorResponse:
        """Advance to the next step in the lesson."""
        self.session.current_step_index += 1
        self.session.save()
        
        step = self.current_step
        if not step:
            return self._session_complete_response()
        
        # Generate tutor message for new step
        response = self._call_llm()
        
        self._save_turn(
            role=SessionTurn.Role.TUTOR,
            content=response.content,
            step=step,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            metadata={"attempt": 1, "hint_level": 0},
        )
        
        return TutorResponse(
            message=response.content,
            step_index=self.session.current_step_index,
            step_type=step.step_type,
            is_waiting_for_answer=step.requires_response(),
            is_session_complete=False,
            mastery_achieved=False,
            attempts_remaining=step.max_attempts if step.requires_response() else None,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
    
    def _get_or_create_progress(self) -> StudentLessonProgress:
        """Get or create progress record for this student/lesson."""
        progress, created = StudentLessonProgress.objects.get_or_create(
            institution=self.session.institution,
            student=self.session.student,
            lesson=self.lesson,
            defaults={"mastery_level": StudentLessonProgress.MasteryLevel.IN_PROGRESS},
        )
        if created or progress.mastery_level == StudentLessonProgress.MasteryLevel.NOT_STARTED:
            progress.mastery_level = StudentLessonProgress.MasteryLevel.IN_PROGRESS
            progress.save()
        return progress
    
    def _check_mastery(self, progress: StudentLessonProgress) -> bool:
        """Check if student has achieved mastery based on lesson rules."""
        mastery_rule = self.lesson.mastery_rule
        
        if mastery_rule == Lesson.MasteryRule.STREAK_3:
            return progress.correct_streak >= 3
        elif mastery_rule == Lesson.MasteryRule.STREAK_5:
            return progress.correct_streak >= 5
        elif mastery_rule == Lesson.MasteryRule.COMPLETE_ALL:
            return self.session.current_step_index >= len(self.steps) - 1
        elif mastery_rule == Lesson.MasteryRule.PASS_QUIZ:
            # Check if current step is quiz and was passed
            step = self.current_step
            return step and step.step_type == LessonStep.StepType.QUIZ
        
        return False
    
    def _generate_correct_response(self, grading: GradingOutcome, mastery_achieved: bool) -> TutorResponse:
        """Generate response for a correct answer."""
        step = self.current_step
        
        # If mastery achieved, mark session complete
        if mastery_achieved:
            self._complete_session(mastery=True)
        
        # Call LLM to generate encouraging response
        # (In a more sophisticated version, we'd pass grading context)
        response = self._call_llm()
        
        self._save_turn(
            role=SessionTurn.Role.TUTOR,
            content=response.content,
            step=step,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            metadata={"grading": grading.result.value, "correct": True},
        )
        
        # Auto-advance if not waiting for more input
        is_last_step = self.session.current_step_index >= len(self.steps) - 1
        
        return TutorResponse(
            message=response.content,
            step_index=self.session.current_step_index,
            step_type=step.step_type if step else "unknown",
            is_waiting_for_answer=False,  # Correct, so move on
            is_session_complete=mastery_achieved or is_last_step,
            mastery_achieved=mastery_achieved,
            grading=grading,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
    
    def _generate_retry_response(
        self,
        step: LessonStep,
        grading: GradingOutcome,
        attempt_number: int,
        hint_level: int,
        attempts_remaining: int,
        previous_answer: str,
    ) -> TutorResponse:
        """Generate response for wrong answer with hint."""
        response = self._call_llm(
            attempt_number=attempt_number,
            previous_answer=previous_answer,
            hint_level=hint_level,
        )
        
        self._save_turn(
            role=SessionTurn.Role.TUTOR,
            content=response.content,
            step=step,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            metadata={
                "attempt": attempt_number,
                "hint_level": hint_level,
                "grading": grading.result.value,
            },
        )
        
        return TutorResponse(
            message=response.content,
            step_index=self.session.current_step_index,
            step_type=step.step_type,
            is_waiting_for_answer=True,
            is_session_complete=False,
            mastery_achieved=False,
            attempts_remaining=attempts_remaining,
            grading=grading,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
    
    def _generate_reveal_answer_response(self, step: LessonStep, grading: GradingOutcome) -> TutorResponse:
        """Generate response that reveals the correct answer."""
        # For now, just include the answer in the prompt context
        # The LLM should handle revealing it gracefully
        response = self._call_llm(
            attempt_number=step.max_attempts + 1,  # Signal we're past max
            hint_level=len(step.hints),  # All hints used
        )
        
        self._save_turn(
            role=SessionTurn.Role.TUTOR,
            content=response.content,
            step=step,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            metadata={"revealed_answer": True, "grading": grading.result.value},
        )
        
        return TutorResponse(
            message=response.content,
            step_index=self.session.current_step_index,
            step_type=step.step_type,
            is_waiting_for_answer=False,  # Moving on
            is_session_complete=False,
            mastery_achieved=False,
            attempts_remaining=0,
            grading=grading,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
    
    def _session_complete_response(self) -> TutorResponse:
        """Generate response when session is complete."""
        self._complete_session(mastery=self.session.mastery_achieved)
        
        return TutorResponse(
            message="Great job completing this lesson! You've worked through all the material.",
            step_index=len(self.steps),
            step_type="complete",
            is_waiting_for_answer=False,
            is_session_complete=True,
            mastery_achieved=self.session.mastery_achieved,
        )
    
    def _complete_session(self, mastery: bool):
        """Mark the session as complete."""
        self.session.status = TutorSession.Status.COMPLETED
        self.session.mastery_achieved = mastery
        self.session.ended_at = timezone.now()
        self.session.save()
        
        # Update progress
        progress = self._get_or_create_progress()
        if mastery:
            progress.mastery_level = StudentLessonProgress.MasteryLevel.MASTERED
        progress.last_session_at = timezone.now()
        progress.save()


# ----- Helper functions for creating sessions -----

def create_tutor_session(
    student,
    lesson: Lesson,
    institution: Institution,
    prompt_pack: Optional[PromptPack] = None,
    model_config: Optional[ModelConfig] = None,
) -> TutorSession:
    """
    Create a new tutoring session.
    
    If prompt_pack or model_config not provided, uses the active ones
    for the institution.
    """
    if prompt_pack is None:
        prompt_pack = PromptPack.objects.filter(
            institution=institution,
            is_active=True
        ).first()
    
    if model_config is None:
        model_config = ModelConfig.objects.filter(
            institution=institution,
            is_active=True
        ).first()
    
    if not prompt_pack or not model_config:
        raise ValueError("No active PromptPack or ModelConfig found for institution")
    
    return TutorSession.objects.create(
        institution=institution,
        student=student,
        lesson=lesson,
        prompt_pack=prompt_pack,
        model_config=model_config,
        status=TutorSession.Status.ACTIVE,
    )
