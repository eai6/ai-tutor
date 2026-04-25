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

from pydantic import BaseModel, Field
from django.utils import timezone
from django.conf import settings

from apps.curriculum.models import Lesson, LessonStep
from apps.tutoring.models import TutorSession, SessionTurn, StudentLessonProgress
from apps.tutoring.grader import check_math_answer, MathCheckResult
from apps.tutoring.praise_filter import strip_praise_if_wrong
from apps.tutoring.validator import validate_tutor_response

logger = logging.getLogger(__name__)


# =============================================================================
# STRUCTURED OUTPUT SCHEMAS
# =============================================================================

class EvaluationResult(BaseModel):
    """LLM-returned evaluation of whether a student answered correctly."""
    correct: bool = Field(description="True if the student answered correctly, False otherwise")


class StepEvaluationResult(BaseModel):
    """Merged evaluator: answer correctness + step completion in one call.

    answer_correct can be None when the student wasn't being asked a
    specific verifiable question — e.g. they're acknowledging a teach
    step, asking a clarifying question, or expressing confusion. None
    means "no evidence either way" and downstream code should treat it
    as a no-op (don't penalize the student, don't advance the streak).
    """
    answer_correct: Optional[bool] = Field(
        default=None,
        description=(
            "True if the student demonstrably answered correctly. "
            "False if the student demonstrably answered incorrectly. "
            "null/None if no specific question with a verifiable answer "
            "was being evaluated (e.g. teach steps, conversational "
            "engagement, clarifying questions, confusion signals)."
        ),
    )
    step_complete: bool = Field(description="Is this step done — ready to advance?")
    reasoning: str = Field(default="", description="Brief explanation (for logging)")


class ConceptCoverageResult(BaseModel):
    """LLM-returned list of exit ticket concept indices that were meaningfully covered."""
    covered_indices: List[int] = Field(
        default_factory=list,
        description="List of 1-based concept numbers that were meaningfully covered, e.g. [1, 3]. Empty if none covered.",
    )


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
- Never present more than 1-2 sentences of explanation without prompting the
  student to respond. Keep each turn under ~60 words -- even a comprehension
  check like "In your own words, what is the first step?"
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
- NEVER re-explain a concept the student already demonstrated understanding of.
  If they got it right, say "Great!" and move on immediately. Do not rephrase
  or summarize what they just proved they know.
- Keep responses SHORT. 2-3 sentences for feedback, 3-5 sentences for teaching.
  Long responses waste time. This is a 20-minute lesson — every exchange counts.
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
- Present ONE idea at a time. One to two sentences maximum per idea.
- Before asking the student to solve a new type of problem, show a WORKED EXAMPLE
  with labelled subgoals (Step 1: ..., Step 2: ..., Step 3: ...).
- Use concrete numbers and visuals before abstract notation.
- Use dual coding: pair verbal explanations with diagrams, number lines, tables,
  or visual representations whenever possible. Use the media catalog IDs to
  display visual aids at the moment they're most useful. See <media_catalog>.
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

<principle id="math_specific">
MATHEMATICS-SPECIFIC TEACHING (apply when subject is Math/Mathematics)
- ALWAYS require students to SHOW WORKING, not just give final answers.
  If they just write "35", ask: "Can you show me how you got 35? Write out each step."
- CHECK INTERMEDIATE STEPS, not just the final answer. A correct answer with wrong
  method means the student doesn't truly understand.
- COMMON MISTAKES: Proactively address typical errors:
  * BIDMAS/order of operations: students add before multiplying
  * Negative numbers: losing the sign during operations
  * Fractions: adding numerators AND denominators (3/4 + 1/2 ≠ 4/6)
  * Algebra: distributing negatives incorrectly
- MULTIPLE APPROACHES: After solving one way, ask "Is there another way to check this?"
- ESTIMATION: Before calculating, ask "Roughly what answer do you expect?" to build number sense
- VISUAL AIDS: Describe number lines, diagrams, and tables in text when no image is available
- WORD PROBLEMS: Help students extract the math from the words:
  "What do we know? What are we looking for? What operation connects them?"
- PROGRESSIVE DIFFICULTY within one concept:
  1. Simple calculation (e.g., 4 × 3)
  2. Same concept, harder numbers (e.g., 4.5 × 3.2)
  3. Word problem context (e.g., "A rectangle is 4.5m by 3.2m. What's the area?")
  4. Reverse problem (e.g., "Area is 14.4m². Width is 3.2m. What's the length?")
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

<principle id="grade_calibration">
CALIBRATE TO STUDENT LEVEL
- You are teaching {grade_level} students. Adapt your tone, vocabulary, and examples
  to match their maturity and expected prior knowledge.
- For senior secondary students (S3-S5), do NOT use primary-school-level analogies
  (e.g., "have you ever split food?") unless the student demonstrates they need them.
- If the step content seems too basic for the student's grade and the student
  demonstrates prior knowledge, acknowledge it, deliver the core concept efficiently,
  and add grade-appropriate depth.
- If the student has completed prior lessons in this unit, you may open with a brief
  diagnostic question to gauge retention before spending time on basics.
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

6. MISCONCEPTION DETECTED (any attempt):
   - If you identify a systematic misconception (not just a careless error), you MUST:
     a. Name the misconception clearly.
     b. Explain WHY the approach fails -- what principle it violates.
     c. Show the correct first step toward the right method.
   - Only THEN ask the student to try again using the correct approach.
   - Do NOT just say "that's a common mistake" and repeat a worked example.

CRITICAL: When a student asks for a hint, NEVER repeat a worked example that has
already been shown. Instead, provide the next-level hint from the HINT LADDER.
If no hints are defined, ask a leading question that narrows the student's thinking
toward the answer.
</feedback_protocol>

<principle id="follow_script">
FOLLOW THE LESSON SCRIPT
- Each lesson has pre-generated steps with specific content, questions, and media.
  The CURRENT TEACHING GUIDANCE in every prompt is your script for THIS exchange.
- For TEACH steps: deliver the provided teaching content using the teacher script.
  Do not paraphrase loosely or skip key points. Explain it clearly, then ask a
  comprehension check.
- For PRACTICE/QUIZ steps: ask the EXACT question provided — do not rephrase it or
  invent your own question. Grade the student's answer against the expected answer.
- For WORKED_EXAMPLE steps: walk through the provided example step by step.
- Do NOT skip ahead to future steps. Do NOT read ahead in the lesson context and
  jump to a later concept. Stay on the current step until it is complete.
- When media is attached to a step, show it using the provided |||MEDIA:N||| signal.
</principle>

<session_structure>
SESSION FLOW
You follow a sequence of lesson steps. Each step has a type and a 5E phase
(engage, explore, explain, practice, evaluate).
Execute the CURRENT STEP DIRECTIVE completely before the system advances you.
For teach steps: deliver the content, ask a comprehension check.
For practice/quiz: ask the exact question, grade the answer.
For worked_example: walk through step by step.
For summary: state key takeaways, confirm understanding.
Do NOT skip ahead or rush. The system controls advancement.
After all steps are complete, the system will trigger the EXIT TICKET.
</session_structure>

<safety>
{safety_prompt}
Keep all content and language age-appropriate for {grade_level} students.
If the student seems distressed, frustrated, or disengaged, pause the lesson
and check in: "Hey, how are you feeling about this? We can slow down or try
a different approach -- no rush."
</safety>

<format_rules>
- STRICT LIMIT: Respond in 1-2 sentences + a question, ~60 words max. If your draft
  is longer, deliver the explanation first, wait for the student to respond, then continue.
- Always end with a question or a prompt for student action.
- Never produce a wall of text.
- Use **bold** for key terms and vocabulary words being introduced.
- When listing steps or comparing items, use a numbered list or bullet points — but
  keep each item to one line.
- Use LaTeX or clear notation for mathematical expressions.
- To show an image, write |||MEDIA:N||| as the VERY LAST line. See <media_catalog>.
- You CAN show images ONLY from the media catalog below using |||MEDIA:N|||.
  Do NOT reference figures, maps, charts, or diagrams that are not in the media catalog.
  Do NOT say "let me show you a map" or "here's a diagram" unless you have a matching |||MEDIA:N||| to display.
  If no visual exists in the catalog, teach with text descriptions instead.
- Do NOT include suggested quick-reply options or response choices in your messages.
  Just ask your question and let the student answer in their own words.
- NEVER say "which of these", "which one of the following", or reference a list of
  options you haven't actually written out. If your question requires choices, either
  list them explicitly or rephrase as an open-ended question instead.
- To show interactive content (data tables, comparison charts, diagrams), wrap HTML in:
  |||ARTIFACT:html|||
  <table style="width:100%;border-collapse:collapse">
    <tr><th style="padding:8px;border:1px solid #ddd">Header</th></tr>
    <tr><td style="padding:8px;border:1px solid #ddd">Data</td></tr>
  </table>
  |||/ARTIFACT|||
  Use this for data tables, comparison charts, or visual aids that enhance understanding.
  Keep HTML simple — use inline styles only, no external resources.
  You CAN include <img> tags referencing uploaded textbook/worksheet figures if available in the media catalog.
  Use artifacts when a table, diagram, or structured visualization would be clearer than plain text.
</format_rules>

</system_prompt>"""


# =============================================================================
# DATA STRUCTURES
# =============================================================================

class SessionState(Enum):
    """Minimal session state — steps are the single source of truth."""
    TUTORING = "tutoring"        # Working through lesson steps
    EXIT_TICKET = "exit_ticket"  # Exit ticket modal active
    COMPLETED = "completed"      # Session finished


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
    
    # Step progress
    step_number: int = 0
    total_steps: int = 0

    # In-conversation gamification
    is_correct: bool = False
    streak_count: int = 0
    practice_score: str = ""
    milestone: Optional[str] = None

    # Rich HTML artifact (rendered in sandboxed iframe)
    artifact_html: Optional[str] = None

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

        # Load enabling objectives for systematic coverage (P1.2)
        self.enabling_objectives = self._load_enabling_objectives()

        # Build lesson context including exit ticket requirements
        self.lesson_context = self._build_lesson_context()
        
        # Load conversation history
        self.conversation = self._load_conversation()
        
        # Load state
        self._load_state()
        
        # Initialize services
        self._llm_client = None
        self._instructor_client = None
        self._instructor_provider = None
        self._knowledge_base = None

        # Skill assessment and personalization (R2, R3)
        self._lesson_skills = None
        self._skill_assessment_service = None
        self._personalization = None
        self._remediation_plan = None
        self._interleaved_practice_block_cache = None

        # Cache student grade level for grade-calibrated delivery (Issue 3)
        self._student_grade_level = self._load_student_grade_level()
    
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
        Select `count` questions from the bank ensuring concept AND format coverage.

        1. Ensure at least 3 different question_types if available (P1.3)
        2. Group by concept_tag, pick one per unique concept
        3. Fill remaining slots randomly from unused questions
        4. Shuffle the final set
        """
        from collections import defaultdict

        selected = []
        used_ids = set()

        # Step 0: Ensure format diversity — pick one per unique question_type (P1.3)
        by_type = defaultdict(list)
        for q in all_questions:
            q_type = getattr(q, 'question_type', 'mcq') or 'mcq'
            by_type[q_type].append(q)

        for q_type, group in by_type.items():
            if len(selected) >= count:
                break
            pick = random.choice(group)
            selected.append(pick)
            used_ids.add(pick.id)

        # Step 1: one per concept tag (from remaining)
        by_tag = defaultdict(list)
        for q in all_questions:
            if q.id in used_ids:
                continue
            tag = q.concept_tag.strip() if q.concept_tag else ''
            if tag:
                by_tag[tag].append(q)

        for tag, group in by_tag.items():
            if len(selected) >= count:
                break
            pick = random.choice(group)
            selected.append(pick)
            used_ids.add(pick.id)

        # Step 2: fill remaining from unused
        remaining = [q for q in all_questions if q.id not in used_ids]
        random.shuffle(remaining)
        for q in remaining:
            if len(selected) >= count:
                break
            selected.append(q)

        random.shuffle(selected)
        return selected[:count]
    
    def _load_enabling_objectives(self) -> List[Dict]:
        """Load enabling objectives for systematic coverage tracking (P1.2).

        Collects objectives from the lesson model and from individual step fields.
        Returns empty list for old lessons without objectives (graceful fallback).
        """
        all_objectives = set()

        # From lesson-level enabling_objectives
        for obj in (self.lesson.enabling_objectives or []):
            if obj and obj.strip():
                all_objectives.add(obj.strip())

        # From step-level enabling_objective fields
        for step in self.steps:
            eo = getattr(step, 'enabling_objective', '')
            if eo and eo.strip():
                all_objectives.add(eo.strip())

        return [{'objective': obj, 'covered': False} for obj in sorted(all_objectives)]

    def _load_student_grade_level(self) -> str:
        """Load student's grade level from profile for grade-calibrated delivery."""
        try:
            from apps.accounts.models import StudentProfile
            profile = StudentProfile.objects.filter(user=self.student).first()
            if profile and profile.grade_level:
                return profile.grade_level
        except Exception:
            pass
        return ""

    def _load_state(self):
        """Load session state (backward compatible with old phase-based state)."""
        state = self.session.engine_state or {}

        # Load session_state — backward compat: map old phase values
        state_str = state.get('session_state', state.get('phase', 'tutoring'))
        if state_str in ('warmup', 'introduction', 'instruction', 'practice', 'wrapup'):
            self.session_state = SessionState.TUTORING
        elif state_str == 'exit_ticket':
            self.session_state = SessionState.EXIT_TICKET
        elif state_str == 'completed':
            self.session_state = SessionState.COMPLETED
        else:
            try:
                self.session_state = SessionState(state_str)
            except ValueError:
                self.session_state = SessionState.TUTORING

        self.exchange_count = state.get('exchange_count', 0)
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
        self._failed_eos = state.get('failed_eos', [])

        # Track whether last answer was correct
        self.last_answer_correct = state.get('last_practice_correct', False)

        # Review mode flag (P4-2)
        self.is_review = state.get('is_review', False)

        # Media deduplication (P2)
        self.shown_media_urls = set(state.get('shown_media_urls', []))

        # Concept-boundary gating
        self.concept_boundary_attempts = state.get('concept_boundary_attempts', 0)

        # Step-level exchange tracking
        self.step_exchange_count = state.get('step_exchange_count', 0)

        # Turn media for resume (artifact panel)
        self._turn_media = state.get('turn_media', {})

        # Worked example deduplication (Issue 1)
        self.shown_worked_example_indices = set(state.get('shown_worked_example_indices', []))

        # Difficulty signal (ZPD adjustment)
        self.difficulty_level = state.get('difficulty_level', 0)

        # Correct-answer streak for in-conversation gamification
        self._correct_streak = state.get('correct_streak', 0)

        # Cognitive load adaptation
        self.cognitive_load = state.get('cognitive_load', 0.5)
        self.consecutive_wrong = state.get('consecutive_wrong', 0)
        self.consecutive_correct_streak = state.get('consecutive_correct_streak', 0)

        # Bare-answer count per step (M9 — pedagogy layer 4). Keys stringified
        # because JSON object keys are strings; we coerce on read.
        raw_bare_counts = state.get('bare_answer_counts_by_step', {}) or {}
        self.bare_answer_counts_by_step: Dict[int, int] = {
            int(k): int(v) for k, v in raw_bare_counts.items()
        }

        # Restore exit concept coverage status
        covered_concept_ids = state.get('covered_concept_ids', [])
        for concept in self.exit_ticket_concepts:
            concept['covered'] = concept['id'] in covered_concept_ids

        # Restore enabling objective coverage (P1.2)
        covered_objectives = set(state.get('covered_objectives', []))
        for obj in getattr(self, 'enabling_objectives', []):
            obj['covered'] = obj['objective'] in covered_objectives
    
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
            'session_state': self.session_state.value,
            'display_phase': self._get_display_phase(),
            'exchange_count': self.exchange_count,
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
            'failed_eos': getattr(self, '_failed_eos', []),
            # Track whether last answer was correct
            'last_practice_correct': getattr(self, 'last_answer_correct', False),
            # Review mode flag (P4-2)
            'is_review': getattr(self, 'is_review', False),
            # Media deduplication (P2)
            'shown_media_urls': list(getattr(self, 'shown_media_urls', set())),
            # Concept-boundary gating
            'concept_boundary_attempts': getattr(self, 'concept_boundary_attempts', 0),
            # Step-level exchange tracking
            'step_exchange_count': getattr(self, 'step_exchange_count', 0),
            # Turn media for resume (artifact panel)
            'turn_media': getattr(self, '_turn_media', {}),
            # Worked example deduplication (Issue 1)
            'shown_worked_example_indices': list(getattr(self, 'shown_worked_example_indices', set())),
            # Covered enabling objectives (for competency report)
            'covered_enabling_objectives': [
                o['objective'] for o in getattr(self, 'enabling_objectives', []) if o.get('covered')
            ],
            # Difficulty signal (ZPD adjustment)
            'difficulty_level': getattr(self, 'difficulty_level', 0),
            # Correct-answer streak for in-conversation gamification
            'correct_streak': getattr(self, '_correct_streak', 0),
            # Cognitive load adaptation
            'cognitive_load': getattr(self, 'cognitive_load', 0.5),
            'consecutive_wrong': getattr(self, 'consecutive_wrong', 0),
            'consecutive_correct_streak': getattr(self, 'consecutive_correct_streak', 0),
            # Bare-answer counts per step (M9). JSON requires string keys.
            'bare_answer_counts_by_step': {
                str(k): v for k, v in getattr(self, 'bare_answer_counts_by_step', {}).items()
            },
            # Enabling objective coverage (P1.2)
            'covered_objectives': [
                o['objective'] for o in getattr(self, 'enabling_objectives', [])
                if o.get('covered')
            ],
        }
        self.session.save()
    
    def _load_conversation(self) -> List[Dict]:
        """Load conversation history from session turns.

        Strips legacy [SHOW_MEDIA:...] and new |||MEDIA:N||| tags from
        historical content so signal pollution doesn't leak into LLM prompts.
        """
        turns = SessionTurn.objects.filter(
            session=self.session
        ).order_by('created_at')

        _tag_re = re.compile(
            r'\[SHOW_MEDIA\s*:[^\]]*\]|\|\|\|MEDIA\s*:\s*\d+\s*\|\|\||\|\|\|GENERATE\s*:\s*\w+\s*:.+?\|\|\|',
            re.IGNORECASE,
        )

        conversation = []
        for turn in turns:
            role = "assistant" if turn.role == 'tutor' else "user"
            content = _tag_re.sub('', turn.content).strip()
            conversation.append({
                "role": role,
                "content": content,
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
            "LESSON OVERVIEW (for reference — follow the CURRENT STEP DIRECTIVE, not this overview):",
        ]

        # Collect educational materials across all steps
        all_vocabulary = []
        all_common_mistakes = []
        all_seychelles_context = []

        # Check if steps have concept_tags
        has_concept_tags = any(
            getattr(s, 'concept_tag', '') for s in self.steps
        )

        # Extract key concepts from steps — concept-grouped if tags exist
        if has_concept_tags:
            blocks = self._get_concept_blocks()
            for block in blocks:
                tag = block['tag']
                if tag:
                    context_parts.append(f"  --- Concept: {tag} ---")
                for idx in block['step_indices']:
                    step = self.steps[idx]
                    content_preview = step.teacher_script[:200] if step.teacher_script else ""
                    hints = [h for h in [step.hint_1, step.hint_2, step.hint_3] if h]
                    label = step.step_type.upper()
                    if step.step_type == 'practice':
                        question = step.question[:100] if step.question else content_preview[:100]
                        context_parts.append(f"  {idx+1}. [PRACTICE] {question}...")
                        if step.expected_answer:
                            context_parts.append(f"      Expected: {step.expected_answer}")
                        if hints:
                            context_parts.append(f"      Hints: {' → '.join(h[:80] for h in hints)}")
                    elif step.step_type == 'worked_example':
                        context_parts.append(f"  {idx+1}. [EXAMPLE] {content_preview}...")
                    else:
                        context_parts.append(f"  {idx+1}. [{label}] {content_preview}...")
        else:
            # Flat list for legacy lessons without concept_tags
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

        # Gather educational materials from all steps
        for step in self.steps:
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

                config = ModelConfig.get_for('tutoring')
                if config:
                    self._llm_client = get_llm_client(config)
            except Exception as e:
                logger.error(f"Could not load LLM client: {e}")
        return self._llm_client

    @property
    def instructor_client(self):
        """Lazy load instructor-wrapped client for structured LLM output."""
        if self._instructor_client is None:
            try:
                import instructor
                from apps.llm.models import ModelConfig

                config = ModelConfig.get_for('tutoring')
                if config:
                    PROVIDER_MAP = {
                        'anthropic': 'anthropic',
                        'openai': 'openai',
                        'google': 'google',
                        'local_ollama': 'ollama',
                    }
                    provider = PROVIDER_MAP.get(config.provider, config.provider)
                    self._instructor_provider = config.provider  # store for max_tokens handling
                    self._instructor_client = instructor.from_provider(
                        f"{provider}/{config.model_name}",
                        api_key=config.get_api_key(),
                    )
            except Exception as e:
                logger.error(f"Could not load instructor client: {e}")
        return self._instructor_client

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
        if self.session_state == SessionState.COMPLETED and not self.is_review:
            return TutorMessage(
                content="You've already completed this lesson! Great work!",
                phase="completed",
                is_complete=True,
            )

        # Generate a "welcome back" message with step directive so LLM can reference media
        last_exchange = self.conversation[-1] if self.conversation else None
        current_guidance = self._get_current_guidance()
        media_catalog = self._build_media_catalog()

        prompt = f"""The student is returning to continue the lesson.

Last message in conversation: {last_exchange['content'][:200] if last_exchange else 'None'}

{current_guidance}

{media_catalog}

Generate a brief, warm welcome back message that:
1. Acknowledges they're returning
2. Briefly reminds them where they were
3. Asks a question to re-engage them
4. If media is available for the current step, reference the image and write |||MEDIA:N||| as the LAST line

Keep it to 1-2 sentences + question, ~60 words max."""

        response = self._generate_response(prompt, fallback_context="resume")

        # Parse |||MEDIA:N||| or |||GENERATE:...||| signal BEFORE saving — keeps DB clean
        clean_response, parsed_media, gen_request = self._parse_media_signal(response)
        clean_response, artifact_html = self._parse_artifact_signal(clean_response)
        media = [parsed_media] if parsed_media else []

        # On-the-fly image generation via safety pipeline
        if not media and gen_request:
            generated = self._safe_generate_image(gen_request['category'], gen_request['description'])
            if generated:
                media = [generated]

        # Fallback: if resume message references visual content but no signal emitted
        if not media:
            visual_refs = ['diagram', 'figure', 'image', 'picture', 'illustration', 'chart', 'graph', 'map']
            if any(ref in clean_response.lower() for ref in visual_refs):
                step_media = self._get_step_media()
                if step_media:
                    media = [step_media[0]]
                    logger.info(f"Auto-attached step media on resume (visual reference fallback)")

        # Record media for this turn (for resume artifact panel)
        if media:
            turn_index = len(self.conversation)  # index before appending
            self._turn_media[str(turn_index)] = media[0]

        # Don't persist fallback messages — they pollute conversation history (Fix 2)
        if not self._last_response_was_fallback:
            self._save_turn("tutor", clean_response)
            self.conversation.append({"role": "assistant", "content": clean_response})
            self._save_state()

        return self._create_message(clean_response, media=media, artifact_html=artifact_html)

    def start_review(self) -> TutorMessage:
        """Start a review session for a completed lesson.

        Re-activates the session so chat_respond allows messages through,
        uses RemediationService to identify weak skills, and starts an
        instruction-phase remediation flow.
        """
        # Bug fix: re-activate so chat_respond doesn't block with "already complete"
        self.session.status = TutorSession.Status.ACTIVE
        self.session.ended_at = None

        # Use remediation system for targeted review
        self.session_state = SessionState.TUTORING
        self.practice_correct = 0
        self.practice_total = 0
        self.last_answer_correct = False
        self.is_review = True
        self.is_remediation = True

        # Use RemediationService to identify weak skills
        weak_skills = []
        try:
            from apps.tutoring.personalization import RemediationService
            remediation_service = RemediationService(self.student, self.lesson)
            self._remediation_plan = remediation_service.get_remediation_plan(
                exit_ticket_score=0.8,  # They passed, reviewing for mastery
            )
            if self._remediation_plan and self._remediation_plan.get('weak_skills'):
                weak_skills = [s.name for s in self._remediation_plan['weak_skills'][:5]]
            elif self._remediation_plan and self._remediation_plan.get('prerequisite_gaps'):
                weak_skills = [s.name for s in self._remediation_plan['prerequisite_gaps'][:5]]
        except Exception as e:
            logger.warning(f"Failed to get remediation plan for review: {e}")
            self._remediation_plan = None

        self._save_state()
        self.session.save()

        # Generate a targeted opening message
        content = self._generate_review_opening(weak_skills)
        self._save_turn("tutor", content)
        self.conversation.append({"role": "assistant", "content": content})

        return self._create_message(content)

    def _generate_review_opening(self, weak_skills: list) -> str:
        """Generate a review opening that references specific weak areas."""
        if weak_skills:
            skills_text = ", ".join(weak_skills[:3])
            prompt = f"""The student has completed this lesson and is returning to review it.
They want to strengthen their understanding.

Lesson: {self.lesson.title}
Areas to focus on: {skills_text}

Generate a warm, encouraging opening that:
1. Welcomes them back for review
2. Mentions the specific areas we'll focus on: {skills_text}
3. Starts with a question about one of these areas

Keep it to 2-3 sentences. Be specific about what we'll review."""
        else:
            prompt = f"""The student has completed this lesson and is returning to review it.

Lesson: {self.lesson.title}

Generate a warm, encouraging opening that:
1. Welcomes them back for review
2. Says we'll go through the key concepts again
3. Starts with a question about the main topic

Keep it to 2-3 sentences."""

        return self._generate_response(prompt)

    def respond(self, student_input: str) -> TutorMessage:
        """
        Generate a response to student input.

        This is the main conversation loop.
        Media selection: LLM signals via |||MEDIA:N||| tail-line, parsed before saving.
        """
        self._step_just_advanced = False

        # H3: short-circuit when group session is awaiting teacher approval.
        # See memory/group_lessons_v2_plan.md.
        if self.session.group_approval_status == TutorSession.GroupApprovalStatus.PENDING:
            return TutorMessage(
                content=(
                    "⏳ Your group session is awaiting teacher approval. "
                    "Please wait while your teacher reviews — once approved, "
                    "I'll be ready to start. If you want to continue solo, "
                    "have the other students leave the session."
                ),
                phase="awaiting_approval",
                is_complete=False,
            )

        # Track lesson start time on first interaction
        if not self.session.started_lesson_at:
            self.session.started_lesson_at = timezone.now()
            self.session.save(update_fields=['started_lesson_at'])

        # Save student message
        self._save_turn("student", student_input)
        self.conversation.append({"role": "user", "content": student_input})

        # Update counts
        self.exchange_count += 1
        self.step_exchange_count += 1

        # Check if exit ticket phase
        if self.session_state == SessionState.EXIT_TICKET:
            return self._handle_exit_ticket()

        # Check if student is requesting a visual
        visual_request = self._detect_visual_request(student_input)

        # Get curriculum context from knowledge base
        kb_context = self._get_knowledge_context(student_input)

        # Deterministic math check BEFORE response generation (Layer 1 + 2 of
        # math-tutor false-positive fix). When the expected answer is numeric
        # and the student's reply parses as a number, we compute correctness
        # up-front and inject a signal into the system prompt so the LLM
        # cannot hallucinate praise for a wrong answer. See
        # memory/math_tutor_fix_plan.md.
        self._pending_math_check = self._deterministic_math_check(student_input)
        # Bare-answer detection (M9 / Layer 4). Tracked even when the
        # deterministic check didn't produce a verdict so metadata is
        # consistent for teacher review.
        self._pending_bare_answer = False
        if self._pending_math_check is not None:
            try:
                is_math_step = self.lesson.unit.course.is_math
            except Exception:
                is_math_step = False
            step_type = ''
            if self.current_topic_index < len(self.steps):
                step_type = self.steps[self.current_topic_index].step_type or ''
            if is_math_step and step_type in ('practice', 'quiz'):
                self._pending_bare_answer = self._is_bare_math_answer(student_input)
                if self._pending_bare_answer:
                    self.bare_answer_counts_by_step[self.current_topic_index] = (
                        self.bare_answer_counts_by_step.get(self.current_topic_index, 0) + 1
                    )
            self._pending_math_student_input = student_input
            logger.info(
                "[MathCheck] session=%s step=%s is_correct=%s bare=%s student=%s expected=%s",
                self.session.id,
                self.current_topic_index,
                self._pending_math_check.is_correct,
                self._pending_bare_answer,
                self._pending_math_check.student_parsed,
                self._pending_math_check.expected_parsed,
            )
        else:
            self._pending_math_student_input = None

        # Generate response — LLM picks media via |||MEDIA:N||| tail-line signal
        response = self._generate_contextual_response(
            student_input,
            kb_context,
            media_context="",
            visual_requested=bool(visual_request)
        )

        # Cache the math check for analyze/persistence, then clear the
        # per-turn signal so it cannot leak into later generations this turn
        # (e.g. exit-ticket intro, fallback responses).
        turn_math_check = self._pending_math_check
        turn_math_student_input = getattr(self, '_pending_math_student_input', student_input)
        turn_bare_answer = getattr(self, '_pending_bare_answer', False)
        self._pending_math_check = None
        self._pending_math_student_input = None
        self._pending_bare_answer = False

        # Parse |||MEDIA:N||| or |||GENERATE:...||| signal BEFORE saving — keeps DB clean
        clean_response, parsed_media, gen_request = self._parse_media_signal(response)
        clean_response, artifact_html = self._parse_artifact_signal(clean_response)

        # Verify calculations in math responses
        if self.lesson.unit.course.is_math:
            from apps.tutoring.math_tools import verify_calculations
            clean_response, corrections = verify_calculations(clean_response)
            if corrections:
                logger.info(f"[MathCheck] Fixed {len(corrections)} calculation(s) in response")

        # Post-generation praise filter (Layer 3). Defense-in-depth: strip
        # praise when the deterministic math check said wrong OR when the
        # student gave a bare answer (no working). For bare answers, praise
        # must be withheld regardless of numeric correctness — per
        # math_teaching Rule 1, there is no confirmation without working.
        praise_stripped = False
        should_strip = (
            (turn_math_check is not None and turn_math_check.is_correct is False)
            or turn_bare_answer
        )
        if should_strip:
            clean_response, praise_stripped = strip_praise_if_wrong(
                clean_response, is_correct=False,
            )
            if praise_stripped:
                logger.info(
                    "[MathCheck] Praise filter triggered session=%s step=%s bare=%s",
                    self.session.id, self.current_topic_index, turn_bare_answer,
                )

        media = [parsed_media] if parsed_media else []

        # On-the-fly image generation via safety pipeline
        if not media and gen_request:
            generated = self._safe_generate_image(gen_request['category'], gen_request['description'])
            if generated:
                media = [generated]

        # Fallback: if tutor references visual content but no signal emitted
        if not media:
            visual_refs = ['diagram', 'figure', 'image', 'picture', 'illustration', 'chart', 'graph', 'map']
            if any(ref in clean_response.lower() for ref in visual_refs):
                step_media = self._get_step_media()
                if step_media:
                    media = [step_media[0]]

        # Analyze student response for adaptation (returns metadata dict).
        turn_metadata = self._analyze_student_response(
            student_input, clean_response, math_check=turn_math_check,
        )
        if praise_stripped:
            turn_metadata['praise_stripped'] = True
        if turn_bare_answer:
            turn_metadata['bare_answer'] = True
            step_bare_count = self.bare_answer_counts_by_step.get(
                self.current_topic_index, 0,
            )
            turn_metadata['bare_answer_count_for_step'] = step_bare_count
            if step_bare_count >= 3:
                turn_metadata['bare_answer_flagged'] = True

        # Socratic validator V1 — universal praise gate + structural checks.
        # Runs AFTER the math-specific praise filter so it acts as a
        # catch-all for non-math subjects where the math check didn't fire.
        # See memory/socratic_validator_plan.md.
        current_step = (
            self.steps[self.current_topic_index]
            if self.current_topic_index < len(self.steps)
            else None
        )
        validation = validate_tutor_response(
            clean_response,
            is_correct=turn_metadata.get('is_correct'),
            bare_answer=turn_bare_answer,
            step_type=(current_step.step_type if current_step else None),
            lesson=self.lesson,
            llm_client=self.llm_client,
        )
        if validation.content != clean_response:
            clean_response = validation.content
        if validation.issues:
            turn_metadata['validator_issues'] = list(validation.issues)
            turn_metadata['validator_passed'] = validation.passed
        # Always attach fact-check metadata (counts + unverified claims)
        # so the teacher dashboard can show it even on clean turns.
        for k in (
            'factual_claims_checked', 'factual_claims_unverified',
            'factual_claims_contradicted', 'fact_check_skipped',
            'fact_check_skip_reason',
        ):
            if k in validation.metadata:
                turn_metadata[k] = validation.metadata[k]

        # Record media for this turn (for resume artifact panel)
        if media:
            turn_index = len(self.conversation)  # index before appending
            self._turn_media[str(turn_index)] = media[0]

        # Check if all steps complete — trigger exit ticket (NOT during remediation)
        if (self.current_topic_index >= len(self.steps)
                and self.session_state == SessionState.TUTORING
                and not getattr(self, 'is_remediation', False)):
            self.session_state = SessionState.EXIT_TICKET
            self._save_state()
            # Save tutor response first, then return exit ticket
            self._save_turn("tutor", clean_response, metadata=turn_metadata)
            self.conversation.append({"role": "assistant", "content": clean_response})
            return self._handle_exit_ticket()

        # Remediation: check if all failed concepts re-covered
        if getattr(self, 'is_remediation', False) and self._remediation_steps_complete():
            self.session_state = SessionState.EXIT_TICKET
            self._save_state()
            self._save_turn("tutor", clean_response, metadata=turn_metadata)
            self.conversation.append({"role": "assistant", "content": clean_response})
            return self._handle_exit_ticket()

        # Remediation safety valve: force exit ticket after 15 remediation exchanges
        if getattr(self, 'is_remediation', False) and self.exchange_count >= 15:
            self.session_state = SessionState.EXIT_TICKET
            self._save_state()
            self._save_turn("tutor", clean_response, metadata=turn_metadata)
            self.conversation.append({"role": "assistant", "content": clean_response})
            return self._handle_exit_ticket()

        # Save state
        self._save_state()

        # Save CLEAN tutor response (no signal tags in DB)
        self._save_turn("tutor", clean_response, metadata=turn_metadata)
        self.conversation.append({"role": "assistant", "content": clean_response})

        return self._create_message(clean_response, media=media, artifact_html=artifact_html)

    def _prepare_response(self, student_input: str) -> Optional[Dict]:
        """
        Shared pre-generation logic for respond() and respond_stream().

        Saves student turn, updates counts, builds prompt context.
        Returns context dict, or None if exit_ticket phase.
        """
        self._step_just_advanced = False

        # Track lesson start time on first interaction
        if not self.session.started_lesson_at:
            self.session.started_lesson_at = timezone.now()
            self.session.save(update_fields=['started_lesson_at'])

        # Save student message
        self._save_turn("student", student_input)
        self.conversation.append({"role": "user", "content": student_input})

        # Update counts
        self.exchange_count += 1
        self.step_exchange_count += 1

        # Check if student is requesting a visual
        visual_request = self._detect_visual_request(student_input)

        # Get curriculum context from knowledge base
        kb_context = self._get_knowledge_context(student_input)

        # Deterministic math check BEFORE response generation (mirrors
        # respond()). Keeps streaming path consistent if it is re-enabled.
        self._pending_math_check = self._deterministic_math_check(student_input)
        self._pending_math_student_input = student_input if self._pending_math_check else None
        self._pending_bare_answer = False
        if self._pending_math_check is not None:
            try:
                is_math_step = self.lesson.unit.course.is_math
            except Exception:
                is_math_step = False
            step_type = ''
            if self.current_topic_index < len(self.steps):
                step_type = self.steps[self.current_topic_index].step_type or ''
            if is_math_step and step_type in ('practice', 'quiz'):
                self._pending_bare_answer = self._is_bare_math_answer(student_input)
                if self._pending_bare_answer:
                    self.bare_answer_counts_by_step[self.current_topic_index] = (
                        self.bare_answer_counts_by_step.get(self.current_topic_index, 0) + 1
                    )

        # Exit ticket is handled separately (non-streamable)
        if self.session_state == SessionState.EXIT_TICKET:
            return None

        # No pre-selected media — LLM picks via |||MEDIA:N||| in its output
        return {
            'student_input': student_input,
            'kb_context': kb_context,
            'media_context': '',
            'visual_requested': bool(visual_request),
            'media': [],
        }

    def _finalize_response(self, full_response: str, student_input: str, media: List[Dict]) -> Dict:
        """
        Shared post-generation logic for respond() and respond_stream().

        Parses |||MEDIA:N||| signal from LLM output, resolves media, then runs
        post-processing (concept tracking, state save).
        Returns metadata dict including clean_content for the done chunk.
        """
        # Cache + clear per-turn math check (signal must not leak to other
        # generations in the same turn).
        turn_math_check = self._pending_math_check
        turn_math_student_input = getattr(self, '_pending_math_student_input', student_input)
        turn_bare_answer = getattr(self, '_pending_bare_answer', False)
        self._pending_math_check = None
        self._pending_math_student_input = None
        self._pending_bare_answer = False

        # Parse |||MEDIA:N||| or |||GENERATE:...||| signal BEFORE saving — keeps DB clean
        clean_content, parsed_media, gen_request = self._parse_media_signal(full_response)

        # Verify calculations in math responses
        if self.lesson.unit.course.is_math:
            from apps.tutoring.math_tools import verify_calculations
            clean_content, corrections = verify_calculations(clean_content)
            if corrections:
                logger.info(f"[MathCheck] Fixed {len(corrections)} calculation(s) in response")

        # Post-generation praise filter (Layer 3, streaming parity).
        praise_stripped = False
        should_strip = (
            (turn_math_check is not None and turn_math_check.is_correct is False)
            or turn_bare_answer
        )
        if should_strip:
            clean_content, praise_stripped = strip_praise_if_wrong(
                clean_content, is_correct=False,
            )
            if praise_stripped:
                logger.info(
                    "[MathCheck] Praise filter triggered (stream) session=%s step=%s bare=%s",
                    self.session.id, self.current_topic_index, turn_bare_answer,
                )

        # Media from LLM signal, with fallback for phantom references
        media = [parsed_media] if parsed_media else []

        # On-the-fly image generation via safety pipeline
        if not media and gen_request:
            generated = self._safe_generate_image(gen_request['category'], gen_request['description'])
            if generated:
                media = [generated]

        # Fallback: if tutor references visual content but no signal was emitted,
        # auto-attach the current step's media to avoid "look at the diagram" with no diagram
        if not media:
            visual_refs = ['diagram', 'figure', 'image', 'picture', 'illustration', 'chart', 'graph', 'map']
            if any(ref in clean_content.lower() for ref in visual_refs):
                step_media = self._get_step_media()
                if step_media:
                    media = [step_media[0]]
                    logger.info(f"Auto-attached step media (visual reference fallback): {step_media[0].get('alt', '')[:50]}")

        # Record media for this turn (for resume artifact panel)
        if media:
            turn_index = len(self.conversation)  # index before appending
            self._turn_media[str(turn_index)] = media[0]

        # Analyze student response for adaptation (returns metadata dict)
        turn_metadata = self._analyze_student_response(
            student_input, clean_content, math_check=turn_math_check,
        )
        if praise_stripped:
            turn_metadata['praise_stripped'] = True
        if turn_bare_answer:
            turn_metadata['bare_answer'] = True
            step_bare_count = self.bare_answer_counts_by_step.get(
                self.current_topic_index, 0,
            )
            turn_metadata['bare_answer_count_for_step'] = step_bare_count
            if step_bare_count >= 3:
                turn_metadata['bare_answer_flagged'] = True

        # Socratic validator V1 (streaming parity).
        current_step = (
            self.steps[self.current_topic_index]
            if self.current_topic_index < len(self.steps)
            else None
        )
        validation = validate_tutor_response(
            clean_content,
            is_correct=turn_metadata.get('is_correct'),
            bare_answer=turn_bare_answer,
            step_type=(current_step.step_type if current_step else None),
            lesson=self.lesson,
            llm_client=self.llm_client,
        )
        if validation.content != clean_content:
            clean_content = validation.content
        if validation.issues:
            turn_metadata['validator_issues'] = list(validation.issues)
            turn_metadata['validator_passed'] = validation.passed
        for k in (
            'factual_claims_checked', 'factual_claims_unverified',
            'factual_claims_contradicted', 'fact_check_skipped',
            'fact_check_skip_reason',
        ):
            if k in validation.metadata:
                turn_metadata[k] = validation.metadata[k]

        # Check if all steps complete — trigger exit ticket (NOT during remediation)
        show_exit_ticket = False
        exit_ticket = None
        if (self.current_topic_index >= len(self.steps)
                and self.session_state == SessionState.TUTORING
                and not getattr(self, 'is_remediation', False)):
            self.session_state = SessionState.EXIT_TICKET
            show_exit_ticket = True

        # Remediation: check if all failed concepts re-covered
        if (not show_exit_ticket and getattr(self, 'is_remediation', False)
                and self._remediation_steps_complete()):
            self.session_state = SessionState.EXIT_TICKET
            show_exit_ticket = True

        # Remediation safety valve
        if (not show_exit_ticket and getattr(self, 'is_remediation', False)
                and self.exchange_count >= 15):
            self.session_state = SessionState.EXIT_TICKET
            show_exit_ticket = True

        if show_exit_ticket:
            et_msg = self._handle_exit_ticket()
            exit_ticket = et_msg.exit_ticket_data
            show_exit_ticket = et_msg.show_exit_ticket

        # Save state
        self._save_state()

        # Save CLEAN tutor response (no signal tags in DB)
        self._save_turn("tutor", clean_content, metadata=turn_metadata)
        self.conversation.append({"role": "assistant", "content": clean_content})

        step_num = min(self.current_topic_index + 1, len(self.steps)) if self.steps else 0
        total = len(self.steps)
        return {
            'phase': self._get_display_phase(),
            'media': media,
            'clean_content': clean_content,
            'show_exit_ticket': show_exit_ticket,
            'exit_ticket': exit_ticket,
            'is_complete': self.session_state == SessionState.COMPLETED,
            'step_number': step_num,
            'total_steps': total,
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

        # Build the prompt (shared with _generate_contextual_response)
        visual_instructions = ""
        if ctx['media_context']:
            visual_instructions = f"\n{ctx['media_context']}\n"
        elif ctx['visual_requested']:
            visual_instructions = (
                "\n⚠️ VISUAL REQUESTED BUT NOT AVAILABLE:\n"
                "The student asked for a visual, but no matching image was found.\n"
                "- Acknowledge their request\n"
                "- Provide a clear verbal description instead\n"
                "- Continue with the lesson\n"
            )

        prompt = self._build_response_prompt(
            ctx['student_input'], ctx['kb_context'], visual_instructions
        )

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
            "content": metadata.get('clean_content', full_content),
            **metadata,
        })

    def _get_proactive_media(self) -> List[Dict]:
        """Get media that would proactively help with current topic.

        Gated by phase and exchange cadence to avoid showing images
        too frequently or during practice/assessment phases.
        Note: first-exchange-on-step media is handled upstream in respond()
        and _finalize_response(), so this method only handles cadence-based
        proactive media.
        """
        # Only show proactive media during tutoring
        if self.session_state != SessionState.TUTORING:
            return []

        # Trigger on odd exchanges within a step (1st, 3rd, 5th, ...)
        if self.step_exchange_count % 2 != 1:
            return []

        if self.current_topic_index >= len(self.steps):
            return []

        step = self.steps[self.current_topic_index]

        if not step.media or 'images' not in step.media:
            return []

        media = []
        topic_terms = self._extract_topic_terms()
        if not topic_terms:
            return []

        for img in step.media['images'][:3]:
            if not img.get('url'):
                continue

            img_description = f"{img.get('alt', '')} {img.get('caption', '')}".lower()

            # Compute numeric relevance: fraction of topic terms that match
            matches = sum(1 for term in topic_terms if term in img_description)
            relevance = matches / len(topic_terms)

            if relevance >= 0.3:
                media.append({
                    'type': 'image',
                    'url': img['url'],
                    'alt': img.get('alt', ''),
                    'caption': img.get('caption', ''),
                    'description': img.get('alt', '') or img.get('caption', ''),
                })
                break  # One proactive image per exchange is enough

        return media

    def _get_step_media(self) -> List[Dict]:
        """Get all media for the current step. No relevance filtering."""
        step_media_ids = getattr(self, '_step_media_ids', {}).get(self.current_topic_index, [])
        media_id_map = getattr(self, '_media_id_map', {})
        return [media_id_map[mid] for mid in step_media_ids if mid in media_id_map]

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

    def _deduplicate_media(self, media: List[Dict]) -> List[Dict]:
        """Remove media already shown in this session."""
        unique = []
        for m in media:
            url = m.get('url', '')
            if url and url not in self.shown_media_urls:
                unique.append(m)
                self.shown_media_urls.add(url)
        return unique

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
    
    def _safe_generate_image(self, category: str, description: str) -> Optional[Dict]:
        """Disabled — tutor only uses pre-attached step media (teacher-reviewed).
        On-the-fly generation was unreliable and created broken figure references."""
        return None

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
            visual_need = self._determine_visual_need(response)
            if visual_need:
                generated = self._safe_generate_image('diagram', visual_need)
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

        # Include step directive + media catalog so LLM can reference media
        current_guidance = self._get_current_guidance()
        media_catalog = self._build_media_catalog()

        # Determine if student has actual prior knowledge (Fix 3)
        has_prior = bool(retrieval_block) or (
            retrieval_context
            and "first lesson" not in retrieval_context.lower()
            and "not available" not in retrieval_context.lower()
        )

        if has_prior:
            prior_instruction = (
                "3. Briefly recall 1-2 key concepts from earlier lessons "
                "that today's topic builds on, to activate the student's memory"
            )
        else:
            prior_instruction = (
                "3. This is the student's first lesson on this topic — do NOT "
                "reference prior lessons. Instead, connect the topic to everyday "
                "experiences the student can relate to"
            )

        prompt = f"""Generate an opening message for this tutoring session.

{self.lesson_context}

{student_profile}

{retrieval_block if retrieval_block else f"PREVIOUS KNOWLEDGE TO REVIEW:\\n{retrieval_context}"}

{current_guidance}

{media_catalog}

Generate a warm, engaging opening that:
1. Greets the student warmly
2. Clearly states today's learning objective so the student knows what they will learn
{prior_instruction}
4. If retrieval questions are provided above, present one as a warmup activity
5. Otherwise, present a brief warm-up question related to today's topic
6. If media is available for this step, reference the image in your text and write |||MEDIA:N||| as the LAST line

End with a question. Keep it to 2-3 sentences max.
IMPORTANT: Any question you ask must be complete and self-contained. Never say "which of these" or reference options/choices you haven't listed."""

        response = self._generate_response(prompt, fallback_context="opening")

        # Parse |||MEDIA:N||| or |||GENERATE:...||| signal BEFORE saving — keeps DB clean
        clean_response, parsed_media, gen_request = self._parse_media_signal(response)
        clean_response, artifact_html = self._parse_artifact_signal(clean_response)
        media = [parsed_media] if parsed_media else []

        # On-the-fly image generation via safety pipeline
        if not media and gen_request:
            generated = self._safe_generate_image(gen_request['category'], gen_request['description'])
            if generated:
                media = [generated]

        # Fallback: if opening references visual content but no signal emitted
        if not media:
            visual_refs = ['diagram', 'figure', 'image', 'picture', 'illustration', 'chart', 'graph', 'map']
            if any(ref in clean_response.lower() for ref in visual_refs):
                step_media = self._get_step_media()
                if step_media:
                    media = [step_media[0]]
                    logger.info(f"Auto-attached step media in opening (visual reference fallback)")

        # Record media for this turn (for resume artifact panel)
        if media:
            turn_index = len(self.conversation)  # index before appending
            self._turn_media[str(turn_index)] = media[0]

        # Save
        self._save_turn("tutor", clean_response)
        self.conversation.append({"role": "assistant", "content": clean_response})
        self._save_state()

        return self._create_message(clean_response, media=media, artifact_html=artifact_html)

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

    def _build_enabling_objectives_block(self) -> str:
        """Build [ENABLING OBJECTIVES] status block for the response prompt (P1.2)."""
        if not self.enabling_objectives:
            return ""

        covered = sum(1 for o in self.enabling_objectives if o['covered'])
        total = len(self.enabling_objectives)
        lines = []
        for i, obj in enumerate(self.enabling_objectives):
            status = "COVERED" if obj['covered'] else "NOT YET COVERED"
            lines.append(f"  EO{i+1}. [{status}] {obj['objective']}")

        return f"""[ENABLING OBJECTIVES] {covered}/{total} covered
{chr(10).join(lines)}
Prioritize uncovered objectives in your teaching. Ensure each is explicitly addressed before the session ends.
[/ENABLING OBJECTIVES]"""

    def _build_teacher_guidance_block(self) -> str:
        """Build context block with active teacher/monitor guidance."""
        from apps.tutoring.models import TeacherGuidance
        guidances = TeacherGuidance.objects.filter(
            session=self.session, is_active=True
        ).order_by('-created_at')[:3]

        if not guidances:
            return ""

        lines = []
        for g in guidances:
            source = "MONITOR AI" if g.is_from_ai else "TEACHER"
            lines.append(f"[{source}]: {g.message}")

        return (
            "\n\n[TEACHER/MONITOR GUIDANCE — follow these instructions]:\n"
            + "\n".join(lines)
            + "\nApply this guidance in your next response.\n"
        )

    def _build_difficulty_signal_block(self) -> str:
        """Build [DIFFICULTY ADJUSTMENT] + [COGNITIVE LOAD] context blocks.

        Combines:
        1. difficulty_level (-2 to +2) — explicit student signal (ZPD)
        2. cognitive_load (0.0-1.0) — implicit load from correctness/confusion patterns

        Returns an empty string when both are neutral.
        """
        blocks = []

        # --- Explicit difficulty signal (ZPD) ---
        level = getattr(self, 'difficulty_level', 0)
        if level >= 1:
            intensity = "moderately" if level == 1 else "significantly"
            blocks.append(f"""[DIFFICULTY ADJUSTMENT]
The student has signaled this material is TOO EASY for them (confidence level: {level}/2).
Apply the expertise_reversal principle {intensity}:
- SKIP intermediate explanation steps — do NOT ask the student to explain basic sub-steps they clearly understand.
- Go straight to independent practice with NO worked examples or hints unless they ask.
- If the student answers correctly, do NOT ask follow-up comprehension questions on the same point — move forward.
- Offer HARDER variants: more complex numbers, multi-step problems, or edge cases.
- Use concise, peer-level language — no over-scaffolding.
- If they get it right on the first try, acknowledge briefly and advance: "Solid. Let's try something harder."
CRITICAL: When the student answers correctly, do NOT revert to asking them to explain intermediate steps. Trust their demonstrated competence.
[/DIFFICULTY ADJUSTMENT]""")
        elif level <= -1:
            intensity = "moderately" if level == -1 else "significantly"
            blocks.append(f"""[DIFFICULTY ADJUSTMENT]
The student has signaled this material is TOO HARD for them (struggle level: {abs(level)}/2).
Apply the cognitive_load and targeted_remediation principles {intensity}:
- Break the current concept into SMALLER sub-steps than the directive specifies.
- Use a fully worked example BEFORE asking the student to try independently.
- Use simpler numbers, shorter problems, and more concrete/real-world examples.
- Provide MORE scaffolding: after each sub-step, check understanding before proceeding.
- If they struggle, offer a hint proactively rather than waiting for a second attempt.
- Be extra encouraging — normalize difficulty: "This is a tough one. Let's break it down together."
- Consider whether a prerequisite gap is the real issue.
[/DIFFICULTY ADJUSTMENT]""")

        # --- Implicit cognitive load signal ---
        load = getattr(self, 'cognitive_load', 0.5)
        if load >= 0.7:
            blocks.append(
                "\n[COGNITIVE LOAD: HIGH — Student is struggling]\n"
                "- Use SIMPLER language and shorter sentences\n"
                "- Break the current concept into SMALLER steps\n"
                "- Give a worked example BEFORE asking them to try\n"
                "- Provide hints IMMEDIATELY, don't wait for a second attempt\n"
                "- Be extra encouraging — acknowledge effort, not just correctness\n"
                "- If they've been wrong 3+ times, try a COMPLETELY different approach\n"
            )
        elif load <= 0.3:
            blocks.append(
                "\n[COGNITIVE LOAD: LOW — Student is doing well]\n"
                "- Challenge them with harder variations\n"
                "- Skip worked examples — go straight to practice\n"
                "- Ask them to explain their reasoning, not just give answers\n"
                "- Introduce connections to other concepts\n"
            )

        return "\n".join(blocks)

    def _build_worked_example_block(self) -> str:
        """Build [WORKED EXAMPLE] context block for teach/worked_example steps (R14).

        Tracks which step indices have already had their worked example presented
        to prevent the LLM from repeating the same example verbatim.
        """
        step = self.steps[self.current_topic_index] if self.current_topic_index < len(self.steps) else None
        if not step or step.step_type not in ('teach', 'worked_example'):
            return ""

        if self.current_topic_index >= len(self.steps):
            return ""

        # Skip if this step's worked example was already presented
        if self.current_topic_index in self.shown_worked_example_indices:
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

        # Mark this step's worked example as shown
        self.shown_worked_example_indices.add(self.current_topic_index)

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
        """Build [INTERLEAVED PRACTICE] context block for practice/quiz steps (R6)."""
        step = self.steps[self.current_topic_index] if self.current_topic_index < len(self.steps) else None
        if not step or step.step_type not in ('practice', 'quiz'):
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

    def _build_hint_request_block(self, student_input: str) -> str:
        """Detect explicit hint requests and return a graduated hint instruction.

        Hint level escalates based on step_exchange_count:
        - 1st hint request → Level 1: leading question / nudge
        - 2nd hint request → Level 2: partial step / structured hint
        - 3rd+ hint request → Level 3: full scaffold (but not full answer)
        """
        hint_keywords = [
            'hint', 'help me', "i'm stuck", "i am stuck", "don't understand",
            "do not understand", 'clue', 'guide me', 'confused', 'not sure how',
            'can you help', 'show me how', "don't get it", "don't know how",
        ]
        input_lower = student_input.lower()
        if not any(kw in input_lower for kw in hint_keywords):
            return ""

        # Determine hint level from exchange count on this step
        if self.step_exchange_count <= 1:
            level = 1
            level_desc = "a leading question or nudge that points toward the answer"
        elif self.step_exchange_count <= 3:
            level = 2
            level_desc = "a partial step or structured hint (e.g., 'Try converting X to Y')"
        else:
            level = 3
            level_desc = "a full scaffold showing the method step by step, but still ask the student to compute the final answer"

        return (
            f"\nHINT REQUEST DETECTED: The student explicitly asked for help.\n"
            f"Provide HINT LEVEL {level}: {level_desc}.\n"
            f"Do NOT repeat a worked example that has already been shown.\n"
            f"Do NOT give the full answer directly.\n"
            f"If a HINT LADDER is defined above, use hint {level} from it.\n"
            f"If no hints are defined, provide a leading question that narrows "
            f"the student's thinking toward the answer.\n"
        )

    def _build_response_prompt(
        self,
        student_input: str,
        kb_context: str,
        visual_instructions: str = "",
    ) -> str:
        """Build the LLM user prompt for generating a tutoring response.

        Shared by _generate_contextual_response() and respond_stream()
        to prevent the two copies from diverging.
        """
        # During remediation, override step guidance with EO-focused instructions
        if getattr(self, 'is_remediation', False):
            failed_eos = getattr(self, '_failed_eos', [])
            eo_list = "\n".join(f"  - {eo}" for eo in failed_eos) if failed_eos else "  - (review general concepts)"
            current_guidance = (
                f"REMEDIATION: The student scored below 8/10 on the exit ticket.\n"
                f"Focus on these enabling objectives they got wrong:\n{eo_list}\n\n"
                f"For each EO: re-teach with a DIFFERENT explanation than before, "
                f"give a new example, then ask a check question.\n"
                f"Do NOT repeat the original lesson steps. Use fresh approaches."
            )
        else:
            current_guidance = self._get_current_guidance()
        step_phase_instructions = self._get_step_phase_instructions()
        concept_coverage = self._get_concept_coverage_summary()
        next_concept = self._get_next_uncovered_concept() if not getattr(self, 'is_remediation', False) else ""
        student_profile = self._build_student_profile_block()
        difficulty_block = self._build_difficulty_signal_block()
        worked_example_block = self._build_worked_example_block()
        interleaved_block = self._build_interleaved_practice_block()
        enabling_obj_block = self._build_enabling_objectives_block()

        # Teacher/Monitor AI guidance
        guidance_block = self._build_teacher_guidance_block()

        # Detect explicit hint requests and inject graduated hint instruction
        hint_block = self._build_hint_request_block(student_input)

        # Step progress indicator
        if getattr(self, 'is_remediation', False):
            failed_eos = getattr(self, '_failed_eos', [])
            step_progress = f"REMEDIATION MODE | Reviewing {len(failed_eos)} enabling objective(s)"
        else:
            step_num = min(self.current_topic_index + 1, len(self.steps))
            total_steps = len(self.steps)
            display_phase = self._get_display_phase().upper()
            step_progress = f"STEP PROGRESS: {step_num}/{total_steps} | Phase: {display_phase}"

        # Build media reminder — always present so LLM never claims it can't show images
        media_reminder = ""
        step_media_ids = getattr(self, '_step_media_ids', {}).get(self.current_topic_index, [])
        if step_media_ids:
            media_reminder = (
                f"\n14. MEDIA AVAILABLE for this step — show it by writing "
                f"|||MEDIA:{step_media_ids[0]}||| as the VERY LAST line of your response"
            )
        else:
            media_reminder = (
                "\n14. Only show images from the media catalog using |||MEDIA:N|||. "
                "Do NOT reference figures or diagrams that are not in the catalog."
            )

        return f"""CONVERSATION CONTEXT:
{self._format_recent_conversation(5)}

STUDENT JUST SAID: "{student_input}"

LESSON CONTEXT:
{self.lesson_context}

CURRICULUM KNOWLEDGE:
{kb_context}

CURRENT STEP DIRECTIVE (follow this exactly):
{current_guidance}
{visual_instructions}
{worked_example_block}
{hint_block}
{concept_coverage}

{next_concept}

{interleaved_block}

{step_progress}
{step_phase_instructions}

{student_profile}

{difficulty_block}

{enabling_obj_block}
{guidance_block}
Generate your response following these rules:
1. EXECUTE the CURRENT STEP DIRECTIVE above — deliver its content, ask its question, or walk through its example
2. Do NOT skip ahead, invent your own questions, or deviate from the current step
3. For PRACTICE/QUIZ steps: ask the EXACT question provided, then grade the answer
4. RESPOND to what the student said (acknowledge their answer)
5. If correct: praise specifically, then continue the current step or prepare for the next
6. If incorrect: encourage, give a hint from the HINT LADDER, ask again
7. If confused: simplify, use an example from the step content
8. If the student asks to see an image/figure/diagram, show one using |||MEDIA:N||| ONLY if one exists in the catalog. Otherwise describe it in text.
9. Use KEY VOCABULARY terms naturally in your explanation — introduce and define them
10. Watch for COMMON MISTAKES listed in the directive and address them proactively
11. Weave in local Seychelles context where relevant to make the lesson relatable
12. END with a question or "Try this:" prompt
13. Keep it concise (1-2 sentences + question, ~60 words max){media_reminder}

YOUR RESPONSE:"""

    def _generate_contextual_response(
        self,
        student_input: str,
        kb_context: str,
        media_context: str = "",
        visual_requested: bool = False
    ) -> str:
        """Generate a response based on student input and context."""
        visual_instructions = ""
        if media_context:
            visual_instructions = f"\n{media_context}\n"
        elif visual_requested:
            visual_instructions = (
                "\n⚠️ VISUAL REQUESTED BUT NOT AVAILABLE:\n"
                "The student asked for a visual, but no matching image was found.\n"
                "- Acknowledge their request\n"
                "- Provide a clear verbal description instead\n"
                "- Continue with the lesson\n"
            )

        prompt = self._build_response_prompt(student_input, kb_context, visual_instructions)
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

        return f"""UPCOMING EXIT TICKET CONCEPT (for awareness):
Question students will face: "{concept['question']}"
Correct answer: "{concept['correct_text']}"
Key understanding needed: "{concept.get('explanation', 'Understand this concept thoroughly')}"

Follow the current step; this concept will be covered in sequence."""
    
    def _generate_response(self, prompt: str, fallback_context: str = "conversation") -> str:
        """Call the LLM to generate a response."""
        self._last_response_was_fallback = False

        if not self.llm_client:
            logger.warning(
                f"No LLM client available for session={self.session.id} "
                f"lesson='{self.lesson.title}'"
            )
            return self._fallback_response(fallback_context)

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
            logger.error(
                f"LLM generation failed for session={self.session.id} "
                f"lesson='{self.lesson.title}': {e}",
                exc_info=True,
            )
            return self._fallback_response(fallback_context)

    def _fallback_response(self, context: str = "conversation") -> str:
        """Context-aware fallback when LLM is unavailable.

        The tutor must LEAD — fallbacks present concrete questions from
        lesson content, never ask open-ended "what do you know?" questions.
        """
        self._last_response_was_fallback = True

        if context == "opening":
            question = self._get_opening_fallback_question()
            return (
                f"Welcome! Before we start {self.lesson.title}, "
                f"let's review what you already know — {question}"
            )
        elif context == "resume":
            question = self._get_resume_fallback_question()
            return (
                f"Welcome back! Let's continue with {self.lesson.title}. "
                f"Let us review what we covered last time — {question}"
            )
        else:
            fallbacks = [
                "Let's work through this step by step. Try this: what is the first thing you notice?",
                "Let me help you think through this. Start by identifying the key information given.",
                "Let's break this down. What operation or method do you think applies here?",
            ]
            return random.choice(fallbacks)

    def _get_opening_fallback_question(self) -> str:
        """Get a practice question from early steps for opening fallback."""
        for step in self.steps[:5]:
            if step.step_type in ('practice', 'quiz') and step.question:
                return step.question
        if self.steps and self.steps[0].teacher_script:
            return f"what do you think {self.lesson.title} is about?"
        return f"what comes to mind when you hear '{self.lesson.title}'?"

    def _get_resume_fallback_question(self) -> str:
        """Get a review question from already-covered steps for resume fallback."""
        for i in range(min(self.current_topic_index, len(self.steps)) - 1, -1, -1):
            step = self.steps[i]
            if step.step_type in ('practice', 'quiz') and step.question:
                return step.question
        return f"can you explain in your own words what we learned about {self.lesson.title} so far?"

    def _build_system_prompt(self) -> str:
        """Build the system prompt with session-specific context (R9)."""
        from collections import defaultdict
        from apps.llm.prompts import get_active_prompt_pack

        institution = self.session.institution
        course = self.lesson.unit.course

        # Get grade level from course or student profile
        grade_level = "secondary school"
        personality_prompt = None
        try:
            from apps.accounts.models import StudentProfile
            profile = StudentProfile.objects.select_related('tutor_personality').filter(user=self.student).first()
            if profile and profile.grade_level:
                grade_level = profile.grade_level
            if profile and profile.tutor_personality and profile.tutor_personality.is_active:
                personality_prompt = profile.tutor_personality.system_prompt_modifier
        except Exception:
            pass

        # Build safety prompt — use PromptPack override if set
        safety_prompt = "Ensure all interactions are safe and age-appropriate."
        institution_id = institution.id if institution else None
        prompt_pack = get_active_prompt_pack(institution_id)
        if prompt_pack and prompt_pack.safety_prompt and prompt_pack.safety_prompt.strip():
            safety_prompt = prompt_pack.safety_prompt

        template_vars = defaultdict(str, {
            'institution_name': institution.name if institution else "our school",
            'locale_context': "Seychelles",
            'tutor_name': "Tutor",
            'language': "English",
            'grade_level': grade_level,
            'safety_prompt': safety_prompt,
        })

        # Use custom tutor_system_prompt if set in PromptPack
        template = TUTOR_SYSTEM_PROMPT_TEMPLATE
        if prompt_pack and prompt_pack.tutor_system_prompt and prompt_pack.tutor_system_prompt.strip():
            template = prompt_pack.tutor_system_prompt

        system_prompt = template.format_map(template_vars)

        # Inject tutor personality modifier if student has one selected
        if personality_prompt:
            system_prompt += f"\n\n<personality>\n{personality_prompt}\n</personality>"

        # Universal Socratic rules — apply to every subject, not just math.
        # See memory/socratic_validator_plan.md.
        system_prompt += (
            "\n\n<socratic_rules>"
            "\nThese rules apply to EVERY subject (not just math):"
            "\n"
            "\n1. NEVER praise an answer until you've seen the student's reasoning."
            "\n   Words like 'brilliant', 'perfect', 'exactly right', 'you got it',"
            "\n   'great job', 'spot on', 'well done', 'excellent' are forbidden when"
            "\n   the student gave a one-line answer with no explanation. Instead"
            "\n   ask 'What makes you think that?' or 'Walk me through how you got"
            "\n   there.'"
            "\n"
            "\n2. ASK A QUESTION. Almost every turn should END with a question that"
            "\n   moves the student forward. Rare exception: a wrap-up turn after"
            "\n   the student has demonstrated mastery."
            "\n"
            "\n3. ONE NEW IDEA AT A TIME. Do not list 5 facts in a single turn."
            "\n   Introduce one concept, ask the student to engage with it, then"
            "\n   layer the next concept based on their response."
            "\n"
            "\n4. BE HONEST WHEN YOU'RE UNCERTAIN. If you're stating a specific"
            "\n   number, comparison, or named fact, only do so when it's grounded"
            "\n   in the curriculum context provided to you below. If you're not"
            "\n   sure, say so and ask the student to look it up — never invent."
            "\n</socratic_rules>"
        )

        # Append math-specific instructions
        if self.lesson.unit.course.is_math:
            system_prompt += (
                "\n\n<math_teaching>"
                "\nThis is a MATHEMATICS lesson. Math is a subskill discipline — every"
                "\nproblem is a stack of small skills. Your job is to make those skills"
                "\nvisible, practiced, and mastered one at a time."
                "\n"
                "\n=== RULE 1 (NON-NEGOTIABLE): WORKING BEFORE EVALUATION ==="
                "\nNEVER evaluate a student's answer without seeing their working first."
                "\nIf the student replies with a bare answer (e.g. '35', 'x=4', 'yes'),"
                "\nyou MUST refuse to confirm or deny it. Instead, respond:"
                "\n  'Before I check that — show me your working. Write each step you took.'"
                "\nDo NOT say 'correct', 'right', 'good answer', 'well done', or 'no, that's"
                "\nwrong' until the student has shown their working. This rule overrides"
                "\nany urge to be encouraging. A confirmed answer with hidden working"
                "\ntrains a guess; a checked working trains a skill."
                "\n"
                "\nThe ONLY exception: a one-step mental-math drill the tutor explicitly"
                "\nframed as 'just the answer' (e.g. flash-card warmup). Otherwise, working first."
                "\n"
                "\n=== RULE 2: NAME THE SUBSKILLS ==="
                "\nFor every problem, explicitly identify the subskills involved before"
                "\nthe student attempts it. Format:"
                "\n  'This problem uses 3 subskills: (1) substitution, (2) BIDMAS,"
                "\n   (3) simplifying fractions. Let's go through each.'"
                "\nIf the student gets a problem wrong, diagnose WHICH subskill failed"
                "\nand isolate it: give a simpler problem that only exercises that subskill,"
                "\nthen return to the original."
                "\n"
                "\n=== RULE 3: GIVE TIPS, NOT ANSWERS ==="
                "\nBefore (or during) a step, surface a short, named TIP — a strategy or"
                "\nmemory hook the student can reuse. Format:"
                "\n  'Tip: when multiplying fractions, multiply tops × tops, bottoms × bottoms."
                "\n   No common denominator needed.'"
                "\nTips are 1–2 lines. They go BEFORE the student attempts a new subskill,"
                "\nnot after. Repeat the tip if the student forgets it."
                "\n"
                "\n=== RULE 4: BUILD COMPLEXITY IN RUNGS ==="
                "\nWithin one concept, ladder up only after the current rung is solid:"
                "\n  Rung 1: simplest calculation (whole numbers, one step)"
                "\n  Rung 2: harder numbers (decimals, fractions, negatives)"
                "\n  Rung 3: word problem with Seychelles context"
                "\n  Rung 4: reverse/inverse problem"
                "\nDo NOT skip rungs for a struggling student. Do NOT linger on Rung 1"
                "\nfor a student who showed mastery — say so and move them up."
                "\n"
                "\n=== NOTATION ==="
                "\n- Use LaTeX for ALL math: $\\frac{1}{2}$, $3x + 5 = 20$"
                "\n- Display math on own line: $$\\frac{1}{4} + \\frac{1}{2} = \\frac{3}{4}$$"
                "\n- ALWAYS use \\frac{}{} for fractions, never plain text '1/2'"
                "\n- Show EVERY line of working in worked examples — never skip steps"
                "\n- Before calculating, ask for an ESTIMATE: 'Roughly what do you expect?'"
                "\n"
                "\n=== COMMON MISTAKES TO WATCH FOR ==="
                "\n- BIDMAS: adding before multiplying (3 + 4 × 2 = 14 instead of 11)"
                "\n- Negatives: losing signs (-3 × -2 = -6 instead of 6)"
                "\n- Fractions: adding tops AND bottoms (1/2 + 1/3 ≠ 2/5)"
                "\n- Algebra: not distributing negatives (-(x+3) = -x+3 instead of -x-3)"
                "\n- Percentages: confusing 'of' with 'is' (20% of 50 vs 20% is...)"
                "\nWhen any of these appear in student work, name the misconception"
                "\nexplicitly so the student can label it themselves next time."
                "\n"
                "\n=== WORD PROBLEMS ==="
                "\n- Help extract the math: 'What do we know? What do we need? What connects them?'"
                "\n- Use Seychelles context: fishing catches, tourism revenue, island distances"
                "\n"
                "\n=== WHEN STUDENT IS WRONG ==="
                "\n- Ask 'Walk me through your steps' before correcting (see Rule 1)"
                "\n- Pinpoint WHICH subskill failed, not just that the answer is wrong"
                "\n- Drop one rung: give a simpler problem isolating that subskill"
                "\n- Once the subskill is solid, return to the original problem"
                "\n</math_teaching>"
            )

        # Append group-session block when there is more than one active
        # participant (G3). The tutor addresses the group collectively.
        system_prompt += self._build_group_session_block()

        # Append Seychelles context library (P1.5)
        system_prompt += self._build_seychelles_context_block()

        # Append media catalog so the LLM knows what figures are available
        system_prompt += self._build_media_catalog()

        # Inject deterministic math evaluation signal if the pre-response
        # check produced a definite result. Must go LAST so it is the final
        # thing the LLM reads before generating (highest-salience position).
        pending_check = getattr(self, '_pending_math_check', None)
        if pending_check is not None:
            student_input = getattr(self, '_pending_math_student_input', '') or ''
            bare = getattr(self, '_pending_bare_answer', False)
            bare_count = self.bare_answer_counts_by_step.get(
                self.current_topic_index, 0,
            )
            system_prompt += self._build_math_eval_signal_block(
                pending_check,
                student_input,
                bare_answer=bare,
                bare_answer_count_for_step=bare_count,
            )

        return system_prompt

    def _build_group_session_block(self) -> str:
        """Render group-session addressing block when the session has more
        than one active participant.

        See memory/group_lessons_plan.md.
        """
        try:
            if not self.session.is_group:
                return ""
            students = list(self.session.active_students.values_list("username", flat=True))
        except Exception:
            return ""
        if len(students) < 2:
            return ""
        names = ", ".join(students)
        return (
            "\n\n<group_session>"
            f"\nThis is a GROUP session with {len(students)} students: {names}."
            "\nAddress them as a group (\"everyone\", \"all of you\", \"you all\") or"
            " by name when appropriate."
            "\nThey share one device and answer as a collective — do not ask"
            " individual students to take turns unless they explicitly"
            " coordinate it themselves. Their answers represent the group's"
            " shared thinking. When someone is confused, encourage a peer"
            " to explain in their own words."
            "\n</group_session>"
        )

    def _build_seychelles_context_block(self) -> str:
        """Build Seychelles context library block for the system prompt (P1.5)."""
        try:
            from apps.curriculum.models import SeychellesContext
            course = self.lesson.unit.course
            subject = (course.title.split()[0] if course else '').lower()

            entries = SeychellesContext.objects.filter(is_active=True)
            if subject:
                entries = entries.filter(subject_tags__contains=subject)
            entries = list(entries.values('category', 'title', 'content')[:10])

            if not entries:
                return ""

            lines = "\n".join(
                f"- [{e['category'].upper()}] {e['title']}: {e['content']}"
                for e in entries
            )
            return (
                f"\n\n<seychelles_context>\n"
                f"Use these verified Seychelles facts when relevant. Do NOT invent local data.\n"
                f"{lines}\n"
                f"</seychelles_context>"
            )
        except Exception as e:
            logger.warning(f"Failed to build Seychelles context block: {e}")
            return ""

    def _build_media_catalog(self) -> str:
        """Build a numbered catalog of available media for the LLM.

        Populates self._media_id_map = {int: media_dict} for O(1) lookup
        when parsing |||MEDIA:N||| signals from LLM output.
        Deduplicates by URL across both sources.
        """
        from apps.llm.prompts import get_lesson_media

        seen_urls = {}  # url -> 1-indexed catalog position
        media_items = []  # list of (label, media_dict)

        # From LessonStep.media JSON via get_lesson_media()
        try:
            for m in get_lesson_media(self.lesson):
                url = m.get('url')
                title = m.get('title', '')
                if not url or not title or url in seen_urls:
                    continue
                media_items.append((title, {
                    'type': m.get('type', 'image'),
                    'url': url,
                    'alt': m.get('alt_text', '') or title,
                    'caption': m.get('caption', '') or title,
                    'description': title,
                }))
                seen_urls[url] = len(media_items)  # 1-indexed catalog ID
        except Exception:
            pass

        # From step.media JSONField images
        # Track which catalog IDs belong to which step
        step_media_positions = {}  # {step_index: [catalog_id, ...]}
        for step_idx, step in enumerate(self.steps):
            if not step.media or 'images' not in step.media:
                continue
            for img in step.media['images']:
                url = img.get('url')
                if not url:
                    continue
                # If URL already in catalog (from get_lesson_media), reuse its ID
                if url in seen_urls:
                    step_media_positions.setdefault(step_idx, []).append(seen_urls[url])
                    continue
                alt = img.get('alt', '')
                caption = img.get('caption', '')
                label = alt or caption
                if not label:
                    continue
                media_items.append((label, {
                    'type': 'image',
                    'url': url,
                    'alt': alt,
                    'caption': caption,
                    'description': alt or caption,
                }))
                seen_urls[url] = len(media_items)  # 1-indexed catalog ID
                step_media_positions.setdefault(step_idx, []).append(len(media_items))

        # From KB figure descriptions (textbook/worksheet figures)
        try:
            from apps.curriculum.knowledge_base import CurriculumKnowledgeBase
            from apps.accounts.models import Institution
            course = self.lesson.unit.course
            institution_id = course.institution_id or Institution.get_global().id
            kb = CurriculumKnowledgeBase(institution_id=institution_id)
            subject = course.title.split()[0] if course else 'General'
            figures = kb.query_for_figure_descriptions(
                topic=f"{self.lesson.title} {self.lesson.objective or ''}",
                subject=subject,
                n_results=5,
                grade_level=course.grade_level or '',
            )
            for fig in (figures or []):
                url = fig.get('image_url', '')
                desc = fig.get('description', '')
                if not url or not desc or url in seen_urls:
                    continue
                label = f"[{fig.get('figure_type', 'figure').upper()}] {desc[:100]}"
                media_items.append((label, {
                    'type': fig.get('figure_type', 'image'),
                    'url': url,
                    'alt': desc[:200],
                    'caption': f"From textbook/worksheet (p.{fig.get('figure_page', '?')})",
                    'description': desc,
                }))
                seen_urls[url] = len(media_items)
        except Exception as e:
            logger.debug(f"KB figure query for media catalog: {e}")

        # Build numbered ID map
        self._media_id_map = {}
        self._step_media_ids = step_media_positions  # {step_index: [catalog_id, ...]}
        if not media_items:
            return (
                "\n\n<media_catalog>\n"
                "No media available for this lesson.\n"
                "Do NOT reference figures, maps, or diagrams. Use text descriptions instead.\n"
                "</media_catalog>"
            )

        lines = []
        for idx, (label, media_dict) in enumerate(media_items, start=1):
            self._media_id_map[idx] = media_dict
            lines.append(f"  [{idx}] {label}")

        catalog = "\n\n<media_catalog>\n"
        catalog += "AVAILABLE MEDIA (use ID number to reference):\n"
        catalog += "\n".join(lines)
        catalog += "\n\nTo show media, write EXACTLY |||MEDIA:N||| as the LAST line."
        catalog += "\nDo NOT embed media references anywhere in your response text."
        catalog += "\nUse at most ONE media item per response."
        catalog += "\n\nIf none of the above media fits what you need, you may generate a new image:"
        catalog += "\n|||GENERATE:category:description||| (as the LAST line)"
        catalog += "\n</media_catalog>"
        return catalog

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
        """Get context for retrieval practice from previous lessons.

        Only includes lessons the student has actually started or completed,
        verified via StudentLessonProgress records (Fix 4).
        """
        try:
            # Only include lessons the student has actually worked on
            completed_ids = set(
                StudentLessonProgress.objects.filter(
                    student=self.student,
                    lesson__unit=self.lesson.unit,
                    lesson__order_index__lt=self.lesson.order_index,
                    mastery_level__in=['in_progress', 'mastered'],
                ).values_list('lesson_id', flat=True)
            )

            previous_lessons = Lesson.objects.filter(
                id__in=completed_ids,
                is_published=True,
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
        """Get step-type-aware guidance with full content for the current lesson step."""
        if self.current_topic_index >= len(self.steps):
            return "All planned topics covered. Move to wrap-up."

        step = self.steps[self.current_topic_index]
        step_num = self.current_topic_index + 1
        total_steps = len(self.steps)
        step_type = (step.step_type or 'teach').upper()
        teacher_script = (step.teacher_script or '')[:2000]

        parts = [f"=== CURRENT STEP: {step_num}/{total_steps} [{step_type}] ==="]

        # Step-type-specific task directive + content
        if step.step_type == 'teach':
            parts.append("YOUR TASK: Deliver this teaching content. Explain clearly, then ask a comprehension check.")
            parts.append("IMPORTANT: Your comprehension check must be a complete, self-contained question. Never say 'which of these' or reference options you haven't listed.")
            parts.append(f"\nCONTENT TO TEACH:\n{teacher_script}")
        elif step.step_type == 'worked_example':
            if self.current_topic_index in self.shown_worked_example_indices and self.step_exchange_count > 0:
                parts.append(
                    "YOUR TASK: The worked example has ALREADY been presented. "
                    "Do NOT repeat it. Instead, ask the student a follow-up question "
                    "about one of the steps, or give them a similar problem for guided practice."
                )
            else:
                parts.append("YOUR TASK: Walk through this worked example step by step, then ask the student to explain a step back.")
            parts.append(f"\nEXAMPLE:\n{teacher_script}")
        elif step.step_type in ('practice', 'quiz'):
            parts.append("YOUR TASK: Ask the EXACT question below verbatim, then grade the student's answer against the expected answer.")
            if step.question:
                parts.append(f"\nQUESTION (ask verbatim): {step.question}")
            if step.expected_answer:
                parts.append(f"EXPECTED ANSWER: {step.expected_answer}")
            if step.answer_type and step.answer_type != 'none':
                parts.append(f"ANSWER TYPE: {step.answer_type}")
            if step.choices:
                parts.append(f"CHOICES: {step.choices}")
        elif step.step_type == 'summary':
            parts.append("YOUR TASK: Summarize the key takeaways, then confirm the student understands.")
            if teacher_script:
                parts.append(f"\nSUMMARY POINTS:\n{teacher_script}")
        else:
            # Fallback for any other step type
            parts.append(f"YOUR TASK: Deliver this content and check understanding.")
            if teacher_script:
                parts.append(f"\nCONTENT:\n{teacher_script}")

        # Hint ladder (for practice/quiz steps, or any step with hints)
        hints = [h for h in [step.hint_1, step.hint_2, step.hint_3] if h]
        if hints:
            parts.append("\nHINT LADDER (use progressively if student is stuck):")
            for j, hint in enumerate(hints, 1):
                parts.append(f"  Hint {j}: {hint}")

        # Rubric for grading
        if step.rubric:
            parts.append(f"\nRUBRIC: {step.rubric[:300]}")

        # Media for this step — strengthened to REQUIRED
        step_media_ids = getattr(self, '_step_media_ids', {}).get(self.current_topic_index, [])
        if step_media_ids:
            media_dict = getattr(self, '_media_id_map', {}).get(step_media_ids[0], {})
            media_desc = media_dict.get('alt', '') or media_dict.get('caption', '')
            parts.append(f"\nMEDIA (REQUIRED): Write |||MEDIA:{step_media_ids[0]}||| as the LAST line.")
            if media_desc:
                parts.append(f"The image shows: {media_desc}")
                parts.append("Reference this image in your explanation — describe what the student should observe.")

        # Educational content
        ed = step.educational_content if isinstance(step.educational_content, dict) else {}

        vocab = ed.get('key_vocabulary', [])
        if vocab:
            terms = []
            for t in vocab:
                terms.append(t.get('term', str(t)) if isinstance(t, dict) else str(t))
            parts.append(f"\nKEY VOCABULARY: {', '.join(terms)}")

        mistakes = ed.get('common_mistakes', [])
        if mistakes:
            items = []
            for m in mistakes:
                items.append(m.get('mistake', m.get('description', str(m))) if isinstance(m, dict) else str(m))
            parts.append(f"COMMON MISTAKES: {'; '.join(items)}")

        sey_ctx = ed.get('seychelles_context', '')
        if sey_ctx:
            parts.append(f"SEYCHELLES CONTEXT: {sey_ctx[:200]}")

        key_points = ed.get('key_points', [])
        if key_points:
            parts.append(f"KEY POINTS: {'; '.join(str(p) for p in key_points)}")

        # Teaching strategies from curriculum context
        cur = step.curriculum_context if isinstance(step.curriculum_context, dict) else {}
        strategies = cur.get('teaching_strategies', [])
        if strategies:
            parts.append(f"TEACHING STRATEGIES: {'; '.join(str(s) for s in strategies)}")

        # Concept block position info
        concept_tag = getattr(step, 'concept_tag', '') or ''
        if concept_tag:
            block = self._get_current_concept_block()
            if block:
                pos = block['step_indices'].index(self.current_topic_index) + 1
                total = len(block['step_indices'])
                parts.append(f"\nCONCEPT BLOCK: step {pos}/{total} in '{concept_tag}'")
                if self._is_at_concept_boundary():
                    parts.append(
                        "CONCEPT GATE: Student must answer the practice check "
                        "correctly before you move to the next concept."
                    )

        # Grade calibration note for senior students
        grade = self._student_grade_level
        if grade and grade.upper() in ('S3', 'S4', 'S5'):
            parts.append(
                f"\nGRADE NOTE: This student is in {grade}. If the content above seems "
                "too basic, adapt it upward — deliver the core idea efficiently and "
                "add grade-appropriate challenge."
            )

        # Step exchange info
        parts.append(f"\nExchanges on this step: {self.step_exchange_count}")

        return "\n".join(parts)
    
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
    # SESSION STATE & STEP EVALUATION
    # =========================================================================

    def _get_display_phase(self) -> str:
        """Get the display phase from the current step's 5E phase label."""
        if getattr(self, 'is_remediation', False):
            return 'remediation'
        if self.session_state == SessionState.TUTORING:
            if self.current_topic_index < len(self.steps):
                step = self.steps[self.current_topic_index]
                return getattr(step, 'phase', '') or 'explain'
            return 'explain'
        return self.session_state.value  # "exit_ticket" or "completed"

    def _deterministic_math_check(self, student_input: str) -> Optional[MathCheckResult]:
        """Pre-generation deterministic math answer check (Layer 1 of the
        math-tutor false-positive fix).

        Runs BEFORE the tutor LLM is called, so its result can be injected
        into the system prompt — forcing the LLM to treat the student's
        answer as already-known-correct or already-known-wrong.

        Returns None when the check is inapplicable:
          - lesson is not math
          - no current step
          - step has no expected_answer
          - either student_input or expected_answer cannot be parsed numerically

        When None is returned, the caller falls through to the existing LLM
        evaluator path (which handles free-text explanations, multi-part
        answers, etc.).

        See memory/math_tutor_fix_plan.md Phase M2.
        """
        try:
            if not self.lesson.unit.course.is_math:
                return None
        except Exception:
            return None

        if self.current_topic_index >= len(self.steps):
            return None

        step = self.steps[self.current_topic_index]
        expected = (step.expected_answer or "").strip()
        if not expected:
            return None

        student_stripped = (student_input or "").strip()
        if not student_stripped:
            return None

        return check_math_answer(student_stripped, expected)

    # Pattern for detecting "bare" math answers: short input that parses as a
    # number and contains no explanatory words. A bare answer on a practice/
    # quiz step violates the math rule "working before evaluation."
    _BARE_WORKING_MARKERS = re.compile(
        r"\b(because|since|so|then|i (got|think|found|divided|multiplied|added|subtracted)"
        r"|first|next|after|therefore|step|\bwork|\btotal|divide|multiply|add|subtract"
        r"|numerator|denominator|ratio|convert)\b",
        re.IGNORECASE,
    )

    def _is_bare_math_answer(self, student_input: str) -> bool:
        """Return True when the student's reply looks like a naked numeric
        answer with no working/explanation (M9 pedagogy layer).

        Heuristic:
          - Input has content and parses as a single number
          - Input is short (<= 40 chars)
          - Input contains none of the 'working' marker words
        """
        if not student_input:
            return False
        text = student_input.strip()
        if len(text) > 40:
            return False
        parsed = check_math_answer(text, text)  # cheap reuse of parser
        if parsed is None:
            return False
        if self._BARE_WORKING_MARKERS.search(text):
            return False
        return True

    def _build_math_eval_signal_block(
        self,
        check: MathCheckResult,
        student_input: str,
        bare_answer: bool = False,
        bare_answer_count_for_step: int = 0,
    ) -> str:
        """Render the <evaluation_signal> block appended to the system prompt
        when a deterministic math check produced a definite result."""
        verdict = "CORRECT" if check.is_correct else "INCORRECT"
        if bare_answer:
            # Regardless of correctness, a bare answer must not be confirmed
            # without working (math_teaching Rule 1). The signal therefore
            # forces the tutor to ask for working FIRST.
            guidance = (
                "The student submitted a BARE numeric answer with no working"
                " shown. Per math_teaching Rule 1, you MUST NOT say 'correct',"
                " 'right', 'brilliant', 'you got it', or equivalent praise,"
                " even if the answer happens to match.\n"
                f"Echo the student's answer back verbatim ('You said"
                f" {student_input.strip()[:80]}'), then ask them to walk you"
                " through each step they took. Only after you see the working"
                " should you confirm correctness or diagnose a subskill gap."
            )
            if bare_answer_count_for_step >= 2:
                guidance += (
                    "\nNOTE: This is the third+ bare answer on this step. Be"
                    " patient — gently model what 'showing working' looks like"
                    " by writing out one example step yourself, then invite"
                    " them to try the next step that way."
                )
        elif check.is_correct:
            guidance = (
                "The student's numeric answer matches the expected answer.\n"
                "You may confirm correctness, but STILL follow Rule 1 of the"
                " math_teaching block — if the student has not shown their"
                " working, ask them to walk you through their steps before"
                " moving on. Do not skip to the next concept without"
                " verifying the working matches the answer."
            )
        else:
            guidance = (
                "The student's numeric answer does NOT match the expected"
                " answer. You MUST NOT say 'correct', 'right', 'brilliant',"
                " 'well done', 'you got it', 'exactly', 'perfect', or any"
                " equivalent praise. Do not state the correct answer yet.\n"
                "Echo the student's answer back to them verbatim ('You said"
                f" {student_input.strip()[:80]}'), then ask them to walk you"
                " through how they got it. Use the math_teaching rules to"
                " diagnose which subskill failed."
            )
        block = (
            "\n\n<evaluation_signal>"
            f"\nStudent's answer (parsed): {check.student_parsed}"
            f"\nExpected answer (parsed): {check.expected_parsed}"
            f"\nVerdict: {verdict}"
            f"\nBare answer (no working shown): {bare_answer}"
            f"\nReasoning: {check.reasoning}"
            f"\n{guidance}"
            "\n</evaluation_signal>"
        )
        return block

    def _evaluate_step(self, student_input: str, tutor_response: str) -> Optional[StepEvaluationResult]:
        """Merged LLM evaluator: answer correctness + step completion in one call.

        Step-type-specific prompts:
        - teach: complete when content delivered + comprehension check answered correctly
        - worked_example: complete when example walked through + student explained a step back
        - practice/quiz: complete when answered correctly OR exhausted max_attempts
        - summary: complete when key points stated + student acknowledged
        """
        if not self.instructor_client:
            return None

        if self.current_topic_index >= len(self.steps):
            return None

        step = self.steps[self.current_topic_index]
        step_type = step.step_type or 'teach'

        # Build step context
        step_context_parts = [f"Step type: {step_type}"]
        if step.teacher_script:
            step_context_parts.append(f"Teacher script: {step.teacher_script[:500]}")
        if step.question:
            step_context_parts.append(f"Question: {step.question}")
        if step.expected_answer:
            step_context_parts.append(f"Expected answer: {step.expected_answer}")
        step_context_parts.append(f"Exchanges on this step: {self.step_exchange_count}")

        # Step-type-specific completion criteria
        criteria = {
            'teach': "Complete when the teaching content has been delivered AND the student answered a comprehension check correctly.",
            'worked_example': "Complete when the example has been walked through AND the student explained a step back correctly.",
            'practice': "Complete when the student answered the question correctly.",
            'quiz': "Complete when the student answered the question correctly.",
            'summary': "Complete when the key points have been stated AND the student acknowledged understanding.",
        }
        completion_criteria = criteria.get(step_type, criteria['teach'])

        # Last 4 conversation turns for context
        recent = self.conversation[-(4*2):] if len(self.conversation) > 4 else self.conversation
        convo_text = "\n".join(
            f"{'TUTOR' if m['role'] == 'assistant' else 'STUDENT'}: {m['content'][:200]}"
            for m in recent
        )

        prompt = f"""Evaluate this tutoring exchange.

STEP CONTEXT:
{chr(10).join(step_context_parts)}

COMPLETION CRITERIA: {completion_criteria}

RECENT CONVERSATION:
{convo_text}

STUDENT JUST SAID: {student_input[:500]}
TUTOR REPLIED: {tutor_response[:500]}

INSTRUCTIONS FOR answer_correct:
- Return TRUE if the student gave a clear, demonstrably correct answer
  to a specific question.
- Return FALSE only when the student gave a clear, demonstrably wrong
  answer to a specific question.
- Return NULL when none of these apply: the student is acknowledging
  the teaching ("ok", "got it", "interesting"), asking their own
  question, expressing confusion, sharing tangential thoughts, or there
  was no expected_answer to compare against. Default to NULL when
  uncertain — never penalize a student for engaging conversationally.

INSTRUCTIONS FOR step_complete:
- Apply the COMPLETION CRITERIA above. A step is NOT complete just
  because the student is engaged; it is complete when the criteria are
  met or the student has demonstrated mastery of this step's objective.

1. Did the student demonstrably answer correctly, demonstrably answer
   incorrectly, or was there nothing to evaluate (return null)?
2. Is this step complete — should the system advance to the next step?"""

        try:
            create_kwargs = dict(
                response_model=StepEvaluationResult,
                messages=[
                    {"role": "system", "content": "You are a tutoring step evaluator. Assess answer correctness and step completion."},
                    {"role": "user", "content": prompt},
                ],
                max_retries=2,
            )
            if getattr(self, '_instructor_provider', None) == 'google':
                create_kwargs['generation_config'] = {'max_tokens': 1024}
            else:
                create_kwargs['max_tokens'] = 150
            result = self.instructor_client.chat.completions.create(**create_kwargs)
            logger.info(
                f"Step eval [{step_type}] step={self.current_topic_index}: "
                f"correct={result.answer_correct}, complete={result.step_complete}, "
                f"reason={result.reasoning[:80]}"
            )
            return result
        except Exception as e:
            logger.warning(f"Step evaluation failed: {e}")
            return None

    def _get_step_phase_instructions(self) -> str:
        """Minimal step-context instructions (replaces _get_phase_instructions).

        Returns remediation guidance when in remediation mode,
        light context for engage/summary phases, empty otherwise.
        """
        is_remediation = getattr(self, 'is_remediation', False)
        failed_count = len(getattr(self, 'failed_exit_questions', []))
        attempt = getattr(self, 'remediation_attempt', 0)

        if is_remediation:
            prereq_gap_context = ""
            remediation_plan = getattr(self, '_remediation_plan', None)
            if remediation_plan and remediation_plan.get('prerequisite_gaps'):
                gap_names = [s.name for s in remediation_plan['prerequisite_gaps'][:5]]
                prereq_gap_context = f"""
PREREQUISITE GAPS DETECTED:
{chr(10).join(f'  - {name}' for name in gap_names)}
Address these gaps FIRST before re-teaching the failed concepts.
"""
            failed_eos = getattr(self, '_failed_eos', [])
            eo_context = ""
            if failed_eos:
                eo_lines = "\n".join(f"  - {eo}" for eo in failed_eos[:4])
                eo_context = f"""
ENABLING OBJECTIVES TO REMEDIATE:
{eo_lines}

Work through each enabling objective one at a time:
1. RE-TEACH the concept using a DIFFERENT explanation than before
2. Give a simple example
3. Ask a check question to verify understanding
4. Move to the next EO once they demonstrate understanding
"""
            return f"""
REMEDIATION MODE (Attempt #{attempt})
The student scored {failed_count} wrong on the exit ticket.
{eo_context}{prereq_gap_context}
Be encouraging. Break concepts into smaller steps. Use different examples than before."""

        # Light context based on step position
        if self.current_topic_index < len(self.steps):
            step = self.steps[self.current_topic_index]
            phase = getattr(step, 'phase', '') or ''

            if phase == 'engage' or self.current_topic_index <= 1:
                return "Build rapport, connect to prior knowledge, preview the lesson."

            if step.step_type == 'summary' or phase == 'evaluate':
                return "Summarize key takeaways. Prepare student for the exit quiz."

        return ""  # The step directive is sufficient

    def _remediation_steps_complete(self) -> bool:
        """Check if remediation has covered enough ground before re-presenting exit ticket.

        Requires BOTH:
          - A meaningful floor of exchanges per failed EO (so student gets real re-teaching)
          - All failed concepts re-covered (keyword check) OR safety valve hit
        """
        if not getattr(self, 'is_remediation', False):
            return False

        failed_eos = getattr(self, '_failed_eos', [])
        n_failed = max(1, len(failed_eos))
        # At least 3 exchanges per failed EO, hard floor of 6 — prevents premature re-quiz
        min_exchanges = max(6, n_failed * 3)

        # Safety valve: max 15 exchanges in remediation
        if self.exchange_count >= 15:
            logger.info(f"[Remediation] Safety valve fired at {self.exchange_count} exchanges")
            return True

        if self.exchange_count < min_exchanges:
            return False

        # Floor met — also require that the concepts we're remediating got re-covered
        failed_ids = {fq['id'] for fq in getattr(self, 'failed_exit_questions', [])}
        if failed_ids:
            uncovered_failed = [
                c for c in self.exit_ticket_concepts
                if c['id'] in failed_ids and not c.get('covered')
            ]
            if uncovered_failed:
                logger.info(
                    f"[Remediation] {self.exchange_count} exchanges done but "
                    f"{len(uncovered_failed)}/{len(failed_ids)} failed concepts still uncovered"
                )
                return False

        logger.info(f"[Remediation] Complete: {self.exchange_count} exchanges, "
                    f"failed EOs re-covered. Re-presenting exit ticket.")
        return True

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

    # =========================================================================
    # CONCEPT-BOUNDARY HELPERS
    # =========================================================================

    def _get_concept_blocks(self) -> List[Dict]:
        """Group lesson steps by concept_tag into blocks with practice indices.

        Returns list of dicts:
            [{'tag': 'relief_rainfall', 'step_indices': [2,3,4], 'practice_indices': [4]}, ...]
        Empty-tag steps are each their own block (preserves old behavior).
        """
        blocks = []
        current_tag = None
        current_block = None

        for i, step in enumerate(self.steps):
            tag = getattr(step, 'concept_tag', '') or ''
            if not tag:
                # Empty tag = standalone block
                blocks.append({
                    'tag': '',
                    'step_indices': [i],
                    'practice_indices': [i] if step.step_type in ('practice', 'quiz') else [],
                })
                current_tag = None
                current_block = None
            elif tag != current_tag:
                # New concept block
                current_block = {
                    'tag': tag,
                    'step_indices': [i],
                    'practice_indices': [i] if step.step_type in ('practice', 'quiz') else [],
                }
                blocks.append(current_block)
                current_tag = tag
            else:
                # Same concept block
                current_block['step_indices'].append(i)
                if step.step_type in ('practice', 'quiz'):
                    current_block['practice_indices'].append(i)

        return blocks

    def _is_at_concept_boundary(self) -> bool:
        """Return True if the next step has a different (non-empty) concept_tag.

        Returns False for empty-tag lessons (backward compat).
        """
        if self.current_topic_index >= len(self.steps) - 1:
            return False

        current_step = self.steps[self.current_topic_index]
        next_step = self.steps[self.current_topic_index + 1]

        current_tag = getattr(current_step, 'concept_tag', '') or ''
        next_tag = getattr(next_step, 'concept_tag', '') or ''

        # Only gate when both tags are non-empty and different
        if not current_tag or not next_tag:
            return False

        return current_tag != next_tag

    def _current_concept_practice_passed(self) -> bool:
        """Check if the student answered the current concept block's practice correctly.

        Uses the success signals from the most recent tutor response.
        """
        return getattr(self, 'last_answer_correct', False)

    def _get_current_concept_block(self) -> Optional[Dict]:
        """Get the concept block containing the current step index."""
        blocks = self._get_concept_blocks()
        for block in blocks:
            if self.current_topic_index in block['step_indices']:
                return block
        return None

    # =========================================================================
    # STUDENT ANALYSIS
    # =========================================================================

    def _analyze_student_response(
        self,
        student_input: str,
        tutor_response: str,
        math_check: Optional[MathCheckResult] = None,
    ) -> Dict:
        """Analyze student response to adapt future instruction and track concept coverage.

        When math_check is provided (from the pre-generation deterministic
        numeric comparison), its verdict takes precedence over the LLM
        evaluator — numeric equality is always authoritative for math.

        Returns a metadata dict suitable for attaching to the tutor turn's
        SessionTurn.metadata JSONField (see M4 of math_tutor_fix_plan.md).
        """
        input_lower = student_input.lower()
        response_lower = tutor_response.lower()
        combined_text = f"{input_lower} {response_lower}"

        # Detect confusion
        confusion_signals = ["i don't know", "confused", "don't understand", "help", "?", "not sure", "what"]
        if any(signal in input_lower for signal in confusion_signals):
            current_topic = self._get_current_topic()[:50]
            if current_topic not in self.student_struggles:
                self.student_struggles.append(current_topic)

        # Single LLM evaluation: correctness + step completion in one call
        # (Replaces separate _llm_evaluate_response + _evaluate_step calls)
        current_step = self.steps[self.current_topic_index] if self.current_topic_index < len(self.steps) else None
        step_type = (current_step.step_type or 'teach') if current_step else 'teach'

        # Layer 1 (deterministic) short-circuits the LLM evaluator when
        # we already have a numeric-equality verdict. The LLM is unreliable
        # for math correctness and this is the core math-tutor fix.
        # is_correct can be True / False / None. None means "no evidence
        # either way" — student was engaging conversationally, asked a
        # clarifying question, or there was no expected_answer to compare
        # against. Downstream code treats None as "do not penalize, do not
        # advance the streak".
        eval_layer = None
        eval_reasoning = None
        step_eval_result = None
        is_correct: Optional[bool] = None
        if math_check is not None:
            is_correct = math_check.is_correct
            eval_layer = 'deterministic_numeric'
            eval_reasoning = math_check.reasoning
        else:
            if step_type in ('practice', 'quiz', 'teach', 'worked_example') and self.instructor_client:
                step_eval_result = self._evaluate_step(student_input, tutor_response)
            if step_eval_result is not None:
                is_correct = step_eval_result.answer_correct  # may be None
                eval_layer = 'llm_evaluator'
                eval_reasoning = getattr(step_eval_result, 'reasoning', '') or ''
            else:
                # Keyword fallback only applies when the tutor's response
                # gives a clear positive/negative signal. Otherwise leave
                # is_correct as None.
                kw = self._keyword_evaluate_response(tutor_response)
                is_correct = kw.get('correct') if kw.get('signal') else None
                eval_layer = 'keyword_fallback'
                eval_reasoning = 'no instructor client; used keyword heuristic'

        # Tri-state handling — None means "no signal", neither penalize
        # nor advance. is_correct is True / False / None.
        verdict_correct = (is_correct is True)
        verdict_wrong = (is_correct is False)

        # Detect success — update strength tracking
        if verdict_correct:
            self.practice_correct += 1
            current_topic = self._get_current_topic()[:50]
            if current_topic not in self.student_strengths:
                self.student_strengths.append(current_topic)

        # Track practice attempts (only when there was an actual answer
        # being evaluated — avoids inflating practice_total with
        # conversational engagement).
        if step_type in ('practice', 'quiz') and (verdict_correct or verdict_wrong):
            self.practice_total += 1

        # Update last_answer_correct for concept boundary gating. Keep
        # the previous value when there is no new signal so that
        # conversational engagement doesn't reset state.
        if is_correct is not None:
            self.last_answer_correct = is_correct

        # Update correct-answer streak for in-conversation gamification.
        if verdict_correct:
            self._correct_streak = getattr(self, '_correct_streak', 0) + 1
        elif verdict_wrong:
            self._correct_streak = 0

        # Update cognitive load based on correctness
        if verdict_correct:
            self.consecutive_wrong = 0
            self.consecutive_correct_streak = getattr(self, 'consecutive_correct_streak', 0) + 1
            # Decrease load (student is doing well)
            self.cognitive_load = max(0.0, self.cognitive_load - 0.1)
        elif verdict_wrong:
            self.consecutive_correct_streak = 0
            self.consecutive_wrong = getattr(self, 'consecutive_wrong', 0) + 1
            # Increase load (student is struggling)
            self.cognitive_load = min(1.0, self.cognitive_load + 0.15)

        # Confusion signals increase load
        if any(signal in input_lower for signal in confusion_signals):
            self.cognitive_load = min(1.0, self.cognitive_load + 0.1)

        # Record skill practice via SkillAssessmentService (R2). Only
        # log the practice if we actually evaluated an answer; logging
        # was_correct=False on every conversational engagement would
        # destroy the skill-graph signal.
        try:
            if (
                self.lesson_skills
                and self.skill_assessment_service
                and (verdict_correct or verdict_wrong)
            ):
                current_skill = self._get_current_skill()
                if current_skill:
                    self.skill_assessment_service.record_practice(
                        skill=current_skill,
                        was_correct=verdict_correct,
                        lesson_step=current_step,
                        practice_type='remediation' if self.is_remediation else 'initial',
                        hints_used=0,
                    )
        except Exception as e:
            logger.warning(f"Failed to record skill practice: {e}")

        # Record EO-linked skill practice (P1.2 enabling objective mastery)
        # Only when we have a clear verdict — see comment on the previous
        # record_practice block.
        try:
            if (
                self.skill_assessment_service
                and current_step
                and (verdict_correct or verdict_wrong)
            ):
                eo_text = getattr(current_step, 'enabling_objective', '')
                if eo_text:
                    eo_skill = self._get_eo_skill(eo_text)
                    if eo_skill:
                        self.skill_assessment_service.record_practice(
                            skill=eo_skill,
                            was_correct=verdict_correct,
                            lesson_step=current_step,
                            practice_type='remediation' if self.is_remediation else 'initial',
                            hints_used=0,
                        )
        except Exception as e:
            logger.warning(f"Failed to record EO skill practice: {e}")

        # Track exit ticket concept coverage (keyword-only — no extra LLM call)
        self._keyword_concept_coverage_check(combined_text)

        # Advance topic based on step-type completion criteria (NOT during remediation)
        if self.session_state == SessionState.TUTORING and not getattr(self, 'is_remediation', False):
            should_advance = self._should_advance_step(student_input, tutor_response, is_correct, step_eval_result)
            if should_advance and self.current_topic_index < len(self.steps) - 1:
                # Check concept boundary gating
                if self._is_at_concept_boundary():
                    boundary_attempts = getattr(self, 'concept_boundary_attempts', 0)
                    if self._current_concept_practice_passed():
                        self.concept_boundary_attempts = 0
                        self._mark_step_objective_covered(self.current_topic_index)
                        self.current_topic_index += 1
                        self.step_exchange_count = 0
                        self._step_just_advanced = True
                        logger.info(f"Concept boundary crossed at step {self.current_topic_index}")
                    elif boundary_attempts >= 4:
                        self.concept_boundary_attempts = 0
                        self._mark_step_objective_covered(self.current_topic_index)
                        self.current_topic_index += 1
                        self.step_exchange_count = 0
                        self._step_just_advanced = True
                        logger.info(f"Safety valve: forced concept boundary crossing after {boundary_attempts} attempts")
                    else:
                        self.concept_boundary_attempts = boundary_attempts + 1
                        block = self._get_current_concept_block()
                        tag = block['tag'] if block else 'this concept'
                        logger.info(f"Concept boundary blocked (attempt {self.concept_boundary_attempts}): {tag}")
                else:
                    # No boundary or empty tags — advance normally
                    self._mark_step_objective_covered(self.current_topic_index)
                    self.current_topic_index += 1
                    self.step_exchange_count = 0
                    self._step_just_advanced = True
            elif should_advance and self.current_topic_index >= len(self.steps) - 1:
                # Last step complete — mark index past end so exit ticket triggers
                self._mark_step_objective_covered(self.current_topic_index)
                self.current_topic_index = len(self.steps)
                self._step_just_advanced = True

        # Build metadata dict for tutor-turn persistence (M4).
        metadata: Dict = {
            'is_correct': bool(is_correct),
            'eval_layer': eval_layer,
            'eval_reasoning': (eval_reasoning or '')[:500],
            'step_index': self.current_topic_index,
            'step_type': step_type,
        }
        if math_check is not None:
            metadata['student_answer_parsed'] = math_check.student_parsed
            metadata['expected_answer_parsed'] = math_check.expected_parsed
        return metadata

    def _get_eo_skill(self, eo_text: str):
        """Look up the Skill linked to an enabling objective text.

        Tries exact match first, then partial match for LLM text variations.
        """
        from apps.tutoring.skills_models import Skill
        text = eo_text.strip()
        if not text:
            return None
        course = self.lesson.unit.course
        # Exact match
        skill = Skill.objects.filter(
            enabling_objective_text=text,
            is_enabling_objective=True,
            course=course,
        ).first()
        if skill:
            return skill
        # Partial match — first 50 chars (LLM may truncate or rephrase)
        skill = Skill.objects.filter(
            enabling_objective_text__icontains=text[:50],
            is_enabling_objective=True,
            course=course,
        ).first()
        return skill

    def _mark_step_objective_covered(self, step_index: int):
        """Mark the enabling objective of the given step as covered (P1.2)."""
        if not self.enabling_objectives:
            return
        if step_index < 0 or step_index >= len(self.steps):
            return
        step_eo = getattr(self.steps[step_index], 'enabling_objective', '')
        if not step_eo:
            return
        for obj in self.enabling_objectives:
            if obj['objective'] == step_eo.strip():
                obj['covered'] = True
                break

    def _llm_evaluate_response(self, student_input: str, tutor_response: str) -> dict:
        """Use LLM to semantically evaluate whether the student answered correctly.

        Uses instructor for structured output — returns a validated EvaluationResult.
        Falls back to keyword matching if the instructor client is unavailable or fails.
        """
        if not self.instructor_client:
            return self._keyword_evaluate_response(tutor_response)

        # Get current step context
        step = None
        if self.current_topic_index < len(self.steps):
            step = self.steps[self.current_topic_index]

        step_context = ""
        if step:
            step_context += f"Step type: {step.step_type}\n"
            if step.answer_type:
                step_context += f"Answer type: {step.answer_type}\n"
            if step.question:
                step_context += f"Question asked: {step.question}\n"
            if step.expected_answer:
                step_context += f"Expected answer: {step.expected_answer}\n"
            if step.rubric:
                step_context += f"Rubric: {step.rubric}\n"

        prompt = f"""Evaluate whether the student answered correctly.

{step_context}
Student said: {student_input[:500]}

Tutor replied: {tutor_response[:500]}

Judge the student's answer against the expected answer SEMANTICALLY — the student does not need
to use the exact same words, but their answer must convey the correct meaning. If the question
asks for a specific item (e.g. "which is smallest"), the answer must identify that item."""

        try:
            create_kwargs = dict(
                response_model=EvaluationResult,
                messages=[
                    {"role": "system", "content": "You are a grading assistant. Evaluate student answers semantically against the expected answer and rubric. Focus on whether the student demonstrates correct understanding, not on exact wording."},
                    {"role": "user", "content": prompt},
                ],
                max_retries=2,
            )
            if getattr(self, '_instructor_provider', None) == 'google':
                create_kwargs['generation_config'] = {'max_tokens': 1024}
            else:
                create_kwargs['max_tokens'] = 50
            result = self.instructor_client.chat.completions.create(**create_kwargs)
            logger.info(f"LLM evaluation: {'correct' if result.correct else 'incorrect'} (step {self.current_topic_index})")
            return {"correct": result.correct}
        except Exception as e:
            logger.warning(f"LLM evaluation failed, falling back to keywords: {e}")

        return self._keyword_evaluate_response(tutor_response)

    def _keyword_evaluate_response(self, tutor_response: str) -> dict:
        """Keyword-based correctness check (fallback for LLM evaluator).

        Returns {"correct": bool, "signal": bool}.
          - signal=True when the tutor response contains a clear
            positive or negative correctness signal.
          - signal=False when the tutor said something neutral (no
            evidence either way). Callers should treat signal=False as
            None / no-op rather than implicitly False, otherwise every
            conversational engagement is mis-counted as wrong.
        """
        response_lower = tutor_response.lower()
        negative_signals = [
            "not correct", "not quite", "incorrect", "not right",
            "try again", "not exactly", "that's wrong", "think again",
            "not the answer", "let's try", "let's reconsider",
        ]
        if any(s in response_lower for s in negative_signals):
            return {"correct": False, "signal": True}
        positive_signals = [
            "correct", "excellent", "great job", "perfect",
            "well done", "good job", "exactly right", "that's right",
            "you got it", "nice work", "spot on",
        ]
        if any(s in response_lower for s in positive_signals):
            return {"correct": True, "signal": True}
        return {"correct": False, "signal": False}

    def _should_advance_step(self, student_input: str, tutor_response: str, is_correct: bool, eval_result=None) -> bool:
        """Determine if the current step is complete using merged LLM evaluator.

        Args:
            eval_result: Pre-computed StepEvaluationResult from _analyze_student_response.
                         If provided, avoids a redundant _evaluate_step() LLM call.

        Safety valves (hard rules, not LLM):
        | Rule                  | Threshold                                        |
        |-----------------------|--------------------------------------------------|
        | Hard cap (any step)   | 8 exchanges -> force advance                     |
        | Min exchanges before  | teach/worked_example: 2, practice/quiz/summary: 1 |
        | Practice fast-path    | correct answer -> immediate advance               |
        | Practice attempt cap  | max_attempts + 2 exchanges -> force advance       |
        | Evaluator failure     | Fall back to exchange-count rules                 |
        """
        if self.current_topic_index >= len(self.steps):
            return False

        step = self.steps[self.current_topic_index]
        step_type = step.step_type or 'teach'
        exchanges = self.step_exchange_count

        # 1. Hard cap: always advance after 6 exchanges on any step
        if exchanges >= 6:
            logger.info(f"Hard cap: advancing step {self.current_topic_index} after {exchanges} exchanges")
            return True

        # 2. Min exchange floor
        min_exchanges = {'teach': 1, 'worked_example': 1}.get(step_type, 1)
        if exchanges < min_exchanges:
            return False

        # 3. Practice fast-path: correct answer -> immediate advance
        if step_type in ('practice', 'quiz') and is_correct:
            logger.info(f"Practice fast-path: correct answer on step {self.current_topic_index}")
            return True

        # 4. Practice attempt cap
        if step_type in ('practice', 'quiz'):
            max_attempts = getattr(step, 'max_attempts', 3) or 3
            if exchanges >= max_attempts + 1:
                logger.info(f"Practice attempt cap: advancing step {self.current_topic_index} after {exchanges} exchanges")
                return True

        # 5. Use pre-computed eval result or call LLM evaluator
        if eval_result is None:
            eval_result = self._evaluate_step(student_input, tutor_response)
        if eval_result is not None:
            return eval_result.step_complete

        # 6. Fallback to exchange-count rules if evaluator fails
        logger.info(f"Evaluator fallback for step {self.current_topic_index} ({step_type})")
        if step_type in ('teach', 'worked_example'):
            return exchanges >= 3
        if step_type in ('practice', 'quiz'):
            return is_correct or exchanges >= 4
        if step_type == 'summary':
            return exchanges >= 1
        return exchanges >= 2

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

        if not self.instructor_client:
            self._keyword_concept_coverage_check(conversation_text)
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

Which concept numbers were meaningfully covered?"""

        try:
            create_kwargs = dict(
                response_model=ConceptCoverageResult,
                messages=[
                    {"role": "system", "content": "You are an educational assessment assistant. Identify which concepts were covered."},
                    {"role": "user", "content": prompt},
                ],
                max_retries=2,
            )
            if getattr(self, '_instructor_provider', None) == 'google':
                create_kwargs['generation_config'] = {'max_tokens': 1024}
            else:
                create_kwargs['max_tokens'] = 100
            result = self.instructor_client.chat.completions.create(**create_kwargs)
            for idx in result.covered_indices:
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
            q_type = getattr(q, 'question_type', 'mcq') or 'mcq'
            q_data = {
                'index': i,
                'question_type': q_type,
                'question': q.question_text,
            }
            if q_type == 'mcq':
                q_data['options'] = [
                    {'letter': 'A', 'text': q.option_a},
                    {'letter': 'B', 'text': q.option_b},
                    {'letter': 'C', 'text': q.option_c},
                    {'letter': 'D', 'text': q.option_d},
                ]
                q_data['correct'] = q.correct_answer
                # Include source HTML for source-based MCQ questions
                if q.answer_data and q.answer_data.get('source'):
                    q_data['source'] = q.answer_data['source']
            else:
                q_data['answer_data'] = q.answer_data or {}
            exit_questions.append(q_data)

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
        self.session_state = SessionState.COMPLETED
        self.session.status = TutorSession.Status.COMPLETED
        self.session.ended_at = timezone.now()
        self.session.completed_lesson_at = timezone.now()
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
    
    def _grade_exit_question(self, question, student_answer) -> bool:
        """Grade an exit ticket question based on its type (P1.3).

        Returns True if correct, False otherwise. All grading is deterministic
        (no LLM) for speed and consistency.
        """
        q_type = getattr(question, 'question_type', 'mcq') or 'mcq'
        data = question.answer_data or {}

        # Safety: if student_answer is a list/dict, it's NOT an MCQ answer
        if isinstance(student_answer, (list, dict)):
            if q_type == 'mcq':
                q_type = 'fill_in_blank' if isinstance(student_answer, list) else 'matching'

        # MCQ: deterministic — letter match only
        if q_type == 'mcq':
            ans = student_answer if isinstance(student_answer, str) else str(student_answer or '')
            return ans.upper().strip() == (question.correct_answer or '').upper().strip()

        # All other types: use LLM-based evaluation
        return self._llm_grade_exit_question(question, student_answer, q_type, data)

    def _llm_grade_exit_question(self, question, student_answer, q_type: str, data: dict) -> bool:
        """Use LLM to evaluate non-MCQ exit ticket answers semantically.
        For math: tries numerical comparison first, falls back to LLM."""

        # Math: try numerical comparison first (no LLM needed for calculation answers)
        is_math = self.lesson.unit.course.is_math if self.lesson.unit and self.lesson.unit.course else False
        if is_math and q_type == 'fill_in_blank':
            blanks = data.get('blanks', [])
            student_blanks = student_answer if isinstance(student_answer, list) else [student_answer]
            from apps.tutoring.math_tools import safe_eval_expression
            correct_count = 0
            for idx, expected in enumerate(blanks):
                given = str(student_blanks[idx] if idx < len(student_blanks) else '').strip()
                # Try numerical comparison
                expected_val = safe_eval_expression(expected)
                given_val = safe_eval_expression(given)
                if expected_val is not None and given_val is not None:
                    if abs(expected_val - given_val) < 0.01:
                        correct_count += 1
                        continue
                # Fallback: string match
                if given.lower() == expected.lower():
                    correct_count += 1
            if correct_count >= max(1, len(blanks) // 2 + 1):
                return True
            if correct_count == 0 and blanks:
                return False  # Clearly wrong, skip LLM

        if is_math and q_type in ('short_answer', 'data_interpretation'):
            # For math short answers, check if the key numerical result is present
            model_answer = data.get('model_answer', '')
            keywords = data.get('keywords', [])
            text = str(student_answer or '').strip()
            from apps.tutoring.math_tools import safe_eval_expression
            # Extract numbers from both answers
            import re as _re
            student_numbers = set(_re.findall(r'-?\d+\.?\d*', text))
            expected_numbers = set(kw for kw in keywords if _re.match(r'-?\d+\.?\d*$', kw))
            if expected_numbers:
                matched = len(student_numbers & expected_numbers)
                min_kw = data.get('min_keywords', 2)
                if matched >= min_kw:
                    return True

        # Build context based on question type
        if q_type == 'fill_in_blank':
            blanks = data.get('blanks', [])
            student_blanks = student_answer if isinstance(student_answer, list) else [student_answer]
            correct_info = f"Expected answers: {', '.join(blanks)}"
            student_info = f"Student filled in: {', '.join(str(b) for b in student_blanks)}"
        elif q_type == 'matching':
            pairs = data.get('pairs', [])
            correct_info = "Correct pairs: " + "; ".join(f"{p['left']} → {p['right']}" for p in pairs)
            student_map = student_answer if isinstance(student_answer, dict) else {}
            student_info = "Student matched: " + "; ".join(f"{k} → {v}" for k, v in student_map.items())
        else:  # short_answer, data_interpretation
            model_answer = data.get('model_answer', '')
            keywords = data.get('keywords', [])
            correct_info = f"Model answer: {model_answer}"
            if keywords:
                correct_info += f"\nKey concepts: {', '.join(keywords)}"
            student_info = f"Student answer: {student_answer}"

        # Try LLM evaluation
        if self.instructor_client:
            try:
                math_note = (
                    "For MATH answers: check the NUMERICAL RESULT is correct. "
                    "The working/method matters less than getting the right number. "
                    "Use Python to verify: eval the expression if needed. "
                ) if is_math else ""
                eval_prompt = (
                    f"QUESTION: {question.question_text}\n"
                    f"TYPE: {q_type}\n"
                    f"{correct_info}\n"
                    f"{student_info}\n\n"
                    f"Grade this answer. {math_note}"
                    f"The student does NOT need exact wording — "
                    f"accept synonyms, paraphrasing, and equivalent meaning. "
                    f"For fill-in-blank: accept if the meaning is right even if spelling differs slightly. "
                    f"For matching: accept if the majority of pairs are correctly matched. "
                    f"For written answers: accept if they demonstrate understanding of the key concepts.\n\n"
                    f"Reply ONLY with the single word 'correct' or 'incorrect'."
                )
                eval_response = self._generate_response(eval_prompt, max_tokens=10)
                return 'correct' in eval_response.lower()
            except Exception as e:
                logger.warning(f"LLM grading failed, falling back to deterministic: {e}")

        # Fallback: deterministic grading if LLM unavailable
        if q_type == 'fill_in_blank':
            blanks = data.get('blanks', [])
            alternatives = data.get('accept_alternatives', [])
            student_blanks = student_answer if isinstance(student_answer, list) else [student_answer]
            correct_count = 0
            for idx, expected in enumerate(blanks):
                given = (student_blanks[idx] if idx < len(student_blanks) else '').strip().lower()
                accepted = {expected.lower()}
                if idx < len(alternatives):
                    accepted.update(a.lower() for a in alternatives[idx])
                if given in accepted or expected.lower() in given:
                    correct_count += 1
            return correct_count >= max(1, len(blanks) // 2 + 1) if blanks else False

        if q_type == 'matching':
            pairs = data.get('pairs', [])
            correct_map = {p['left'].lower(): p['right'].lower() for p in pairs}
            student_map = student_answer if isinstance(student_answer, dict) else {}
            correct_count = sum(
                1 for left, right in student_map.items()
                if correct_map.get(left.lower(), '') == right.lower()
            )
            return correct_count >= max(1, len(correct_map) // 2 + 1) if correct_map else False

        # short_answer / data_interpretation fallback
        keywords = data.get('keywords', [])
        min_kw = data.get('min_keywords', 2)
        text = (student_answer if isinstance(student_answer, str) else '').lower()
        matched = sum(1 for kw in keywords if kw.lower() in text)
        return matched >= min_kw

    def submit_exit_ticket(self, answers) -> TutorMessage:
        """Process exit ticket submission using the pre-selected randomized questions.

        answers: List of answers — each is a string (MCQ letter), list (fill_in_blank),
                 dict (matching pairs), or string (short answer/data interpretation).
        """
        try:
            return self._submit_exit_ticket_inner(answers)
        except Exception as e:
            logger.error(f"Exit ticket submission failed: {e}", exc_info=True)
            print(f"[ExitTicket] CRASH: {e}", flush=True)
            import traceback; traceback.print_exc()
            # Don't crash — complete the session gracefully
            self._save_state()
            return TutorMessage(
                content=f"There was an issue grading your quiz. Your session has been saved. Score could not be calculated.",
                phase="completed",
                is_complete=True,
            )

    def _submit_exit_ticket_inner(self, answers) -> TutorMessage:
        """Inner implementation of exit ticket submission."""
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
            raw_answer = answers[i] if i < len(answers) else ''
            # Support both old format (plain string) and new format ({type, answer})
            if isinstance(raw_answer, dict) and 'answer' in raw_answer:
                student_answer = raw_answer['answer']
            elif isinstance(raw_answer, dict) and 'type' not in raw_answer and len(raw_answer) > 0:
                # Matching answer: dict of {left: right} pairs
                student_answer = raw_answer
            else:
                student_answer = raw_answer

            is_correct = self._grade_exit_question(q, student_answer)

            # Record EO-skill practice from exit ticket (P1.2)
            try:
                concept = getattr(q, 'concept_tag', '') or ''
                if concept and self.skill_assessment_service:
                    eo_skill = self._get_eo_skill(concept)
                    if eo_skill:
                        self.skill_assessment_service.record_practice(
                            skill=eo_skill,
                            was_correct=is_correct,
                            practice_type='initial',
                            hints_used=0,
                        )
            except Exception:
                pass

            # Track EO (concept_tag) for competency reporting
            eo_tag = getattr(q, 'concept_tag', '') or ''

            if is_correct:
                correct += 1
            else:
                q_type = getattr(q, 'question_type', 'mcq') or 'mcq'
                failed_questions.append({
                    'id': q.id,
                    'index': i,
                    'question': q.question_text,
                    'concept_tag': eo_tag,
                    'student_answer': student_answer,
                    'correct_answer': q.correct_answer if q_type == 'mcq' else str(q.answer_data or {}),
                    'correct_text': getattr(q, f'option_{(q.correct_answer or "a").lower()}', '') if q_type == 'mcq' else '',
                    'explanation': q.explanation,
                })

            results.append({
                'index': i,
                'question': q.question_text,
                'question_type': getattr(q, 'question_type', 'mcq') or 'mcq',
                'concept_tag': eo_tag,
                'selected': student_answer,
                'correct_answer': q.correct_answer if (getattr(q, 'question_type', 'mcq') or 'mcq') == 'mcq' else q.answer_data,
                'is_correct': is_correct,
                'explanation': q.explanation,
            })

        # Use the lesson's configured passing_score (no longer hardcoded
        # to 8 — that was a bug). Fallback of 8 preserves behavior for
        # older lessons that didn't explicitly set the field.
        from apps.tutoring.models import ExitTicket
        exit_ticket = ExitTicket.objects.filter(lesson=self.lesson).first()
        passing_threshold = exit_ticket.passing_score if exit_ticket else 8
        passed = correct >= passing_threshold

        self.session.mastery_achieved = passed

        if passed:
            self._save_state()
            return self._complete_session_with_results(results, correct)
        else:
            # FAILED - Start remediation!
            return self._start_remediation(results, correct, failed_questions)
    
    def _update_competency(self, score: int, total: int, passed: bool) -> None:
        """Update StudentLessonProgress fields for every active participant.

        Source of truth: ExitTicketAttempt rows. This method is idempotent
        and monotonic:
          - best_score only rises (0.0-1.0 fraction).
          - mastery_level only promotes (never demotes from 'mastered').
          - attempts_count increments by 1 per call (one per submission).

        In group sessions every active participant is updated with the same
        score, and last_completion_was_group is set to True (H5).
        See memory/group_lessons_plan.md and memory/group_lessons_v2_plan.md.
        """
        score_pct = (score / total) if total else 0.0
        score_pct = max(0.0, min(1.0, round(score_pct, 4)))

        # Collect participants. Fallback to the legacy single-student session
        # owner so this works before G1 ships and for solo sessions after.
        try:
            participants = list(self.session.active_students)
        except Exception:
            participants = []
        if not participants:
            participants = [self.student]
        was_group = len(participants) > 1

        now = timezone.now()
        for student in participants:
            progress, _ = StudentLessonProgress.objects.get_or_create(
                student=student,
                lesson=self.lesson,
                defaults={'institution': self.session.institution},
            )
            if progress.best_score is None or score_pct > progress.best_score:
                progress.best_score = score_pct
            progress.attempts_count = (progress.attempts_count or 0) + 1
            progress.last_attempt_at = now
            progress.last_completion_session = self.session
            progress.last_completion_was_group = was_group
            if passed and progress.mastery_level != 'mastered':
                progress.mastery_level = 'mastered'
            elif progress.mastery_level == 'not_started' and progress.attempts_count > 0:
                progress.mastery_level = 'in_progress'
            progress.save(
                update_fields=[
                    'best_score',
                    'attempts_count',
                    'last_attempt_at',
                    'last_completion_session',
                    'last_completion_was_group',
                    'mastery_level',
                    'updated_at',
                ]
            )

    def _complete_session_with_results(self, results: List[Dict], score: int) -> TutorMessage:
        """Complete the session with exit ticket results."""
        self.session_state = SessionState.COMPLETED
        self.session.status = TutorSession.Status.COMPLETED
        self.session.ended_at = timezone.now()
        self.session.completed_lesson_at = timezone.now()
        self.session.mastery_achieved = True

        # Save per-EO exit ticket results for competency reporting
        achieved_eos = set()
        failed_eos = set()
        for r in results:
            eo = r.get('concept_tag', '')
            if not eo:
                continue
            if r.get('is_correct'):
                achieved_eos.add(eo)
            else:
                failed_eos.add(eo)

        # Update engine_state with EO results
        state = self.session.engine_state or {}
        state['exit_ticket_achieved_eos'] = list(achieved_eos)
        state['exit_ticket_failed_eos'] = list(failed_eos)
        state['exit_ticket_score'] = score
        state['exit_ticket_total'] = len(results)
        # Merge with covered_enabling_objectives from tutoring
        covered = set(state.get('covered_enabling_objectives', []))
        covered.update(achieved_eos)
        state['covered_enabling_objectives'] = list(covered)
        self.session.engine_state = state

        self._save_state()
        self.session.save()

        # Record ExitTicketAttempt per active participant (G3: group-aware)
        try:
            from apps.tutoring.models import ExitTicket, ExitTicketAttempt
            exit_ticket = ExitTicket.objects.filter(lesson=self.lesson).first()
            if exit_ticket:
                # Build per-question answer data (shared across participants)
                answer_data = []
                for r in results:
                    answer_data.append({
                        'concept_tag': r.get('concept_tag', ''),
                        'correct': r.get('is_correct', False),
                        'selected': r.get('selected', ''),
                        'question_type': r.get('question_type', 'mcq'),
                    })
                try:
                    participants = list(self.session.active_students)
                except Exception:
                    participants = []
                if not participants:
                    participants = [self.student]
                for participant in participants:
                    ExitTicketAttempt.objects.create(
                        exit_ticket=exit_ticket,
                        student=participant,
                        session=self.session,
                        score=score,
                        passed=True,
                        answers=answer_data,
                        completed_at=timezone.now(),
                    )
        except Exception as e:
            logger.warning(f"Failed to save ExitTicketAttempt: {e}")

        # Update progress (shared helper for pass + fail paths).
        self._update_competency(score=score, total=len(results), passed=True)

        # ── Gamification: XP + streak + achievements ──
        xp_earned = 0
        leveled_up = False
        earned_achievements = []
        try:
            from apps.tutoring.skills_models import StudentKnowledgeProfile
            from apps.tutoring.achievements import check_and_award
            from datetime import date

            course = self.lesson.unit.course
            profile, _ = StudentKnowledgeProfile.objects.get_or_create(
                student=self.student, course=course
            )

            # Update streak
            today = date.today()
            if profile.last_activity:
                last_date = profile.last_activity.date()
                delta = (today - last_date).days
                if delta == 1:
                    profile.current_streak_days += 1
                elif delta > 1:
                    profile.current_streak_days = 1
                # delta == 0 means same day, no change
            else:
                profile.current_streak_days = 1
            profile.longest_streak_days = max(profile.longest_streak_days, profile.current_streak_days)
            profile.last_activity = timezone.now()
            profile.save(update_fields=['current_streak_days', 'longest_streak_days', 'last_activity'])

            # Award XP
            total = len(results)
            xp_earned += 50  # exit ticket pass
            if total > 0 and score == total:
                xp_earned += 25  # perfect score bonus
            xp_earned += 100  # lesson mastery
            leveled_up = profile.add_xp(xp_earned, reason='lesson_complete')

            # Check achievements
            ctx = {'score': score, 'total': total}
            earned_achievements += check_and_award(self.student, 'exit_ticket_pass', ctx)
            if total > 0 and score == total:
                earned_achievements += check_and_award(self.student, 'perfect_score', ctx)
            earned_achievements += check_and_award(self.student, 'first_lesson', ctx)
            earned_achievements += check_and_award(self.student, 'lessons_completed', ctx)
            earned_achievements += check_and_award(self.student, 'streak_days', ctx)
            earned_achievements += check_and_award(self.student, 'xp_threshold', ctx)
            earned_achievements += check_and_award(self.student, 'level_reached', ctx)
        except Exception as e:
            logger.warning(f"Gamification error in _complete_session_with_results: {e}")

        # Build gamification payload for frontend
        gamification = {
            'xp_earned': xp_earned,
            'leveled_up': leveled_up,
            'achievements': [
                {'name': a.name, 'emoji': a.emoji, 'description': a.description}
                for a in earned_achievements
            ],
        }

        return TutorMessage(
            content=f"🎉 Excellent! You scored {score}/{len(results)}! You've mastered this lesson!",
            phase="completed",
            is_complete=True,
            exit_ticket_data={
                'results': results, 'score': score, 'passed': True,
                'gamification': gamification,
            },
        )
    
    def _start_remediation(
        self,
        results: List[Dict],
        score: int,
        failed_questions: List[Dict]
    ) -> TutorMessage:
        """
        Start targeted remediation based on failed ENABLING OBJECTIVES.

        Identifies which EOs the student missed on the exit ticket
        (via concept_tag = EO text), then re-teaches those specific objectives.
        """
        self.remediation_attempt = getattr(self, 'remediation_attempt', 0) + 1
        self.is_remediation = True

        # Use RemediationService to identify weak skills + prerequisite gaps
        # for the failed exit ticket. Persisted on self for downstream use
        # (system prompt context, dashboard reporting). See
        # apps/tutoring/tests/test_r5_remediation_wiring.py.
        try:
            from apps.tutoring.personalization import RemediationService
            remediation_service = RemediationService(self.student, self.lesson)
            total_count = len(results) or 1
            self._remediation_plan = remediation_service.get_remediation_plan(
                exit_ticket_score=score / total_count,
            )
        except Exception as e:
            logger.warning(f"Failed to get remediation plan: {e}")
            self._remediation_plan = None

        # Update competency (failed attempt still counts toward best_score and
        # attempt count; mastery only promotes, never demotes).
        self._update_competency(score=score, total=len(results), passed=False)

        # Record ExitTicketAttempt per active participant (G3: group-aware)
        try:
            from apps.tutoring.models import ExitTicket, ExitTicketAttempt
            exit_ticket = ExitTicket.objects.filter(lesson=self.lesson).first()
            if exit_ticket:
                answer_data = []
                for r in results:
                    answer_data.append({
                        'concept_tag': r.get('concept_tag', ''),
                        'correct': r.get('is_correct', False),
                        'selected': r.get('selected', ''),
                        'question_type': r.get('question_type', 'mcq'),
                    })
                try:
                    participants = list(self.session.active_students)
                except Exception:
                    participants = []
                if not participants:
                    participants = [self.student]
                for participant in participants:
                    ExitTicketAttempt.objects.create(
                        exit_ticket=exit_ticket,
                        student=participant,
                        session=self.session,
                        score=score,
                        passed=False,
                        answers=answer_data,
                        completed_at=timezone.now(),
                    )
        except Exception as e:
            logger.warning(f"Failed to save ExitTicketAttempt: {e}")

        # Extract failed enabling objectives from exit ticket concept_tags
        # First try concept_tag from the failed_questions dict (added in grading)
        failed_eos = set()
        for fq in failed_questions:
            tag = fq.get('concept_tag', '')
            if tag:
                failed_eos.add(tag)

        # Fallback: query DB for concept_tags
        if not failed_eos:
            from apps.tutoring.models import ExitTicketQuestion
            for fq in failed_questions:
                q = ExitTicketQuestion.objects.filter(id=fq['id']).first()
                if q and q.concept_tag:
                    failed_eos.add(q.concept_tag)

        # Last resort: use question text
        if not failed_eos:
            failed_eos = {fq['question'][:100] for fq in failed_questions[:5]}

        self.failed_exit_questions = failed_questions
        self._failed_eos = list(failed_eos)

        # Mark failed EO concepts as NOT covered
        failed_ids = {fq['id'] for fq in failed_questions}
        for concept in self.exit_ticket_concepts:
            if concept['id'] in failed_ids:
                concept['covered'] = False

        # Reset to tutoring state for targeted review
        self.session_state = SessionState.TUTORING
        self.exchange_count = 0
        # Reset step index to 0 so remediation doesn't jump back to exit ticket
        self.current_topic_index = 0
        self.step_exchange_count = 0

        self._save_state()

        # Generate EO-focused remediation message
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
                'failed_count': len(failed_questions),
            },
        )
    
    def _generate_remediation_opening(
        self,
        score: int,
        total: int,
        failed_questions: List[Dict]
    ) -> str:
        """Generate EO-focused remediation opening."""
        failed_eos = getattr(self, '_failed_eos', [])

        eo_list = "\n".join(f"  - {eo}" for eo in failed_eos[:4]) if failed_eos else "  - (general review needed)"

        prompt = f"""The student just completed the exit ticket but didn't pass.
Score: {score}/{total} (needed 8 to pass)
Attempt number: {self.remediation_attempt}

ENABLING OBJECTIVES they need to work on:
{eo_list}

Generate an encouraging message that:
1. Acknowledges their effort positively (no shame!)
2. Names the specific enabling objectives they'll review (use the EO text above)
3. Reassures them this is normal — learning takes practice
4. Starts teaching the FIRST enabling objective from the list above
5. Ask a simple question to check their understanding of that first EO

Keep it warm, supportive, and focused. 3-4 sentences + a question.
Do NOT just ask a quiz question — actually RE-TEACH the concept first, then check understanding."""

        return self._generate_response(prompt)
    
    # =========================================================================
    # HELPERS
    # =========================================================================
    
    def _save_turn(self, role: str, content: str, metadata: Optional[Dict] = None):
        """Save a conversation turn.

        metadata is an optional JSON-serializable dict attached to the
        SessionTurn.metadata JSONField. Used (for example) to record
        answer-evaluation results on tutor turns so the teacher dashboard
        and regression queries can see why the tutor judged a given
        student answer.
        """
        SessionTurn.objects.create(
            session=self.session,
            role=role,
            content=content,
            metadata=metadata or {},
        )
    
    def _parse_media_signal(self, text: str) -> Tuple[str, Optional[Dict], Optional[Dict]]:
        """Parse |||MEDIA:N||| or |||GENERATE:category:description||| from LLM output.

        Returns (clean_text, media_dict or None, generate_request or None).
        Signals are always stripped so nothing leaks into DB or student chat.
        """
        # Check for existing media signal
        match = re.search(r'\|\|\|MEDIA\s*:\s*(\d+)\s*\|\|\|', text)
        if match:
            clean_text = text[:match.start()].rstrip()
            media_id = int(match.group(1))
            if media_id == 0:
                return clean_text, None, None
            media_id_map = getattr(self, '_media_id_map', {})
            return clean_text, media_id_map.get(media_id), None

        # Check for generation signal
        gen_match = re.search(r'\|\|\|GENERATE\s*:\s*(\w+)\s*:\s*(.+?)\s*\|\|\|', text)
        if gen_match:
            clean_text = text[:gen_match.start()].rstrip()
            category = gen_match.group(1).lower()
            description = gen_match.group(2).strip()
            return clean_text, None, {
                'generate': True,
                'category': category,
                'description': description,
            }

        return text, None, None

    def _check_milestone(self) -> Optional[str]:
        """Check if the student hit a milestone worth celebrating."""
        step_num = min(self.current_topic_index + 1, len(self.steps)) if self.steps else 0
        total = len(self.steps)
        streak = getattr(self, '_correct_streak', 0)

        if streak >= 5:
            return "streak_5"
        if streak >= 3:
            return "streak_3"

        if getattr(self, '_step_just_advanced', False) and total > 2:
            if step_num == (total + 1) // 2:
                return "halfway"
            if step_num == total:
                return "final_step"

        if (self.practice_total >= 3
                and self.practice_correct == self.practice_total):
            return "perfect_run"

        return None

    def _parse_artifact_signal(self, text: str) -> Tuple[str, Optional[str]]:
        """Parse |||ARTIFACT:html|||...|||/ARTIFACT||| from LLM output.

        Returns (clean_text, artifact_html_or_None).
        """
        pattern = r'\|\|\|ARTIFACT:html\|\|\|(.*?)\|\|\|/ARTIFACT\|\|\|'
        match = re.search(pattern, text, re.DOTALL)
        if match:
            clean_text = text[:match.start()].rstrip() + text[match.end():].lstrip()
            artifact_html = match.group(1).strip()
            return clean_text, self._sanitize_artifact_html(artifact_html)
        return text, None

    _SAFE_TAGS = frozenset(
        'div span table thead tbody tr th td p h1 h2 h3 h4 h5 h6 '
        'ul ol li strong em b i br hr sup sub code pre '
        'figure figcaption caption '
        'svg path circle rect line text g polyline polygon'.split()
    )
    _SAFE_ATTRS = frozenset(
        'class style id colspan rowspan width height viewBox '
        'd cx cy r x y x1 y1 x2 y2 fill stroke stroke-width '
        'font-size text-anchor transform points xmlns'.split()
    )

    def _sanitize_artifact_html(self, html: str) -> str:
        """Defense-in-depth: strip dangerous tags/attributes from artifact HTML.

        Primary security is the frontend sandboxed iframe; this is belt-and-suspenders.
        """
        # Strip script, iframe, object, embed, form, link tags entirely
        html = re.sub(r'<\s*script[^>]*>.*?</\s*script\s*>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<\s*iframe[^>]*>.*?</\s*iframe\s*>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<\s*/?(object|embed|form|link|meta|base)\b[^>]*>', '', html, flags=re.IGNORECASE)
        # Strip event handler attributes (onclick, onerror, onload, etc.)
        html = re.sub(r'\s+on\w+\s*=\s*["\'][^"\']*["\']', '', html, flags=re.IGNORECASE)
        html = re.sub(r'\s+on\w+\s*=\s*\S+', '', html, flags=re.IGNORECASE)
        # Strip javascript: URLs
        html = re.sub(r'href\s*=\s*["\']javascript:[^"\']*["\']', '', html, flags=re.IGNORECASE)
        return html.strip()

    def _create_message(self, content: str, media: List[Dict] = None, artifact_html: str = None) -> TutorMessage:
        """Create a TutorMessage from content."""
        # Defense-in-depth: strip legacy, MEDIA, GENERATE, and ARTIFACT signal tags
        content = re.sub(r'\[SHOW_MEDIA\s*:[^\]]*\]', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\|\|\|MEDIA\s*:\s*\d+\s*\|\|\|', '', content)
        content = re.sub(r'\|\|\|GENERATE\s*:\s*\w+\s*:.+?\|\|\|', '', content)
        content = re.sub(r'\|\|\|ARTIFACT:html\|\|\|.*?\|\|\|/ARTIFACT\|\|\|', '', content, flags=re.DOTALL)
        content = re.sub(r' {2,}', ' ', content)
        content = re.sub(r'\n{3,}', '\n\n', content)
        content = content.strip()
        step_num = min(self.current_topic_index + 1, len(self.steps)) if self.steps else 0
        total = len(self.steps)
        return TutorMessage(
            content=content,
            phase=self._get_display_phase(),
            media=media or [],
            artifact_html=artifact_html,
            expects_response=self.session_state != SessionState.COMPLETED,
            step_number=step_num,
            total_steps=total,
            is_correct=getattr(self, 'last_answer_correct', False),
            streak_count=getattr(self, '_correct_streak', 0),
            practice_score=f"{self.practice_correct}/{self.practice_total}" if self.practice_total > 0 else "",
            milestone=self._check_milestone(),
        )