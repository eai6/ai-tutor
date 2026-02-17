"""
Tutor Engine - Orchestrates tutoring sessions with Science of Learning principles.

This engine supports TWO modes:
1. STEP MODE - Structured lessons with pre-defined steps
2. CONVERSATIONAL MODE - AI-driven sessions following the pedagogy flow:
   - RETRIEVAL (2-3 min) - Activate prior knowledge
   - INSTRUCTION (5-7 min) - Explicit teaching with visuals
   - PRACTICE (10-15 min) - Guided practice with scaffolding
   - EXIT TICKET (5 min) - Assessment (8/10 to pass)

The Django models hold all state; the engine just processes it.

Image Generation:
- Uses DALL-E when available (API key + internet)
- Falls back to existing media library when offline
"""

import json
import re
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum
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
from apps.media_library.models import StepMedia

logger = logging.getLogger(__name__)


# ============================================================================
# Data Classes
# ============================================================================

class SessionPhase(Enum):
    """Phases in a structured tutoring session."""
    RETRIEVAL = "retrieval"
    INSTRUCTION = "instruction"
    PRACTICE = "practice"
    EXIT_TICKET = "exit_ticket"
    COMPLETE = "complete"


@dataclass
class TutorResponse:
    """What the tutor says + metadata about the turn."""
    message: str
    step_index: int
    step_type: str
    is_waiting_for_answer: bool
    is_session_complete: bool
    mastery_achieved: bool
    phase: str = "retrieval"
    commands: List[Dict[str, Any]] = field(default_factory=list)
    attempts_remaining: Optional[int] = None
    grading: Optional[GradingOutcome] = None
    tokens_in: int = 0
    tokens_out: int = 0


# ============================================================================
# Main Engine Class
# ============================================================================

class TutorEngine:
    """
    Manages a tutoring session with Science of Learning principles.
    
    Assessment Types:
    - FORMATIVE: Conversational, during lesson, adaptive to student responses
    - SUMMATIVE: Pre-defined exit ticket (10 MCQs from database)
    
    Usage:
        engine = TutorEngine(session)
        response = engine.start()
        response = engine.process_student_answer("my answer")
    """
    
    # Phase configuration
    RETRIEVAL_QUESTIONS = 2
    PRACTICE_QUESTIONS = 3
    EXIT_TICKET_QUESTIONS = 10  # Standardized summative assessment
    EXIT_TICKET_PASS_THRESHOLD = 8  # 80% to pass
    MAX_ATTEMPTS_PER_QUESTION = 3
    
    def __init__(self, session: TutorSession, use_mock_llm: bool = False):
        self.session = session
        self.lesson = session.lesson
        self.prompt_pack = session.prompt_pack
        self.model_config = session.model_config
        self.use_mock_llm = use_mock_llm
        
        # Load lesson steps
        self.steps = list(self.lesson.steps.order_by('order_index'))
        
        # Lazy-load LLM client
        self._llm_client: Optional[BaseLLMClient] = None
        
        # Load lesson content and media
        self.lesson_content = self._load_lesson_content()
        self.lesson_media = self._load_lesson_media()
        
        # Load pre-defined exit ticket (summative assessment)
        self.exit_ticket = self._load_exit_ticket()
        
        # Initialize phase state
        self.phase_state = self._load_phase_state()
    
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
    
    @property
    def current_phase(self) -> SessionPhase:
        """Get current session phase."""
        return SessionPhase(self.phase_state.get('phase', 'retrieval'))
    
    # ========================================================================
    # Content Loading
    # ========================================================================
    
    def _load_lesson_content(self) -> Dict:
        """Load all lesson content into a structured format."""
        content = {
            'title': self.lesson.title,
            'objective': self.lesson.objective,
            'topics': [],
            'teaching_content': '',
            'worked_examples': [],
            'practice_questions': [],
            'exit_ticket_questions': [],
        }
        
        for step in self.steps:
            if step.step_type == LessonStep.StepType.TEACH:
                content['teaching_content'] += (step.teacher_script or '') + '\n\n'
            elif step.step_type == LessonStep.StepType.WORKED_EXAMPLE:
                content['worked_examples'].append({
                    'content': step.teacher_script,
                    'question': step.question,
                    'answer': step.expected_answer,
                })
            elif step.step_type == LessonStep.StepType.PRACTICE:
                content['practice_questions'].append({
                    'question': step.question,
                    'answer': step.expected_answer,
                    'hints': step.hints or [],
                    'choices': step.choices,
                })
            elif step.step_type == LessonStep.StepType.QUIZ:
                content['exit_ticket_questions'].append({
                    'question': step.question,
                    'answer': step.expected_answer,
                    'choices': step.choices,
                })
        
        return content
    
    def _load_lesson_media(self) -> List[Dict]:
        """Load all media for this lesson."""
        attachments = StepMedia.objects.filter(
            lesson_step__lesson=self.lesson
        ).select_related('media_asset')
        
        return [{
            'id': att.media_asset.id,
            'title': att.media_asset.title,
            'type': att.media_asset.asset_type,
            'url': att.media_asset.file.url if att.media_asset.file else None,
            'caption': att.media_asset.caption,
            'alt_text': att.media_asset.alt_text,
        } for att in attachments]
    
    def _load_exit_ticket(self) -> Optional[Dict]:
        """
        Load pre-defined exit ticket (summative assessment) from database.
        
        Returns dict with questions if exit ticket exists, None otherwise.
        Exit tickets are standardized: 10 MCQs, need 8/10 to pass.
        """
        try:
            from apps.tutoring.models import ExitTicket
            
            exit_ticket = ExitTicket.objects.filter(lesson=self.lesson).first()
            if not exit_ticket:
                logger.info(f"No exit ticket found for lesson {self.lesson.id}")
                return None
            
            questions = exit_ticket.questions.order_by('order_index')
            if questions.count() < 10:
                logger.warning(f"Exit ticket for lesson {self.lesson.id} has only {questions.count()} questions (need 10)")
            
            return {
                'id': exit_ticket.id,
                'passing_score': exit_ticket.passing_score,
                'time_limit': exit_ticket.time_limit_minutes,
                'instructions': exit_ticket.instructions,
                'questions': [q.to_dict() for q in questions],
            }
        except ImportError:
            # ExitTicket model doesn't exist yet - that's OK
            logger.info("ExitTicket model not available")
            return None
        except Exception as e:
            logger.error(f"Error loading exit ticket: {e}")
            return None
    
    def _load_phase_state(self) -> Dict:
        """Load or initialize phase state."""
        if self.session.engine_state:
            return self.session.engine_state
        return {
            'phase': 'retrieval',
            'questions_asked': 0,
            'questions_correct': 0,
            'current_question': None,
            'attempts_on_current': 0,
        }
    
    def _save_phase_state(self):
        """Save phase state to session."""
        self.session.engine_state = self.phase_state
        self.session.save(update_fields=['engine_state'])
    
    # ========================================================================
    # Session Control
    # ========================================================================
    
    def _is_conversational_mode(self) -> bool:
        """Check if using AI-driven conversational mode."""
        if len(self.steps) == 1:
            step = self.steps[0]
            return (step.step_type == LessonStep.StepType.TEACH and 
                    not step.requires_response())
        return False
    
    def get_conversation_history(self) -> list[dict]:
        """Build conversation history from session turns."""
        turns = self.session.turns.order_by('created_at')
        messages = []
        for turn in turns:
            if turn.role == SessionTurn.Role.TUTOR:
                messages.append({"role": "assistant", "content": turn.content})
            elif turn.role == SessionTurn.Role.STUDENT:
                messages.append({"role": "user", "content": turn.content})
        return messages
    
    def start(self) -> TutorResponse:
        """Start the tutoring session."""
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
        
        # Initialize phase state
        self.phase_state = {
            'phase': 'retrieval',
            'questions_asked': 0,
            'questions_correct': 0,
            'current_question': None,
            'attempts_on_current': 0,
        }
        self._save_phase_state()
        
        if self._is_conversational_mode():
            return self._start_conversational_session()
        else:
            return self._start_step_session()
    
    def resume(self) -> TutorResponse:
        """Resume an existing session - AI generates continuation with next question."""
        phase = self.current_phase.value if self.current_phase else 'retrieval'
        
        # Build context for resumption
        phase_names = {
            'retrieval': 'warm-up questions',
            'instruction': 'the lesson explanation',
            'practice': 'practice problems',
            'exit_ticket': 'the exit quiz',
        }
        phase_desc = phase_names.get(phase, 'the lesson')
        
        questions_correct = self.phase_state.get('questions_correct', 0)
        questions_asked = self.phase_state.get('questions_asked', 0)
        
        # Build a resume prompt for the AI
        base_system_prompt = self._build_structured_system_prompt()
        
        resume_context = f"""
[SESSION RESUME]

The student is returning to continue this lesson. They were on the {phase_desc} phase.

Progress so far:
- Questions answered: {questions_asked}
- Correct answers: {questions_correct}
- Current phase: {phase}

YOUR TASK:
1. Welcome them back briefly (one sentence)
2. IMMEDIATELY provide the next question or content for their current phase
3. DO NOT just say "let's continue" - actually give them something to work on!

"""
        
        # Add phase-specific instructions
        if phase == 'retrieval':
            resume_context += """
Generate a warm-up question using [ARTIFACT:question] format.
Example: "Welcome back! Let's continue with another warm-up question..."
Then OUTPUT the question artifact.
"""
        elif phase == 'instruction':
            resume_context += """
Continue explaining the concept. Show a diagram or key concept using artifacts.
"""
        elif phase == 'practice':
            resume_context += f"""
Generate practice problem #{questions_asked + 1} using [ARTIFACT:question] format.
"""
        elif phase == 'exit_ticket':
            resume_context += f"""
Continue the exit quiz. Current score: {questions_correct}/10
Generate exit ticket question #{questions_asked + 1} using [ARTIFACT:question] format.
"""
        
        full_system_prompt = base_system_prompt + "\n\n" + resume_context
        
        messages = [{"role": "user", "content": "I'm back! Let's continue."}]
        
        response = self.llm_client.generate(messages=messages, system_prompt=full_system_prompt)
        
        # Parse structured commands from response
        clean_message, commands = self._parse_ai_response(response.content)
        
        # Add phase command
        commands.insert(0, {'type': 'set_phase', 'phase': phase})
        
        # Save the turn
        self._save_turn(
            role=SessionTurn.Role.TUTOR,
            content=clean_message,
            step=self.current_step,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            metadata={'phase': phase, 'resumed': True}
        )
        
        return TutorResponse(
            message=clean_message,
            step_index=self.session.current_step_index,
            step_type="conversational",
            is_waiting_for_answer=True,
            is_session_complete=False,
            mastery_achieved=False,
            phase=phase,
            commands=commands,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
    
    def _start_step_session(self) -> TutorResponse:
        """Start a step-based session."""
        step = self.current_step
        response = self._call_llm()
        
        self._save_turn(
            role=SessionTurn.Role.TUTOR,
            content=response.content,
            step=step,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
        
        return TutorResponse(
            message=response.content,
            step_index=self.session.current_step_index,
            step_type=step.step_type,
            is_waiting_for_answer=step.requires_response(),
            is_session_complete=False,
            mastery_achieved=False,
            phase=self.phase_state['phase'],
            attempts_remaining=step.max_attempts if step.requires_response() else None,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
    
    def _start_conversational_session(self) -> TutorResponse:
        """Start an AI-driven conversational session."""
        step = self.current_step
        
        # Build structured prompt for retrieval phase
        # Include phase context IN the system prompt so AI treats it as instructions
        base_system_prompt = self._build_structured_system_prompt()
        context = self._build_phase_context(is_start=True)
        
        # Combine system prompt with phase context
        full_system_prompt = base_system_prompt + "\n\n" + context
        
        # Start with a simple user message to trigger the response
        messages = [{"role": "user", "content": "Please begin the tutoring session now."}]
        
        response = self.llm_client.generate(messages=messages, system_prompt=full_system_prompt)
        
        # Parse structured commands from response
        clean_message, commands = self._parse_ai_response(response.content)
        
        self._save_turn(
            role=SessionTurn.Role.TUTOR,
            content=clean_message,
            step=step,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            metadata={'commands': commands, 'phase': 'retrieval'},
        )
        
        return TutorResponse(
            message=clean_message,
            step_index=0,
            step_type="conversational",
            is_waiting_for_answer=True,
            is_session_complete=False,
            mastery_achieved=False,
            phase='retrieval',
            commands=commands,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
    
    # ========================================================================
    # Answer Processing
    # ========================================================================
    
    def process_student_answer(self, answer: str) -> TutorResponse:
        """Process a student's answer."""
        step = self.current_step
        if not step:
            return self._session_complete_response()
        
        if self._is_conversational_mode():
            return self._process_conversational_answer(answer)
        else:
            return self._process_step_answer(answer)
    
    def _process_step_answer(self, answer: str) -> TutorResponse:
        """Process answer in step-based mode."""
        step = self.current_step
        
        if not step.requires_response():
            return self.advance_step()
        
        # Save student answer
        metadata = self._get_step_metadata()
        attempt_number = metadata.get("attempt", 1)
        
        self._save_turn(
            role=SessionTurn.Role.STUDENT,
            content=answer,
            step=step,
            metadata={"attempt": attempt_number},
        )
        
        # Grade answer
        grading = grade_answer(step, answer, self.llm_client)
        
        # Update progress
        progress = self._get_or_create_progress()
        progress.total_attempts += 1
        
        if grading.result == GradeResult.CORRECT:
            progress.correct_streak += 1
            progress.total_correct += 1
            progress.save()
            
            mastery_achieved = self._check_mastery(progress)
            return self._generate_correct_response(grading, mastery_achieved)
        else:
            progress.correct_streak = 0
            progress.save()
            
            hint_level = metadata.get("hint_level", 0) + 1
            attempts_remaining = step.max_attempts - attempt_number
            
            if attempts_remaining <= 0:
                return self._generate_reveal_answer_response(step, grading)
            
            return self._generate_retry_response(
                step, grading, attempt_number + 1, hint_level, attempts_remaining, answer
            )
    
    def _process_conversational_answer(self, answer: str) -> TutorResponse:
        """Process answer in conversational mode with structured phases."""
        step = self.current_step
        phase = self.current_phase
        
        # Save student message
        self._save_turn(
            role=SessionTurn.Role.STUDENT,
            content=answer,
            step=step,
            metadata={'phase': phase.value},
        )
        
        # Check answer if there's a current question
        feedback_correct = None
        if self.phase_state.get('current_question'):
            feedback_correct = self._check_answer(
                self.phase_state['current_question'],
                answer
            )
            self.phase_state['questions_asked'] += 1
            if feedback_correct:
                self.phase_state['questions_correct'] += 1
        
        # Determine if we should transition phases
        transition = False
        if phase == SessionPhase.RETRIEVAL:
            if self.phase_state['questions_asked'] >= self.RETRIEVAL_QUESTIONS:
                self.phase_state['phase'] = 'instruction'
                self.phase_state['questions_asked'] = 0
                self.phase_state['questions_correct'] = 0
                transition = True
        
        elif phase == SessionPhase.INSTRUCTION:
            # Check for readiness signals
            readiness = ['yes', 'ready', 'got it', 'understand', 'ok', 'okay', 'sure', 'yep']
            if any(s in answer.lower() for s in readiness):
                self.phase_state['phase'] = 'practice'
                self.phase_state['questions_asked'] = 0
                self.phase_state['questions_correct'] = 0
                transition = True
        
        elif phase == SessionPhase.PRACTICE:
            if self.phase_state['questions_asked'] >= self.PRACTICE_QUESTIONS:
                self.phase_state['phase'] = 'exit_ticket'
                self.phase_state['questions_asked'] = 0
                self.phase_state['questions_correct'] = 0
                transition = True
        
        elif phase == SessionPhase.EXIT_TICKET:
            if self.phase_state['questions_asked'] >= self.EXIT_TICKET_QUESTIONS:
                passed = self.phase_state['questions_correct'] >= self.EXIT_TICKET_PASS_THRESHOLD
                if passed:
                    self.phase_state['phase'] = 'complete'
                    self._save_phase_state()
                    self._complete_session(mastery=True)
                    return self._generate_completion_response(passed=True)
                else:
                    return self._generate_completion_response(passed=False)
        
        self._save_phase_state()
        
        # Generate AI response
        system_prompt = self._build_structured_system_prompt()
        context = self._build_phase_context(
            feedback_correct=feedback_correct,
            transition=transition
        )
        
        history = self.get_conversation_history()
        messages = history + [{"role": "user", "content": context}]
        
        response = self.llm_client.generate(messages=messages, system_prompt=system_prompt)
        
        # Parse response
        clean_message, commands = self._parse_ai_response(response.content)
        
        # Check for session complete signal
        is_complete = "[SESSION_COMPLETE]" in response.content
        clean_message = clean_message.replace("[SESSION_COMPLETE]", "").strip()
        
        self._save_turn(
            role=SessionTurn.Role.TUTOR,
            content=clean_message,
            step=step,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            metadata={'commands': commands, 'phase': self.phase_state['phase']},
        )
        
        if is_complete:
            self._complete_session(mastery=True)
        
        return TutorResponse(
            message=clean_message,
            step_index=0,
            step_type="conversational",
            is_waiting_for_answer=not is_complete,
            is_session_complete=is_complete,
            mastery_achieved=is_complete,
            phase=self.phase_state['phase'],
            commands=commands,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
    
    # ========================================================================
    # Structured Prompts
    # ========================================================================
    
    def _build_structured_system_prompt(self) -> str:
        """Build system prompt with phase awareness and structured artifacts."""
        base_prompt = ""
        if self.prompt_pack:
            base_prompt = self.prompt_pack.get_full_system_prompt()
        
        phase = self.phase_state.get('phase', 'retrieval')
        student_name = self.session.student.first_name or 'Student'
        
        # Build media list for context
        media_list = ", ".join([m['title'] for m in self.lesson_media]) if self.lesson_media else "None"
        
        return base_prompt + f"""

STRUCTURED SESSION PROTOCOL (Science of Learning):

You are conducting a structured tutoring session. Display content in the ARTIFACT PANEL using structured commands.

Current Phase: {phase.upper()}
Student: {student_name}
Questions in phase: {self.phase_state.get('questions_asked', 0)}
Correct answers: {self.phase_state.get('questions_correct', 0)}

═══════════════════════════════════════════════════════════════════
PHASE FLOW & REQUIRED ARTIFACTS:
═══════════════════════════════════════════════════════════════════

1. RETRIEVAL (2-3 min)
   → Start: Show objectives artifact
   → Then: {self.RETRIEVAL_QUESTIONS} warm-up MCQ questions

2. INSTRUCTION (5-7 min)  
   → Show concept diagrams/images
   → Display worked examples step-by-step
   → Use available media

3. PRACTICE (10-15 min)
   → {self.PRACTICE_QUESTIONS} interactive practice problems
   → Show hints when needed

4. EXIT_TICKET (5 min)
   → {self.EXIT_TICKET_QUESTIONS} MCQs (need {self.EXIT_TICKET_PASS_THRESHOLD} correct to pass)
   → Show results at end

═══════════════════════════════════════════════════════════════════
ARTIFACT OUTPUT FORMAT (Always use these for rich content):
═══════════════════════════════════════════════════════════════════

For OBJECTIVES (start of session):
[ARTIFACT:objective]
{{
    "lesson_title": "Title Here",
    "objectives": ["Objective 1", "Objective 2"],
    "estimated_time": 15
}}
[/ARTIFACT]

For CONCEPT/DIAGRAM (during instruction):
[ARTIFACT:concept]
{{
    "title": "Concept Name",
    "explanation": "Clear explanation here...",
    "key_points": ["Point 1", "Point 2"]
}}
[/ARTIFACT]

For QUESTIONS (retrieval, practice, exit ticket):
[ARTIFACT:question]
{{
    "label": "Warm-up 1 of 2",
    "question": "What is...?",
    "type": "mcq",
    "options": ["A) Option 1", "B) Option 2", "C) Option 3", "D) Option 4"],
    "correct": "B"
}}
[/ARTIFACT]

For WORKED EXAMPLES (during instruction):
[ARTIFACT:worked_example]
{{
    "problem": "If a fisherman catches 24 fish...",
    "steps": [
        {{"step": 1, "text": "First, identify what we know..."}},
        {{"step": 2, "text": "Next, apply the formula..."}}
    ],
    "answer": "The answer is 8 fish per hour"
}}
[/ARTIFACT]

For HINTS (when student struggles):
[ARTIFACT:hint]
{{
    "hint": "Think about what we learned about...",
    "hint_number": 1,
    "total_hints": 3
}}
[/ARTIFACT]

For RESULTS (end of exit ticket):
[ARTIFACT:result]
{{
    "score": 4,
    "total": 5,
    "passed": true,
    "message": "Great job! You've mastered this lesson!"
}}
[/ARTIFACT]

═══════════════════════════════════════════════════════════════════
IMPORTANT RULES:
═══════════════════════════════════════════════════════════════════

1. ALWAYS use artifact commands for questions - they render interactively
2. Keep chat messages SHORT and conversational
3. The artifact panel shows the detailed content (questions, diagrams, etc.)
4. To show an image, use [ARTIFACT:media] with the EXACT title from available media
5. Use [SESSION_COMPLETE] ONLY when exit ticket is passed (8/10+)

⚠️ CRITICAL IMAGE RULE:
- NEVER say "Let me show you" or "Look at this image" unless you OUTPUT an [ARTIFACT:media] tag
- NEVER describe images you cannot show
- If no media is available, DO NOT pretend to show images - just explain with words
- Only reference images if AVAILABLE MEDIA list below is not empty

For IMAGES (use exact title from available media):
[ARTIFACT:media]
{{
    "title": "EXACT TITLE FROM AVAILABLE MEDIA",
    "caption": "Description of what to notice in this image"
}}
[/ARTIFACT]

LESSON: {self.lesson.title}
OBJECTIVE: {self.lesson.objective}

AVAILABLE MEDIA (use these EXACT titles to show images):
{self._format_media_list()}
"""
    
    def _format_media_list(self) -> str:
        """Format media list with full details for AI."""
        if not self.lesson_media:
            return "⚠️ NO IMAGES AVAILABLE - Do not say 'let me show you' or describe any images. Teach with words only."
        
        lines = ["The following images are available. Use [ARTIFACT:media] to show them:"]
        for m in self.lesson_media:
            lines.append(f"- Title: \"{m['title']}\" (Type: {m['type']})")
            if m.get('caption'):
                lines.append(f"  Caption: {m['caption']}")
        return "\n".join(lines)
    
    def _build_phase_context(
        self,
        is_start: bool = False,
        feedback_correct: Optional[bool] = None,
        transition: bool = False,
    ) -> str:
        """Build context for current phase with structured artifact instructions."""
        phase = self.phase_state.get('phase', 'retrieval')
        
        # Get available media for context
        media_context = ""
        if self.lesson_media:
            media_context = f"\nAVAILABLE MEDIA: {[m['title'] for m in self.lesson_media]}"
        
        if phase == 'retrieval':
            if is_start:
                return f"""
[PHASE: RETRIEVAL - Starting Session]

YOU MUST OUTPUT THESE EXACT ARTIFACT TAGS IN YOUR RESPONSE:

1. First, output this objectives artifact (copy and fill in):
[ARTIFACT:objective]
{{
    "lesson_title": "{self.lesson.title}",
    "objectives": ["{self.lesson.objective}"],
    "estimated_time": {self.lesson.estimated_minutes}
}}
[/ARTIFACT]

2. Then write a brief greeting to the student.

3. Then output this question artifact with a real question about prerequisite knowledge:
[ARTIFACT:question]
{{
    "label": "Warm-up 1 of {self.RETRIEVAL_QUESTIONS}",
    "question": "YOUR QUESTION HERE about basic concepts related to {self.lesson.title}",
    "type": "mcq",
    "options": ["A) First option", "B) Second option", "C) Third option", "D) Fourth option"],
    "correct": "THE CORRECT LETTER"
}}
[/ARTIFACT]

IMPORTANT: You MUST include both [ARTIFACT:objective] and [ARTIFACT:question] tags in your response!
The artifacts render in a side panel - keep your chat text brief.
{media_context}
"""
            else:
                feedback = "✓ Correct! Well done." if feedback_correct else "Not quite, but good try!"
                q_num = self.phase_state['questions_asked'] + 1
                return f"""
[PHASE: RETRIEVAL - Question {q_num} of {self.RETRIEVAL_QUESTIONS}]

Previous answer was {'CORRECT' if feedback_correct else 'INCORRECT'}.

Give brief feedback: "{feedback}"
Then show the next warm-up question:

[ARTIFACT:question]
{{
    "label": "Warm-up {q_num} of {self.RETRIEVAL_QUESTIONS}",
    "question": "[Next retrieval question]",
    "type": "mcq",
    "options": ["A) ...", "B) ...", "C) ...", "D) ..."],
    "correct": "[correct letter]"
}}
[/ARTIFACT]

Keep responses short - questions appear in artifact panel.
"""
        
        elif phase == 'instruction':
            if transition:
                # Get worked examples if available
                examples = self.lesson_content.get('worked_examples', [])
                example_text = ""
                if examples:
                    ex = examples[0]
                    example_text = f"""
WORKED EXAMPLE TO SHOW:
Problem: {ex.get('question', ex.get('content', ''))}
Answer: {ex.get('answer', '')}
"""
                
                # Build media instructions - MUST show images if available
                media_instructions = ""
                if self.lesson_media:
                    media_instructions = f"""
═══════════════════════════════════════════════════════════════════
IMPORTANT: YOU MUST SHOW IMAGES DURING INSTRUCTION!
═══════════════════════════════════════════════════════════════════

Available images for this lesson (USE THEM!):
"""
                    for m in self.lesson_media:
                        media_instructions += f'- "{m["title"]}" - {m.get("caption", "Educational visual")}\n'
                    
                    media_instructions += f"""
To show an image, output this EXACT format:
[ARTIFACT:media]
{{"title": "{self.lesson_media[0]['title']}", "caption": "Look at this diagram..."}}
[/ARTIFACT]

Show at least ONE image during instruction to help the student visualize the concept!
"""
                else:
                    media_instructions = f"""
═══════════════════════════════════════════════════════════════════
NO PRE-MADE IMAGES AVAILABLE - USE DALL-E TO GENERATE!
═══════════════════════════════════════════════════════════════════

Since no images exist for this lesson, you MUST generate one using:
[GENERATE_IMAGE:detailed description of educational diagram for {self.lesson.title}]

Example:
[GENERATE_IMAGE:educational diagram showing the difference between physical geography (mountains, rivers, forests) and human geography (cities, roads, farms) in Seychelles]

Generate at least ONE image to help visualize the concept!
DO NOT say "let me show you" without using [GENERATE_IMAGE:...] tag.
"""
                
                return f"""
[PHASE: INSTRUCTION - Beginning Teaching]

Great warm-up! Now transition to teaching the main content.

CONTENT TO TEACH:
{self.lesson_content.get('teaching_content', self.lesson.objective)}
{example_text}
{media_instructions}

INSTRUCTIONS:
1. Start with: "Great job on the warm-up! Now let's learn about..."

2. Generate an educational image using [GENERATE_IMAGE:description]

3. Show a key concept:
[ARTIFACT:concept]
{{
    "title": "[Main concept name]",
    "explanation": "[Clear explanation with Seychelles examples]",
    "key_points": ["Point 1", "Point 2", "Point 3"]
}}
[/ARTIFACT]

4. Walk through a worked example if available

5. Ask if they're ready to practice when done explaining.

CRITICAL: Use [GENERATE_IMAGE:...] to create a visual - don't just describe images!
"""
            else:
                # Build quick media reminder
                media_hint = ""
                if self.lesson_media:
                    media_hint = f"\nAvailable images: {[m['title'] for m in self.lesson_media]}\nUse [ARTIFACT:media] to show them!"
                
                return f"""
[PHASE: INSTRUCTION - Responding to Student]

Continue teaching based on student's response.
- If they have questions, answer clearly with examples
- If they ask to see a figure/diagram, USE [ARTIFACT:media] with exact title
- If they indicate understanding ("got it", "yes", "ready"), transition to practice
- Use [ARTIFACT:concept] for additional explanations if needed
{media_hint}
"""
        
        elif phase == 'practice':
            if transition:
                questions = self.lesson_content.get('practice_questions', [])
                return f"""
[PHASE: PRACTICE - Beginning Guided Practice]

Transition: "Now it's your turn to practice! Let's work through some problems together."

PRACTICE QUESTIONS AVAILABLE: {len(questions)}

Show the first practice problem:

[ARTIFACT:question]
{{
    "label": "Practice 1 of {self.PRACTICE_QUESTIONS}",
    "question": "[First practice problem - start easy]",
    "type": "mcq",
    "options": ["A) ...", "B) ...", "C) ...", "D) ..."],
    "correct": "[correct letter]"
}}
[/ARTIFACT]

Be encouraging! Don't give away the answer.
"""
            else:
                feedback = "✓ Excellent!" if feedback_correct else "Not quite..."
                q_num = self.phase_state['questions_asked'] + 1
                
                if feedback_correct:
                    return f"""
[PHASE: PRACTICE - Correct! Moving to question {q_num}]

Praise specifically (not just "good job" - mention what they did well).
Present the next practice problem:

[ARTIFACT:question]
{{
    "label": "Practice {q_num} of {self.PRACTICE_QUESTIONS}",
    "question": "[Next problem - slightly harder]",
    "type": "mcq",
    "options": ["A) ...", "B) ...", "C) ...", "D) ..."],
    "correct": "[correct letter]"
}}
[/ARTIFACT]
"""
                else:
                    attempts = self.phase_state.get('attempts_on_current', 1)
                    hints = self.lesson_content.get('practice_questions', [{}])[0].get('hints', [])
                    hint = hints[attempts-1] if attempts <= len(hints) else "Think about what we learned..."
                    
                    return f"""
[PHASE: PRACTICE - Incorrect, attempt {attempts}]

Show a helpful hint:

[ARTIFACT:hint]
{{
    "hint": "{hint}",
    "hint_number": {attempts},
    "total_hints": 3
}}
[/ARTIFACT]

Encourage them to try again. Keep the same question in the artifact.
"""
        
        elif phase == 'exit_ticket':
            # Use pre-defined exit ticket questions (summative assessment)
            if self.exit_ticket and self.exit_ticket.get('questions'):
                questions = self.exit_ticket['questions']
                q_idx = self.phase_state.get('questions_asked', 0)
                
                if transition:
                    # Show first pre-defined question
                    if q_idx < len(questions):
                        q = questions[q_idx]
                        return f"""
[PHASE: EXIT_TICKET - Standardized Assessment]

This is the summative assessment. Use ONLY the pre-defined questions below.
DO NOT generate your own questions. Read the question EXACTLY as provided.

Transition: "Excellent practice! Now let's check your understanding with a final quiz."
Explain: "This is a {len(questions)}-question quiz. You need {self.exit_ticket['passing_score']} correct to pass."

Show this EXACT question in the artifact:

[ARTIFACT:question]
{{
    "label": "Exit Ticket - Question 1 of {len(questions)}",
    "question": "{q['question']}",
    "type": "mcq",
    "options": {json.dumps(q['options'])},
    "correct": "{q['correct']}",
    "question_id": {q['id']}
}}
[/ARTIFACT]

Just introduce the quiz briefly - the question displays in the artifact.
"""
                else:
                    score = self.phase_state.get('questions_correct', 0)
                    feedback = "✓ Correct!" if feedback_correct else "✗ Incorrect."
                    
                    if q_idx < len(questions):
                        q = questions[q_idx]
                        return f"""
[PHASE: EXIT_TICKET - Question {q_idx + 1} of {len(questions)}]

Previous: {feedback}
Score: {score}/{q_idx}

Show this EXACT pre-defined question:

[ARTIFACT:question]
{{
    "label": "Exit Ticket - Question {q_idx + 1} of {len(questions)}",
    "question": "{q['question']}",
    "type": "mcq",
    "options": {json.dumps(q['options'])},
    "correct": "{q['correct']}",
    "question_id": {q['id']}
}}
[/ARTIFACT]

Brief feedback only. Move efficiently through the quiz.
"""
                    else:
                        # All questions answered
                        passed = score >= self.exit_ticket['passing_score']
                        return f"""
[PHASE: EXIT_TICKET - Complete]

Score: {score}/{len(questions)}
Passed: {passed}

Show results:

[ARTIFACT:result]
{{
    "score": {score},
    "total": {len(questions)},
    "passed": {str(passed).lower()},
    "message": "{'Congratulations! You have mastered this lesson!' if passed else 'Keep practicing. You can try again.'}"
}}
[/ARTIFACT]

{'Include [SESSION_COMPLETE] since they passed.' if passed else 'Offer to review and retry.'}
"""
            else:
                # Fallback if no pre-defined exit ticket - generate questions but inject artifacts directly
                q_num = self.phase_state.get('questions_asked', 0) + 1
                
                if transition:
                    return f"""
[PHASE: EXIT_TICKET - Beginning Final Assessment]

Transition to exit ticket. Say: "Great work on the practice! Now let's check your understanding with a 10-question quiz. You need 8 correct to complete this lesson."

Then output this question artifact:

[ARTIFACT:question]
{{
    "label": "Exit Ticket - Question 1 of {self.EXIT_TICKET_QUESTIONS}",
    "question": "Based on today's lesson about {self.lesson.title}, which of the following best describes the main concept?",
    "type": "mcq",
    "options": ["A) First option related to {self.lesson.title}", "B) Second option", "C) Third option", "D) Fourth option"],
    "correct": "A"
}}
[/ARTIFACT]

YOU MUST include the [ARTIFACT:question] block above - it displays in the side panel.
"""
                else:
                    score = self.phase_state.get('questions_correct', 0)
                    feedback = "✓ Correct!" if feedback_correct else "✗ Not quite."
                    
                    if q_num <= self.EXIT_TICKET_QUESTIONS:
                        return f"""
[PHASE: EXIT_TICKET - Question {q_num} of {self.EXIT_TICKET_QUESTIONS}]

Previous answer: {feedback}
Current score: {score}/{q_num - 1}

Give brief feedback, then IMMEDIATELY output the next question artifact:

[ARTIFACT:question]
{{
    "label": "Exit Ticket - Question {q_num} of {self.EXIT_TICKET_QUESTIONS}",
    "question": "Question {q_num} about {self.lesson.title} - generate an appropriate MCQ",
    "type": "mcq", 
    "options": ["A) ...", "B) ...", "C) ...", "D) ..."],
    "correct": "THE_CORRECT_LETTER"
}}
[/ARTIFACT]

YOU MUST include the [ARTIFACT:question] block - it's REQUIRED for the interface to work.
Keep your message SHORT - the question shows in the artifact panel.
"""
                    else:
                        # All questions done
                        passed = score >= self.EXIT_TICKET_PASS_THRESHOLD
                        return f"""
[PHASE: EXIT_TICKET - Complete]

Final score: {score}/{self.EXIT_TICKET_QUESTIONS}
Passed: {passed}

Output this results artifact:

[ARTIFACT:result]
{{
    "score": {score},
    "total": {self.EXIT_TICKET_QUESTIONS},
    "passed": {str(passed).lower()},
    "message": "{'Congratulations! You have completed this lesson!' if passed else 'You need 8/10 to pass. Would you like to review and try again?'}"
}}
[/ARTIFACT]

{'Add [SESSION_COMPLETE] at the end since they passed.' if passed else 'Encourage them to try again.'}
"""
        
        return ""
    
    def _parse_ai_response(self, content: str) -> tuple:
        """Parse AI response and extract structured artifact commands."""
        commands = []
        
        # Extract new [ARTIFACT:type]{...}[/ARTIFACT] format
        artifact_pattern = r'\[ARTIFACT:(\w+)\]([\s\S]*?)\[/ARTIFACT\]'
        matches = re.findall(artifact_pattern, content, re.DOTALL)
        for artifact_type, artifact_data in matches:
            try:
                data = json.loads(artifact_data.strip())
                
                # Special handling for media artifacts - resolve URL from title
                if artifact_type == 'media':
                    title = data.get('title', '')
                    media = next((m for m in self.lesson_media if m['title'].lower() == title.lower()), None)
                    if media:
                        data['url'] = media['url']
                        data['type'] = media['type']
                        data['alt_text'] = media.get('alt_text', '')
                    else:
                        logger.warning(f"Media not found: {title}")
                        continue  # Skip if media not found
                
                commands.append({
                    'type': f'show_{artifact_type}',
                    'data': data
                })
                # Store question for answer checking
                if artifact_type == 'question':
                    # Normalize options to ensure consistent A, B, C, D format
                    data = self._normalize_question(data)
                    self.phase_state['current_question'] = data
                    logger.info(f"[MCQ] Stored question with correct answer: {data.get('correct')}")
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse artifact {artifact_type}: {e}")
        content = re.sub(artifact_pattern, '', content, flags=re.DOTALL)
        
        # Also support legacy [QUESTION:...] format for backwards compatibility
        question_pattern = r'\[QUESTION:(.*?)\[/QUESTION\]'
        matches = re.findall(question_pattern, content, re.DOTALL)
        for match in matches:
            try:
                question_data = json.loads(match.strip())
                commands.append({'type': 'show_question', 'data': question_data})
                self.phase_state['current_question'] = question_data
            except json.JSONDecodeError:
                pass
        content = re.sub(question_pattern, '', content, flags=re.DOTALL)
        
        # Extract [INSTRUCTION:...] commands (legacy)
        instruction_pattern = r'\[INSTRUCTION:(.*?)\[/INSTRUCTION\]'
        matches = re.findall(instruction_pattern, content, re.DOTALL)
        for match in matches:
            commands.append({
                'type': 'show_concept',
                'data': {'explanation': match.strip()}
            })
        content = re.sub(instruction_pattern, '', content, flags=re.DOTALL)
        
        # Extract [SHOW_MEDIA:...] commands
        media_pattern = r'\[SHOW_MEDIA:(.*?)\]'
        matches = re.findall(media_pattern, content)
        for title in matches:
            media = next((m for m in self.lesson_media if m['title'].lower() == title.strip().lower()), None)
            if media:
                commands.append({'type': 'show_media', 'data': media})
        content = re.sub(media_pattern, '', content)
        
        # AUTO-INJECT MEDIA: If AI mentions showing a diagram/figure but didn't use artifact tags
        has_media_command = any(cmd['type'] == 'show_media' for cmd in commands)
        if not has_media_command and self.lesson_media:
            # Check if AI is talking about showing something visual
            show_keywords = ['let me show you', 'here\'s a diagram', 'look at this', 'this diagram', 
                           'this image', 'the figure shows', 'as you can see', 'take a look']
            content_lower = content.lower()
            
            if any(kw in content_lower for kw in show_keywords):
                # Find the most relevant media based on content keywords
                best_media = self._find_relevant_media(content_lower)
                if best_media:
                    commands.append({'type': 'show_media', 'data': best_media})
                    logger.info(f"[AUTO-INJECT] Added media: {best_media['title']}")
        
        # Extract [GENERATE_IMAGE:...] commands - now with DALL-E support
        gen_pattern = r'\[GENERATE_IMAGE:(.*?)\]'
        matches = re.findall(gen_pattern, content)
        for prompt in matches:
            # Try to generate or find existing image
            try:
                from apps.tutoring.image_service import get_image_for_lesson
                image_result = get_image_for_lesson(self.lesson, prompt.strip(), "diagram")
                if image_result:
                    commands.append({'type': 'show_media', 'data': image_result})
                    logger.info(f"Image {'generated' if image_result.get('generated') else 'found'}: {prompt[:50]}")
            except ImportError:
                logger.warning("Image service not available")
            except Exception as e:
                logger.error(f"Image generation error: {e}")
        content = re.sub(gen_pattern, '', content)
        
        # Add phase command at the start
        commands.insert(0, {'type': 'set_phase', 'phase': self.phase_state.get('phase', 'retrieval')})
        
        # ALWAYS inject exit ticket question for guaranteed synchronization
        phase = self.phase_state.get('phase', 'retrieval')
        logger.info(f"[SYNC] Phase: {phase}, questions_asked: {self.phase_state.get('questions_asked', 0)}")
        
        if phase == 'exit_ticket' and self.exit_ticket and self.exit_ticket.get('questions'):
            q_idx = self.phase_state.get('questions_asked', 0)
            questions = self.exit_ticket['questions']
            logger.info(f"[SYNC] Exit ticket: q_idx={q_idx}, total_questions={len(questions)}")
            
            if q_idx < len(questions):
                q = questions[q_idx]
                question_data = {
                    'label': f"Exit Ticket - Question {q_idx + 1} of {len(questions)}",
                    'question': q['question'],
                    'type': 'mcq',
                    'options': q['options'],
                    'correct': q['correct'],
                    'question_id': q.get('id'),
                }
                
                # Remove any existing question commands (AI might have generated wrong one)
                old_count = len(commands)
                commands = [cmd for cmd in commands if cmd['type'] != 'show_question']
                if old_count != len(commands):
                    logger.info(f"[SYNC] Removed {old_count - len(commands)} AI-generated question commands")
                
                # Add the correct pre-defined question
                commands.append({'type': 'show_question', 'data': question_data})
                self.phase_state['current_question'] = question_data
                logger.info(f"[SYNC] Injected exit ticket question {q_idx + 1}: {q['question'][:50]}...")
            
            elif q_idx >= len(questions):
                # Show results
                score = self.phase_state.get('questions_correct', 0)
                passed = score >= self.exit_ticket['passing_score']
                result_data = {
                    'score': score,
                    'total': len(questions),
                    'passed': passed,
                    'message': 'Congratulations! You have mastered this lesson!' if passed else 'Keep practicing. You can try again.'
                }
                commands = [cmd for cmd in commands if cmd['type'] != 'show_result']
                commands.append({'type': 'show_result', 'data': result_data})
                logger.info(f"[SYNC] Injected results: {score}/{len(questions)}, passed={passed}")
        
        logger.info(f"[SYNC] Final commands: {[cmd['type'] for cmd in commands]}")
        return content.strip(), commands
    
    def _find_relevant_media(self, content: str) -> Optional[Dict]:
        """Find the most relevant media based on content keywords."""
        if not self.lesson_media:
            return None
        
        best_match = None
        best_score = 0
        
        content_words = set(content.lower().split())
        
        for media in self.lesson_media:
            if not media.get('url'):
                continue
            
            score = 0
            title_words = set(media['title'].lower().split())
            caption_words = set((media.get('caption') or '').lower().split())
            
            # Title word matches
            score += len(content_words & title_words) * 3
            
            # Caption word matches
            score += len(content_words & caption_words) * 2
            
            # Specific topic matches
            topic_keywords = {
                'layer': ['layer', 'crust', 'mantle', 'core', 'structure'],
                'rain': ['rain', 'rainfall', 'precipitation', 'convection', 'relief', 'frontal'],
                'earth': ['earth', 'planet', 'globe', 'spheroid'],
                'diagram': ['diagram', 'figure', 'illustration', 'chart'],
            }
            
            for topic, keywords in topic_keywords.items():
                if any(kw in content for kw in keywords):
                    if any(kw in media['title'].lower() for kw in keywords):
                        score += 5
            
            if score > best_score:
                best_score = score
                best_match = media
        
        # Return if we found a reasonable match
        if best_score >= 2:
            return best_match
        
        # Fallback: return first media if nothing matched
        return self.lesson_media[0] if self.lesson_media else None
    
    def _check_answer(self, question: Dict, answer: str) -> bool:
        """Check if answer is correct."""
        if not question:
            return False
        
        correct = question.get('correct', question.get('answer', ''))
        user_answer = answer.strip().upper()
        correct_answer = correct.strip().upper()
        
        logger.info(f"[MCQ] Checking answer: user='{user_answer}' vs correct='{correct_answer}'")
        
        # Direct letter match (A, B, C, D)
        if user_answer == correct_answer:
            return True
        
        # Handle case where user sends full option text
        if len(user_answer) > 1:
            # Check if it starts with a letter
            if user_answer[0] in 'ABCD':
                return user_answer[0] == correct_answer
        
        return False
    
    def _normalize_question(self, data: Dict) -> Dict:
        """Normalize question options to ensure consistent A, B, C, D format."""
        if not data.get('options'):
            return data
        
        options = data['options']
        normalized_options = []
        letter_map = {}  # Maps original letter to normalized position
        
        for i, opt in enumerate(options):
            expected_letter = chr(65 + i)  # A, B, C, D
            
            # Check if option already has a letter prefix
            match = re.match(r'^([A-D])\)\s*(.+)$', opt.strip())
            if match:
                original_letter = match.group(1)
                text = match.group(2)
                letter_map[original_letter] = expected_letter
            else:
                text = opt.strip()
            
            # Normalize to consistent format
            normalized_options.append(f"{expected_letter}) {text}")
        
        data['options'] = normalized_options
        
        # If correct answer was using original lettering, map it
        correct = data.get('correct', '').strip().upper()
        if correct in letter_map:
            data['correct'] = letter_map[correct]
            logger.info(f"[MCQ] Remapped correct answer from {correct} to {letter_map[correct]}")
        
        return data
    
    def _generate_completion_response(self, passed: bool) -> TutorResponse:
        """Generate completion response."""
        score = self.phase_state['questions_correct']
        total = self.EXIT_TICKET_QUESTIONS
        
        if passed:
            message = f"""🎉 **Congratulations!** You scored **{score}/{total}** on the exit ticket!

You've mastered {self.lesson.title}. Great work!"""
            commands = [
                {'type': 'set_phase', 'phase': 'complete'},
                {'type': 'show_completion', 'passed': True, 'score': score, 'total': total}
            ]
        else:
            message = f"""You scored **{score}/{total}**. You need {self.EXIT_TICKET_PASS_THRESHOLD}/{total} to pass.

Would you like to review and try again?"""
            commands = [
                {'type': 'show_completion', 'passed': False, 'score': score, 'total': total}
            ]
        
        return TutorResponse(
            message=message,
            step_index=0,
            step_type="conversational",
            is_waiting_for_answer=not passed,
            is_session_complete=passed,
            mastery_achieved=passed,
            phase='complete' if passed else 'exit_ticket',
            commands=commands,
        )
    
    # ========================================================================
    # Helper Methods
    # ========================================================================
    
    def advance_step(self) -> TutorResponse:
        """Advance to the next step."""
        self.session.current_step_index += 1
        self.session.save()
        
        step = self.current_step
        if not step:
            return self._session_complete_response()
        
        response = self._call_llm()
        
        self._save_turn(
            role=SessionTurn.Role.TUTOR,
            content=response.content,
            step=step,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
        
        return TutorResponse(
            message=response.content,
            step_index=self.session.current_step_index,
            step_type=step.step_type,
            is_waiting_for_answer=step.requires_response(),
            is_session_complete=False,
            mastery_achieved=False,
            phase=self.phase_state.get('phase', 'retrieval'),
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
    
    def _call_llm(self, attempt_number: int = 1, previous_answer: Optional[str] = None, hint_level: int = 0) -> LLMResponse:
        """Make an LLM call."""
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
    
    def _save_turn(
        self,
        role: str,
        content: str,
        step: Optional[LessonStep] = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        metadata: Optional[dict] = None,
    ) -> SessionTurn:
        """Save a turn."""
        return SessionTurn.objects.create(
            session=self.session,
            role=role,
            content=content,
            step=step,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            metadata=metadata or {},
        )
    
    def _get_step_metadata(self) -> dict:
        """Get metadata for current step."""
        step = self.current_step
        if not step:
            return {}
        recent = self.session.turns.filter(step=step).order_by('-created_at').first()
        return recent.metadata if recent else {}
    
    def _get_or_create_progress(self) -> StudentLessonProgress:
        """Get or create progress record."""
        progress, created = StudentLessonProgress.objects.get_or_create(
            institution=self.session.institution,
            student=self.session.student,
            lesson=self.lesson,
            defaults={"mastery_level": StudentLessonProgress.MasteryLevel.IN_PROGRESS},
        )
        if progress.mastery_level == StudentLessonProgress.MasteryLevel.NOT_STARTED:
            progress.mastery_level = StudentLessonProgress.MasteryLevel.IN_PROGRESS
            progress.save()
        return progress
    
    def _check_mastery(self, progress: StudentLessonProgress) -> bool:
        """Check if mastery achieved."""
        rule = self.lesson.mastery_rule
        if rule == Lesson.MasteryRule.STREAK_3:
            return progress.correct_streak >= 3
        elif rule == Lesson.MasteryRule.STREAK_5:
            return progress.correct_streak >= 5
        elif rule == Lesson.MasteryRule.COMPLETE_ALL:
            return self.session.current_step_index >= len(self.steps) - 1
        elif rule == Lesson.MasteryRule.PASS_QUIZ:
            step = self.current_step
            return step and step.step_type == LessonStep.StepType.QUIZ
        return False
    
    def _generate_correct_response(self, grading: GradingOutcome, mastery: bool) -> TutorResponse:
        """Generate response for correct answer."""
        step = self.current_step
        if mastery:
            self._complete_session(mastery=True)
        
        response = self._call_llm()
        self._save_turn(
            role=SessionTurn.Role.TUTOR,
            content=response.content,
            step=step,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
        
        is_last = self.session.current_step_index >= len(self.steps) - 1
        
        return TutorResponse(
            message=response.content,
            step_index=self.session.current_step_index,
            step_type=step.step_type if step else "unknown",
            is_waiting_for_answer=False,
            is_session_complete=mastery or is_last,
            mastery_achieved=mastery,
            grading=grading,
            phase=self.phase_state.get('phase', 'retrieval'),
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
    
    def _generate_retry_response(self, step, grading, attempt, hint_level, remaining, prev_answer) -> TutorResponse:
        """Generate retry response with hint."""
        response = self._call_llm(attempt_number=attempt, previous_answer=prev_answer, hint_level=hint_level)
        
        self._save_turn(
            role=SessionTurn.Role.TUTOR,
            content=response.content,
            step=step,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            metadata={"attempt": attempt, "hint_level": hint_level},
        )
        
        return TutorResponse(
            message=response.content,
            step_index=self.session.current_step_index,
            step_type=step.step_type,
            is_waiting_for_answer=True,
            is_session_complete=False,
            mastery_achieved=False,
            attempts_remaining=remaining,
            grading=grading,
            phase=self.phase_state.get('phase', 'retrieval'),
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
    
    def _generate_reveal_answer_response(self, step, grading) -> TutorResponse:
        """Reveal answer after max attempts."""
        response = self._call_llm(attempt_number=step.max_attempts + 1, hint_level=len(step.hints or []))
        
        self._save_turn(
            role=SessionTurn.Role.TUTOR,
            content=response.content,
            step=step,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            metadata={"revealed_answer": True},
        )
        
        return TutorResponse(
            message=response.content,
            step_index=self.session.current_step_index,
            step_type=step.step_type,
            is_waiting_for_answer=False,
            is_session_complete=False,
            mastery_achieved=False,
            attempts_remaining=0,
            grading=grading,
            phase=self.phase_state.get('phase', 'retrieval'),
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
    
    def _session_complete_response(self) -> TutorResponse:
        """Generate session complete response."""
        self._complete_session(mastery=self.session.mastery_achieved)
        
        return TutorResponse(
            message="Great job completing this lesson!",
            step_index=len(self.steps),
            step_type="complete",
            is_waiting_for_answer=False,
            is_session_complete=True,
            mastery_achieved=self.session.mastery_achieved,
            phase='complete',
        )
    
    def _complete_session(self, mastery: bool):
        """Mark session complete."""
        self.session.status = TutorSession.Status.COMPLETED
        self.session.mastery_achieved = mastery
        self.session.ended_at = timezone.now()
        self.session.save()
        
        progress = self._get_or_create_progress()
        if mastery:
            progress.mastery_level = StudentLessonProgress.MasteryLevel.MASTERED
        progress.last_session_at = timezone.now()
        progress.save()


# ============================================================================
# Helper Functions
# ============================================================================

def create_tutor_session(
    student,
    lesson: Lesson,
    institution: Institution,
    prompt_pack: Optional[PromptPack] = None,
    model_config: Optional[ModelConfig] = None,
) -> TutorSession:
    """Create a new tutoring session."""
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
