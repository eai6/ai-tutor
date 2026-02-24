"""
Conversational Tutor Engine

An LLM-driven tutoring system that actively leads learning conversations.
The tutor dynamically generates responses based on:
- Lesson objectives and content (as guidance)
- Curriculum knowledge base (RAG)
- Student's responses and understanding
- Science of learning principles
- Visual aids (existing media + on-demand generation)

Key Principles:
1. TUTOR LEADS - Always asks questions, guides discovery
2. NEVER GIVES DIRECT ANSWERS - Scaffolds towards understanding
3. USES KNOWLEDGE BASE - RAG for curriculum-aligned content
4. ADAPTS TO STUDENT - Adjusts based on responses
5. INCORPORATES MEDIA - Shows images/diagrams when helpful
6. RETRIEVAL PRACTICE - Reviews previous topics
7. VISUAL LEARNING - Generates diagrams when needed
"""

import json
import logging
import random
import re
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum

from django.utils import timezone
from django.conf import settings

from apps.curriculum.models import Lesson, LessonStep
from apps.tutoring.models import TutorSession, SessionTurn, StudentLessonProgress

logger = logging.getLogger(__name__)


# =============================================================================
# SYSTEM PROMPTS
# =============================================================================

TUTOR_SYSTEM_PROMPT_TEMPLATE = """<system_prompt>

<identity>
You are a friendly, encouraging tutor for secondary school students at
{institution_name} ({locale_context}). Your name is {tutor_name}.
You speak in {language} appropriate for {grade_level} students.
You are warm, patient, and believe every student can succeed with the right support.
</identity>

<core_philosophy>
You follow the science of learning. Every interaction must advance the student's
long-term memory, not just their momentary understanding. "Following along" is not
learning -- only active retrieval and successful independent problem-solving count.
Your teaching must be ACTIVE and DIRECT: you explicitly teach concepts, then
immediately have the student practice with corrective feedback.
</core_philosophy>

<principle id="active_learning">
ACTIVE OVER PASSIVE
- Keep explanations to a MINIMUM EFFECTIVE DOSE: explain just enough for the
  student to attempt a problem, then immediately get them doing something.
- Never present more than 3 sentences of explanation without prompting the
  student to respond -- even a comprehension check like "In your own words,
  what is the first step?"
- The student should be DOING something (answering, computing, explaining back,
  choosing, comparing) at least 60% of interaction turns.
- If you find yourself writing a long explanation, STOP. Break it into a short
  explanation + a question, then continue explaining after the student responds.
</principle>

<principle id="direct_instruction">
DIRECT + GUIDED, NOT DISCOVERY
- Explicitly teach the method or concept BEFORE asking the student to apply it.
  Do not ask students to "discover" or "figure out" a new concept on their own.
- The cycle is: short, clear instruction -> student practice -> feedback -> repeat.
- Socratic questions are for CHECKING understanding, not for teaching new content.
  Teach first, then question. Never replace direct instruction with open-ended
  discovery questions on material the student hasn't seen yet.
</principle>

<principle id="deliberate_practice">
DELIBERATE PRACTICE AT THE EDGE OF ABILITY
- Target practice at the boundary of what the student can and cannot do.
- If they get 3+ in a row correct easily, acknowledge it and move to harder material
  or a new concept: "You've clearly got this -- let's level up."
- If they struggle, slow down, provide a simpler variant, and build back up.
- Never let practice become mindless repetition of something already mastered.
- Use the [STUDENT PROFILE] data if available to calibrate difficulty.
</principle>

<principle id="mastery_learning">
MASTERY BEFORE ADVANCEMENT
- Do not advance to a new concept until the student demonstrates they can solve
  problems on the current concept independently (without hints).
- If the student cannot solve a problem because of a weak PREREQUISITE, address
  the prerequisite FIRST. Say: "Let's take a quick detour -- I think the tricky
  part here is [prerequisite skill]. Let me give you a quick practice on that."
- After prerequisite remediation, return to the original problem.
- Never just tell the student the answer and move on.
</principle>

<principle id="cognitive_load">
MINIMISE COGNITIVE LOAD
- Present ONE idea at a time. Short paragraphs (2-3 sentences max).
- Before asking the student to solve a new type of problem, show a WORKED EXAMPLE
  with labelled subgoals (Step 1: ..., Step 2: ..., Step 3: ...).
- Use concrete numbers and visuals before abstract notation.
- Use dual coding: pair verbal explanations with diagrams, number lines, tables,
  or visual representations whenever possible. Use [SHOW_MEDIA:title] syntax to
  display available media assets at the moment they're most useful.
- If the student seems overwhelmed, break the current step into even smaller pieces.
</principle>

<principle id="automaticity">
BUILD AUTOMATICITY ON BASICS
- If you notice the student is slow or error-prone on a basic skill during a lesson
  (e.g., arithmetic errors while learning algebra), briefly flag it:
  "I notice multiplying negatives is tripping you up -- let's do two quick ones."
- Speed and accuracy on fundamentals matter because they free up working memory
  for higher-order thinking.
</principle>

<principle id="layering">
LAYER AND CONNECT
- When introducing a new concept, explicitly connect it to something the student
  already knows: "Remember when we learned X? This is the same idea, but now..."
- Practice problems should authentically require earlier skills, not artificially
  simplify them away.
- Reference the student's prior successes to build confidence:
  "You did great with [earlier topic] -- this builds right on top of that."
</principle>

<principle id="non_interference">
AVOID CONFUSING SIMILAR CONCEPTS
- When the current topic is easily confused with a related one (e.g., area vs.
  perimeter, permutations vs. combinations), explicitly name the difference:
  "Be careful -- this looks like [related concept], but the key difference is..."
- Give a quick discrimination example when relevant.
</principle>

<principle id="testing_effect">
RETRIEVAL FIRST, HINTS LATER
- When a student gives an incorrect answer, your FIRST response should prompt them
  to try again with a targeted nudge -- NOT a hint.
  Example: "Not quite. Before I give you a hint, try once more -- what operation
  should you start with?"
- Only offer a structured hint after the student has made a genuine second attempt.
- On review problems, provide LESS scaffolding than on first-encounter problems.
  The goal is retrieval from memory, not recognition from prompts.
</principle>

<principle id="spaced_repetition">
REFERENCE SPACED PRACTICE
- At the beginning of a session, if retrieval questions are provided in the
  [WARMUP RETRIEVAL] context, use them for active warmup practice.
- At the end of a session, briefly preview what they'll revisit next time:
  "We'll come back to this in a few days to make sure it sticks."
- Celebrate review success: "Great -- you remembered this from last week!"
</principle>

<principle id="interleaving">
MIX IT UP
- During practice, if interleaved review questions are provided in the
  [INTERLEAVED PRACTICE] context, weave them in naturally:
  "Before we continue, quick question from an earlier topic..."
- Make the student identify WHICH strategy to apply, not just execute one on repeat.
</principle>

<principle id="targeted_remediation">
TARGETED REMEDIATION, NOT LOWERED BARS
- When a student struggles repeatedly on a problem, diagnose the ROOT CAUSE.
  Is it the new concept, or a weak prerequisite?
- Never "give away" the full answer just to move on. Instead:
  1. Identify the specific sub-skill causing difficulty.
  2. Give a simpler problem that isolates that sub-skill.
  3. Once they succeed on the simpler problem, return to the original.
- Phrase it positively: "Let's build up to this."
</principle>

<principle id="gamification">
MOTIVATE AND CELEBRATE
- Celebrate correct answers with genuine, specific praise:
  "Exactly right -- and you did that without any hints!"
- Track streaks informally: "That's 3 in a row -- nice momentum!"
- Normalise mistakes: "Mistakes are how your brain builds stronger connections.
  Let's see what happened."
- Frame difficulty positively (desirable difficulty): "If it feels a bit hard,
  that's a sign you're learning -- your brain is working harder, and that's
  what builds real understanding."
</principle>

<principle id="expertise_reversal">
FADE SCAFFOLDING AS MASTERY GROWS
- First encounter: full worked example -> guided practice -> independent practice.
- Later encounters / reviews: skip worked example -> go straight to problems with
  no hints -> only provide a hint if the student explicitly asks.
- If the student demonstrates fluency: "You clearly know this well. Let's
  challenge you with something new."
- Use [STUDENT PROFILE] mastery data to determine scaffolding level.
</principle>

<feedback_protocol>
HOW TO GIVE FEEDBACK ON ANSWERS
1. CORRECT ANSWER:
   - Confirm immediately: "Yes, that's correct!"
   - Add a brief explanation of WHY it's correct to reinforce the concept.
   - If they solved it on the first try, add specific praise.

2. INCORRECT ANSWER (1st attempt):
   - Do NOT reveal the answer. Do NOT give a hint yet.
   - Give a brief, targeted nudge pointing to the type of error without solving it:
     "Almost -- check your sign in the second step."
   - Ask them to try again.

3. INCORRECT ANSWER (2nd attempt):
   - Now offer a structured hint from the available hints.
   - If available, offer a visual or worked sub-step.
   - Ask them to try again.

4. INCORRECT ANSWER (3rd+ attempt):
   - Offer a stronger hint.
   - Consider whether the real issue is a prerequisite gap. If so, pivot:
     "I think the challenge here is actually [prerequisite]. Let's practice that first."

5. INCORRECT ANSWER (final attempt / giving up):
   - Walk through the full solution step-by-step.
   - Ask them to explain each step back to you in their own words.
   - Then give ONE more similar problem to confirm they can now do it.
   - Never show the answer and move on silently.
</feedback_protocol>

<session_structure>
SESSION FLOW (adapt timing to student pace)
1. WARMUP (1-2 exchanges): Retrieval practice on a previously learned skill.
   If [WARMUP RETRIEVAL] questions are provided, use them. Otherwise, ask a
   quick recall question related to a prerequisite of today's lesson.
2. INTRODUCTION (2-3 exchanges): State the learning objective. Connect to prior
   knowledge. Preview what the student will be able to do by the end.
3. INSTRUCTION (4-6 exchanges): Direct instruction with immediate comprehension
   checks. Show worked examples with labelled subgoals. Alternate explanation
   and student response every 2-3 sentences.
4. PRACTICE (4-6 exchanges): Student solves problems with decreasing support.
   Mix in interleaved review questions if provided. Track accuracy.
5. WRAPUP (1-2 exchanges): Summarise key takeaways. Preview next session.
   Check concept coverage before proceeding to exit ticket.
6. EXIT TICKET: Present assessment. No hints, no scaffolding.
</session_structure>

<safety>
{safety_prompt}
Keep all content and language age-appropriate for {grade_level} students.
If the student seems distressed, frustrated, or disengaged, pause the lesson
and check in: "Hey, how are you feeling about this? We can slow down or try
a different approach -- no rush."
</safety>

<format_rules>
- Respond in 2-4 sentences maximum per turn.
- Always end with a question or a prompt for student action.
- Use short paragraphs. Never produce a wall of text.
- Use LaTeX or clear notation for mathematical expressions.
- Use [SHOW_MEDIA:title] to display available media assets at the relevant moment.
- Suggested quick-reply responses should include at least one "I'm not sure" or
  "Can you explain that differently?" option to lower the barrier for honest confusion.
</format_rules>

</system_prompt>"""


# =============================================================================
# DATA STRUCTURES
# =============================================================================

class ConversationPhase(Enum):
    WARMUP = "warmup"
    INTRODUCTION = "introduction"  
    INSTRUCTION = "instruction"
    PRACTICE = "practice"
    WRAPUP = "wrapup"
    EXIT_TICKET = "exit_ticket"
    COMPLETED = "completed"


@dataclass
class TutorMessage:
    """A message from the tutor."""
    content: str
    phase: str
    media: List[Dict] = field(default_factory=list)
    
    # For questions
    expects_response: bool = True
    suggested_responses: List[str] = field(default_factory=list)
    
    # Session state
    is_complete: bool = False
    show_exit_ticket: bool = False
    exit_ticket_data: Optional[Dict] = None
    
    # Metadata
    skills_covered: List[str] = field(default_factory=list)
    tokens_used: int = 0


# =============================================================================
# CONVERSATIONAL TUTOR ENGINE
# =============================================================================

class ConversationalTutor:
    """
    LLM-driven tutoring engine that leads active learning conversations.
    
    Uses:
    - Lesson steps as GUIDANCE (not scripts)
    - Knowledge base for curriculum context
    - Student responses to adapt instruction
    - Media when relevant to the discussion
    """
    
    def __init__(self, session: TutorSession):
        self.session = session
        self.lesson = session.lesson
        self.student = session.student
        
        # Load lesson context
        self.steps = list(
            LessonStep.objects.filter(lesson=self.lesson)
            .order_by('order_index')
        )
        
        # Load exit ticket concepts (CRITICAL for ensuring coverage)
        self.exit_ticket_concepts = self._load_exit_ticket_concepts()
        
        # Build lesson context including exit ticket requirements
        self.lesson_context = self._build_lesson_context()
        
        # Load conversation history
        self.conversation = self._load_conversation()
        
        # Load state
        self._load_state()
        
        # Initialize services
        self._llm_client = None
        self._knowledge_base = None

        # Skill assessment and personalization (R2, R3)
        self._lesson_skills = None
        self._skill_assessment_service = None
        self._personalization = None
        self._remediation_plan = None
        self._interleaved_practice_block_cache = None
    
    def _load_exit_ticket_concepts(self) -> List[Dict]:
        """
        Load exit ticket questions and select a randomized subset of 10.

        If the question bank has 30+ questions, selects 10 with concept coverage:
        1. Group by concept_tag
        2. Pick one question per unique concept tag (random within each group)
        3. Fill remaining slots randomly from unused questions
        4. Shuffle the final 10

        If resuming, loads the previously selected question IDs from engine_state.
        """
        from apps.tutoring.models import ExitTicket, ExitTicketQuestion

        concepts = []

        try:
            exit_ticket = ExitTicket.objects.filter(lesson=self.lesson).first()
            if not exit_ticket:
                return concepts

            # Check if we have previously selected questions (resume)
            state = self.session.engine_state or {}
            selected_ids = state.get('selected_exit_ticket_ids')

            if selected_ids:
                # Resume: load the exact previously-selected questions
                questions = ExitTicketQuestion.objects.filter(
                    id__in=selected_ids
                )
                # Preserve original selection order
                id_order = {qid: idx for idx, qid in enumerate(selected_ids)}
                questions = sorted(questions, key=lambda q: id_order.get(q.id, 0))
            else:
                # New session: select 10 from the full bank
                all_questions = list(ExitTicketQuestion.objects.filter(
                    exit_ticket=exit_ticket
                ).order_by('order_index'))

                if len(all_questions) > 10:
                    questions = self._select_randomized_questions(all_questions, count=10)
                else:
                    questions = all_questions

            for q in questions:
                concepts.append({
                    'id': q.id,
                    'question': q.question_text,
                    'correct_answer': q.correct_answer,
                    'correct_text': getattr(q, f'option_{q.correct_answer.lower()}', ''),
                    'explanation': q.explanation,
                    'difficulty': q.difficulty,
                    'concept_tag': q.concept_tag,
                    'covered': False,
                })

            logger.info(f"Loaded {len(concepts)} exit ticket concepts for {self.lesson.title}")

        except Exception as e:
            logger.warning(f"Could not load exit ticket concepts: {e}")

        return concepts

    def _select_randomized_questions(
        self, all_questions: list, count: int = 10
    ) -> list:
        """
        Select `count` questions from the bank ensuring concept coverage.

        1. Group by concept_tag
        2. Pick one per unique concept tag (random within group)
        3. Fill remaining slots randomly from unused questions
        4. Shuffle the final set
        """
        from collections import defaultdict

        # Group by concept_tag
        by_tag = defaultdict(list)
        no_tag = []
        for q in all_questions:
            tag = q.concept_tag.strip() if q.concept_tag else ''
            if tag:
                by_tag[tag].append(q)
            else:
                no_tag.append(q)

        selected = []
        used_ids = set()

        # Step 1: one per concept tag
        for tag, group in by_tag.items():
            pick = random.choice(group)
            selected.append(pick)
            used_ids.add(pick.id)
            if len(selected) >= count:
                break

        # Step 2: fill remaining from unused
        remaining = [q for q in all_questions if q.id not in used_ids]
        random.shuffle(remaining)
        for q in remaining:
            if len(selected) >= count:
                break
            selected.append(q)

        random.shuffle(selected)
        return selected[:count]
    
    def _load_state(self):
        """Load session state."""
        state = self.session.engine_state or {}
        
        phase_str = state.get('phase', 'warmup')
        try:
            self.phase = ConversationPhase(phase_str)
        except ValueError:
            self.phase = ConversationPhase.WARMUP
        
        self.exchange_count = state.get('exchange_count', 0)
        self.phase_exchange_count = state.get('phase_exchange_count', 0)
        self.concepts_covered = state.get('concepts_covered', [])
        self.student_struggles = state.get('student_struggles', [])
        self.student_strengths = state.get('student_strengths', [])
        self.current_topic_index = state.get('current_topic_index', 0)
        self.practice_correct = state.get('practice_correct', 0)
        self.practice_total = state.get('practice_total', 0)
        
        # Remediation state
        self.is_remediation = state.get('is_remediation', False)
        self.remediation_attempt = state.get('remediation_attempt', 0)
        self.failed_exit_questions = state.get('failed_exit_questions', [])

        # Mastery-based transition tracking (R10)
        self.instruction_checks_correct = state.get('instruction_checks_correct', 0)
        
        # Restore exit concept coverage status
        covered_concept_ids = state.get('covered_concept_ids', [])
        for concept in self.exit_ticket_concepts:
            concept['covered'] = concept['id'] in covered_concept_ids
    
    def _save_state(self):
        """Save session state."""
        # Get list of covered concept IDs
        covered_concept_ids = [
            c['id'] for c in self.exit_ticket_concepts if c.get('covered')
        ]

        # Persist the selected question IDs so resume gets the same set
        selected_exit_ticket_ids = [
            c['id'] for c in self.exit_ticket_concepts
        ]

        self.session.engine_state = {
            'phase': self.phase.value,
            'exchange_count': self.exchange_count,
            'phase_exchange_count': self.phase_exchange_count,
            'concepts_covered': self.concepts_covered,
            'student_struggles': self.student_struggles,
            'student_strengths': self.student_strengths,
            'current_topic_index': self.current_topic_index,
            'practice_correct': self.practice_correct,
            'practice_total': self.practice_total,
            'covered_concept_ids': covered_concept_ids,
            'selected_exit_ticket_ids': selected_exit_ticket_ids,
            # Remediation state
            'is_remediation': getattr(self, 'is_remediation', False),
            'remediation_attempt': getattr(self, 'remediation_attempt', 0),
            'failed_exit_questions': getattr(self, 'failed_exit_questions', []),
            # Mastery-based transition tracking (R10)
            'instruction_checks_correct': getattr(self, 'instruction_checks_correct', 0),
        }
        self.session.save()
    
    def _load_conversation(self) -> List[Dict]:
        """Load conversation history from session turns."""
        turns = SessionTurn.objects.filter(
            session=self.session
        ).order_by('created_at')
        
        conversation = []
        for turn in turns:
            role = "assistant" if turn.role == 'tutor' else "user"
            conversation.append({
                "role": role,
                "content": turn.content
            })
        
        return conversation
    
    def _build_lesson_context(self) -> str:
        """Build context from lesson steps and exit ticket for the LLM."""
        context_parts = [
            f"LESSON: {self.lesson.title}",
            f"OBJECTIVE: {self.lesson.objective}",
            f"UNIT: {self.lesson.unit.title}",
            f"COURSE: {self.lesson.unit.course.title}",
            "",
            "KEY CONCEPTS TO COVER:",
        ]

        # Collect educational materials across all steps
        all_vocabulary = []
        all_common_mistakes = []
        all_seychelles_context = []

        # Extract key concepts from steps
        for i, step in enumerate(self.steps):
            content_preview = step.teacher_script[:200] if step.teacher_script else ""
            hints = [h for h in [step.hint_1, step.hint_2, step.hint_3] if h]

            if step.step_type == 'teach':
                context_parts.append(f"  {i+1}. [TEACH] {content_preview}...")
            elif step.step_type == 'practice':
                question = step.question[:100] if step.question else content_preview[:100]
                context_parts.append(f"  {i+1}. [PRACTICE] {question}...")
                if step.expected_answer:
                    context_parts.append(f"      Expected: {step.expected_answer}")
                if hints:
                    context_parts.append(f"      Hints: {' → '.join(h[:80] for h in hints)}")
            elif step.step_type == 'worked_example':
                context_parts.append(f"  {i+1}. [EXAMPLE] {content_preview}...")

            # Gather educational materials
            ed = step.educational_content if isinstance(step.educational_content, dict) else {}
            vocab = ed.get('key_vocabulary', [])
            if vocab:
                all_vocabulary.extend(vocab)
            mistakes = ed.get('common_mistakes', [])
            if mistakes:
                all_common_mistakes.extend(mistakes)
            sey_ctx = ed.get('seychelles_context', '')
            if sey_ctx:
                all_seychelles_context.append(sey_ctx)

        # Add aggregated educational materials section
        if all_vocabulary or all_common_mistakes or all_seychelles_context:
            context_parts.append("")
            context_parts.append("EDUCATIONAL MATERIALS:")

            if all_vocabulary:
                context_parts.append("  Key Vocabulary:")
                for term in all_vocabulary:
                    if isinstance(term, dict):
                        context_parts.append(f"    - {term.get('term', '')}: {term.get('definition', '')}")
                    else:
                        context_parts.append(f"    - {term}")

            if all_common_mistakes:
                context_parts.append("  Common Mistakes to Watch For:")
                for mistake in all_common_mistakes:
                    if isinstance(mistake, dict):
                        context_parts.append(f"    - {mistake.get('mistake', mistake.get('description', str(mistake)))}")
                    else:
                        context_parts.append(f"    - {mistake}")

            if all_seychelles_context:
                context_parts.append("  Seychelles Context:")
                for ctx in all_seychelles_context:
                    context_parts.append(f"    - {ctx[:200]}")

        # Add terminal objectives if available
        if self.lesson.metadata and 'terminal_objectives' in self.lesson.metadata:
            context_parts.append("")
            context_parts.append("TERMINAL OBJECTIVES:")
            for obj in self.lesson.metadata['terminal_objectives']:
                context_parts.append(f"  • {obj}")

        # CRITICAL: Add exit ticket concepts that MUST be covered
        if self.exit_ticket_concepts:
            context_parts.append("")
            context_parts.append("=" * 50)
            context_parts.append("EXIT TICKET CONCEPTS (MUST COVER THESE!):")
            context_parts.append("The student will be assessed on these questions.")
            context_parts.append("Make sure to teach the concepts needed to answer them.")
            context_parts.append("")

            for i, concept in enumerate(self.exit_ticket_concepts):
                status = "✓ COVERED" if concept.get('covered') else "⚠ NOT YET COVERED"
                context_parts.append(f"  Q{i+1}. [{status}] {concept['question'][:150]}")
                context_parts.append(f"      Answer: {concept['correct_text'][:100]}")
                if concept.get('explanation'):
                    context_parts.append(f"      Key concept: {concept['explanation'][:100]}")
                context_parts.append("")

        return "\n".join(context_parts)
    
    @property
    def llm_client(self):
        """Lazy load LLM client."""
        if self._llm_client is None:
            try:
                from apps.llm.models import ModelConfig
                from apps.llm.client import get_llm_client
                
                config = ModelConfig.objects.filter(is_active=True).first()
                if config:
                    self._llm_client = get_llm_client(config)
            except Exception as e:
                logger.error(f"Could not load LLM client: {e}")
        return self._llm_client
    
    @property
    def knowledge_base(self):
        """Lazy load knowledge base."""
        if self._knowledge_base is None:
            try:
                from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
                self._knowledge_base = CurriculumKnowledgeBase(
                    institution_id=self.session.institution_id
                )
            except Exception as e:
                logger.warning(f"Could not load knowledge base: {e}")
        return self._knowledge_base

    @property
    def lesson_skills(self):
        """Lazy load skills for this lesson (R2)."""
        if self._lesson_skills is None:
            try:
                from apps.tutoring.skills_models import Skill
                self._lesson_skills = list(Skill.objects.filter(lessons=self.lesson))
            except Exception:
                self._lesson_skills = []
        return self._lesson_skills

    @property
    def skill_assessment_service(self):
        """Lazy load skill assessment service (R2)."""
        if self._skill_assessment_service is None:
            try:
                from apps.tutoring.personalization import SkillAssessmentService
                self._skill_assessment_service = SkillAssessmentService(
                    self.student, session=self.session
                )
            except Exception:
                self._skill_assessment_service = None
        return self._skill_assessment_service

    def _get_current_skill(self):
        """Get the most relevant skill for the current topic (R2)."""
        if not self.lesson_skills:
            return None

        if self.current_topic_index < len(self.steps):
            step = self.steps[self.current_topic_index]
            step_text = (step.teacher_script or "").lower()
            best_match = None
            best_score = 0
            for skill in self.lesson_skills:
                keywords = skill.name.lower().split()
                score = sum(1 for kw in keywords if len(kw) > 3 and kw in step_text)
                if score > best_score:
                    best_score = score
                    best_match = skill
            if best_match:
                return best_match

        return self.lesson_skills[0] if self.lesson_skills else None

    # =========================================================================
    # PUBLIC API
    # =========================================================================

    def start(self) -> TutorMessage:
        """Start the tutoring conversation."""
        if self.conversation:
            # Resume existing conversation
            return self.resume()

        # Load personalization before generating opening (R3)
        self._load_personalization()

        # Generate opening message
        return self._generate_opening()
    
    def resume(self) -> TutorMessage:
        """Resume an existing conversation."""
        if self.phase == ConversationPhase.COMPLETED:
            return TutorMessage(
                content="🎉 You've already completed this lesson! Great work!",
                phase="completed",
                is_complete=True,
            )
        
        # Generate a "welcome back" message
        last_exchange = self.conversation[-1] if self.conversation else None
        
        prompt = f"""The student is returning to continue the lesson.
        
Last message in conversation: {last_exchange['content'][:200] if last_exchange else 'None'}

Generate a brief, warm welcome back message that:
1. Acknowledges they're returning
2. Briefly reminds them where they were
3. Asks a question to re-engage them

Keep it to 2-3 sentences."""

        response = self._generate_response(prompt)
        return self._create_message(response)
    
    def respond(self, student_input: str) -> TutorMessage:
        """
        Generate a response to student input.
        
        This is the main conversation loop.
        """
        # Save student message
        self._save_turn("student", student_input)
        self.conversation.append({"role": "user", "content": student_input})
        
        # Update counts
        self.exchange_count += 1
        self.phase_exchange_count += 1
        
        # Check if student is requesting a visual
        visual_request = self._detect_visual_request(student_input)
        
        # Get curriculum context from knowledge base
        kb_context = self._get_knowledge_context(student_input)
        
        # Check if we should transition phases
        self._maybe_transition_phase()
        
        # Generate response based on current phase
        if self.phase == ConversationPhase.EXIT_TICKET:
            return self._handle_exit_ticket()
        
        # Determine if we should show media and what media is available
        media = []
        media_context = ""
        
        if visual_request:
            # Student explicitly requested a visual
            media = self._find_matching_media(student_input, min_relevance=0.3)
            if not media:
                # Try to generate one
                generated = self._generate_visual_aid(student_input)
                if generated:
                    media = [generated]
        else:
            # Check if this topic would benefit from a visual
            media = self._get_proactive_media()
        
        # Build media context for the LLM so it knows what's being shown
        if media:
            media_context = self._build_media_context(media)
        
        # Generate response WITH media context so LLM can describe accurately
        response = self._generate_contextual_response(
            student_input, 
            kb_context, 
            media_context=media_context,
            visual_requested=bool(visual_request)
        )
        
        # If response mentions visual but we don't have one, try to generate
        if not media and self._response_needs_visual(response):
            visual_need = self._determine_visual_need(response)
            if visual_need:
                generated = self._generate_visual_aid(visual_need)
                if generated:
                    media = [generated]
        
        # Analyze student response for adaptation
        self._analyze_student_response(student_input, response)
        
        # Save state
        self._save_state()
        
        # Save tutor response
        self._save_turn("tutor", response)
        self.conversation.append({"role": "assistant", "content": response})
        
        return self._create_message(response, media=media)

    def _prepare_response(self, student_input: str) -> Optional[Dict]:
        """
        Shared pre-generation logic for respond() and respond_stream().

        Saves student turn, updates counts, builds prompt context, checks
        phase transitions. Returns context dict, or None if exit_ticket phase.
        """
        # Save student message
        self._save_turn("student", student_input)
        self.conversation.append({"role": "user", "content": student_input})

        # Update counts
        self.exchange_count += 1
        self.phase_exchange_count += 1

        # Check if student is requesting a visual
        visual_request = self._detect_visual_request(student_input)

        # Get curriculum context from knowledge base
        kb_context = self._get_knowledge_context(student_input)

        # Check if we should transition phases
        self._maybe_transition_phase()

        # Exit ticket is handled separately (non-streamable)
        if self.phase == ConversationPhase.EXIT_TICKET:
            return None

        # Determine media
        media = []
        media_context = ""

        if visual_request:
            media = self._find_matching_media(student_input, min_relevance=0.3)
            if not media:
                generated = self._generate_visual_aid(student_input)
                if generated:
                    media = [generated]
        else:
            media = self._get_proactive_media()

        if media:
            media_context = self._build_media_context(media)

        return {
            'student_input': student_input,
            'kb_context': kb_context,
            'media_context': media_context,
            'visual_requested': bool(visual_request),
            'media': media,
        }

    def _finalize_response(self, full_response: str, student_input: str, media: List[Dict]) -> Dict:
        """
        Shared post-generation logic for respond() and respond_stream().

        Runs post-processing (concept tracking, state save, media handling).
        Returns metadata dict.
        """
        # Try generating visual if response references one but we don't have it
        if not media and self._response_needs_visual(full_response):
            visual_need = self._determine_visual_need(full_response)
            if visual_need:
                generated = self._generate_visual_aid(visual_need)
                if generated:
                    media.append(generated)

        # Analyze student response for adaptation
        self._analyze_student_response(student_input, full_response)

        # Save state
        self._save_state()

        # Save tutor response
        self._save_turn("tutor", full_response)
        self.conversation.append({"role": "assistant", "content": full_response})

        return {
            'phase': self.phase.value,
            'media': media,
            'show_exit_ticket': False,
            'exit_ticket': None,
            'is_complete': self.phase == ConversationPhase.COMPLETED,
        }

    def respond_stream(self, student_input: str):
        """
        Streaming version of respond(). Yields SSE-compatible chunks.

        Chunk format:
            {"type": "token", "content": "Hello "}
            {"type": "done", "phase": "instruction", "media": [...], ...}
        """
        import json as _json

        ctx = self._prepare_response(student_input)

        # Exit ticket phase - not streamable, yield as single chunk
        if ctx is None:
            et_msg = self._handle_exit_ticket()
            yield _json.dumps({
                "type": "done",
                "content": et_msg.content,
                "phase": et_msg.phase,
                "media": et_msg.media,
                "show_exit_ticket": et_msg.show_exit_ticket,
                "exit_ticket": et_msg.exit_ticket_data,
                "is_complete": et_msg.is_complete,
            })
            return

        # Build the prompt (same as _generate_contextual_response)
        current_guidance = self._get_current_guidance()
        phase_instructions = self._get_phase_instructions()
        concept_coverage = self._get_concept_coverage_summary()
        next_concept = self._get_next_uncovered_concept()
        student_profile = self._build_student_profile_block()
        worked_example_block = self._build_worked_example_block()
        interleaved_block = self._build_interleaved_practice_block()

        visual_instructions = ""
        if ctx['media_context']:
            visual_instructions = f"\n{ctx['media_context']}\n"
        elif ctx['visual_requested']:
            visual_instructions = "\n⚠️ VISUAL REQUESTED BUT NOT AVAILABLE:\nThe student asked for a visual, but no matching image was found.\n- Acknowledge their request\n- Provide a clear verbal description instead\n- Continue with the lesson\n"

        prompt = f"""CONVERSATION CONTEXT:
{self._format_recent_conversation(5)}

STUDENT JUST SAID: "{ctx['student_input']}"

LESSON CONTEXT:
{self.lesson_context}

CURRICULUM KNOWLEDGE:
{ctx['kb_context']}

CURRENT TEACHING GUIDANCE:
{current_guidance}
{visual_instructions}
{worked_example_block}
{concept_coverage}

{next_concept}

{interleaved_block}

PHASE: {self.phase.value.upper()}
{phase_instructions}

{student_profile}

Generate your response following these rules:
1. RESPOND to what the student said (acknowledge their answer)
2. If correct: praise specifically, then advance to the next concept
3. If incorrect: encourage, give a hint, ask a simpler question
4. If confused: simplify, use an example
5. PRIORITIZE teaching uncovered exit ticket concepts
6. If an image is being shown, DESCRIBE WHAT IT ACTUALLY SHOWS
7. Use KEY VOCABULARY terms naturally
8. Watch for COMMON MISTAKES and address them proactively
9. Weave in local Seychelles context where relevant
10. Use the full HINT LADDER for progressive scaffolding
11. END with a question or "Try this:" prompt
12. Keep it concise (2-4 sentences + question)

YOUR RESPONSE:"""

        # Stream from LLM
        full_content = ""
        if self.llm_client:
            try:
                messages = [{"role": "user", "content": prompt}]
                system_prompt = self._build_system_prompt()

                for token in self.llm_client.generate_stream(messages, system_prompt):
                    full_content += token
                    yield _json.dumps({"type": "token", "content": token})
            except Exception as e:
                logger.error(f"LLM streaming failed: {e}")
                full_content = self._fallback_response()
                yield _json.dumps({"type": "token", "content": full_content})
        else:
            full_content = self._fallback_response()
            yield _json.dumps({"type": "token", "content": full_content})

        # Post-processing
        metadata = self._finalize_response(
            full_content, ctx['student_input'], ctx['media']
        )

        yield _json.dumps({
            "type": "done",
            "content": full_content,
            **metadata,
        })

    def _get_proactive_media(self) -> List[Dict]:
        """Get media that would proactively help with current topic."""
        if self.current_topic_index >= len(self.steps):
            return []
        
        step = self.steps[self.current_topic_index]
        
        if not step.media or 'images' not in step.media:
            return []
        
        media = []
        topic_terms = self._extract_topic_terms()
        
        for img in step.media['images'][:1]:
            if not img.get('url'):
                continue
            
            # Check if image matches current topic
            img_description = f"{img.get('alt', '')} {img.get('caption', '')}".lower()
            
            # Require at least one topic term to match
            if any(term in img_description for term in topic_terms):
                media.append({
                    'type': 'image',
                    'url': img['url'],
                    'alt': img.get('alt', ''),
                    'caption': img.get('caption', ''),
                    'description': img.get('alt', '') or img.get('caption', ''),
                })
                break
        
        return media
    
    def _build_media_context(self, media: List[Dict]) -> str:
        """Build context about what images are being shown for the LLM."""
        if not media:
            return ""
        
        context = "\n📷 IMAGES BEING SHOWN TO STUDENT:\n"
        for i, m in enumerate(media):
            desc = m.get('description') or m.get('alt') or m.get('caption') or 'Image'
            context += f"  Image {i+1}: {desc}\n"
        
        context += """
IMPORTANT: You are showing these images with your response.
- Reference the actual image content in your explanation
- Point out specific features the student should notice
- If the image doesn't match what you're explaining, don't reference it
- Describe what the image ACTUALLY shows, not what you wish it showed
"""
        return context
    
    def _response_needs_visual(self, response: str) -> bool:
        """Check if the response references a visual that isn't provided."""
        response_lower = response.lower()
        visual_refs = ['look at', 'see the', 'this diagram', 'this image', 
                       'notice how', 'in the picture', 'the figure shows']
        return any(ref in response_lower for ref in visual_refs)
    
    def _detect_visual_request(self, student_input: str) -> Optional[str]:
        """Detect if student is asking for a visual aid."""
        input_lower = student_input.lower()
        
        visual_triggers = [
            'show me', 'can you show', 'draw', 'diagram', 'picture', 
            'image', 'visual', 'figure', 'illustrate', 'see this',
            'what does it look like', 'visualize', 'graph', 'chart',
            'can i see', 'display', 'example image'
        ]
        
        for trigger in visual_triggers:
            if trigger in input_lower:
                return trigger
        
        return None
    
    def _handle_visual_request(
        self, 
        student_input: str, 
        trigger: str, 
        kb_context: str
    ) -> Tuple[str, List[Dict]]:
        """Handle a request for visual aid."""
        media = []
        
        # First, check if we have existing media that matches
        existing_media = self._find_matching_media(student_input)
        if existing_media:
            media = existing_media
            response = self._generate_visual_explanation(student_input, kb_context, has_image=True)
            return response, media
        
        # Try to generate a new image
        generated = self._generate_visual_aid(student_input)
        if generated:
            media = [generated]
            response = self._generate_visual_explanation(student_input, kb_context, has_image=True)
            return response, media
        
        # Fallback: describe it verbally
        response = self._generate_visual_explanation(student_input, kb_context, has_image=False)
        return response, media
    
    def _find_matching_media(self, query: str, min_relevance: float = 0.4) -> List[Dict]:
        """
        Find existing media that STRONGLY matches the query.
        
        Uses stricter matching to avoid showing irrelevant images.
        Returns media with relevance metadata for the LLM.
        """
        media = []
        query_lower = query.lower()
        
        # Extract meaningful keywords (longer words, no common words)
        stop_words = {'this', 'that', 'what', 'which', 'would', 'could', 'should', 
                      'show', 'have', 'been', 'they', 'their', 'there', 'about',
                      'please', 'want', 'need', 'like', 'help', 'explain'}
        query_words = [w for w in query_lower.split() if len(w) > 3 and w not in stop_words]
        
        if not query_words:
            return []
        
        # Search through all lesson steps for matching media
        candidates = []
        
        for step in self.steps:
            if not step.media or 'images' not in step.media:
                continue
            
            for img in step.media['images']:
                if not img.get('url'):
                    continue
                
                # Build searchable text from image metadata
                img_alt = img.get('alt', '').lower()
                img_caption = img.get('caption', '').lower()
                step_content = (step.teacher_script or '').lower()[:300]
                
                img_text = f"{img_alt} {img_caption} {step_content}"
                
                # Calculate relevance score
                matches = sum(1 for w in query_words if w in img_text)
                relevance = matches / len(query_words) if query_words else 0
                
                # Also check for exact phrase matches (higher weight)
                if any(phrase in img_text for phrase in [query_lower[:20], query_lower[-20:]]):
                    relevance += 0.3
                
                # Check if image description contains topic-specific terms
                topic_terms = self._extract_topic_terms()
                topic_matches = sum(1 for t in topic_terms if t in img_text)
                if topic_terms:
                    relevance += (topic_matches / len(topic_terms)) * 0.3
                
                if relevance >= min_relevance:
                    candidates.append({
                        'type': 'image',
                        'url': img['url'],
                        'alt': img.get('alt', ''),
                        'caption': img.get('caption', ''),
                        'relevance': relevance,
                        'description': img_alt or img_caption or 'Educational diagram',
                    })
        
        # Sort by relevance and take top matches
        candidates.sort(key=lambda x: x['relevance'], reverse=True)
        
        # Only return if we have good matches
        for c in candidates[:2]:
            if c['relevance'] >= min_relevance:
                media.append(c)
        
        return media
    
    def _extract_topic_terms(self) -> List[str]:
        """Extract key topic terms from the lesson for better matching."""
        terms = []
        
        # From lesson title
        title_words = self.lesson.title.lower().split()
        terms.extend([w for w in title_words if len(w) > 4])
        
        # From objective
        if self.lesson.objective:
            obj_words = self.lesson.objective.lower().split()
            terms.extend([w for w in obj_words if len(w) > 5])
        
        return list(set(terms))[:10]  # Limit to 10 unique terms
    
    def _generate_visual_aid(self, request: str) -> Optional[Dict]:
        """Generate a visual aid using Gemini Imagen."""
        # Image safety check
        try:
            from apps.safety import ImageSafetyFilter, SafetyAuditLog
            safety_result = ImageSafetyFilter.check_image_request(
                request, lesson_title=self.lesson.title
            )
            if safety_result.blocked:
                SafetyAuditLog.log(
                    'image_blocked',
                    user=self.student,
                    session_id=self.session.id,
                    details={'prompt': request[:200], 'reason': safety_result.block_reason},
                    severity='warning',
                )
                logger.warning(f"Image request blocked: {request[:50]}...")
                return None
        except Exception as e:
            logger.warning(f"Image safety check failed (allowing): {e}")

        try:
            from apps.tutoring.image_service import ImageGenerationService
            
            service = ImageGenerationService(
                lesson=self.lesson,
                institution=self.session.institution
            )
            
            # Determine the type of visual
            request_lower = request.lower()
            if 'diagram' in request_lower:
                category = 'diagram'
            elif 'graph' in request_lower or 'chart' in request_lower:
                category = 'chart'
            elif 'map' in request_lower:
                category = 'map'
            else:
                category = 'diagram'
            
            # Build a good prompt from the lesson context
            prompt = self._build_image_prompt(request)
            
            result = service.get_or_generate_image(prompt, category)
            
            if result:
                logger.info(f"Generated visual aid: {result.get('url', 'unknown')}")
                return {
                    'type': 'image',
                    'url': result['url'],
                    'alt': result.get('alt_text', prompt),
                    'caption': result.get('caption', f"Diagram: {prompt[:100]}"),
                }
            
        except Exception as e:
            logger.warning(f"Could not generate visual aid: {e}")
        
        return None
    
    def _build_image_prompt(self, request: str) -> str:
        """Build a detailed prompt for image generation."""
        # Get current topic context
        current_topic = self._get_current_topic()
        
        prompt_parts = [
            f"Educational diagram for secondary school: {self.lesson.title}.",
            f"Topic: {current_topic[:100]}.",
            f"Student asked for: {request[:100]}.",
        ]
        
        # Add Seychelles context if relevant
        if any(word in self.lesson.title.lower() for word in ['geography', 'map', 'climate', 'island']):
            prompt_parts.append("Context: Seychelles islands in the Indian Ocean.")
        
        return " ".join(prompt_parts)
    
    def _generate_visual_explanation(
        self, 
        student_input: str, 
        kb_context: str, 
        has_image: bool
    ) -> str:
        """Generate explanation to accompany visual (or explain why we can't show one)."""
        if has_image:
            prompt = f"""The student asked: "{student_input}"

I'm showing them a relevant image/diagram.

Generate a brief response that:
1. Acknowledges their request positively
2. Points out key things to notice in the diagram
3. Asks a question about what they see

Keep it to 2-3 sentences + question."""
        else:
            prompt = f"""The student asked: "{student_input}"

I don't have an image to show right now, but I should help them visualize this.

Generate a response that:
1. Acknowledges their request
2. Provides a clear verbal description to help them visualize
3. Suggests they could draw it themselves
4. Asks a question to check understanding

LESSON CONTEXT: {self.lesson_context[:500]}

Keep it to 3-4 sentences + question."""
        
        return self._generate_response(prompt)
    
    def _get_relevant_media_for_response(self, response: str) -> List[Dict]:
        """
        Intelligently select media that would enhance the tutor's response.
        
        Only includes media if it's highly relevant to what's being discussed.
        Falls back to generating new media if no good match exists.
        """
        media = []
        response_lower = response.lower()
        
        # Keywords that suggest visuals would help
        visual_keywords = [
            'diagram', 'shows', 'look at', 'see how', 'notice', 
            'picture', 'imagine', 'visualize', 'example', 'like this',
            'pyramid', 'chart', 'graph', 'map', 'figure'
        ]
        
        # Check if response would benefit from media
        should_show_media = any(kw in response_lower for kw in visual_keywords)
        
        if not should_show_media:
            return media
        
        # Get current step media if available
        if self.current_topic_index < len(self.steps):
            step = self.steps[self.current_topic_index]
            
            if step.media and 'images' in step.media:
                for img in step.media['images'][:1]:  # Only 1 image to be selective
                    if img.get('url'):
                        # Validate the image is relevant to the response
                        img_description = f"{img.get('alt', '')} {img.get('caption', '')}".lower()
                        
                        # Check for topic match
                        topic_terms = self._extract_topic_terms()
                        topic_match = any(term in img_description for term in topic_terms)
                        
                        # Check for response content match
                        response_terms = [w for w in response_lower.split() if len(w) > 5][:10]
                        response_match = sum(1 for t in response_terms if t in img_description)
                        
                        # Only include if there's a reasonable match
                        if topic_match or response_match >= 2:
                            media.append({
                                'type': 'image',
                                'url': img['url'],
                                'alt': img.get('alt', ''),
                                'caption': img.get('caption', ''),
                                'description': img.get('alt', '') or img.get('caption', ''),
                            })
        
        # If no existing media matches well, try to generate one
        if not media and should_show_media:
            # Determine what visual would help
            visual_need = self._determine_visual_need(response)
            if visual_need:
                generated = self._generate_visual_aid(visual_need)
                if generated:
                    media.append(generated)
        
        return media
    
    def _determine_visual_need(self, response: str) -> Optional[str]:
        """Determine what kind of visual would help based on the response."""
        response_lower = response.lower()
        
        # Check for specific visual types mentioned
        if 'pyramid' in response_lower:
            return f"population pyramid chart showing age distribution with males on left and females on right, for {self.lesson.title}"
        elif 'graph' in response_lower or 'chart' in response_lower:
            return f"educational chart or graph related to {self.lesson.title}"
        elif 'map' in response_lower:
            return f"educational map related to {self.lesson.title}"
        elif 'diagram' in response_lower:
            return f"educational diagram explaining {self.lesson.title}"
        
        # Generic visual for the topic
        topic_terms = self._extract_topic_terms()
        if topic_terms:
            return f"educational diagram showing {' '.join(topic_terms[:3])} for secondary school"
        
        return None
    
    # =========================================================================
    # RESPONSE GENERATION
    # =========================================================================
    
    def _load_personalization(self):
        """Load session personalization data (R3)."""
        try:
            from apps.tutoring.personalization import SessionPersonalizationService
            service = SessionPersonalizationService(self.student, self.lesson)
            self._personalization = service.get_session_personalization()
            logger.info(
                f"Loaded personalization: {len(self._personalization.retrieval_questions)} retrieval Qs, "
                f"pace={self._personalization.recommended_pace}"
            )
        except Exception as e:
            logger.warning(f"Failed to load personalization: {e}")
            self._personalization = None

    def _build_retrieval_block(self) -> str:
        """Build [WARMUP RETRIEVAL] context block for the LLM (R4)."""
        if not self._personalization or not self._personalization.retrieval_questions:
            return ""

        questions = self._personalization.retrieval_questions[:2]

        lines = [
            "[WARMUP RETRIEVAL]",
            "Present these 1-2 retrieval practice questions at the start of the session.",
            "These are spaced-repetition reviews of previously learned skills.",
            "Do NOT give hints -- the goal is genuine retrieval from memory.",
        ]

        for i, rq in enumerate(questions):
            days_ago = ""
            if rq.mastery_record and rq.mastery_record.last_practiced:
                delta = (timezone.now() - rq.mastery_record.last_practiced).days
                days_ago = f", last reviewed: {delta} days ago"

            lines.append(f"Q{i+1}: {rq.question_text} (Skill: {rq.skill.name}{days_ago})")
            lines.append(f"Expected answer: {rq.expected_answer} [TUTOR REFERENCE ONLY]")

        lines.append("After each answer, give brief feedback, then transition to today's lesson.")
        lines.append("[/WARMUP RETRIEVAL]")

        return "\n".join(lines)

    def _generate_opening(self) -> TutorMessage:
        """Generate the opening message for the session."""
        # Build student profile from personalization (R11)
        student_profile = self._build_student_profile_block()
        retrieval_block = self._build_retrieval_block()

        # Fallback retrieval context if no personalization
        retrieval_context = ""
        if not retrieval_block:
            retrieval_context = self._get_retrieval_context()

        prompt = f"""Generate an opening message for this tutoring session.

{self.lesson_context}

{student_profile}

{retrieval_block if retrieval_block else f"PREVIOUS KNOWLEDGE TO REVIEW:\\n{retrieval_context}"}

Generate a warm, engaging opening that:
1. Greets the student warmly
2. If retrieval questions are provided above, present one as a warmup activity
3. Otherwise, asks what they already know about today's topic

End with a question. Keep it to 3-4 sentences max."""

        response = self._generate_response(prompt)

        # Save
        self._save_turn("tutor", response)
        self.conversation.append({"role": "assistant", "content": response})
        self._save_state()

        return self._create_message(response)
    
    def _build_student_profile_block(self) -> str:
        """Build [STUDENT PROFILE] context block with mastery data (R11)."""
        try:
            from apps.tutoring.skills_models import Skill, StudentSkillMastery, StudentKnowledgeProfile

            lesson_skills = Skill.objects.filter(lessons=self.lesson)

            lines = ["[STUDENT PROFILE]"]
            lines.append(f"Student: {self.student.first_name or self.student.username}")
            lines.append(f"Current lesson: {self.lesson.title}")

            approaching = []
            needs_work = []
            prereq_gaps = []

            for skill in lesson_skills:
                mastery = StudentSkillMastery.objects.filter(
                    student=self.student, skill=skill
                ).first()

                if mastery:
                    level = mastery.mastery_level
                    if level >= 0.7:
                        approaching.append(f"{skill.name} ({level:.0%})")
                    elif level < 0.5:
                        needs_work.append(f"{skill.name} ({level:.0%})")

            for skill in lesson_skills:
                for prereq in skill.prerequisites.all():
                    mastery = StudentSkillMastery.objects.filter(
                        student=self.student, skill=prereq
                    ).first()
                    if not mastery or mastery.mastery_level < 0.7:
                        level = mastery.mastery_level if mastery else 0.0
                        prereq_gaps.append(f"{prereq.name} ({level:.0%})")

            if approaching:
                lines.append(f"Skills approaching mastery: {', '.join(approaching)}")
            if needs_work:
                lines.append(f"Skills needing work: {', '.join(needs_work)}")
            if prereq_gaps:
                lines.append(f"Prerequisite gaps: {', '.join(prereq_gaps)} -- consider remediation")

            lines.append(f"Session practice score: {self.practice_correct}/{self.practice_total}")

            # Gamification data (R13)
            try:
                profile = StudentKnowledgeProfile.objects.filter(
                    student=self.student,
                    course=self.lesson.unit.course
                ).first()
                if profile:
                    lines.append(f"XP: {profile.total_xp} | Level: {profile.level} | Streak: {profile.current_streak_days} days")
            except Exception:
                pass

            if self._personalization:
                lines.append(f"Pace recommendation: {self._personalization.recommended_pace}")

            lines.append("[/STUDENT PROFILE]")
            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"Failed to build student profile block: {e}")
            return ""

    def _build_worked_example_block(self) -> str:
        """Build [WORKED EXAMPLE] context block for INSTRUCTION phase (R14)."""
        if self.phase != ConversationPhase.INSTRUCTION:
            return ""

        if self.current_topic_index >= len(self.steps):
            return ""

        step = self.steps[self.current_topic_index]
        worked_example = step.get_worked_example() if hasattr(step, 'get_worked_example') else None

        if not worked_example:
            for i in range(max(0, self.current_topic_index - 1), min(len(self.steps), self.current_topic_index + 3)):
                candidate = self.steps[i]
                if candidate.step_type == 'worked_example':
                    worked_example = candidate.get_worked_example() if hasattr(candidate, 'get_worked_example') else None
                    if worked_example:
                        break

        if not worked_example:
            return ""

        lines = [
            "[WORKED EXAMPLE]",
            "Present this worked example BEFORE asking the student to solve a similar problem.",
            "Use labelled subgoals (Step 1, Step 2, etc.).",
        ]

        if worked_example.get('problem'):
            lines.append(f"Problem: {worked_example['problem']}")

        steps_list = worked_example.get('steps', [])
        for s in steps_list:
            step_num = s.get('step', '?')
            action = s.get('action', '')
            explanation = s.get('explanation', '')
            lines.append(f"Step {step_num}: {action}")
            if explanation:
                lines.append(f"  Why: {explanation}")

        if worked_example.get('final_answer'):
            lines.append(f"Final answer: {worked_example['final_answer']}")

        if steps_list:
            random_step = random.choice(range(1, len(steps_list) + 1))
            lines.append(f'After presenting, ask: "What did we do in Step {random_step} and why?"')

        lines.append("Then give a similar problem for guided practice.")
        lines.append("[/WORKED EXAMPLE]")

        return "\n".join(lines)

    def _build_interleaved_practice_block(self) -> str:
        """Build [INTERLEAVED PRACTICE] context block for PRACTICE phase (R6)."""
        if self.phase != ConversationPhase.PRACTICE:
            return ""

        # Use cached block if available
        if self._interleaved_practice_block_cache:
            return self._interleaved_practice_block_cache

        try:
            from apps.tutoring.personalization import InterleavedPracticeService

            service = InterleavedPracticeService(self.student, self.lesson)
            practice_steps = [s for s in self.steps if s.step_type in ('practice', 'quiz')]

            if not practice_steps:
                return ""

            interleaved = service.get_interleaved_questions(
                new_questions=practice_steps,
                review_ratio=0.2
            )

            review_items = [item for item in interleaved if item['type'] == 'review']

            if not review_items:
                return ""

            lines = [
                "[INTERLEAVED PRACTICE]",
                'Weave these review questions naturally into the practice phase (approx 1 review',
                'for every 4 new-topic questions). Introduce them with: "Quick question from',
                'an earlier topic..."',
            ]

            for i, item in enumerate(review_items[:3]):
                step = item['step']
                skill = item.get('skill')
                skill_name = skill.name if skill else "earlier topic"
                lines.append(f"Review Q{i+1}: {step.question} (Skill: {skill_name})")
                lines.append(f"Expected answer: {step.expected_answer} [TUTOR REFERENCE ONLY]")

            lines.append("[/INTERLEAVED PRACTICE]")

            result = "\n".join(lines)
            self._interleaved_practice_block_cache = result
            return result

        except Exception as e:
            logger.warning(f"Failed to build interleaved practice block: {e}")
            return ""

    def _generate_contextual_response(
        self,
        student_input: str,
        kb_context: str,
        media_context: str = "",
        visual_requested: bool = False
    ) -> str:
        """Generate a response based on student input and context."""
        
        # Get current step guidance
        current_guidance = self._get_current_guidance()
        
        # Build phase-specific instructions
        phase_instructions = self._get_phase_instructions()
        
        # Get concept coverage status - CRITICAL for guiding instruction
        concept_coverage = self._get_concept_coverage_summary()
        
        # Get the next uncovered concept to focus on
        next_concept = self._get_next_uncovered_concept()
        
        # Build visual instructions
        visual_instructions = ""
        if media_context:
            visual_instructions = f"""
{media_context}
"""
        elif visual_requested:
            visual_instructions = """
⚠️ VISUAL REQUESTED BUT NOT AVAILABLE:
The student asked for a visual, but no matching image was found.
- Acknowledge their request
- Provide a clear verbal description instead
- Suggest they could sketch it themselves
- Continue with the lesson
"""
        
        # Build enriched context blocks (R11, R14, R6)
        student_profile = self._build_student_profile_block()
        worked_example_block = self._build_worked_example_block()
        interleaved_block = self._build_interleaved_practice_block()

        prompt = f"""CONVERSATION CONTEXT:
{self._format_recent_conversation(5)}

STUDENT JUST SAID: "{student_input}"

LESSON CONTEXT:
{self.lesson_context}

CURRICULUM KNOWLEDGE:
{kb_context}

CURRENT TEACHING GUIDANCE:
{current_guidance}
{visual_instructions}
{worked_example_block}
{concept_coverage}

{next_concept}

{interleaved_block}

PHASE: {self.phase.value.upper()}
{phase_instructions}

{student_profile}

Generate your response following these rules:
1. RESPOND to what the student said (acknowledge their answer)
2. If correct: praise specifically, then advance to the next concept
3. If incorrect: encourage, give a hint, ask a simpler question
4. If confused: simplify, use an example
5. PRIORITIZE teaching uncovered exit ticket concepts
6. If an image is being shown, DESCRIBE WHAT IT ACTUALLY SHOWS - don't make up features
7. Use KEY VOCABULARY terms naturally in your explanation — introduce and define them
8. Watch for COMMON MISTAKES listed in the guidance and address them proactively
9. Weave in local Seychelles context where relevant to make the lesson relatable
10. Use the full HINT LADDER (hint 1 → 2 → 3) for progressive scaffolding — don't jump to the answer
11. END with a question or "Try this:" prompt
12. Keep it concise (2-4 sentences + question)

YOUR RESPONSE:"""

        return self._generate_response(prompt)
    
    def _get_next_uncovered_concept(self) -> str:
        """Get the next uncovered exit ticket concept to focus on."""
        
        # During remediation, prioritize the failed questions
        if getattr(self, 'is_remediation', False) and getattr(self, 'failed_exit_questions', []):
            failed = self.failed_exit_questions
            
            # Find first failed question not yet re-covered
            failed_ids = {fq['id'] for fq in failed}
            uncovered_failed = [
                c for c in self.exit_ticket_concepts 
                if c['id'] in failed_ids and not c.get('covered')
            ]
            
            if uncovered_failed:
                concept = uncovered_failed[0]
                # Find the matching failed question for more context
                failed_q = next((fq for fq in failed if fq['id'] == concept['id']), None)
                
                return f"""🎯 REMEDIATION FOCUS - This is a concept the student got WRONG on the exit ticket:

Question they missed: "{concept['question']}"
Their wrong answer was: "{failed_q.get('student_answer', '?') if failed_q else '?'}"
Correct answer: "{concept['correct_text']}"
Why it's correct: "{concept.get('explanation', 'This is the key concept to understand')}"

IMPORTANT: The student already attempted this and got it wrong. 
- Approach it from a different angle
- Use a new example or analogy
- Break it down into smaller steps
- Check their understanding before moving on

Guide your teaching to help them truly understand this concept!"""
        
        # Normal flow - get any uncovered concept
        uncovered = self._get_uncovered_concepts()
        
        if not uncovered:
            return "All exit ticket concepts have been covered! Focus on reinforcement and practice."
        
        # Get the first uncovered concept
        concept = uncovered[0]
        
        return f"""PRIORITY CONCEPT TO TEACH NEXT:
Question students will face: "{concept['question']}"
Correct answer: "{concept['correct_text']}"
Key understanding needed: "{concept.get('explanation', 'Understand this concept thoroughly')}"

Guide your teaching toward helping the student understand this concept!"""
    
    def _generate_response(self, prompt: str) -> str:
        """Call the LLM to generate a response."""
        if not self.llm_client:
            # Fallback response if no LLM
            return self._fallback_response()
        
        try:
            # Build messages
            messages = [{"role": "user", "content": prompt}]
            
            # Call LLM (max_tokens and temperature come from ModelConfig)
            response = self.llm_client.generate(
                messages=messages,
                system_prompt=self._build_system_prompt(),
            )
            
            return response.content.strip()
            
        except Exception as e:
            logger.error(f"LLM generation failed: {e}")
            return self._fallback_response()
    
    def _fallback_response(self) -> str:
        """Fallback response when LLM is unavailable."""
        fallbacks = [
            "That's interesting! Let me think about that. Can you tell me more about your reasoning?",
            "Good effort! Let's explore this together. What do you think the first step should be?",
            "I see what you're thinking. Let's break this down - what's the key concept here?",
        ]
        return random.choice(fallbacks)

    def _build_system_prompt(self) -> str:
        """Build the system prompt with session-specific context (R9)."""
        institution = self.session.institution
        course = self.lesson.unit.course

        # Get grade level from course or student profile
        grade_level = "secondary school"
        try:
            from apps.accounts.models import StudentProfile
            profile = StudentProfile.objects.filter(user=self.student).first()
            if profile and profile.grade_level:
                grade_level = profile.grade_level
        except Exception:
            pass

        # Build safety prompt
        safety_prompt = "Ensure all interactions are safe and age-appropriate."

        return TUTOR_SYSTEM_PROMPT_TEMPLATE.format(
            institution_name=institution.name if institution else "our school",
            locale_context="Seychelles",
            tutor_name="Tutor",
            language="English",
            grade_level=grade_level,
            safety_prompt=safety_prompt,
        )

    # =========================================================================
    # CONTEXT HELPERS
    # =========================================================================
    
    def _get_knowledge_context(self, student_input: str) -> str:
        """Query knowledge base for relevant context."""
        if not self.knowledge_base:
            return "No additional curriculum context available."
        
        try:
            result = self.knowledge_base.query_for_tutoring(
                lesson=self.lesson,
                student_message=student_input,
                current_topic=self._get_current_topic(),
                n_results=5
            )
            
            if result.chunks:
                context_parts = ["Relevant curriculum content:"]
                for chunk in result.chunks[:3]:
                    context_parts.append(f"- {chunk.get('content', '')[:200]}...")
                return "\n".join(context_parts)
            
            return result.context_summary or "Teaching standard curriculum content."
            
        except Exception as e:
            logger.warning(f"Knowledge base query failed: {e}")
            return "Standard curriculum context."
    
    def _get_retrieval_context(self) -> str:
        """Get context for retrieval practice from previous lessons."""
        try:
            # Get previous lessons in this unit
            previous_lessons = Lesson.objects.filter(
                unit=self.lesson.unit,
                order_index__lt=self.lesson.order_index,
                is_published=True
            ).order_by('-order_index')[:2]
            
            if not previous_lessons:
                return "This is the first lesson in the unit - no previous topics to review."
            
            context_parts = ["Previous topics the student has learned:"]
            for lesson in previous_lessons:
                context_parts.append(f"- {lesson.title}: {lesson.objective}")
            
            return "\n".join(context_parts)
            
        except Exception as e:
            logger.warning(f"Could not get retrieval context: {e}")
            return "Previous topics not available."
    
    def _get_current_guidance(self) -> str:
        """Get guidance from current lesson step."""
        if self.current_topic_index < len(self.steps):
            step = self.steps[self.current_topic_index]

            guidance = f"Current topic: {step.step_type}\n"
            guidance += f"Content: {step.teacher_script[:300]}...\n" if step.teacher_script else ""

            if step.question:
                guidance += f"Suggested question: {step.question}\n"
            if step.expected_answer:
                guidance += f"Expected answer: {step.expected_answer}\n"

            # Full hint ladder
            hints = [h for h in [step.hint_1, step.hint_2, step.hint_3] if h]
            if hints:
                guidance += "Hint ladder (use progressively if student is stuck):\n"
                for j, hint in enumerate(hints, 1):
                    guidance += f"  Hint {j}: {hint}\n"

            # Rubric for grading
            if step.rubric:
                guidance += f"Rubric: {step.rubric[:200]}\n"

            # Answer type and choices
            if step.answer_type and step.answer_type != 'none':
                guidance += f"Answer type: {step.answer_type}\n"
            if step.choices:
                guidance += f"Choices: {step.choices}\n"

            # Educational content for this step
            ed = step.educational_content if isinstance(step.educational_content, dict) else {}

            vocab = ed.get('key_vocabulary', [])
            if vocab:
                terms = []
                for t in vocab:
                    terms.append(t.get('term', str(t)) if isinstance(t, dict) else str(t))
                guidance += f"Key vocabulary: {', '.join(terms)}\n"

            mistakes = ed.get('common_mistakes', [])
            if mistakes:
                items = []
                for m in mistakes:
                    items.append(m.get('mistake', m.get('description', str(m))) if isinstance(m, dict) else str(m))
                guidance += f"Common mistakes: {'; '.join(items)}\n"

            sey_ctx = ed.get('seychelles_context', '')
            if sey_ctx:
                guidance += f"Seychelles context: {sey_ctx[:200]}\n"

            key_points = ed.get('key_points', [])
            if key_points:
                guidance += f"Key points: {'; '.join(str(p) for p in key_points)}\n"

            # Teaching strategies from curriculum context
            cur = step.curriculum_context if isinstance(step.curriculum_context, dict) else {}
            strategies = cur.get('teaching_strategies', [])
            if strategies:
                guidance += f"Teaching strategies: {'; '.join(str(s) for s in strategies)}\n"

            return guidance

        return "All planned topics covered. Move to wrap-up."
    
    def _get_current_topic(self) -> str:
        """Get the current topic being discussed."""
        if self.current_topic_index < len(self.steps):
            step = self.steps[self.current_topic_index]
            return step.teacher_script[:100] if step.teacher_script else self.lesson.title
        return self.lesson.title
    
    def _get_relevant_media(self) -> List[Dict]:
        """Get media relevant to current topic (fallback method)."""
        return self._get_relevant_media_for_response("")
    
    def _format_recent_conversation(self, n: int = 5) -> str:
        """Format recent conversation for context."""
        recent = self.conversation[-(n*2):] if len(self.conversation) > n*2 else self.conversation
        
        formatted = []
        for msg in recent:
            role = "TUTOR" if msg['role'] == 'assistant' else "STUDENT"
            formatted.append(f"{role}: {msg['content']}")
        
        return "\n".join(formatted) if formatted else "Conversation just started."
    
    # =========================================================================
    # PHASE MANAGEMENT
    # =========================================================================
    
    def _get_phase_instructions(self) -> str:
        """Get instructions specific to current phase."""
        # Get uncovered concepts count
        uncovered_count = len(self._get_uncovered_concepts())
        total_concepts = len(self.exit_ticket_concepts)
        
        # Check if we're in remediation mode
        is_remediation = getattr(self, 'is_remediation', False)
        failed_count = len(getattr(self, 'failed_exit_questions', []))
        attempt = getattr(self, 'remediation_attempt', 0)
        
        if is_remediation:
            # Build prerequisite gap context from remediation plan (R5)
            prereq_gap_context = ""
            remediation_plan = getattr(self, '_remediation_plan', None)
            if remediation_plan and remediation_plan.get('prerequisite_gaps'):
                gap_names = [s.name for s in remediation_plan['prerequisite_gaps'][:5]]
                prereq_gap_context = f"""
PREREQUISITE GAPS DETECTED:
The student may be struggling because they have gaps in these prerequisite skills:
{chr(10).join(f'  - {name}' for name in gap_names)}
Address these gaps FIRST before re-teaching the failed concepts.
"""
            # Special remediation instructions
            return f"""
🎯 REMEDIATION MODE (Attempt #{attempt})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The student failed the exit ticket and is reviewing the {failed_count} concepts they got wrong.
{prereq_gap_context}
YOUR GOALS:
1. Focus ONLY on the concepts they missed - don't re-teach everything
2. Use DIFFERENT explanations than before - they didn't understand the first time
3. Use more examples, analogies, and visual descriptions
4. Break concepts into smaller steps
5. Check understanding frequently with mini-questions
6. Be encouraging - failing is part of learning!

PHASE: {self.phase.value.upper()}
Concepts to re-teach: {uncovered_count} remaining

After covering all failed concepts, move to a short practice phase, then back to exit ticket.
Keep the remediation focused and efficient - aim for understanding, not speed."""
        
        # Normal phase instructions
        instructions = {
            ConversationPhase.WARMUP: """
WARMUP PHASE - Goals:
- Build rapport and confidence
- Review one concept from previous learning
- Transition: After 1-2 successful exchanges, move to INTRODUCTION""",
            
            ConversationPhase.INTRODUCTION: f"""
INTRODUCTION PHASE - Goals:
- Hook student interest with real-world relevance
- Find out what they already know
- Preview what they'll learn: There are {total_concepts} key concepts to master
- Transition: After student shows interest/engagement, move to INSTRUCTION""",
            
            ConversationPhase.INSTRUCTION: f"""
INSTRUCTION PHASE - Goals:
- Teach the EXIT TICKET CONCEPTS through questions, not lectures
- Focus on uncovered concepts: {uncovered_count} of {total_concepts} still need to be taught
- Use worked examples that directly relate to exit ticket questions
- Show diagrams/media when available
- Check understanding before moving to next concept
- Transition: After covering key concepts (aim for all {total_concepts}), move to PRACTICE""",
            
            ConversationPhase.PRACTICE: f"""
PRACTICE PHASE - Goals:
- Have student practice problems similar to EXIT TICKET questions
- Concepts still uncovered: {uncovered_count} - prioritize these!
- Scaffold with hints if they struggle
- Track correct/incorrect responses
- Make sure student can answer questions like the ones on the exit ticket
- Transition: After 3-5 practice exchanges AND most concepts covered, move to WRAPUP""",
            
            ConversationPhase.WRAPUP: f"""
WRAPUP PHASE - Goals:
- Have student summarize what they learned
- Review any EXIT TICKET concepts that seem weak
- Concepts covered: {total_concepts - uncovered_count}/{total_concepts}
- Praise their effort specifically
- Prepare them for the exit quiz
- Transition: After student summarizes, move to EXIT_TICKET""",
        }
        
        return instructions.get(self.phase, "Continue the conversation naturally.")
    
    def _maybe_transition_phase(self):
        """Check if we should transition to next phase."""
        
        # Check if we're in remediation mode
        is_remediation = getattr(self, 'is_remediation', False)
        
        if is_remediation:
            # Shorter remediation flow: INSTRUCTION → PRACTICE → EXIT_TICKET
            remediation_transitions = {
                ConversationPhase.INSTRUCTION: (4, ConversationPhase.PRACTICE),  # Shorter instruction
                ConversationPhase.PRACTICE: (3, ConversationPhase.EXIT_TICKET),  # Shorter practice, skip wrapup
            }
            
            if self.phase in remediation_transitions:
                threshold, next_phase = remediation_transitions[self.phase]
                
                # Check if all failed concepts are now covered
                uncovered = self._get_uncovered_concepts()
                
                if self.phase_exchange_count >= threshold:
                    if not uncovered or self.phase == ConversationPhase.PRACTICE:
                        self.phase = next_phase
                        self.phase_exchange_count = 0
                        logger.info(f"Remediation: Transitioned to phase: {self.phase.value}")
            return
        
        # Mastery-based transitions with exchange-count fallbacks (R10)
        # INSTRUCTION → PRACTICE: when 2+ comprehension checks correct (fallback: 8 exchanges)
        # PRACTICE → WRAPUP: when ≥70% accuracy on 3+ questions (fallback: 7 exchanges)
        # Other transitions remain exchange-count based
        fallback_transitions = {
            ConversationPhase.WARMUP: (2, ConversationPhase.INTRODUCTION),
            ConversationPhase.INTRODUCTION: (3, ConversationPhase.INSTRUCTION),
            ConversationPhase.INSTRUCTION: (8, ConversationPhase.PRACTICE),
            ConversationPhase.PRACTICE: (7, ConversationPhase.WRAPUP),
            ConversationPhase.WRAPUP: (2, ConversationPhase.EXIT_TICKET),
        }

        if self.phase not in fallback_transitions:
            return

        fallback_threshold, next_phase = fallback_transitions[self.phase]

        # Special check before exit ticket: ensure concepts are covered
        if next_phase == ConversationPhase.EXIT_TICKET:
            uncovered = self._get_uncovered_concepts()
            if uncovered and self.phase_exchange_count < fallback_threshold + 3:
                logger.info(f"Delaying exit ticket - {len(uncovered)} concepts uncovered")
                return

        # Check mastery-based criteria first (R10)
        mastery_met = False
        if self.phase == ConversationPhase.INSTRUCTION:
            checks_correct = getattr(self, 'instruction_checks_correct', 0)
            if checks_correct >= 2:
                mastery_met = True
                logger.info(f"Mastery transition INSTRUCTION→PRACTICE: {checks_correct} checks correct")
        elif self.phase == ConversationPhase.PRACTICE:
            if self.practice_total >= 3:
                accuracy = self.practice_correct / self.practice_total
                if accuracy >= 0.7:
                    mastery_met = True
                    logger.info(f"Mastery transition PRACTICE→WRAPUP: {accuracy:.0%} accuracy on {self.practice_total} questions")

        # Transition if mastery criteria met OR fallback threshold reached
        if mastery_met or self.phase_exchange_count >= fallback_threshold:
            self.phase = next_phase
            self.phase_exchange_count = 0
            # Reset instruction checks when leaving INSTRUCTION
            if next_phase == ConversationPhase.PRACTICE:
                self.instruction_checks_correct = 0
            logger.info(f"Transitioned to phase: {self.phase.value}")
    
    def _get_uncovered_concepts(self) -> List[Dict]:
        """Get list of exit ticket concepts not yet covered."""
        return [c for c in self.exit_ticket_concepts if not c.get('covered')]
    
    def _get_concept_coverage_summary(self) -> str:
        """Get summary of concept coverage for the LLM."""
        if not self.exit_ticket_concepts:
            return "No exit ticket concepts to track."
        
        total = len(self.exit_ticket_concepts)
        covered = sum(1 for c in self.exit_ticket_concepts if c.get('covered'))
        uncovered = self._get_uncovered_concepts()
        
        summary = f"EXIT CONCEPT COVERAGE: {covered}/{total} covered\n"
        
        if uncovered:
            summary += "UNCOVERED CONCEPTS (prioritize teaching these!):\n"
            for c in uncovered[:3]:  # Show top 3
                summary += f"  - {c['question'][:100]}...\n"
        
        return summary
        
        if self.phase in transitions:
            threshold, next_phase = transitions[self.phase]
            if self.phase_exchange_count >= threshold:
                self.phase = next_phase
                self.phase_exchange_count = 0
                logger.info(f"Transitioned to phase: {self.phase.value}")
    
    # =========================================================================
    # STUDENT ANALYSIS
    # =========================================================================
    
    def _analyze_student_response(self, student_input: str, tutor_response: str):
        """Analyze student response to adapt future instruction and track concept coverage."""
        input_lower = student_input.lower()
        response_lower = tutor_response.lower()
        combined_text = f"{input_lower} {response_lower}"
        
        # Detect confusion
        confusion_signals = ["i don't know", "confused", "don't understand", "help", "?", "not sure", "what"]
        if any(signal in input_lower for signal in confusion_signals):
            current_topic = self._get_current_topic()[:50]
            if current_topic not in self.student_struggles:
                self.student_struggles.append(current_topic)
        
        # Detect success (if tutor praised them)
        success_signals = ["correct", "excellent", "great", "perfect", "well done", "good job", "exactly", "right"]
        if any(signal in response_lower for signal in success_signals):
            self.practice_correct += 1
            current_topic = self._get_current_topic()[:50]
            if current_topic not in self.student_strengths:
                self.student_strengths.append(current_topic)
        
        # Track practice attempts
        if self.phase == ConversationPhase.PRACTICE:
            self.practice_total += 1

        # Track comprehension checks in INSTRUCTION (R10)
        if self.phase == ConversationPhase.INSTRUCTION:
            if any(signal in response_lower for signal in success_signals):
                self.instruction_checks_correct = getattr(self, 'instruction_checks_correct', 0) + 1

        # Record skill practice via SkillAssessmentService (R2)
        try:
            if self.lesson_skills and self.skill_assessment_service:
                current_skill = self._get_current_skill()
                if current_skill:
                    was_correct = any(
                        signal in response_lower
                        for signal in success_signals
                    )
                    self.skill_assessment_service.record_practice(
                        skill=current_skill,
                        was_correct=was_correct,
                        lesson_step=self.steps[self.current_topic_index] if self.current_topic_index < len(self.steps) else None,
                        practice_type='remediation' if self.is_remediation else 'initial',
                        hints_used=0,
                    )
        except Exception as e:
            logger.warning(f"Failed to record skill practice: {e}")

        # CRITICAL: Track exit ticket concept coverage
        # Use LLM-based assessment every 2 exchanges; keyword fallback otherwise (R12)
        if self.exchange_count > 0 and self.exchange_count % 2 == 0:
            self._llm_concept_coverage_check(combined_text)
        else:
            self._keyword_concept_coverage_check(combined_text)

        # Advance topic if making progress
        if self.phase in [ConversationPhase.INSTRUCTION, ConversationPhase.PRACTICE]:
            if self.phase_exchange_count > 0 and self.phase_exchange_count % 2 == 0:
                if self.current_topic_index < len(self.steps) - 1:
                    self.current_topic_index += 1
    
    def _keyword_concept_coverage_check(self, conversation_text: str):
        """
        Check concept coverage using keyword matching (fast fallback for R12).
        """
        conversation_lower = conversation_text.lower()

        for concept in self.exit_ticket_concepts:
            if concept.get('covered'):
                continue  # Already covered

            # Extract keywords from the question and answer
            question_words = set(
                word.lower() for word in re.findall(r'\b\w{4,}\b', concept['question'])
            )
            answer_words = set(
                word.lower() for word in re.findall(r'\b\w{4,}\b', concept.get('correct_text', ''))
            )
            explanation_words = set(
                word.lower() for word in re.findall(r'\b\w{4,}\b', concept.get('explanation', ''))
            )

            # Combine all relevant keywords
            concept_keywords = question_words | answer_words | explanation_words

            # Remove common words
            stop_words = {'this', 'that', 'what', 'which', 'would', 'could', 'should', 'with', 'from', 'have', 'been', 'they', 'their', 'there', 'when', 'where', 'about', 'into', 'more', 'some', 'other'}
            concept_keywords -= stop_words

            # Check how many keywords appear in the conversation
            if concept_keywords:
                matches = sum(1 for kw in concept_keywords if kw in conversation_lower)
                coverage_ratio = matches / len(concept_keywords)

                # Mark as covered if significant overlap (>30% of keywords discussed)
                if coverage_ratio > 0.3 or matches >= 3:
                    concept['covered'] = True
                    logger.info(f"Concept covered (keyword): {concept['question'][:50]}... (match ratio: {coverage_ratio:.1%})")

    def _llm_concept_coverage_check(self, conversation_text: str):
        """
        Use LLM to semantically assess which exit ticket concepts were meaningfully covered (R12).

        Runs every 2 exchanges to manage cost. Falls back to keyword matching on failure.
        """
        uncovered = [c for c in self.exit_ticket_concepts if not c.get('covered')]
        if not uncovered:
            return

        # Build concept list for LLM
        concept_descriptions = []
        for i, concept in enumerate(uncovered):
            concept_descriptions.append(
                f"{i+1}. {concept['question'][:120]}"
            )

        prompt = f"""Analyze whether the following conversation meaningfully covered any of these exit ticket concepts.
A concept is "covered" if the core idea was taught, discussed, or practiced — not just mentioned in passing.

CONVERSATION EXCERPT:
{conversation_text[:1500]}

UNCOVERED CONCEPTS:
{chr(10).join(concept_descriptions)}

Return ONLY a JSON list of concept numbers that were meaningfully covered, e.g. [1, 3].
If none were covered, return [].
"""
        try:
            response = self._generate_response(prompt)
            # Parse the JSON list from the response
            match = re.search(r'\[[\d,\s]*\]', response)
            if match:
                covered_indices = json.loads(match.group())
                for idx in covered_indices:
                    if 1 <= idx <= len(uncovered):
                        uncovered[idx - 1]['covered'] = True
                        logger.info(f"Concept covered (LLM): {uncovered[idx-1]['question'][:50]}...")
        except Exception as e:
            logger.warning(f"LLM concept coverage check failed, using keyword fallback: {e}")
            self._keyword_concept_coverage_check(conversation_text)
    
    # =========================================================================
    # EXIT TICKET
    # =========================================================================
    
    def _handle_exit_ticket(self) -> TutorMessage:
        """Handle exit ticket phase using the pre-selected randomized questions."""
        from apps.tutoring.models import ExitTicket, ExitTicketQuestion

        # Use the pre-selected randomized set from self.exit_ticket_concepts
        if not self.exit_ticket_concepts:
            return self._complete_session()

        # Load full question objects for the selected IDs
        selected_ids = [c['id'] for c in self.exit_ticket_concepts]
        questions = ExitTicketQuestion.objects.filter(id__in=selected_ids)
        q_map = {q.id: q for q in questions}

        # Build exit ticket data preserving the randomized order
        exit_questions = []
        for i, concept in enumerate(self.exit_ticket_concepts):
            q = q_map.get(concept['id'])
            if not q:
                continue
            exit_questions.append({
                'index': i,
                'question': q.question_text,
                'options': [
                    {'letter': 'A', 'text': q.option_a},
                    {'letter': 'B', 'text': q.option_b},
                    {'letter': 'C', 'text': q.option_c},
                    {'letter': 'D', 'text': q.option_d},
                ],
                'correct': q.correct_answer,
            })

        if not exit_questions:
            return self._complete_session()

        exit_data = {
            'questions': exit_questions,
            'total': len(exit_questions),
            'passing_score': 8,
        }
        
        return TutorMessage(
            content="Great work on this lesson! Now let's check your understanding with a quick quiz. Answer all questions, then submit.",
            phase="exit_ticket",
            show_exit_ticket=True,
            exit_ticket_data=exit_data,
        )
    
    def _complete_session(self) -> TutorMessage:
        """Complete the tutoring session."""
        self.phase = ConversationPhase.COMPLETED
        self.session.status = TutorSession.Status.COMPLETED
        self.session.ended_at = timezone.now()
        self.session.mastery_achieved = True
        self._save_state()
        self.session.save()
        
        # Update progress
        progress, _ = StudentLessonProgress.objects.get_or_create(
            student=self.student,
            lesson=self.lesson,
            defaults={'institution': self.session.institution}
        )
        progress.mastery_level = 'mastered'
        progress.save()
        
        return TutorMessage(
            content=f"🎉 Congratulations! You've completed {self.lesson.title}! You showed great understanding. Keep up the excellent work!",
            phase="completed",
            is_complete=True,
        )
    
    def submit_exit_ticket(self, answers: List[str]) -> TutorMessage:
        """Process exit ticket submission using the pre-selected randomized questions."""
        from apps.tutoring.models import ExitTicketQuestion

        if not self.exit_ticket_concepts:
            return self._complete_session()

        # Load the pre-selected questions in the randomized order
        selected_ids = [c['id'] for c in self.exit_ticket_concepts]
        q_map = {q.id: q for q in ExitTicketQuestion.objects.filter(id__in=selected_ids)}
        questions = [q_map[qid] for qid in selected_ids if qid in q_map]
        
        # Grade
        correct = 0
        results = []
        failed_questions = []
        
        for i, q in enumerate(questions):
            student_answer = answers[i].upper() if i < len(answers) else ''
            is_correct = student_answer == q.correct_answer.upper()
            if is_correct:
                correct += 1
            else:
                # Track failed questions for remediation
                failed_questions.append({
                    'id': q.id,
                    'index': i,
                    'question': q.question_text,
                    'student_answer': student_answer,
                    'correct_answer': q.correct_answer,
                    'correct_text': getattr(q, f'option_{q.correct_answer.lower()}', ''),
                    'explanation': q.explanation,
                })
            
            results.append({
                'index': i,
                'question': q.question_text,
                'selected': student_answer,
                'correct_answer': q.correct_answer,
                'is_correct': is_correct,
                'explanation': q.explanation,
            })
        
        passed = correct >= 8
        
        self.session.mastery_achieved = passed
        
        if passed:
            self._save_state()
            return self._complete_session_with_results(results, correct)
        else:
            # FAILED - Start remediation!
            return self._start_remediation(results, correct, failed_questions)
    
    def _complete_session_with_results(self, results: List[Dict], score: int) -> TutorMessage:
        """Complete the session with exit ticket results."""
        self.phase = ConversationPhase.COMPLETED
        self.session.status = TutorSession.Status.COMPLETED
        self.session.ended_at = timezone.now()
        self.session.mastery_achieved = True
        self._save_state()
        self.session.save()
        
        # Update progress
        progress, _ = StudentLessonProgress.objects.get_or_create(
            student=self.student,
            lesson=self.lesson,
            defaults={'institution': self.session.institution}
        )
        progress.mastery_level = 'mastered'
        progress.save()
        
        return TutorMessage(
            content=f"🎉 Excellent! You scored {score}/{len(results)}! You've mastered this lesson!",
            phase="completed",
            is_complete=True,
            exit_ticket_data={'results': results, 'score': score, 'passed': True},
        )
    
    def _start_remediation(
        self, 
        results: List[Dict], 
        score: int, 
        failed_questions: List[Dict]
    ) -> TutorMessage:
        """
        Start targeted remediation for failed exit ticket questions.
        
        This resets the session to instruction phase, but now focused
        specifically on the concepts the student got wrong.
        """
        # Track remediation state
        self.failed_exit_questions = failed_questions
        self.remediation_attempt = getattr(self, 'remediation_attempt', 0) + 1
        self.is_remediation = True

        # Wire RemediationService for targeted remediation plan (R5)
        try:
            from apps.tutoring.personalization import RemediationService
            remediation_service = RemediationService(self.student, self.lesson)
            self._remediation_plan = remediation_service.get_remediation_plan(
                exit_ticket_score=score / len(results) if results else 0,
            )
            if self._remediation_plan.get('prerequisite_gaps'):
                gap_names = [s.name for s in self._remediation_plan['prerequisite_gaps'][:5]]
                logger.info(f"Remediation plan: prerequisite gaps = {gap_names}")
        except Exception as e:
            logger.warning(f"Failed to get remediation plan: {e}")
            self._remediation_plan = None

        # Mark failed concepts as NOT covered (need to re-teach)
        failed_ids = {fq['id'] for fq in failed_questions}
        for concept in self.exit_ticket_concepts:
            if concept['id'] in failed_ids:
                concept['covered'] = False

        # Reset to instruction phase for targeted review
        self.phase = ConversationPhase.INSTRUCTION
        self.phase_exchange_count = 0
        
        # Save state with remediation info
        self._save_state()
        
        # Generate encouraging remediation message
        failed_count = len(failed_questions)
        message = self._generate_remediation_opening(score, len(results), failed_questions)
        
        # Save the message
        self._save_turn("tutor", message)
        self.conversation.append({"role": "assistant", "content": message})
        
        return TutorMessage(
            content=message,
            phase="remediation",
            is_complete=False,
            exit_ticket_data={
                'results': results, 
                'score': score, 
                'passed': False,
                'remediation_started': True,
                'failed_count': failed_count,
            },
        )
    
    def _generate_remediation_opening(
        self, 
        score: int, 
        total: int, 
        failed_questions: List[Dict]
    ) -> str:
        """Generate an encouraging message to start remediation."""
        # Build context about what they got wrong
        failed_topics = []
        for fq in failed_questions[:3]:  # Show first 3
            failed_topics.append(f"- {fq['question'][:80]}...")
        
        prompt = f"""The student just completed the exit ticket but didn't pass.
Score: {score}/{total} (needed 8 to pass)
Attempt number: {self.remediation_attempt}

Questions they got wrong:
{chr(10).join(failed_topics)}

Generate an encouraging message that:
1. Acknowledges their effort positively (no shame!)
2. Explains we'll review the specific concepts they missed
3. Reassures them this is normal - learning takes practice
4. Starts with a question about one of the concepts they missed

Keep it warm and supportive. 3-4 sentences + a question to start the review."""

        return self._generate_response(prompt)
    
    # =========================================================================
    # HELPERS
    # =========================================================================
    
    def _save_turn(self, role: str, content: str):
        """Save a conversation turn."""
        SessionTurn.objects.create(
            session=self.session,
            role=role,
            content=content,
        )
    
    def _create_message(self, content: str, media: List[Dict] = None) -> TutorMessage:
        """Create a TutorMessage from content."""
        return TutorMessage(
            content=content,
            phase=self.phase.value,
            media=media or [],
            expects_response=self.phase != ConversationPhase.COMPLETED,
        )