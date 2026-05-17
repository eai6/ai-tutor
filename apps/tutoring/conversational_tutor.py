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
from apps.tutoring.tracing import emit_span

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
- Keep responses SHORT. 1 sentence for feedback, 2-3 sentences for teaching, then a question.
  Long responses waste time. This is a 20-minute lesson — every exchange counts.
  Distil the lesson content; do not restate it verbatim.
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
- If the student showed working (chained arithmetic, equation rearrangement,
  multi-step derivation), evaluate it. Do NOT ask them to repeat or break it
  into more steps for you. Confirm what is right; flag the first specific
  step that is wrong; advance.
- If the student gave a BARE answer with no working, ask ONCE — in your
  own words — for their reasoning. Do not drip-feed step-by-step
  follow-ups across multiple turns; that's interrogation, not teaching.
- PROBE AT THE STEP LEVEL, NOT THE VALUE LEVEL. When the student
  states a correct elementary result (e.g. "50 / 10 = 5", "190 - 90 =
  100", "8 × 25 = 200"), accept it and move on — the operation IS
  the working. Probe questions belong at the strategy / decision
  level, never on the value of a single elementary calculation.
  GOOD probes: "Which operation should we apply first?", "Why did
  you divide?", "What rule are we using here?", "What does this
  result tell us about the original problem?"
  BAD probes: "How did you calculate 50 / 10?", "Walk me through
  8 × 25.", "How did you get 5?" — these are interrogations of
  elementary arithmetic the student already showed.
- CHECK INTERMEDIATE STEPS in working that has been shown, not the final
  answer alone. A correct answer with wrong method means the student
  doesn't truly understand.
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

<principle id="probe_frequency">
NO PROBING ON CORRECT ANSWERS — ADVANCE.

The default and ONLY response to a correct, on-topic student answer
is to CONFIRM (briefly, warmly) and ADVANCE to the next step. Never
ask the student to explain how they got there, what their reasoning
was, what made them choose a particular operation, or to walk
through their working. Even when their answer is bare ("8"), even
when you're curious about their process, even when you've been
explicitly told it's a learning moment — DO NOT PROBE.

Examples of banned probes (do NOT ask any of these on a correct
answer):
  - "How did you get there?"
  - "What was your reasoning?"
  - "Walk me through your steps."
  - "What made you identify X?"
  - "How did you decide to divide?"
  - "What's your reasoning for choosing that?"
  - "Can you explain how you arrived at the equation?"

ALSO BANNED: probing on the value of an elementary operation. If
the student stated a correct single-operation result, the
calculation IS the working. Do not ask them to re-justify it.
  - "How did you calculate 200 ÷ 25 to get 8?" — banned
  - "Can you show me 8 × 25?" — banned
  - "Walk me through 50 - 5." — banned
  - "What is 5 + 3?" (when the next step doesn't need it) — banned

When the answer is WRONG, you may diagnose the specific error in
one sentence — but that's diagnosis, not probing. Don't ask the
student to explain their wrong working; tell them what went wrong
and ask them to retry.

When the answer is partial (e.g. only the math, missing the
context), you may ASK FOR THE MISSING PIECE explicitly — that's a
completion request, not a probe. Example: student writes "50 - 5 =
45" on a word problem; you may ask "What does 45 represent here?"
That's asking for the final answer, not the reasoning.

Why this rule exists: pilot feedback (2026-05-12) showed the
tutor probing on every single correct answer — student felt
interrogated, conversation lost momentum. The pedagogical value of
probing is real but small compared to the cost of breaking flow.
We default to no probing; we do not currently make exceptions.
</principle>

<principle id="encouragement_calibration">
ENCOURAGEMENT — CALIBRATED, GENUINE, WITH A PULSE
The tutor should feel like a teacher who's enjoying the session,
not a checklist. Match the moment.

CORRECT + working shown → celebrate SPECIFICALLY (one short line):
  "Yes — and dividing both sides was exactly the right move."
  "Tight working. You spotted the inverse straight away."
  "Nice — one clean step and you're there."
The specificity makes it land. Generic "Good job!" or "Awesome!"
feels copy-pasted and the student tunes it out.

BARE numeric answer (no working shown) on practice/quiz →
math_specific Rule 1 still binds: do NOT use "correct", "right",
"brilliant", "exactly", "perfect", "you got it". Acknowledge the
value briefly + ask once for reasoning, then move on. Example:
  "You said 8. How did you get there?" — NOT "Perfect!"

WRONG answer → normalise + redirect (warm, not condescending):
  "Mistakes are how it sticks. Look at your second step — what
  changes if you isolate the unknown first?"
  "Close — but the inverse of + is - , not ÷. Try again."
Never: "That's wrong." flat. Never: "Don't worry, you'll get it!"
without pointing at the actual issue.

STREAK / MILESTONE (3+ correct in a row, halfway through the
lesson, lesson end) → celebrate the RUN, not the single answer:
  "Three in a row — you're getting fluent."
  "Halfway. Energy left for the harder ones?"
  "That's the lesson done. You went from rough to confident."

OFF-TOPIC / QUIRKY student aside → match the energy briefly
before steering back. The student is a teenager; a one-beat
acknowledgment ("ha — fair") + return to the math is fine. Stiff
"Let's stay focused, please." reads scolding.

Anti-patterns (avoid):
  - Effusive openers: "That's a fantastic observation!",
    "What a great question!" — banned by validator anyway.
  - Generic praise: "Good job!", "Awesome!", "Well done!" — too
    cheap to land.
  - Robotic acknowledgments: "Confirmed.", "Correct.", "Noted." —
    the student is a person, not a build pipeline.
  - Praise on bare/wrong answers — separately banned.
</principle>

<principle id="scaffold_consistency">
SCAFFOLD CONSISTENCY — COPY THE NUMBERS EXACTLY.

When you scaffold off a bank question (posed via the question_bank
or seen in the conversation), every numeric value, variable, and
equation you reference in your scaffold MUST match the bank
question's stem EXACTLY. Do not paraphrase the numbers.

Common failure mode (pilot 2026-05-12):
  Posed: "If a number x is increased by 15, the result is 40.
         What is x?"
  Correct answer: 25 (since x + 15 = 40)
  Tutor scaffold (WRONG): "To solve x + 15 = 25, what operation
                           should you apply to both sides?"
  Tutor scaffold (RIGHT): "We have x + 15 = 40. What operation
                          isolates x?"

The wrong scaffold confuses the equation (LHS = result) with the
answer (LHS = x). It is a hard pedagogical bug — the student is
forced to follow a contradictory thread.

Rule: before you reference an equation in scaffolding, read the
posed problem stem and copy the numbers verbatim into your
equation. The student's answer goes on its OWN side of the
equation, not in the equation itself.
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
- USE VISUALS WHEN AVAILABLE — for any concept that benefits from a
  visual (maps for geography, diagrams for geometry, processes for
  science, structures for biology, primary-source images for history),
  scan <media_catalog> for a relevant item. If one fits, REFERENCE it
  in your text ("Looking at the map of Africa, you can see…") AND
  emit |||MEDIA:N||| as the LAST line. Visuals make abstract concepts
  concrete; don't teach a map-able concept in pure text when a map
  exists.
- LAZY MEDIA — but don't attach a figure that isn't relevant. A
  numeric warmup with no visual concept should not auto-show a map.
  Match relevance to the current concept being taught.
- INVERSE RULE — if your text DOES reference a figure / diagram / image,
  you MUST emit |||MEDIA:N||| in the same turn so the student actually
  sees it. Saying "Looking at the diagram…" without attaching media
  leaves the student asking "where is the diagram?". Either attach a
  matching media item from the catalog, or rephrase the explanation
  WITHOUT the deictic figure reference.
- BANNED OPENERS — these exact phrases are forbidden in your responses
  because they leaked verbatim across many turns in pilot testing and made
  the tutor sound like a broken record:
    • "Let's check this one together — can you walk me through your steps?"
    • "Let's check this one together"
    • "Walk me through your steps"
    • "Walk me through how you got there"
    • "Show me your working, step by step"
    • "Before I check that — show me your working"
  If you need to ask about reasoning, phrase it your own way every time —
  vary the wording, keep it short, and never reuse the same opener across
  consecutive turns.
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
- HARD LIMIT: 2-3 sentences total, ~40 words max, ending in ONE question.
  This is a mobile chat. Long responses get scrolled past. If you need
  to teach more, split it across turns — deliver one idea, wait for the
  student's response, then continue.
- One paragraph only. No multi-paragraph responses. No "now let's…"
  followed by another full paragraph. If you find yourself starting a
  second paragraph, STOP and let the student respond first.
- Never produce a wall of text. If your draft is longer than 3
  sentences, cut it — pick the single most important sentence and ask.
- BE TERSE. Cut every word that doesn't pull weight. Banned padding:
    • Filler openers: "Great question!", "Let's see…", "I'm thinking…", "That's a good point.",
      "Excellent!", "Awesome!", "Nice work!" (a single 👍 or "Right." is fine).
    • Re-stating the question the student just answered.
    • Summarising what you just said in the previous turn.
    • Recapping what the student has learned so far ("You've mastered X,
      you understand Y, now…") — these summaries pad responses and add
      no teaching value. Just ask the next question.
    • Meta-commentary: "Now I'm going to explain X" — just explain X.
    • Prefacing every paragraph with "So,", "Now,", "Alright,", "Okay,".
- Get to the substance in the FIRST sentence. No warm-up.
- Praise is short. "Right." or "Exactly." — not paragraphs about why.
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

    # Metadata
    skills_covered: List[str] = field(default_factory=list)
    tokens_used: int = 0

    # R2 (2026-05-15): pending bank question for the artifact panel.
    # When the tutor poses a bank question on this turn (or one is
    # already in flight from a previous turn), this dict carries the
    # data the frontend needs to render the question in the side
    # artifact panel (R3) instead of relying on the inline prose.
    # Shape: {kind, question_id, question_type, turn_index, posed_at,
    # stem, options, ...} — backend looks up the canonical bank entry
    # and serialises it. None when no question is in flight.
    pending_question: Optional[Dict] = None


# Strip "thinking leakage" — opening sentences where the LLM narrates
# its own plan instead of just executing it. Triggered by phrases like
# "I need to address...", "Let me first clarify...", "First, I'll...".
# Only fires on the FIRST sentence, only on the start of the response,
# and only on this small known-bad pattern set so we don't accidentally
# strip legitimate teacher voice.
_THINKING_LEAK_RE = re.compile(
    r"^\s*(?:"
    r"I (?:need to|will|should|'?ll|am going to|must|have to|want to)"
    r"|Let me (?:first|start by|address|clarify|think|plan|tackle|handle)"
    r"|First[,]?\s+(?:I|let me|let's address|I'?ll)"
    r"|My (?:plan|approach|response|first step) (?:is|will|here)"
    r")\b[^.\n]*[.\n]\s*",
    re.IGNORECASE,
)


# =============================================================================
# Module helpers
# =============================================================================


# Numeric setup patterns the LLM uses when authoring a question (digits
# followed by units / equation operators). Catches "48 km", "75 SCR",
# "180°", "3x + 20 = 80", "5 kg", etc. without false-positiving on
# transitional phrases like "Try this one:".
_LEAD_IN_NUMERIC_SETUP_RE = re.compile(
    r'\d+\s*(km|kg|m\b|cm|°|deg|SCR|\$|%|/|×|x\s*=|=|\+|-\s*\d)',
    re.IGNORECASE,
)
# Verb phrases that indicate the lead_in is asking the student to do
# something (i.e. carrying a question, not a transition).
_LEAD_IN_QUESTION_VERB_RE = re.compile(
    r'\b(solve|find\s+(x|the|what)|write\s+(the\s+)?equation|what\s+is\s+the|'
    r'calculate|how\s+(many|much|do|does)|determine)\b',
    re.IGNORECASE,
)


# Probe patterns — sentences that ask the student to explain HOW they
# got a correct answer. These are banned on correct-answer turns per
# pilot directive 2026-05-12 ("the system should just move on when a
# correct answer is provided"). Server-side backstop: even if the
# system prompt + eval-signal block fail to keep the LLM in line, we
# strip these sentences out before the response reaches the student.
_PROBE_SENTENCE_RE = re.compile(
    r"(?:^|(?<=[.\n!?]))\s*"
    r"(?:"
    r"how\s+did\s+you\s+(?:get|approach|solve|work|figure|arrive|find|decide|know|choose|pick|determine|set\s+up|come\s+up)|"
    r"how\s+do\s+you\s+(?:know|see|think\s+about|approach|decide|choose|determine|set\s+up)|"
    r"what\s+(?:equation|method|approach|strategy|operation|step)\s+did\s+you|"
    r"what\s+(?:made|let|told|helped)\s+you\s+(?:decide|choose|pick|know|see)|"
    r"why\s+did\s+you\s+(?:choose|pick|decide|divide|multiply|add|subtract|use)|"
    r"what\s+was\s+your\s+(?:reasoning|approach|thinking|process|first\s+step|method|strategy)|"
    r"what\s+was\s+the\s+first\s+thing\s+you\s+(?:noticed|saw|did|tried)|"
    r"what\s+(?:did|do)\s+you\s+(?:notice|see)\s+(?:about|that|here)|"
    r"what\s+stood\s+out\s+(?:to\s+you|about)|"
    r"what(?:'s|\s+is)\s+your\s+(?:reasoning|approach|thinking|method|strategy)|"
    r"walk\s+me\s+through\s+(?:your|how|the\s+steps)|"
    r"can\s+you\s+(?:walk|explain|show)\s+(?:me\s+)?(?:through\s+)?(?:your|how)|"
    r"talk\s+me\s+through|"
    r"explain\s+(?:your|how\s+you)\s+(?:reasoning|thinking|got|approached|solved|decided)|"
    r"tell\s+me\s+(?:how|why|your\s+reasoning)"
    r")"
    # Consume up to the sentence-ending punctuation but NOT the
    # trailing whitespace — otherwise the next sentence's lookbehind
    # sees a space instead of the prior `.` / `!` / `?` and a second
    # consecutive probe is missed. Whitespace is normalized in the
    # post-pass below.
    r"[^.!?\n]*[.!?]",
    re.IGNORECASE,
)


def _strip_probe_sentences(content: str) -> Tuple[str, int]:
    """Remove reasoning-probe sentences from a tutor response.

    Returns ``(stripped_content, n_removed)``. Used only when the
    student's last answer was CORRECT — probing on correct answers
    is banned (pilot directive 2026-05-12).
    """
    if not content:
        return content, 0
    n = 0

    def _sub(_m):
        nonlocal n
        n += 1
        return ' '
    new_content = _PROBE_SENTENCE_RE.sub(_sub, content)
    # Collapse the double spaces / orphan blank lines the substitution leaves.
    new_content = re.sub(r'[ \t]{2,}', ' ', new_content)
    new_content = re.sub(r'\n[ \t]+', '\n', new_content)
    new_content = re.sub(r'\n{3,}', '\n\n', new_content)
    return new_content.strip(), n


# Detects MCQ-style option listings in a tutor text block.
# Examples that match:
#   "A) Physical geography\nB) Human geography\nC) ..."
#   "A. Physical\nB. Human\nC. ..."
# Requires at least 3 letter-labeled options on separate lines to
# avoid false positives on benign lists ("A and B are wrong").
_MCQ_OPTIONS_BLOCK_RE = re.compile(
    r'(?:^|\n)\s*A[\)\.]\s+\S.+\n\s*B[\)\.]\s+\S.+\n\s*C[\)\.]\s+\S',
    re.MULTILINE,
)


def _text_block_has_authored_question(text: str) -> bool:
    """Detect if a tutor text block already authored a question that
    would conflict with a bank pull.

    Two signals (either triggers):
      1) MCQ options block (A) / B) / C) [/ D)] on separate lines).
         Strong signal — the LLM is clearly authoring a multiple-choice
         question that the student will answer.
      2) Trailing question mark — the text ends with "?" indicating
         the LLM authored a question at the tail. Only triggers when
         the trailing question is substantive (>20 chars from the
         last sentence boundary), to skip "ok?" / "right?" tail
         tags inside the narrative.

    Use this when deciding whether a pose_question bank render would
    create a two-question turn. If True, skip the bank render so the
    student answers the AUTHORED question (graded via chat-authored
    grounded grader) instead of being mis-graded against the bank pull.
    """
    if not text or not text.strip():
        return False
    # Signal 1: MCQ options block.
    if _MCQ_OPTIONS_BLOCK_RE.search(text):
        return True
    # Signal 2: substantive tail question.
    stripped = text.rstrip()
    if stripped.endswith('?'):
        # Find the last sentence boundary before the '?'.
        sentences = re.split(r'(?<=[.!?])\s+', stripped)
        tail = sentences[-1].strip() if sentences else stripped
        if len(tail) > 20:
            return True
    return False


def _looks_like_authored_question(lead_in: str) -> bool:
    """Heuristic: does the lead_in carry an authored question / numeric
    setup the LLM should have left to the BANK slot?

    Multiple weak signals OR one strong signal → True. Designed to
    drop authored boat-problem-style lead_ins while leaving benign
    transitions ("Try this:", "Now apply that.", "Here's one more.")
    untouched.

    Strong signals (any one triggers a drop):
      - ends with '?'  (lead_in IS a question)
      - contains numeric setup pattern (digits + unit / operator)
      - contains a question verb phrase ("solve", "find x", etc.)

    Weak signal:
      - length > 120 chars (transitions don't need that much text)
        — only triggers when combined with another signal so genuine
        long transitions like "Now that you understand the rule, let's
        apply it to a slightly trickier case." stay.
    """
    if not lead_in:
        return False
    text = lead_in.strip()
    if not text:
        return False

    ends_with_q = text.endswith('?')
    has_numeric_setup = bool(_LEAD_IN_NUMERIC_SETUP_RE.search(text))
    has_question_verb = bool(_LEAD_IN_QUESTION_VERB_RE.search(text))
    too_long = len(text) > 120

    # Strong signals
    if ends_with_q or has_numeric_setup or has_question_verb:
        return True
    # Weak signal must combine with another (none here besides length,
    # so this is effectively a "very long, possibly authored" guard).
    if too_long:
        return True
    return False


# Sentence splitter for the text-block authored-question strip.
# Keeps the trailing punctuation on each sentence so re-joining
# preserves "Find x." not "Find x". Splits on `.`, `!`, `?` followed
# by whitespace or end-of-string. Conservative — does not try to be
# smart about abbreviations; the tutor doesn't use "Mr." / "Dr." in
# math content so the simple split is fine.
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')

# Detect an inline MCQ pattern in a tutor turn (the LLM authored MCQ
# options instead of calling pose_question). Anchored on the
# "A) ... B) ..." or "A. ... B. ..." run — requires at least two
# consecutive lettered options so a single throwaway "A) ..." in prose
# doesn't false-match. Used by the chat-authored grader fallback so a
# letter-only student reply gets graded against the full stem + options
# instead of being stranded with no anchor (task #173, 2026-05-17).
_INLINE_MCQ_RE = re.compile(
    r'(?m)^\s*A[\.\)]\s+\S.*(?:\r?\n|\r)\s*B[\.\)]\s+\S',
)


def _strip_authored_numeric_questions(text: str) -> Tuple[str, int]:
    """Remove sentences that look like authored numeric questions.

    Used inside the tutor text-block branch — the tutor's narrative
    must NEVER carry a math problem; bank questions go to the
    artifact panel via the pose_question tool. Pilot ground truth
    2026-05-16: user reported the tutor printed
    "Four angles around a point are 20°, 50°, 80°, and x°. Find x."
    as inline text alongside a different bank question in the
    artifact. Two questions on screen confuses students and breaks
    grading.

    A sentence is stripped when it carries STRONG authored-question
    signals:
      - `_LEAD_IN_QUESTION_VERB_RE` match — "find x", "solve",
        "calculate", "what is the", etc. These imperatives ask the
        student to compute and unambiguously mean "authored problem".
      - ends with '?' AND contains `_LEAD_IN_NUMERIC_SETUP_RE` match
        — a numeric question ("What do you get when you add
        20° + 50° + 80°?"). Catches check-questions the LLM smuggles
        into remediation prose.

    Numeric content ALONE is intentionally NOT a trigger: confirmation
    sentences like "Each sector measures 72°." are legitimate narrative
    and must survive. Likewise generic `ends_with_q` alone is allowed
    so Socratic probes like "Can you think of why this makes sense?"
    pass through unchanged.

    Returns (stripped_text, n_removed). When n_removed > 0 the caller
    should log it so we can measure how often the LLM disobeys the
    no-authoring rule.
    """
    if not text:
        return text, 0
    sentences = _SENTENCE_SPLIT_RE.split(text)
    kept: List[str] = []
    n_removed = 0
    for sent in sentences:
        s = sent.strip()
        if not s:
            continue
        has_question_verb = bool(_LEAD_IN_QUESTION_VERB_RE.search(s))
        has_numeric_setup = bool(_LEAD_IN_NUMERIC_SETUP_RE.search(s))
        ends_with_q = s.endswith('?')
        if has_question_verb or (ends_with_q and has_numeric_setup):
            n_removed += 1
            continue
        kept.append(s)
    if n_removed == 0:
        return text, 0
    return ' '.join(kept).strip(), n_removed


def _normalize_for_overlap(s: str) -> str:
    """Lowercase + collapse whitespace + strip leading/trailing punctuation
    so two restatements of the same question match despite trivial
    formatting differences.
    """
    if not s:
        return ''
    s = re.sub(r'\s+', ' ', s.strip().lower())
    # Trim leading/trailing punctuation that varies by author
    s = s.strip('.,;:!?"\'()[]{}')
    return s


def _strip_bank_overlap_sentences(
    text: str,
    bank_rendered_text: str,
    *,
    min_overlap_chars: int = 25,
) -> Tuple[str, int]:
    """Remove sentences from `text` that substantially overlap with the
    rendered bank question text.

    Catches the failure mode the e2e pilot 2026-05-16 surfaced:
    tutor narrative was repeating the bank question's stem AND the
    artifact was rendering the same question — two copies on screen.
    The `_strip_authored_numeric_questions` heuristic only catches
    sentences ending in a question verb ("Find x.") and leaves the
    SETUP sentence ("Three angles around a point are 100°, 50°, and
    x°.") intact. This overlap-based stripper catches both because
    it compares against the actual rendered bank text rather than
    relying on syntactic shape.

    A tutor sentence is dropped when ANY bank-text sentence is
    substantially contained inside it OR it inside the bank sentence,
    where "substantially" means normalized substring of at least
    `min_overlap_chars`. Normalization: lowercase, collapse whitespace,
    strip surrounding punctuation.

    Returns (cleaned_text, n_removed). Safe on empty inputs (returns
    text unchanged).
    """
    if not text or not bank_rendered_text:
        return text, 0
    bank_norm = _normalize_for_overlap(bank_rendered_text)
    if len(bank_norm) < min_overlap_chars:
        return text, 0
    bank_sentences = [
        _normalize_for_overlap(s)
        for s in _SENTENCE_SPLIT_RE.split(bank_rendered_text)
    ]
    bank_sentences = [b for b in bank_sentences if len(b) >= min_overlap_chars]
    # Always also consider the full bank text as one chunk — some
    # bank entries render as a single long sentence without
    # punctuation breaks.
    candidates = bank_sentences + [bank_norm]

    sentences = _SENTENCE_SPLIT_RE.split(text)
    kept: List[str] = []
    n_removed = 0
    for sent in sentences:
        s = sent.strip()
        if not s:
            continue
        s_norm = _normalize_for_overlap(s)
        if len(s_norm) < min_overlap_chars:
            kept.append(s)
            continue
        matched = False
        for bc in candidates:
            if not bc:
                continue
            # Either direction: tutor sentence is in bank, OR bank
            # chunk is in tutor sentence. Both indicate the tutor is
            # reading the bank question to the student.
            if s_norm in bc or bc in s_norm:
                matched = True
                break
        if matched:
            n_removed += 1
            continue
        kept.append(s)
    if n_removed == 0:
        return text, 0
    return ' '.join(kept).strip(), n_removed


# Duck-typed question for inline-authored grading. Matches the
# attribute shape that bank_grader.grade_bank_response + the LLM
# batch grader expect (question_text, question_type, correct_answer,
# answer_data, plus optional option_a..d for MCQ). Created on the fly
# from the tutor's pose_inline_question tool input so we don't need
# a DB row for tutor-authored throwaway questions.
class _InlineAuthoredQuestion:
    def __init__(
        self,
        *,
        question_text: str,
        question_type: str,
        correct_answer: str,
        answer_data: Optional[Dict] = None,
    ):
        self.question_text = question_text or ''
        self.question_type = question_type or 'short_answer'
        self.correct_answer = correct_answer or ''
        self.answer_data = answer_data or {}
        # ExitTicketQuestion attributes the grader/batch-builder may
        # touch; leave as empty defaults.
        self.option_a = ''
        self.option_b = ''
        self.option_c = ''
        self.option_d = ''
        self.id = 0  # not a real DB id


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

        # Load all lesson steps from the DB.
        all_steps = list(
            LessonStep.objects.filter(lesson=self.lesson)
            .order_by('order_index')
        )

        # MAX-DEPTH STEP SELECTION (2026-04-29):
        # Lessons are generated at full depth (10 steps); the engine
        # picks a subset based on the session's target duration. The
        # full step set is kept on `self.all_steps` for reference
        # (debug, teacher review); `self.steps` is the filtered
        # session-active set.
        self.all_steps = all_steps
        # Use the TUTORING budget (full target minus the exit-ticket
        # reserve). If the user picks 15 minutes, tutoring fits into
        # ~10 minutes; the last 5 are held back for the exit ticket.
        self.steps = self._select_steps_for_duration(
            all_steps, self._tutoring_minutes_budget()
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
                # New session: select 10 from the full bank.
                # Skip data_interpretation — disabled platform-wide.
                # Also exclude question IDs already served as a
                # DIAGNOSTIC pre-test for this student so the post-test
                # is disjoint (the bank ships with 35 questions —
                # plenty of room for 10 pre + 10 post).
                from apps.tutoring.models import ExitTicketAttempt
                served_diag_ids = set()
                for prior in ExitTicketAttempt.objects.filter(
                    exit_ticket=exit_ticket,
                    student=self.student,
                    purpose=ExitTicketAttempt.Purpose.DIAGNOSTIC,
                ).only('answers'):
                    for qid in (prior.answers or {}).get('selected_question_ids', []) or []:
                        try:
                            served_diag_ids.add(int(qid))
                        except (TypeError, ValueError):
                            continue

                # Tutoring/exit-ticket mutual exclusivity: exclude the
                # full per-session tutoring bank pool (posed AND unposed)
                # so the exit ticket genuinely tests transfer rather
                # than memory of the specific questions practiced this
                # session. The pool is stored in engine_state when the
                # tutoring bank was first sampled (see
                # apps/tutoring/question_bank.py::sample_session_pool).
                tutoring_pool_ids = set(
                    (self.session.engine_state or {})
                    .get('question_pool_ids') or []
                )

                base_qs = ExitTicketQuestion.objects.filter(
                    exit_ticket=exit_ticket,
                ).exclude(question_type='data_interpretation')
                if served_diag_ids:
                    base_qs = base_qs.exclude(id__in=served_diag_ids)
                if tutoring_pool_ids:
                    base_qs = base_qs.exclude(id__in=tutoring_pool_ids)
                all_questions = list(base_qs.order_by('order_index'))

                # Fallback chain — preserve quality before relaxing.
                # 1. If exclusion of BOTH diagnostic + tutoring pool
                #    left <10 questions, relax tutoring-pool first
                #    (cosmetic — the diagnostic-overlap rule is older
                #    and stricter than the new no-overlap-with-tutoring
                #    constraint). Log so we know the lesson's bank is
                #    under-sized for strict separation.
                if len(all_questions) < 10 and tutoring_pool_ids:
                    logger.warning(
                        "[ExitTicket] lesson=%s bank too small for "
                        "strict tutoring/exit-ticket separation — "
                        "relaxing tutoring-pool exclusion "
                        "(bank=%d after diag-only exclude). Consider "
                        "expanding the published exit-ticket bank.",
                        self.lesson.id,
                        ExitTicketQuestion.objects.filter(
                            exit_ticket=exit_ticket,
                        ).exclude(question_type='data_interpretation')
                        .exclude(id__in=served_diag_ids).count()
                        if served_diag_ids else
                        ExitTicketQuestion.objects.filter(
                            exit_ticket=exit_ticket,
                        ).exclude(question_type='data_interpretation').count(),
                    )
                    relaxed = ExitTicketQuestion.objects.filter(
                        exit_ticket=exit_ticket,
                    ).exclude(question_type='data_interpretation')
                    if served_diag_ids:
                        relaxed = relaxed.exclude(id__in=served_diag_ids)
                    all_questions = list(relaxed.order_by('order_index'))

                # 2. Existing final fallback: if STILL <10, allow the
                # diagnostic IDs back in so we don't ship a tiny test.
                if len(all_questions) < 10 and served_diag_ids:
                    all_questions = list(
                        ExitTicketQuestion.objects.filter(exit_ticket=exit_ticket)
                        .exclude(question_type='data_interpretation')
                        .order_by('order_index')
                    )

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
    
    # Minutes reserved at the end of the session for the exit ticket.
    # The tutoring portion gets (target_minutes - EXIT_TICKET_RESERVE).
    EXIT_TICKET_RESERVE_MINUTES = 5

    def _target_minutes_for_session(self) -> int:
        """Return the target session duration in minutes (FULL budget
        — tutoring + exit ticket).

        Resolution order:
          1. session.engine_state['target_minutes_override'] — set
             when a student picks "I have X minutes" or a teacher
             configures the session explicitly.
          2. lesson.estimated_minutes — the course-level default.
          3. 20 — sensible fallback.
        """
        try:
            override = (self.session.engine_state or {}).get('target_minutes_override')
            if override:
                return int(override)
        except Exception:
            pass
        return self.lesson.estimated_minutes or 20

    def _tutoring_minutes_budget(self) -> int:
        """Tutoring portion of the budget — total minus the exit-ticket
        reserve. If the user picks 15 min, tutoring gets 10. Floor at
        5 so a 5-min session still has SOMETHING for tutoring."""
        target = self._target_minutes_for_session()
        return max(5, target - self.EXIT_TICKET_RESERVE_MINUTES)

    def _minutes_elapsed(self) -> float:
        """Minutes since the lesson started. Returns 0.0 when the
        session hasn't begun yet."""
        started = getattr(self.session, 'started_lesson_at', None)
        if not started:
            return 0.0
        try:
            delta = timezone.now() - started
            return max(0.0, delta.total_seconds() / 60.0)
        except Exception:
            return 0.0

    def _build_time_awareness_block(self) -> str:
        """Render the <time_awareness> block injected into the system
        prompt per turn. Tells the tutor how much tutoring time
        remains and prescribes a pace adjustment.

        Pace bands (steps_left = remaining steps to advance through):
          - remaining_per_step >= 3 min → "on track, normal pace"
          - remaining_per_step in [1.5, 3) → "getting tight, trim
            explanations, accept correct answers fast"
          - remaining_per_step < 1.5 → "behind schedule, advance
            aggressively; one-line confirms, no optional concepts"
        Exit-ticket buffer of EXIT_TICKET_RESERVE_MINUTES is held back
        from the budget so the tutor never burns it.
        """
        budget = self._tutoring_minutes_budget()
        elapsed = self._minutes_elapsed()
        remaining = max(0.0, budget - elapsed)
        steps_left = max(0, len(self.steps) - self.current_topic_index)
        per_step = remaining / steps_left if steps_left > 0 else remaining

        if steps_left == 0 or per_step >= 3.0:
            pace = (
                "On track. Normal pace — explain, ask, advance as usual."
            )
        elif per_step >= 1.5:
            pace = (
                "Getting tight. Trim explanations to one short paragraph,"
                " accept correct answers in one line, skip optional"
                " concepts on this step."
            )
        else:
            pace = (
                "BEHIND SCHEDULE. Advance aggressively: one-line confirms"
                " on correct answers, no optional sub-questions, skip to"
                " the step's CORE concept and move on. The student needs"
                " to reach the exit ticket with "
                f"{self.EXIT_TICKET_RESERVE_MINUTES} min spare."
            )

        avg_line = (
            f"\nAvg time per remaining step: {per_step:.1f}m."
            if steps_left else "\n(no remaining steps)"
        )
        return (
            "\n\n<time_awareness>"
            f"\nSession budget: {budget}m tutoring + "
            f"{self.EXIT_TICKET_RESERVE_MINUTES}m exit-ticket reserve."
            f"\nElapsed: {elapsed:.1f}m."
            f"\nRemaining tutoring time: {remaining:.1f}m."
            f"\nSteps left in this session: {steps_left}."
            + avg_line
            + f"\nPace directive: {pace}"
            + "\n</time_awareness>"
        )

    # Duration → step-count map. Reduced 2026-05-14 alongside the
    # lesson-generation reduction (10 → 5 steps; see commit refs in
    # content_generator.py max_steps comment). 25-min lesson is the
    # max; older 10-step lessons are still supported but get clamped
    # to 5 steps maximum.
    DURATION_TO_STEP_COUNT = {
        15: 3,
        20: 4,
        25: 5,
    }
    MAX_STEPS_PER_SESSION = 5

    def _select_steps_for_duration(self, all_steps: List, target_minutes: int) -> List:
        """Pick the subset of steps that fits the target duration.

        Algorithm:
          - Look up target_count from DURATION_TO_STEP_COUNT (15/20/25
            → 3/4/5). For unmapped durations, derive: 1 step per ~5
            minutes, clamped to [3, MAX_STEPS_PER_SESSION].
          - If we have fewer steps than the target, return everything.
          - Otherwise:
            * Always include the FIRST step (the engage hook).
            * Fill the remaining slots from the rest by ascending
              (priority, order_index) — required steps come in first,
              then core, then enrichment. Ties broken by natural
              lesson order so the 5E flow stays coherent.
          - Re-sort the picked set by order_index so the lesson
            progression is preserved.

        Note: previously the LAST step was also always-included
        because it was the QUIZ. Since the 5-step structure dropped
        the internal QUIZ (exit ticket is separate now), we don't
        force-include the last step anymore — priority does all the
        work. Existing 10-step lessons still work: their last step
        will be picked via priority if it's REQUIRED.

        See memory/max_depth_lesson_steps_plan.md.
        """
        if not all_steps:
            return []

        target_count = self.DURATION_TO_STEP_COUNT.get(target_minutes)
        if target_count is None:
            target_count = max(3, min(self.MAX_STEPS_PER_SESSION, target_minutes // 5))
        if len(all_steps) <= target_count:
            return list(all_steps)

        # Always include the engage hook (first step).
        first_idx = 0
        must_include = {first_idx}

        rest = [
            (i, s) for i, s in enumerate(all_steps) if i not in must_include
        ]
        # Sort by (priority asc, order_index asc): priority-1 first,
        # ties broken by lesson order.
        rest.sort(key=lambda pair: (
            getattr(pair[1], 'priority', 1),
            pair[1].order_index,
        ))

        slots_to_fill = max(0, target_count - len(must_include))
        chosen_indices = set(must_include) | {
            i for i, _ in rest[:slots_to_fill]
        }
        return [all_steps[i] for i in sorted(chosen_indices)]

    def _load_enabling_objectives(self) -> List[Dict]:
        """Load enabling objectives for systematic coverage tracking (P1.2).

        Routes through `combined_objectives_for_lesson` (the single
        source of truth — same helper the summative + competency map
        use) so the tutor never sees a different objective list than
        the matrix that reports on it.
        """
        from apps.curriculum.content_generator import combined_objectives_for_lesson
        all_objectives = set()

        # Lesson-level objectives via the canonical helper (TOs + EOs
        # + lesson.objective fallback chain).
        for obj in combined_objectives_for_lesson(self.lesson):
            if obj and obj.strip():
                all_objectives.add(obj.strip())

        # From step-level enabling_objective fields (per-step
        # granularity used inside this engine for scaffolding —
        # never propagated to cross-attempt reporting).
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

    def _load_recent_diagnostic(self) -> Optional[Dict]:
        """If the student took a DIAGNOSTIC pre-test for this lesson
        recently AND failed it, return the per-EO sub-skill map for
        the tutor's [PRE-TEST RESULT] prompt block.

        Skip if they passed (lesson is already mastered) or if no
        pre-test was taken. Returns the most recent failed attempt's
        digest, or None.
        """
        try:
            from apps.tutoring.models import ExitTicket, ExitTicketAttempt
            exit_ticket = ExitTicket.objects.filter(lesson=self.lesson).first()
            if not exit_ticket:
                return None
            attempt = ExitTicketAttempt.objects.filter(
                exit_ticket=exit_ticket,
                student=self.student,
                purpose=ExitTicketAttempt.Purpose.DIAGNOSTIC,
                completed_at__isnull=False,
            ).order_by('-completed_at').first()
            if not attempt or attempt.passed:
                return None
            ad = attempt.answers or {}
            return {
                'achieved_eos': ad.get('achieved_eos', []),
                'failed_eos': ad.get('failed_eos', []),
                'score': attempt.score,
                'total': ad.get('total', 0),
                'completed_at': attempt.completed_at.isoformat() if attempt.completed_at else None,
            }
        except Exception:
            return None

    def _derive_initial_difficulty(self) -> int:
        """Derive initial `difficulty_level` for a NEW session from the
        student's skills_snapshot for this lesson's course.

        Returns:
            -1 (easy mode — MCQ + fill-in-blank, accept short answers)
                when mean mastery < 40% or no signal at all (treat
                first-time students as low-performers per the pilot
                feedback that lower performers struggled with the
                open-text default).
             0 (balanced — current default behavior) when 40–69%.
            +1 (hard mode — open-text, working required) when ≥ 70%.

        Per memory/feedback_adaptive_difficulty.md. Buttons still
        override this within the session.
        """
        try:
            from apps.accounts.models import StudentProfile
            profile = StudentProfile.objects.filter(user=self.student).first()
            if not profile:
                return -1  # no profile → assume new student → easy
            snap = profile.skills_snapshot or {}
            course = self.lesson.unit.course if self.lesson and self.lesson.unit else None
            if not course:
                return -1
            slice_for_course = snap.get(str(course.id)) or {}
            if not isinstance(slice_for_course, dict) or not slice_for_course:
                return -1  # no signal yet → easy
            pcts = [
                v.get('pct', 0) for v in slice_for_course.values()
                if isinstance(v, dict) and v.get('pct') is not None
            ]
            if not pcts:
                return -1
            mean_pct = sum(pcts) / len(pcts)
            if mean_pct >= 70:
                return 1
            if mean_pct >= 40:
                return 0
            return -1
        except Exception:
            return -1  # fail safe → easy mode

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

        # R2 (2026-05-15): per-turn bank question lookup for the
        # artifact panel + resume system. Same shape as _turn_media:
        # {turn_index_str: {question_id, kind, posed_at}}.
        self._turn_questions = state.get('turn_questions', {})

        # R2 (2026-05-15): "we're awaiting an answer to question X"
        # state. Set when a bank question is posed; cleared when the
        # next student message arrives. Drives the resume system (R5)
        # — on session reload, if awaiting_answer is set we re-render
        # the question in the artifact panel rather than asking the
        # student a brand-new question. None when no question is
        # in flight.
        self._awaiting_answer = state.get('awaiting_answer') or None

        # Worked example deduplication (Issue 1)
        self.shown_worked_example_indices = set(state.get('shown_worked_example_indices', []))

        # Difficulty signal (ZPD adjustment).
        # New sessions seed `difficulty_level` from the student's
        # skills_snapshot for this course (low mastery → -1 = easy mode
        # with MCQ + fill-in-blank scaffolding; high mastery → +1 = hard
        # mode with open-text working). Per the adaptive-difficulty
        # decision (memory/feedback_adaptive_difficulty.md). Returning
        # students keep whatever the buttons set last time.
        if 'difficulty_level' in state:
            self.difficulty_level = state['difficulty_level']
        else:
            self.difficulty_level = self._derive_initial_difficulty()

        # Pre-test diagnostic — if the student took the lesson's
        # diagnostic and DIDN'T pass, the most recent attempt's per-EO
        # results are surfaced as a [PRE-TEST RESULT] prompt block so
        # the tutor focuses on weak sub-skills. Resume sessions keep
        # whatever was already in state; new sessions seed it.
        if 'pretest_diagnostic' in state:
            self.pretest_diagnostic = state['pretest_diagnostic']
        else:
            self.pretest_diagnostic = self._load_recent_diagnostic()
            if self.pretest_diagnostic:
                # Cache on engine_state so this is stable across resumes.
                state['pretest_diagnostic'] = self.pretest_diagnostic
                self.session.engine_state = state

        # Correct-answer streak for in-conversation gamification
        self._correct_streak = state.get('correct_streak', 0)

        # Cognitive load adaptation
        self.cognitive_load = state.get('cognitive_load', 0.5)
        self.consecutive_wrong = state.get('consecutive_wrong', 0)
        self.consecutive_correct_streak = state.get('consecutive_correct_streak', 0)
        # Auto-difficulty (2026-05-17 pilot directive). Tracks
        # consecutive first-try-correct answers so the engine can bump
        # difficulty up after 2 in a row. Reset when verdict is wrong
        # or when the student needed >0 wrong attempts before getting
        # the answer (i.e., it wasn't a first-try correct).
        self.consecutive_first_try_correct = state.get(
            'consecutive_first_try_correct', 0,
        )

        # Bare-answer count per step (M9 — pedagogy layer 4). Keys stringified
        # because JSON object keys are strings; we coerce on read.
        raw_bare_counts = state.get('bare_answer_counts_by_step', {}) or {}
        self.bare_answer_counts_by_step: Dict[int, int] = {
            int(k): int(v) for k, v in raw_bare_counts.items()
        }

        # Track ExitTicketQuestion IDs already posed in this session so
        # the bank picker doesn't recycle the same question after the
        # student answered it. Reset only at session start.
        self.shown_question_ids: set = set(
            int(qid) for qid in (state.get('shown_question_ids', []) or [])
            if str(qid).lstrip('-').isdigit()
        )

        # Track normalised signatures of the last N tutor questions —
        # used by the W14 repeated-question guard to catch cross-turn
        # authored repeats. Cap at 10 (oldest dropped) to keep
        # engine_state JSON compact. See apps/tutoring/repeated_question.py.
        self.recent_tutor_question_sigs: List[str] = list(
            state.get('recent_tutor_question_sigs', []) or []
        )[-10:]

        # Exit-ticket hold gate (2026-05-17, pilot directive). When the
        # tutor reveals a wrong-answered bank Q or the most recent grade
        # was wrong, hold off triggering the exit ticket for one more
        # student turn so the student gets a chance to acknowledge /
        # discuss. Value = exchange_count at which the hold expires
        # (i.e., next exchange after the wrong-revealed turn).
        self.exit_ticket_hold_until_exchange: int = int(
            state.get('exit_ticket_hold_until_exchange', 0) or 0
        )

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
            # R2: per-turn bank question lookup + awaiting-answer state
            # for the resume system. See _load_state for shape.
            'turn_questions': getattr(self, '_turn_questions', {}),
            'awaiting_answer': getattr(self, '_awaiting_answer', None),
            # Worked example deduplication (Issue 1)
            'shown_worked_example_indices': list(getattr(self, 'shown_worked_example_indices', set())),
            # Covered enabling objectives (for competency report)
            'covered_enabling_objectives': [
                o['objective'] for o in getattr(self, 'enabling_objectives', []) if o.get('covered')
            ],
            # Difficulty signal (ZPD adjustment)
            'difficulty_level': getattr(self, 'difficulty_level', 0),
            # Pre-test diagnostic — surfaced as [PRE-TEST RESULT] block
            'pretest_diagnostic': getattr(self, 'pretest_diagnostic', None),
            # Correct-answer streak for in-conversation gamification
            'correct_streak': getattr(self, '_correct_streak', 0),
            # Cognitive load adaptation
            'cognitive_load': getattr(self, 'cognitive_load', 0.5),
            'consecutive_wrong': getattr(self, 'consecutive_wrong', 0),
            'consecutive_correct_streak': getattr(self, 'consecutive_correct_streak', 0),
            'consecutive_first_try_correct': getattr(
                self, 'consecutive_first_try_correct', 0,
            ),
            # Bare-answer counts per step (M9). JSON requires string keys.
            'bare_answer_counts_by_step': {
                str(k): v for k, v in getattr(self, 'bare_answer_counts_by_step', {}).items()
            },
            # Bank questions already posed this session — bank picker
            # excludes these so the tutor doesn't recycle the same
            # question after the student answered it.
            'shown_question_ids': sorted(getattr(self, 'shown_question_ids', set())),
            # W14 — last 10 normalised signatures of tutor questions
            # so the repeated-question guard survives across turns +
            # session resumes.
            'recent_tutor_question_sigs': list(
                getattr(self, 'recent_tutor_question_sigs', []) or []
            )[-10:],
            # Exit-ticket hold gate — prevent the trigger from firing
            # on the same turn as a reveal or right after a wrong answer.
            'exit_ticket_hold_until_exchange': int(
                getattr(self, 'exit_ticket_hold_until_exchange', 0) or 0
            ),
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
            r'\[SHOW_MEDIA\s*:[^\]]*\]|\|\|\|MEDIA\s*:\s*\d+\s*\|\|\||\|\|\|GENERATE\s*:\s*\w+\s*:.+?\|\|\||\|\|\|PROBE\s*:\s*\{.+?\}\s*\|\|\||\|\|\|QUESTION\s*:\s*\d+\s*\|\|\|',
            re.IGNORECASE | re.DOTALL,
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
    def judge_client(self):
        """Lazy load the post-response judge LLM client.

        The judge is a sanity layer (combined_judge: arithmetic +
        factual + rule_compliance) — it doesn't need the tutor's
        reasoning depth. Routing it to a faster / cheaper model
        (Sonnet/Haiku) cuts per-turn latency without compromising
        rule enforcement. Falls back to the tutoring client when no
        Purpose.JUDGE config is active (legacy behaviour).
        """
        if not hasattr(self, '_judge_client'):
            self._judge_client = None
        if self._judge_client is None:
            try:
                from apps.llm.models import ModelConfig
                from apps.llm.client import get_llm_client

                config = ModelConfig.get_for('judge')
                if config:
                    self._judge_client = get_llm_client(config)
                    logger.info(
                        "[QuestionTool] judge_client: provider=%s model=%s",
                        config.provider, config.model_name,
                    )
                else:
                    # No dedicated judge config → fall back to tutoring.
                    self._judge_client = self.llm_client
                    logger.info(
                        "[QuestionTool] judge_client: no Purpose.JUDGE config; "
                        "falling back to tutoring client",
                    )
            except Exception as e:
                logger.error(f"Could not load judge client: {e}")
                self._judge_client = self.llm_client
        return self._judge_client

    @property
    def regen_clients(self):
        """Lazy-load the regen ensemble clients.

        Resolves all active `Purpose.REGEN` ModelConfigs and returns
        the corresponding `BaseLLMClient` instances. When no active
        REGEN configs exist, falls back to a single-element list
        containing the tutoring client (so the ensemble degrades to
        single-model regen).

        The list determines the ensemble size — pilot can ship with 1
        config (single-model regen, same behaviour as before but with
        the focused prompt), or scale up to 2-3 by adding REGEN
        ModelConfigs in the admin.
        """
        if not hasattr(self, '_regen_clients'):
            self._regen_clients = None
        if self._regen_clients is not None:
            return self._regen_clients

        try:
            from apps.llm.models import ModelConfig
            from apps.llm.client import get_llm_client

            inst = self.lesson.unit.course.institution if self.lesson else None
            qs = ModelConfig.objects.filter(
                purpose='regen', is_active=True,
            )
            if inst is not None:
                # Prefer institution-scoped configs; fall back to
                # platform-wide (institution=None) when none exist.
                inst_configs = list(qs.filter(institution=inst))
                if inst_configs:
                    configs = inst_configs
                else:
                    configs = list(qs.filter(institution__isnull=True))
            else:
                configs = list(qs)

            if configs:
                clients = []
                for cfg in configs:
                    try:
                        clients.append(get_llm_client(cfg))
                    except Exception as e:
                        logger.warning(
                            "[Regen] skipping REGEN config %s: %s",
                            cfg.model_name, e,
                        )
                if clients:
                    logger.info(
                        "[Regen] ensemble configured: %d model(s) — %s",
                        len(clients),
                        ", ".join(
                            f"{getattr(getattr(c, 'config', None), 'provider', '?')}/"
                            f"{getattr(getattr(c, 'config', None), 'model_name', '?')}"
                            for c in clients
                        ),
                    )
                    self._regen_clients = clients
                    return self._regen_clients

            # No REGEN configs → degrade to single-model regen with
            # the tutoring client. Same behaviour pre-ensemble.
            logger.info(
                "[Regen] no active Purpose.REGEN configs — falling back "
                "to single-model regen with tutoring client"
            )
            self._regen_clients = [self.llm_client]
        except Exception as e:
            logger.error(f"[Regen] could not load regen clients: {e}")
            self._regen_clients = [self.llm_client]
        return self._regen_clients

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
        msg = self._generate_opening()
        # Attach pending_question (artifact-panel payload) — same as
        # respond() does after _respond_impl. Pilot e2e 2026-05-16:
        # the opener pose_question correctly set awaiting_answer in
        # engine_state, but without this attach the frontend artifact
        # panel never rendered, leaving the student stuck with no
        # question to answer.
        try:
            if isinstance(msg, TutorMessage):
                msg.pending_question = self._build_pending_question_payload()
        except Exception as _exc:
            logger.warning(
                f"[PendingQuestion] start() attach failed: "
                f"{type(_exc).__name__}: {_exc}"
            )
        return msg

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
4. Only show media if your text directly references the figure (e.g. "the diagram below shows…"). Otherwise omit |||MEDIA:N|||.

Keep it to 1-2 sentences + question, ~60 words max."""

        response = self._generate_response(prompt, fallback_context="resume")

        # Parse |||MEDIA:N||| signal BEFORE saving — keeps DB clean
        clean_response, parsed_media = self._parse_media_signal(response)
        media = [parsed_media] if parsed_media else []

        # Trust the |||MEDIA:N||| signal — no inference fallback. If
        # the LLM wanted a figure shown, it emits the signal. Anything
        # else (figurative language, conceptual references, prose
        # mentions of "image"/"diagram") does NOT auto-attach.

        # Record media for this turn (for resume artifact panel)
        if media:
            turn_index = len(self.conversation)  # index before appending
            self._turn_media[str(turn_index)] = media[0]

        # Don't persist fallback messages — they pollute conversation history (Fix 2)
        if not self._last_response_was_fallback:
            self._save_turn("tutor", clean_response)
            self.conversation.append({"role": "assistant", "content": clean_response})
            self._save_state()

        return self._create_message(clean_response, media=media)

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

    def respond(
        self,
        student_input: str,
        *,
        student_metadata: Optional[Dict] = None,
    ) -> TutorMessage:
        """Generate a response to student input.

        Thin wrapper that opens a tracing span buffer for the duration of
        the turn, then delegates to ``_respond_impl`` which holds the
        actual generation logic. The buffer is reset in a finally block so
        partial failures still emit a clean trace.

        Spans accumulated during generation are flushed in ``_save_turn``
        once the tutor turn's SessionTurn ID is known. See
        ``apps.tutoring.tracing`` and Phase 1 of
        ``memory/agentic_platform_architecture_plan.md``.

        Args:
            student_input: The student's message text.
            student_metadata: Optional metadata dict attached to the saved
                student SessionTurn. Used by R1 of the tutor reliability
                plan: when the difficulty button fires, we inject a
                synthetic student turn with metadata={'synthetic_source':
                'difficulty_button'} so analytics / the chat UI can tell
                this isn't a literal student message.
        """
        from apps.tutoring.tracing import start_span_buffer, reset_span_buffer
        token = start_span_buffer()
        try:
            result = self._respond_impl(
                student_input, student_metadata=student_metadata,
            )
            # R2 (2026-05-15): every TutorMessage gets the current
            # pending_question state attached after the impl runs, so
            # the artifact panel (R3) sees the right data regardless of
            # which of the seven internal TutorMessage construction
            # sites produced the result. Cheap (one DB lookup at most;
            # None when no question is in flight).
            try:
                if isinstance(result, TutorMessage):
                    result.pending_question = self._build_pending_question_payload()
            except Exception as _exc:
                logger.warning(
                    f"[PendingQuestion] post-impl attach failed: "
                    f"{type(_exc).__name__}: {_exc}"
                )
            return result
        finally:
            reset_span_buffer(token)

    def _respond_impl(
        self,
        student_input: str,
        *,
        student_metadata: Optional[Dict] = None,
    ) -> TutorMessage:
        """Actual response generation. Wrapped by ``respond()`` for tracing.

        This is the main conversation loop.
        Media selection: LLM signals via |||MEDIA:N||| tail-line, parsed before saving.
        """
        self._step_just_advanced = False

        # Snapshot awaiting_answer's turn_index so we can detect at the
        # end of this turn whether pose_question / pose_inline_question
        # replaced it. If the prior bank answer was correct AND the
        # tutor moved on without posing a new bank question (e.g. moved
        # to a chat-authored conceptual probe), we need to clear
        # awaiting_answer at end-of-turn so the NEXT student input
        # doesn't get mis-routed through the bank grader against a
        # stale question. Pilot 2026-05-16: lesson 538 session 36 turn
        # 14 — student's urban-coastal conceptual reply was graded
        # against an earlier MCQ (expected 'C') and marked wrong,
        # producing an empty tutor turn.
        _prev_aa = getattr(self, '_awaiting_answer', None) or {}
        self._prev_awaiting_turn_index = _prev_aa.get('turn_index')

        # Track lesson start time on first interaction
        if not self.session.started_lesson_at:
            self.session.started_lesson_at = timezone.now()
            self.session.save(update_fields=['started_lesson_at'])

        # Save student message — synthetic-source marker (when set) lets
        # the chat UI suppress re-rendering injected text like the
        # difficulty-button synthetic turns from R1.
        self._save_turn("student", student_input, metadata=student_metadata)
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

        # P3 — deterministic grading for bank-pulled questions. If the
        # PREVIOUS tutor turn rendered a bank question, fetch it and
        # grade the student's reply against the bank's stored answer.
        # The verdict is injected into the prompt as an
        # <evaluation_signal> block so the LLM cannot disagree with
        # the bank. See memory/curriculum_tutor_v2_plan.md item 5
        # (the platform-wide rule).
        #
        # Pre-loaded verdict short-circuit: the artifact-submission
        # endpoint (chat_answer_bank_question) grades against the raw
        # student answer ("210") then injects a SYNTHETIC student
        # message ("I answered: 210") before calling respond(). It
        # sets self._pending_bank_grade + ._pending_bank_question on
        # the engine FIRST, then calls respond(). Re-grading the
        # synthetic "I answered: 210" string here would fail (`_norm`
        # can't strip the prefix) and overwrite the correct verdict
        # with a false negative — exactly what we saw on 2026-05-16
        # ("210" submitted, grader called false-NEG because it tried
        # to match "I answered: 210" vs "210°"). Skip re-grading when
        # a verdict is already in flight.
        self._bank_signal_used_this_turn = False
        if (
            getattr(self, '_pending_bank_grade', None) is not None
            and getattr(self, '_pending_bank_question', None) is not None
        ):
            logger.info(
                "[BankGrade] using pre-loaded verdict (artifact submit) — "
                "skipping re-grade. is_correct=%s expected=%r",
                self._pending_bank_grade.is_correct,
                self._pending_bank_grade.expected,
            )
            # Clear awaiting_answer to mirror the regrade path's effect.
            if getattr(self._pending_bank_grade, 'is_correct', None) is not None:
                self._clear_awaiting_answer()
        else:
            self._pending_bank_grade = None
            self._pending_bank_question = None
            try:
                self._grade_against_last_bank_question(student_input)
            except Exception as e:
                logger.warning(f"[BankGrade] grade attempt crashed: {e}")

        # Deterministic math check BEFORE response generation (Layer 1 + 2 of
        # math-tutor false-positive fix). When the expected answer is numeric
        # and the student's reply parses as a number, we compute correctness
        # up-front and inject a signal into the system prompt so the LLM
        # cannot hallucinate praise for a wrong answer. See
        # memory/math_tutor_fix_plan.md.
        self._pending_math_check = self._deterministic_math_check(student_input)
        # Bare-answer detection (M9 / Layer 4). Tracked even when the
        # deterministic check didn't produce a verdict so metadata is
        # consistent for teacher review. Bare-answer detection runs
        # whether or not the deterministic check produced a result —
        # interim arithmetic the tutor invents on the fly ("what's
        # 95 + 70 + 110?") has no expected_answer, so the math-check
        # path returns None, but Rule 1 still applies: a bare numeric
        # reply on a math practice/quiz step must be met with a
        # request for working, not affirmation.
        self._pending_bare_answer = False
        try:
            is_math_step = self.lesson.unit.course.is_math
        except Exception:
            is_math_step = False
        step_type = ''
        if self.current_topic_index < len(self.steps):
            step_type = self.steps[self.current_topic_index].step_type or ''
        # Bare-answer detection — fires on EVERY math step type, not just
        # practice/quiz. Warmup steps in math lessons routinely see bare
        # arithmetic answers ("False it is 360-90=20") that need the same
        # Rule 1 treatment. Per user guidance: validators run at all steps.
        if is_math_step:
            self._pending_bare_answer = self._is_bare_math_answer(student_input)
            if self._pending_bare_answer:
                self.bare_answer_counts_by_step[self.current_topic_index] = (
                    self.bare_answer_counts_by_step.get(self.current_topic_index, 0) + 1
                )
        self._pending_math_student_input = student_input

        # Layer S — student working analyzer. Runs alongside the
        # existing bare-answer + math-check pipeline. Produces a
        # rich state signal (NO_WORKING / PARTIAL_CORRECT / etc.)
        # plus a `<student_working_analysis>` block that gets
        # injected into the system prompt below. Fires on EVERY math
        # step type — warmups need this just as much as practice.
        self._pending_working_analysis = None
        if is_math_step:
            from apps.tutoring.student_working_analyzer import analyze_working
            current_step = (
                self.steps[self.current_topic_index]
                if self.current_topic_index < len(self.steps)
                else None
            )
            expected = current_step.expected_answer if current_step else None
            self._pending_working_analysis = analyze_working(
                student_input, expected_answer=expected,
            )
            logger.info(
                "[LayerS] session=%s step=%s state=%s steps=%d first_error=%s",
                self.session.id,
                self.current_topic_index,
                self._pending_working_analysis.state.value,
                len(self._pending_working_analysis.steps),
                self._pending_working_analysis.first_error_idx,
            )

        if self._pending_math_check is not None:
            logger.info(
                "[MathCheck] session=%s step=%s is_correct=%s bare=%s student=%s expected=%s",
                self.session.id,
                self.current_topic_index,
                self._pending_math_check.is_correct,
                self._pending_bare_answer,
                self._pending_math_check.student_parsed,
                self._pending_math_check.expected_parsed,
            )
        elif self._pending_bare_answer:
            logger.info(
                "[MathCheck] session=%s step=%s no-expected-answer; bare=True student=%r",
                self.session.id,
                self.current_topic_index,
                (student_input or '')[:40],
            )

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

        # Parse |||MEDIA:N||| signal BEFORE saving — keeps DB clean.
        # MEDIA is the only inline signal channel; questions go through
        # the pose_question Anthropic tool inside _generate_response.
        clean_response, parsed_media = self._parse_media_signal(response)

        # Combined post-response judge — math-only. ONE LLM call
        # evaluating arithmetic + factual + rule_compliance, replacing
        # the three separate calls (verify_arithmetic_claims +
        # validator.L4 fact_check + validator.L5 rule_check). Roughly
        # halves post-response cost on Opus tutoring. The validator
        # below consumes `combined_result` and skips its L4/L5 LLM
        # calls accordingly.
        arithmetic_corrections: List[Dict] = []
        combined_judge_result = None
        # Combined judge is universal (2026-05-05). All subjects get
        # post-response checking — math turns get arithmetic +
        # NO_AUTHORING + RULE_1 + factual + step-eval; non-math turns
        # get factual + step-eval (the math-specific checks short-
        # circuit naturally because there are no arithmetic claims to
        # check and no bank-required-questions to author against).
        # Geography "Mahé has 90,000 people" claims still get fact-
        # checked against the curriculum KB.
        try:
            subject_is_math = bool(self.lesson.unit.course.is_math)
        except Exception:
            subject_is_math = False
        from apps.tutoring.combined_judge import run_combined_judge
        # Route the judge to the dedicated judge_client (Sonnet 4 by
        # default) — separate from the tutoring model so post-response
        # checking doesn't compete with the tutor for bandwidth.
        #
        # Step-eval is folded into this same call (formerly a
        # separate instructor _evaluate_step call). Min-exchange
        # floor: skip step eval on the first turn of teach /
        # worked_example so the LLM doesn't advance prematurely.
        # Pass the figure attached this turn (if any) + a vision-capable
        # image reader so the figure_ref / figure_vision judges can run.
        # parsed_media is a single dict or None; the orchestrator
        # accepts a list (multiple figures per turn possible in future).
        attached_media_list = [parsed_media] if parsed_media else []
        # Phase 2.2.5: pass the conversation BEFORE the current
        # student_input (which is already appended at this point) so
        # the history-aware judges (coherence / factual / rule) can
        # reason about cross-turn signals. JUDGE_HISTORY_TURNS bounds
        # the window — see apps/tutoring/judges/history.py.
        prior_conversation = (
            self.conversation[:-1] if self.conversation else []
        )
        combined_judge_result = run_combined_judge(
            clean_response,
            lesson=self.lesson,
            llm_client=self.judge_client,
            vision_client=self.judge_client,
            image_reader=self._read_image_for_vision,
            attached_media=attached_media_list,
            bank_stems=self._current_bank_stems(),
            student_input=student_input,
            answer_was_bare=turn_bare_answer,
            answer_was_wrong=(
                turn_math_check is not None
                and turn_math_check.is_correct is False
            ),
            step_context=self._build_step_eval_context(
                student_input, clean_response,
                math_check=turn_math_check,
            ),
            subject_is_math=subject_is_math,
            bank_offered=bool(getattr(self, '_question_id_map', None)),
            conversation_history=prior_conversation,
        )
        if combined_judge_result.corrected_response:
            clean_response = combined_judge_result.corrected_response
        arithmetic_corrections = list(
            combined_judge_result.arithmetic_corrections
        )
        if arithmetic_corrections:
            logger.info(
                f"[CombinedJudge] flagged {len(arithmetic_corrections)} arithmetic correction(s) — will trigger regen"
            )

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
            # Pick the right opener for the situation. The previous
            # single-opener behaviour produced "Let's check this together"
            # even on a bare-but-correct answer, then the rest of the
            # response said "✓ correct" — a contradiction. Bare-correct
            # gets an echo-back opener; deterministic-wrong keeps the
            # original "walk me through" opener.
            if turn_bare_answer and turn_math_check is not None and turn_math_check.is_correct:
                praise_context = "bare_correct"
            elif turn_bare_answer:
                praise_context = "bare_unknown"
            else:
                praise_context = "wrong"
            clean_response, praise_stripped = strip_praise_if_wrong(
                clean_response,
                is_correct=False,
                context=praise_context,
                student_input=student_input,
                # Rotate the opener so it doesn't read as a stuck record
                # ("Show me your working, step by step." every turn).
                # Bare-answer counter for this step is a natural rotator.
                rotate_index=self.bare_answer_counts_by_step.get(
                    self.current_topic_index, 0,
                ),
            )
            if praise_stripped:
                logger.info(
                    "[MathCheck] Praise filter triggered session=%s step=%s bare=%s context=%s",
                    self.session.id, self.current_topic_index, turn_bare_answer, praise_context,
                )

        media = [parsed_media] if parsed_media else []

        # Trust the |||MEDIA:N||| signal — no inference fallback. If the
        # LLM wanted a figure shown, it emits the signal.

        # Analyze student response for adaptation (returns metadata dict).
        # Pass the combined_judge_result through so the analyser can
        # consume its step-eval verdict instead of making a second
        # _evaluate_step LLM call.
        turn_metadata = self._analyze_student_response(
            student_input, clean_response,
            math_check=turn_math_check,
            combined_judge_result=combined_judge_result,
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

        # Merge pose_question tool metadata (if the math turn used the
        # tool path, the handler stored bank_question_ref + related
        # fields on _pending_pose_question_meta).
        pose_meta = getattr(self, '_pending_pose_question_meta', None) or {}
        if pose_meta:
            for k, v in pose_meta.items():
                turn_metadata[k] = v
            logger.info(
                "[QuestionTool] merged pose_meta into turn_metadata: keys=%s",
                list(pose_meta.keys()),
            )
        # Reset for the next turn so a stale ref can't bleed across.
        self._pending_pose_question_meta = {}

        # Socratic validator V1 — universal praise gate + structural checks.
        # Runs AFTER the math-specific praise filter so it acts as a
        # catch-all for non-math subjects where the math check didn't fire.
        # See memory/socratic_validator_plan.md.
        current_step = (
            self.steps[self.current_topic_index]
            if self.current_topic_index < len(self.steps)
            else None
        )
        with emit_span('audit', 'validator') as _audit_span:
            validation = validate_tutor_response(
                clean_response,
                is_correct=turn_metadata.get('is_correct'),
                bare_answer=turn_bare_answer,
                step_type=(current_step.step_type if current_step else None),
                lesson=self.lesson,
                llm_client=self.judge_client,  # judge runs on Sonnet, not Opus
                student_input=student_input,
                bank_stems=self._current_bank_stems(),
                arithmetic_corrections=arithmetic_corrections,
                bank_signal_used=bool(getattr(self, '_bank_signal_used_this_turn', False)),
                combined_result=combined_judge_result,
                # Combined judge already covered fact + rule. Skip the
                # legacy L4/L5 LLM calls so they don't run twice.
                fact_check=(combined_judge_result is None),
                rule_check=(combined_judge_result is None),
                # Used by the figure-ref-without-signal check.
                media_attached=bool(media),
            )
            if _audit_span is not None:
                _audit_span['payload'] = {
                    'passed': bool(getattr(validation, 'passed', True)),
                    'issues': list(getattr(validation, 'issues', []) or [])[:20],
                }
        if validation.content != clean_response:
            clean_response = validation.content

        # W1 — answer-leak structural guard. Runs AFTER the validator so
        # we can attach ISSUE_ANSWER_LEAK to validation.issues and let
        # the existing regen branch fire. Skips silently when the
        # bank grader verdict was correct (no leak possible), or when
        # wrong_attempts has reached the reveal-allowed threshold (>=3).
        # See memory/hint_vs_reveal_guards_plan.md W1.
        try:
            _bank_grade_for_leak = getattr(self, '_pending_bank_grade', None)
            _bank_verdict_for_leak = getattr(_bank_grade_for_leak, 'is_correct', None) if _bank_grade_for_leak else None
            if _bank_verdict_for_leak is False:
                # Only fire leak check when the student JUST got wrong.
                from apps.tutoring.answer_leak import detect_answer_leak
                from apps.tutoring.validator import ISSUE_ANSWER_LEAK
                _awaiting = getattr(self, '_awaiting_answer', None) or {}
                _wrong_attempts = int(_awaiting.get('wrong_attempts', 0) or 0)
                # Chat-authored Q text — pulled from previous tutor turn
                # if no bank question is in flight.
                _chat_authored_q = None
                if getattr(self, '_pending_bank_question', None) is None:
                    from apps.tutoring.models import SessionTurn
                    _prev_tt = (
                        SessionTurn.objects
                        .filter(session=self.session, role='tutor')
                        .order_by('-created_at')
                        .first()
                    )
                    if _prev_tt and _prev_tt.content:
                        _content = _prev_tt.content.strip()
                        if _content.endswith('?'):
                            _sentences = re.split(r'(?<=[.!?])\s+', _content)
                            _chat_authored_q = next(
                                (s for s in reversed(_sentences) if s.strip().endswith('?')),
                                _content,
                            ).strip()
                _leak_verdict = detect_answer_leak(
                    response=clean_response,
                    bank_question=getattr(self, '_pending_bank_question', None),
                    chat_authored_q=_chat_authored_q,
                    wrong_attempts=_wrong_attempts,
                    llm_client=self.judge_client,
                    reveal_threshold=self._reveal_threshold(),
                )
                if _leak_verdict is not None:
                    validation.issues.append(ISSUE_ANSWER_LEAK)
                    validation.metadata['answer_leak_reason'] = _leak_verdict.reason
                    validation.metadata['answer_leak_sources'] = _leak_verdict.sources
                    validation.metadata['answer_leak_ms'] = _leak_verdict.elapsed_ms
                    logger.info(
                        "[LeakDetect] FLAGGED session=%s sources=%s ms=%d reason=%r",
                        self.session.id, _leak_verdict.sources,
                        _leak_verdict.elapsed_ms, _leak_verdict.reason[:200],
                    )
        except Exception as _exc:
            logger.warning(
                "[LeakDetect] guard crashed: %s: %s — continuing without regen flag",
                type(_exc).__name__, _exc,
            )

        # W14 — repeated-question structural guard. Runs on every turn
        # (not just wrong-answer turns) since cross-turn repetition
        # can happen anywhere. Looks for: cross-turn authored repeats,
        # paraphrases of already-shown bank questions, paraphrases of
        # the active pending bank question.
        try:
            from apps.tutoring.repeated_question import (
                detect_repeated_question, extract_questions,
                normalise_question_signature,
            )
            from apps.tutoring.validator import ISSUE_REPEATED_QUESTION
            # Build shown_bank_stems from shown_question_ids (lookup the
            # current stems for cross-Q semantic compare).
            _shown_bank_stems: List[str] = []
            try:
                _shown_ids = list(getattr(self, 'shown_question_ids', set()) or [])
                if _shown_ids:
                    from apps.tutoring.models import ExitTicketQuestion
                    _shown_bank_stems = list(
                        ExitTicketQuestion.objects
                        .filter(id__in=_shown_ids)
                        .values_list('question_text', flat=True)
                    )
            except Exception:
                _shown_bank_stems = []
            # Active bank stem — the question student is currently
            # trying to answer.
            _active_aa = getattr(self, '_awaiting_answer', None) or {}
            _active_qid = _active_aa.get('question_id')
            _active_stem: Optional[str] = None
            if _active_qid and _active_aa.get('kind') == 'exit_ticket_question':
                try:
                    from apps.tutoring.models import ExitTicketQuestion
                    _aq = ExitTicketQuestion.objects.filter(id=_active_qid).first()
                    if _aq:
                        _active_stem = _aq.question_text
                except Exception:
                    _active_stem = None
            _repeat_verdict = detect_repeated_question(
                response=clean_response,
                recent_question_signatures=list(getattr(self, 'recent_tutor_question_sigs', []) or []),
                shown_bank_stems=_shown_bank_stems,
                active_bank_stem=_active_stem,
                llm_client=self.judge_client,
            )
            if _repeat_verdict is not None:
                validation.issues.append(ISSUE_REPEATED_QUESTION)
                validation.metadata['repeated_question_reason'] = _repeat_verdict.reason
                validation.metadata['repeated_question_kind'] = _repeat_verdict.repeat_kind
                validation.metadata['repeated_question_sources'] = _repeat_verdict.sources
                validation.metadata['repeated_question_matched'] = _repeat_verdict.matched_question[:200]
                validation.metadata['repeated_question_ms'] = _repeat_verdict.elapsed_ms
                logger.info(
                    "[RepeatDetect] FLAGGED session=%s kind=%s sources=%s reason=%r",
                    self.session.id, _repeat_verdict.repeat_kind,
                    _repeat_verdict.sources, _repeat_verdict.reason[:200],
                )
            # Append the NEW questions extracted from this response to
            # recent_tutor_question_sigs (capped at 10) so the next
            # turn can detect cross-turn repeats.
            _new_qs = extract_questions(clean_response)
            for _q in _new_qs:
                _sig = normalise_question_signature(_q)
                if _sig and _sig not in self.recent_tutor_question_sigs:
                    self.recent_tutor_question_sigs.append(_sig)
            self.recent_tutor_question_sigs = self.recent_tutor_question_sigs[-10:]
        except Exception as _exc:
            logger.warning(
                "[RepeatDetect] guard crashed: %s: %s — continuing without regen flag",
                type(_exc).__name__, _exc,
            )

        if validation.issues:
            turn_metadata['validator_issues'] = list(validation.issues)
            turn_metadata['validator_passed'] = validation.passed

        # PROBE-STRIP REMOVED 2026-05-16 per pilot directive (no
        # surgical stripping — produces incoherent turns). The probe
        # was previously removed when the grader said the student's
        # answer was correct, but stripping breaks tutor flow,
        # especially when the probe IS the pose_inline_question text.
        # If the LLM keeps probing on correct answers despite the
        # eval_signal telling it not to, we'll fix that with a
        # stronger prompt + (last resort) regen, not strip. Probe
        # detection still tracked for metrics below.
        bank_grade = getattr(self, '_pending_bank_grade', None)
        bank_correct = bank_grade is not None and bank_grade.is_correct is True
        math_correct = (
            turn_math_check is not None
            and turn_math_check.is_correct is True
        )

        # Always attach fact-check + rule-check metadata so the teacher
        # dashboard can show it even on clean turns.
        for k in (
            'factual_claims_checked', 'factual_claims_unverified',
            'factual_claims_contradicted', 'fact_check_skipped',
            'fact_check_skip_reason',
            'rule_check_skipped', 'rule_check_skip_reason',
            'rule_violations',
            # Phase 2.2.5 — bounded history window the judges saw.
            # Zero on legacy traces / when no history was passed.
            'judge_history_turns',
        ):
            if k in validation.metadata:
                turn_metadata[k] = validation.metadata[k]

        # Per-judge breakdown for benchmark eval — see
        # memory/eval_benchmark_v2_simplified.md. Stored on a private
        # key here; _save_turn pops it into SessionTurn.judge_outputs so
        # the persisted metadata dict stays clean.
        if combined_judge_result is not None and not combined_judge_result.skipped:
            try:
                turn_metadata['_judge_outputs'] = combined_judge_result.to_judge_outputs()
            except Exception as exc:  # belt-and-braces — never block a tutor turn
                logger.warning("[JudgeOutputs] failed to capture: %s", exc)

        # Prompt-pack fingerprints — tutor system prompt hash + per-judge
        # prompt hashes, combined for the benchmark snapshot. Lets the
        # annotation UI display which prompt revision produced a given
        # response/verdict so prompt iterations are auditable.
        try:
            tutor_meta = getattr(self, '_last_tutor_prompt_meta', None) or {}
            judges_meta = {}
            if combined_judge_result is not None:
                judges_meta = dict(
                    getattr(combined_judge_result, 'prompt_versions', {}) or {}
                )
            if tutor_meta or judges_meta:
                turn_metadata['prompt_pack'] = {
                    'tutor_system': tutor_meta,
                    'judges': judges_meta,
                }
        except Exception as exc:
            logger.warning("[PromptPack] failed to capture: %s", exc)

        # V3 — regen ensemble. Production logs (2026-05-07) showed the
        # previous single-call regen (which appended a constraint
        # block to the 30KB tutor system prompt) was being IGNORED by
        # the LLM — regenerated text often had the same violations
        # as the original. The new path:
        #   1) builds a focused 1-2KB rewrite prompt (no tutor framing)
        #   2) fans out to N concurrent REGEN ModelConfigs (or falls
        #      back to single-model regen with the tutoring client)
        #   3) judges every candidate concurrently and scores them
        #   4) picks the best clean candidate; if none clean, lowers
        #      temperature by 0.05 and retries (max 3 cycles)
        #   5) on cap, sends the highest-scoring candidate or a
        #      stock fallback
        if validation.needs_regeneration:
            logger.info(
                "[Validator] regenerating session=%s (issues=%s)",
                self.session.id, validation.issues,
            )
            from apps.tutoring.regen import run_regen_ensemble

            # Carry session_id into validation metadata for log
            # correlation in run_regen_ensemble.
            validation.metadata = dict(validation.metadata or {})
            validation.metadata.setdefault('session_id', self.session.id)

            with emit_span('regen', 'ensemble') as _regen_span:
                ensemble_result = run_regen_ensemble(
                    previous_response=clean_response,
                    validation=validation,
                    lesson=self.lesson,
                    step_context=self._build_step_eval_context(
                        student_input, clean_response,
                        math_check=turn_math_check,
                    ),
                    bank_stems=self._current_bank_full_render(),
                    media_catalog_text=getattr(self, '_last_media_catalog_text', '') or '',
                    attached_media=attached_media_list,
                    regen_clients=self.regen_clients,
                    judge_client=self.judge_client,
                    vision_client=self.judge_client,
                    image_reader=self._read_image_for_vision,
                    subject_is_math=subject_is_math,
                    bank_offered=bool(getattr(self, '_question_id_map', None)),
                    student_input=student_input,
                    answer_was_bare=turn_bare_answer,
                    answer_was_wrong=(
                        turn_math_check is not None
                        and turn_math_check.is_correct is False
                    ),
                    conversation_history=prior_conversation,
                    # Pass the bank's ground truth to the regen LLM
                    # so it can't contradict the explanation when
                    # rewriting. Pilot 2026-05-16: regen was
                    # inventing answers (e.g. "cultural geography"
                    # for a Human-geography question that the
                    # student got right) because it had no ground
                    # truth in its prompt.
                    #
                    # W3 leak-aware regen: when the regen was
                    # triggered by ISSUE_ANSWER_LEAK, suppress the
                    # canonical answer from the context — the
                    # rewrite LLM literally cannot leak it again if
                    # it doesn't have it. The regen prompt also gets
                    # a stricter "concept-hint only" directive via
                    # the bank_context.suppress_reason field.
                    bank_context=self._build_regen_bank_context(
                        suppress_canonical=(
                            'answer_leak' in (validation.issues or [])
                        ),
                    ),
                )
                if _regen_span is not None:
                    _regen_span['payload'] = {
                        'picked_model': str(getattr(ensemble_result, 'picked_model', '') or '')[:80],
                        'cycles': int(getattr(ensemble_result, 'cycles', 0) or 0),
                        'clean': bool(getattr(ensemble_result, 'clean', False)),
                        'fallback_used': bool(getattr(ensemble_result, 'fallback_used', False)),
                    }

            # Re-parse a possible |||MEDIA:N||| in the chosen candidate.
            regen_clean, regen_media = self._parse_media_signal(ensemble_result.text)
            # PROBE-STRIP-ON-REGEN REMOVED 2026-05-16 (same directive
            # as the initial-path removal above). No surgical strip.
            # REGEN-OUTPUT STRIPS REMOVED 2026-05-16 per pilot
            # directive: incoherent-turn rate from strip post-regen
            # was too high. The grader is robust to authored
            # questions without an answer key
            # (grade_chat_authored_question) so even a regen that
            # still authors a question won't strand the student —
            # they reply, the grader judges via LLM. Coherence wins
            # over surgery.
            clean_response = regen_clean

            turn_metadata['regenerated'] = True
            turn_metadata['regeneration_reason'] = list(validation.issues)
            turn_metadata['regen_cycles'] = ensemble_result.cycles_run
            turn_metadata['regen_picked_model'] = ensemble_result.picked_model
            turn_metadata['regen_clean'] = ensemble_result.clean
            turn_metadata['regen_fallback_used'] = ensemble_result.fallback_used
            turn_metadata['regen_elapsed_seconds'] = round(
                ensemble_result.elapsed_seconds, 2,
            )
            # Per-cycle audit trail (Phase 2.x) — every candidate's
            # text preview + judge breakdown so annotators can see
            # what each regen attempt looked like. Without this the
            # ensemble's per-cycle judge_result objects are dropped
            # once respond() returns.
            try:
                from apps.tutoring.regen import summarise_regen_cycles
                turn_metadata['regen_audit'] = summarise_regen_cycles(
                    ensemble_result,
                )
            except Exception as exc:
                logger.warning("[Regen] audit capture failed: %s", exc)

            if not ensemble_result.clean:
                # Add an explicit issue so the [TurnSummary] log line
                # surfaces "regen ended dirty" as a metric. The chosen
                # candidate is still surfaced to the student (or stock
                # fallback) — there's nothing better to send.
                merged = set(turn_metadata.get('validator_issues', []))
                merged.add('regen_did_not_clean')
                turn_metadata['validator_issues'] = list(merged)

            # 2026-05-17 — POST-REGEN LEAK CHECK. The regen ensemble's
            # per-cycle judges don't include the answer-leak detector,
            # so a regen prompted to fix `repeated_question` can still
            # introduce a canonical reveal in its replacement text.
            # Lesson 540 session 50 turn 861: regen fixed repeated_question
            # but produced "Not quite. The correct answer is B - a grid
            # system provides a systematic way to locate and reference
            # specific locations." Pilot directive 2026-05-17: reveal
            # is NEVER allowed regardless of attempt count.
            try:
                _bg_post = getattr(self, '_pending_bank_grade', None)
                if (
                    _bg_post is not None
                    and getattr(_bg_post, 'is_correct', None) is False
                ):
                    from apps.tutoring.answer_leak import detect_answer_leak as _det_leak_post
                    from apps.tutoring.validator import ISSUE_ANSWER_LEAK as _LEAK_ISSUE_POST
                    _aa_post = getattr(self, '_awaiting_answer', None) or {}
                    _wa_post = int(_aa_post.get('wrong_attempts', 0) or 0)
                    _post_verdict = _det_leak_post(
                        response=clean_response,
                        bank_question=getattr(self, '_pending_bank_question', None),
                        chat_authored_q=None,
                        wrong_attempts=_wa_post,
                        llm_client=self.judge_client,
                        reveal_threshold=self._reveal_threshold(),
                    )
                    if _post_verdict is not None:
                        logger.warning(
                            "[LeakDetect] POST-REGEN FLAGGED session=%s "
                            "sources=%s reason=%r — regen INTRODUCED a "
                            "leak while fixing %s — substituting safe "
                            "no-reveal fallback",
                            self.session.id, _post_verdict.sources,
                            _post_verdict.reason[:200],
                            list(validation.issues or [])[:5],
                        )
                        merged = set(turn_metadata.get('validator_issues', []))
                        merged.add(_LEAK_ISSUE_POST)
                        merged.add('post_regen_leak')
                        merged.add('safe_fallback_used')
                        turn_metadata['validator_issues'] = list(merged)
                        turn_metadata['post_regen_leak_reason'] = _post_verdict.reason
                        # Safe fallback: a concept-level nudge that's
                        # guaranteed not to reveal. Better to lose
                        # specificity than to leak the canonical.
                        clean_response = (
                            "That's not quite it. Let's try a different "
                            "angle — think about what this map feature "
                            "actually shows, then take another look at "
                            "the options. Which one matches the function "
                            "you're picturing?"
                        )
            except Exception as _exc:
                logger.warning(
                    "[LeakDetect] post-regen check crashed: %s: %s",
                    type(_exc).__name__, _exc,
                )

            # Update attached media when the regen picked a different one.
            if regen_media:
                media = [regen_media]

            # NOTE: force-inject of bank questions on persistent
            # authoring_violation was tried and removed (2026-05-04) —
            # the truncate-at-'?'-then-append helper produced half-baked
            # responses ("Quick check before we apply it:" with no
            # question following) when the LLM's question shape didn't
            # match the helper's expectations. The helper itself
            # (_force_inject_bank_question) is still defined below for
            # potential future use, but is no longer invoked. Cleaner
            # alternatives discussed in the chat — leaning toward
            # bank-only system-prompt scaffolding instead of post-hoc
            # surgery on the response text.

        # Record media for this turn (for resume artifact panel)
        if media:
            turn_index = len(self.conversation)  # index before appending
            self._turn_media[str(turn_index)] = media[0]

        # Check if all steps complete — trigger exit ticket (NOT during remediation)
        if (self.current_topic_index >= len(self.steps)
                and self.session_state == SessionState.TUTORING
                and not getattr(self, 'is_remediation', False)):
            if not self._can_trigger_exit_ticket():
                # Hold the trigger; mark the hold window so the next
                # student turn clears it once acknowledgement happens.
                self.exit_ticket_hold_until_exchange = self.exchange_count + 1
                logger.info(
                    "[ExitTicket] HOLD session=%s — current bank Q not "
                    "yet answered correctly / reveal in progress (next "
                    "trigger eligible at exchange %d)",
                    self.session.id, self.exit_ticket_hold_until_exchange,
                )
            else:
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

        # Remediation safety valve: force exit ticket after 30 remediation exchanges
        if getattr(self, 'is_remediation', False) and self.exchange_count >= 30:
            self.session_state = SessionState.EXIT_TICKET
            self._save_state()
            self._save_turn("tutor", clean_response, metadata=turn_metadata)
            self.conversation.append({"role": "assistant", "content": clean_response})
            return self._handle_exit_ticket()

        # P4 — remediation walkthrough advancement. If the student
        # just answered a walkthrough question, advance to the next
        # failed question and append it to the tutor's response. The
        # bank grader (P3) already verdicted the previous question,
        # the LLM's response provided scaffolding; now we move on.
        clean_response = self._maybe_advance_walkthrough(
            clean_response, turn_metadata,
        )
        # If the walkthrough just finished, _finish_remediation flipped
        # session_state → EXIT_TICKET. Hand off to a FRESH exit ticket
        # on this same turn (requiz phase was removed 2026-05-12).
        if (self.session_state == SessionState.EXIT_TICKET
                and not getattr(self, 'is_remediation', False)):
            self._save_state()
            self._save_turn("tutor", clean_response, metadata=turn_metadata)
            self.conversation.append({"role": "assistant", "content": clean_response})
            return self._handle_exit_ticket()

        # Persist any attached figure URL on turn_metadata so the
        # teacher chat-history template can render the image inline
        # under the tutor bubble. Same shape the frontend uses:
        # {url, alt, caption, description}.
        if media:
            turn_metadata['attached_media'] = [
                {
                    'url': m.get('url') or '',
                    'alt': m.get('alt') or m.get('caption') or m.get('description') or '',
                    'caption': m.get('caption') or m.get('description') or '',
                }
                for m in media if m and m.get('url')
            ]

        # Clear awaiting_answer when the prior bank question was
        # answered correctly AND this turn didn't pose a new bank
        # question. Without this, a chat-authored conceptual probe
        # leaves awaiting_answer pointing at the just-answered bank
        # question — and the next student input gets graded against
        # that stale question. Pilot 2026-05-16: lesson 538 session
        # 36 turn 14 (see _prev_awaiting_turn_index snapshot above).
        bank_grade_now = getattr(self, '_pending_bank_grade', None)
        cur_aa = getattr(self, '_awaiting_answer', None) or {}
        cur_turn_idx = cur_aa.get('turn_index')
        prev_turn_idx = getattr(self, '_prev_awaiting_turn_index', None)
        if (
            bank_grade_now is not None
            and getattr(bank_grade_now, 'is_correct', None) is True
            and prev_turn_idx is not None
            and cur_turn_idx == prev_turn_idx
        ):
            logger.info(
                "[BankGrade] prior bank answer correct; clearing "
                "stale awaiting_answer (turn_index=%s) — no new "
                "pose_question this turn",
                prev_turn_idx,
            )
            self._clear_awaiting_answer()

        # Save state
        self._save_state()

        # Save CLEAN tutor response (no signal tags in DB)
        self._save_turn("tutor", clean_response, metadata=turn_metadata)
        self.conversation.append({"role": "assistant", "content": clean_response})

        return self._create_message(clean_response, media=media)

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
        self._pending_math_student_input = student_input
        self._pending_bare_answer = False
        try:
            is_math_step = self.lesson.unit.course.is_math
        except Exception:
            is_math_step = False
        step_type = ''
        if self.current_topic_index < len(self.steps):
            step_type = self.steps[self.current_topic_index].step_type or ''
        # Bare-answer detection on EVERY math step (streaming parity
        # with respond()). Per user guidance: validators run at all steps.
        if is_math_step:
            self._pending_bare_answer = self._is_bare_math_answer(student_input)
            if self._pending_bare_answer:
                self.bare_answer_counts_by_step[self.current_topic_index] = (
                    self.bare_answer_counts_by_step.get(self.current_topic_index, 0) + 1
                )

        # Layer S — student working analyzer (streaming parity).
        # Fires on EVERY math step type, not just practice.
        self._pending_working_analysis = None
        if is_math_step:
            from apps.tutoring.student_working_analyzer import analyze_working
            current_step = (
                self.steps[self.current_topic_index]
                if self.current_topic_index < len(self.steps)
                else None
            )
            expected = current_step.expected_answer if current_step else None
            self._pending_working_analysis = analyze_working(
                student_input, expected_answer=expected,
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
        clean_content, parsed_media = self._parse_media_signal(full_response)
        # NOTE: |||QUESTION:N|||, |||GENERATE:...|||, |||ARTIFACT:...|||,
        # |||PROBE:...||| signals all removed. Streaming path is unused
        # in production (Azure CA buffers); if it is revived it will
        # need to thread the pose_question tool path here too.

        # Verify calculations (streaming parity with respond()) — LLM-based.
        if self.lesson.unit.course.is_math:
            from apps.tutoring.llm_arithmetic_verifier import verify_arithmetic_claims
            clean_content, corrections = verify_arithmetic_claims(
                clean_content, llm_client=self.llm_client,
            )
            if corrections:
                logger.info(
                    f"[MathCheck] LLM flagged {len(corrections)} arithmetic correction(s)"
                )

        # Post-generation praise filter (Layer 3, streaming parity).
        praise_stripped = False
        should_strip = (
            (turn_math_check is not None and turn_math_check.is_correct is False)
            or turn_bare_answer
        )
        if should_strip:
            if turn_bare_answer and turn_math_check is not None and turn_math_check.is_correct:
                praise_context = "bare_correct"
            elif turn_bare_answer:
                praise_context = "bare_unknown"
            else:
                praise_context = "wrong"
            clean_content, praise_stripped = strip_praise_if_wrong(
                clean_content,
                is_correct=False,
                context=praise_context,
                student_input=student_input,
            )
            if praise_stripped:
                logger.info(
                    "[MathCheck] Praise filter triggered (stream) session=%s step=%s bare=%s context=%s",
                    self.session.id, self.current_topic_index, turn_bare_answer, praise_context,
                )

        # Media from LLM signal — no inference fallback. Trust |||MEDIA:N|||.
        media = [parsed_media] if parsed_media else []

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
        with emit_span('audit', 'validator') as _audit_span:
            validation = validate_tutor_response(
                clean_content,
                is_correct=turn_metadata.get('is_correct'),
                bare_answer=turn_bare_answer,
                step_type=(current_step.step_type if current_step else None),
                lesson=self.lesson,
                llm_client=self.llm_client,
                student_input=student_input,
                bank_stems=self._current_bank_stems(),
                media_attached=bool(media),
            )
            if _audit_span is not None:
                _audit_span['payload'] = {
                    'passed': bool(getattr(validation, 'passed', True)),
                    'issues': list(getattr(validation, 'issues', []) or [])[:20],
                }
        if validation.content != clean_content:
            clean_content = validation.content
        if validation.issues:
            turn_metadata['validator_issues'] = list(validation.issues)
            turn_metadata['validator_passed'] = validation.passed

        # Probe-strip on correct answers (pilot directive 2026-05-12):
        # the system must NOT probe ("how did you solve…") when the
        # student answered correctly. The eval-signal block + system
        # prompt principles try to suppress this at generation time;
        # this is the server-side backstop for when the LLM slips.
        bank_grade = getattr(self, '_pending_bank_grade', None)
        bank_correct = bank_grade is not None and bank_grade.is_correct is True
        math_correct = (
            turn_math_check is not None
            and turn_math_check.is_correct is True
        )
        if bank_correct or math_correct:
            stripped, n_probes = _strip_probe_sentences(clean_content)
            if n_probes > 0:
                clean_content = stripped
                turn_metadata['probe_stripped_count'] = n_probes
                logger.info(
                    "[ProbeStrip] removed %d probe sentence(s) on correct "
                    "answer session=%s step=%s source=%s",
                    n_probes, self.session.id, self.current_topic_index,
                    'bank' if bank_correct else 'math',
                )
        for k in (
            'factual_claims_checked', 'factual_claims_unverified',
            'factual_claims_contradicted', 'fact_check_skipped',
            'fact_check_skip_reason',
            'rule_check_skipped', 'rule_check_skip_reason',
            'rule_violations',
            # Phase 2.2.5 — bounded history window the judges saw.
            # Zero on legacy traces / when no history was passed.
            'judge_history_turns',
        ):
            if k in validation.metadata:
                turn_metadata[k] = validation.metadata[k]

        # Check if all steps complete — trigger exit ticket (NOT during remediation)
        show_exit_ticket = False
        exit_ticket = None
        if (self.current_topic_index >= len(self.steps)
                and self.session_state == SessionState.TUTORING
                and not getattr(self, 'is_remediation', False)):
            if not self._can_trigger_exit_ticket():
                self.exit_ticket_hold_until_exchange = self.exchange_count + 1
                logger.info(
                    "[ExitTicket] HOLD session=%s (alt-path) — wrong/"
                    "reveal in progress; eligible at exchange %d",
                    self.session.id, self.exit_ticket_hold_until_exchange,
                )
            else:
                self.session_state = SessionState.EXIT_TICKET
                show_exit_ticket = True

        # Remediation: check if all failed concepts re-covered
        if (not show_exit_ticket and getattr(self, 'is_remediation', False)
                and self._remediation_steps_complete()):
            self.session_state = SessionState.EXIT_TICKET
            show_exit_ticket = True

        # Remediation safety valve
        if (not show_exit_ticket and getattr(self, 'is_remediation', False)
                and self.exchange_count >= 30):
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

    # Cache: avoid re-reading the same image bytes per turn within a
    # session. Keyed by (current_topic_index, media_url) → image_block.
    def _get_step_vision_block(self):
        """Build a content block to attach the current step's primary
        image to the LLM call so the model can SEE the figure rather
        than relying on metadata alone.

        Format is provider-agnostic: returns Anthropic's image content
        block shape ({"type":"image","source":{"type":"base64", ...}}).
        The Anthropic SDK accepts it directly; OpenAIClient and
        GeminiClient have multimodal adapters that translate this
        shape (see _build_contents in GeminiClient).

        Returns None when:
          - the current step has no attached media
          - the file can't be read (URL unreachable, missing file)

        Universal across subjects (was math-only): geography maps,
        science diagrams, history primary-source images all benefit
        from the tutor SEEING the figure. The "is_math" gate was
        dropped 2026-05-05 per user direction.
        """
        if self.current_topic_index >= len(self.steps):
            return None
        # Per-course image gate — when disabled, don't attach the step
        # figure to the multimodal context. Same gate as
        # _build_media_catalog so the LLM has zero image affordance.
        try:
            if self.lesson and self.lesson.unit and self.lesson.unit.course:
                if not self.lesson.unit.course.tutoring_images_enabled:
                    return None
        except Exception:
            pass
        media = self._get_step_media()
        if not media:
            return None
        primary = media[0]
        url = primary.get('url') or ''
        if not url:
            return None
        # Per-session cache to avoid re-reading the same file every turn.
        cache = getattr(self, '_vision_cache', None)
        if cache is None:
            cache = {}
            self._vision_cache = cache
        cache_key = (self.current_topic_index, url)
        if cache_key in cache:
            return cache[cache_key]

        try:
            data, media_type = self._read_image_for_vision(url)
        except Exception as e:
            logger.warning("[Vision] failed to read step image %s: %s", url, e)
            cache[cache_key] = None
            return None
        if not data:
            cache[cache_key] = None
            return None
        # Anthropic image content block shape — also recognised by
        # GeminiClient._build_contents and OpenAIClient (when wired
        # up to handle multimodal user messages).
        block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        }
        cache[cache_key] = block
        logger.info(
            "[Vision] attaching step=%d image url=%s media_type=%s bytes=%d",
            self.current_topic_index, url[-60:], media_type, len(data) * 3 // 4,
        )
        return block

    def _read_image_for_vision(self, url: str):
        """Read an image at `url` (filesystem path, /media/... URL, or
        absolute http(s) URL) and return (base64_str, media_type).

        Strategy:
          1. If local file path or /media/... URL → read from disk
             relative to settings.MEDIA_ROOT.
          2. If absolute http(s) URL → download via requests with a
             short timeout. Fail silently → return ('', '').
        """
        import base64
        import os
        from django.conf import settings

        media_type = 'image/png'
        if url.lower().endswith(('.jpg', '.jpeg')):
            media_type = 'image/jpeg'
        elif url.lower().endswith('.gif'):
            media_type = 'image/gif'
        elif url.lower().endswith('.webp'):
            media_type = 'image/webp'

        # Local-path branch
        if url.startswith('/media/') or not url.startswith(('http://', 'https://')):
            relative = url[len('/media/'):] if url.startswith('/media/') else url
            local_path = os.path.join(settings.MEDIA_ROOT, relative.lstrip('/'))
            if not os.path.exists(local_path):
                return '', ''
            with open(local_path, 'rb') as f:
                raw = f.read()
            return base64.b64encode(raw).decode('ascii'), media_type

        # Remote-URL branch
        try:
            import requests
            resp = requests.get(url, timeout=4)
            if resp.status_code != 200:
                return '', ''
            return base64.b64encode(resp.content).decode('ascii'), (
                resp.headers.get('Content-Type', media_type).split(';')[0]
            )
        except Exception:
            return '', ''

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
    
    # NOTE (2026-05-05): _safe_generate_image REMOVED. On-the-fly
    # image generation via |||GENERATE:category:description||| was
    # disabled before, then deleted here. The tutor only surfaces
    # pre-attached step media via the |||MEDIA:N||| signal.

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
        
        # On-the-fly generation removed — return whatever we matched
        # from existing step media (or empty list if no good match).
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
6. Only show media if your text directly references the figure (e.g. "the diagram shows…"). A numeric warm-up question with no visual reference should NOT attach a figure.

End with a question. Keep it to 2-3 sentences max.
IMPORTANT: Any question you ask must be complete and self-contained. Never say "which of these" or reference options/choices you haven't listed."""

        response = self._generate_response(prompt, fallback_context="opening")

        # Parse |||MEDIA:N||| signal BEFORE saving — keeps DB clean
        clean_response, parsed_media = self._parse_media_signal(response)
        media = [parsed_media] if parsed_media else []
        # Trust the |||MEDIA:N||| signal — no inference fallback.

        # Record media for this turn (for resume artifact panel)
        if media:
            turn_index = len(self.conversation)  # index before appending
            self._turn_media[str(turn_index)] = media[0]

        # Save
        self._save_turn("tutor", clean_response)
        self.conversation.append({"role": "assistant", "content": clean_response})
        self._save_state()

        return self._create_message(clean_response, media=media)

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

            # Baseline-driven competency snapshot (per teaching objective).
            # Drives whether the tutor fast-forwards (mastered), uses
            # standard pacing (developing), or scaffolds heavily (weak).
            try:
                from apps.tutoring.competency_tracker import student_skills_snapshot
                from apps.curriculum.content_generator import combined_objectives_for_lesson

                course = self.lesson.unit.course if self.lesson.unit else None
                if course:
                    snapshot = student_skills_snapshot(self.student, course)
                    lesson_objectives = combined_objectives_for_lesson(self.lesson)
                    norm = lambda s: ' '.join((s or '').split()).strip()

                    # Pacing directive for THIS lesson's objective(s).
                    lesson_levels = []
                    for obj in lesson_objectives:
                        info = snapshot.get(norm(obj))
                        if info:
                            lesson_levels.append((obj, info))

                    if lesson_levels:
                        worst = min(lesson_levels, key=lambda kv: kv[1]['pct'])
                        obj_text, info = worst
                        level = info['level']
                        pct = info['pct']
                        if level == 'mastered':
                            directive = (
                                f"BASELINE PACE — student already shows {pct:.0f}% on "
                                f"this objective. Keep it tight: a short check-for-understanding, "
                                f"one applied problem, then move on. Do not over-teach."
                            )
                        elif level == 'developing':
                            directive = (
                                f"BASELINE PACE — student is {pct:.0f}% on this objective "
                                f"(developing). Standard pacing: short teach, worked example, "
                                f"guided practice, check."
                            )
                        else:  # weak
                            directive = (
                                f"BASELINE PACE — student is only {pct:.0f}% on this objective "
                                f"(weak). Scaffold heavily: anchor in concrete examples, "
                                f"use one-step problems before multi-step, check often."
                            )
                        lines.append(directive)

                    # Course-wide weakest objectives (cross-lesson signal).
                    weak_others = sorted(
                        ((tag, info) for tag, info in snapshot.items()
                         if info['level'] == 'weak' and norm(tag) not in {norm(o) for o in lesson_objectives}),
                        key=lambda kv: kv[1]['pct'],
                    )[:3]
                    if weak_others:
                        lines.append(
                            "Other weak objectives in this course (avoid assuming they're known): "
                            + "; ".join(f"{tag} ({info['pct']:.0f}%)" for tag, info in weak_others)
                        )
            except Exception as e:
                logger.debug(f"Skills snapshot unavailable: {e}")

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

    def _build_regen_bank_context(
        self, suppress_canonical: bool = False,
    ) -> Optional[Dict]:
        """Bundle the bank question + grader verdict as ground truth
        for the regen LLM. Returns None when no bank context exists.

        The shape matches what build_regen_prompt's `bank_context`
        parameter consumes: {question_text, options, correct_answer,
        explanation, student_answer, verdict, suppress_reason}.

        Pilot 2026-05-16: without this context, regen invented
        contradicting answers (student picked C=Human geography for
        a Human-geography question, grader confirmed CORRECT, regen
        rewrote with "cultural or social geography" which wasn't even
        one of the four options).

        Pilot 2026-05-17 (W3, leak-aware regen): when the regen is
        triggered by ISSUE_ANSWER_LEAK, pass suppress_canonical=True.
        That STRIPS the correct_answer and explanation fields from the
        context, so the rewrite LLM literally cannot leak them again.
        The regen prompt also gets a stricter directive (set via
        suppress_reason field) — see apps/tutoring/regen/prompt.py.
        """
        q = getattr(self, '_pending_bank_question', None)
        grade = getattr(self, '_pending_bank_grade', None)
        if q is None and grade is None:
            return None
        ctx: Dict[str, Any] = {}
        if q is not None:
            ctx['question_text'] = (
                getattr(q, 'question_text', None)
                or getattr(q, 'question', None)
                or getattr(q, 'teacher_script', '')
                or ''
            )
            # MCQ options if applicable
            qtype = (getattr(q, 'question_type', '') or '').lower()
            if qtype == 'mcq':
                if suppress_canonical:
                    # In leak-regen mode keep the OPTIONS visible (the
                    # rewrite needs to see them so it can ELIMINATE
                    # wrong ones in its hint) but blank out which
                    # letter is correct.
                    ctx['options'] = {
                        'A': getattr(q, 'option_a', '') or '',
                        'B': getattr(q, 'option_b', '') or '',
                        'C': getattr(q, 'option_c', '') or '',
                        'D': getattr(q, 'option_d', '') or '',
                    }
                else:
                    ctx['options'] = {
                        'A': getattr(q, 'option_a', '') or '',
                        'B': getattr(q, 'option_b', '') or '',
                        'C': getattr(q, 'option_c', '') or '',
                        'D': getattr(q, 'option_d', '') or '',
                    }
            if suppress_canonical:
                # Leak-aware regen: hide the canonical so the rewrite
                # cannot leak it.
                ctx['suppress_reason'] = 'answer_leak_regen'
            else:
                ctx['correct_answer'] = (
                    getattr(q, 'correct_answer', None)
                    or getattr(q, 'expected_answer', None)
                    or ''
                )
                ctx['explanation'] = getattr(q, 'explanation', '') or ''
        if grade is not None:
            ctx['verdict'] = getattr(grade, 'is_correct', None)
            sp = getattr(grade, 'student_parsed', '')
            ctx['student_answer'] = str(sp)[:200] if sp is not None else ''
        return ctx if ctx else None

    def _can_trigger_exit_ticket(self) -> bool:
        """Pilot directive 2026-05-17 — hold the exit-ticket transition
        until the just-answered bank question is fully discussed.

        Holds when ANY of:
          - The most recent bank grade verdict was wrong (the student
            shouldn't be ejected into assessment right after a wrong).
          - A reveal just happened this turn (wrong_attempts >= reveal
            threshold on the current bank Q). The reveal walkthrough
            needs at least one acknowledgement turn before assessment.
          - We're inside the explicit hold window set by a prior turn.

        Symptom this fixes: lesson 540 session 47 turn 828 — student
        answered "D" wrong on the legend MCQ; the chat-authored grader
        mis-graded it correct after W3 regen rewrote the stem; engine
        advanced step 2 → 3 → triggered exit ticket immediately. Even
        with the chat-authored mis-grade, this gate would have held the
        trigger because the BANK verdict (the authoritative source) was
        wrong on the just-answered question.
        """
        # Hold-window: previous turn marked a hold; honour until the
        # next student exchange has happened.
        if self.exchange_count < int(getattr(self, 'exit_ticket_hold_until_exchange', 0) or 0):
            return False
        # Last bank grade — if wrong, hold. _pending_bank_grade is set
        # by _grade_against_last_bank_question on the current turn; we
        # also consult _last_bank_grade_correct (persisted) for the
        # most recent answered bank Q's verdict across turns.
        bg = getattr(self, '_pending_bank_grade', None)
        if bg is not None and getattr(bg, 'is_correct', None) is False:
            return False
        # Reveal-just-happened — current bank Q has wrong_attempts
        # at or past the difficulty-tiered reveal threshold AND the
        # most recent grade was wrong (the engine layer that set
        # is_correct=True via chat-authored mis-grade still flips the
        # bank verdict via the BANK_OVERRIDE path, so we trust that
        # signal when present).
        aa = getattr(self, '_awaiting_answer', None) or {}
        wrong_attempts = int(aa.get('wrong_attempts', 0) or 0)
        if wrong_attempts >= self._reveal_threshold():
            # Reveal just unlocked. Hold for ≥1 more turn.
            return False
        return True

    def _reveal_threshold(self) -> int:
        """Number of wrong attempts at which reveal becomes allowed.
        Difficulty-tiered (decision 2026-05-17):

          easy (-2, -1) → 2  (1 hint, reveal on 2nd wrong)
          medium (0)    → 3  (2 hints, reveal on 3rd wrong — original default)
          hard  (+1,+2) → 4  (3 hints, reveal on 4th wrong)

        Lower-performing students get to canonical faster; higher-performing
        students get more productive struggle time.
        """
        level = int(getattr(self, 'difficulty_level', 0) or 0)
        if level <= -1:
            return 2
        if level >= 1:
            return 4
        return 3

    def _build_hint_calibration_block(
        self,
        correct_option_letter: Optional[str] = None,
        correct_option_text: Optional[str] = None,
        reveal_allowed: bool = False,
    ) -> str:
        """W2 — FORBIDDEN/ACCEPTABLE phrase list + difficulty-tiered
        obviousness directive. Rendered inside both
        `_build_active_bank_question_block` and
        `_build_bank_grade_signal_block` so the hint policy stays
        consistent whether the LLM is composing the hint pre-attempt
        or past the move-on threshold.

        Pilot directive 2026-05-17: reveal is NEVER allowed — past
        threshold, the tutor MOVES ON (re-explains concept, pivots to
        easier Q) instead of stating the canonical answer. The
        FORBIDDEN list therefore stays in force on every turn while a
        bank Q is active. The `reveal_allowed` parameter is kept for
        signature stability but the block always renders.

        Concrete examples beat abstract prohibitions on Sonnet
        (learning L14). The forbidden list uses the actual option text
        from the live question when known so the tutor can't
        rationalise "well, that wasn't a paraphrase of THIS option".
        """
        # reveal_allowed kept for caller signature stability — no
        # longer gates the block; reveal is never allowed under the
        # 2026-05-17 directive.
        _ = reveal_allowed  # noqa: F841

        lines: List[str] = ["", "[HINT CALIBRATION]"]

        # --- FORBIDDEN / ACCEPTABLE phrase list ---
        letter = (correct_option_letter or "").strip().upper()
        opt = (correct_option_text or "").strip()
        forbidden_examples: List[str] = []
        if letter:
            forbidden_examples += [
                f'  - "The answer is {letter}."',
                f'  - "The correct option is {letter}."',
                f'  - "Option {letter} is right."',
                f'  - "It\'s {letter}." / "{letter} is correct."',
            ]
        if opt:
            short = opt[:120]
            forbidden_examples += [
                f'  - Restating the canonical answer text in different '
                f'words. For example, if the correct option says '
                f'"{short}", these all count as REVEAL:',
                f'      • "Think about how it relates to {short.lower()}."',
                f'      • "The key idea here is {short.lower()}."',
                f'      • Any sentence whose main noun phrase is a '
                f'paraphrase of the canonical answer text.',
            ]
        if not forbidden_examples:
            # Chat-authored / non-MCQ — generic list.
            forbidden_examples = [
                '  - Stating the canonical answer directly.',
                '  - Paraphrasing the canonical answer (substituting '
                'synonyms still counts as REVEAL).',
                '  - Walking through the full canonical explanation '
                'before the student has attempted again.',
            ]
        lines.append("FORBIDDEN (counts as REVEAL — triggers regen):")
        lines.extend(forbidden_examples)

        lines.append("ACCEPTABLE (concept-level hints):")
        lines += [
            '  - Name the underlying concept the question is testing '
            'without naming the answer ("Think about what this feature '
            'is used for").',
            '  - Eliminate ONE wrong option by describing what it is '
            'about ("One option is about scale, which is a different '
            'idea") — do NOT name the correct option.',
            '  - Ask a sub-question that probes prerequisite knowledge.',
        ]

        # --- Difficulty-tiered obviousness directive ---
        # difficulty_level: -2 very easy ←→ +2 very hard.
        # Reveal threshold stays uniform (wrong_attempts >= 3 for
        # everyone, per pilot directive 2026-05-17). Difficulty steers
        # how OBVIOUS each hint should be, not when reveal fires.
        level = int(getattr(self, 'difficulty_level', 0) or 0)
        if level <= -2:
            obviousness = (
                "OBVIOUSNESS LEVEL — VERY OBVIOUS (very-easy mode): "
                "near-Socratic. Eliminate at least one wrong option by "
                "describing what it is about (without naming the right "
                "one), then ask a yes/no sub-question that points at "
                "the correct concept."
            )
        elif level == -1:
            obviousness = (
                "OBVIOUSNESS LEVEL — OBVIOUS (easy mode): name the "
                "concept being tested directly and prompt the student "
                "to apply it. Do not name the answer."
            )
        elif level == 0:
            obviousness = (
                "OBVIOUSNESS LEVEL — CONCEPT (default): one short "
                "sentence that names the concept being tested. Let the "
                "student do the connecting work."
            )
        elif level == 1:
            obviousness = (
                "OBVIOUSNESS LEVEL — SUBTLE (hard mode): a single "
                "question that requires inference. Do not name the "
                "concept directly; gesture at it."
            )
        else:  # level >= 2
            obviousness = (
                "OBVIOUSNESS LEVEL — MINIMAL (very-hard mode): one "
                "short signal of WHICH idea to revisit, nothing more. "
                "No concept naming, no sub-question. The student does "
                "the work."
            )
        lines.append(obviousness)
        return "\n".join(lines)

    def _build_active_bank_question_block(self) -> str:
        """[ACTIVE BANK QUESTION] context block for the system prompt.

        Injected when self._awaiting_answer is set. Carries:
          - The question stem + (for MCQ) the four options
          - The verified expected_answer / correct_answer + explanation
            so the tutor scaffolds against truth, not its own guess.
          - student_status: 'awaiting_answer' | 'answered_correct' |
            'answered_wrong' — drives the scaffolding rules below.

        Scaffolding rules per memory/tutor_reliability_v2_plan.md
        (decided 2026-05-15):

          awaiting_answer  → hints only, no Socratic probes; let the
                             student answer first.
          answered_correct → confirm + explain WHY using the bank's
                             explanation field. NEVER ask the student
                             to show working on a known-correct answer.
                             NEVER ask "let's check that" — the tutor
                             knows it's correct.
          answered_wrong   → acknowledge gently; THEN it's OK to probe
                             ("what was your reasoning?") to find the
                             misconception, then re-explain.

        Returns "" when no question is awaiting (most turns).
        """
        rec = getattr(self, '_awaiting_answer', None)
        if not rec:
            return ""
        # inline_authored / inline_mcq have no question_id (the question
        # lives only on the previous tutor turn's metadata or on the
        # awaiting_answer record itself). Allow them through; the
        # kind-dispatch below reads from turn metadata / the record.
        if (
            not rec.get('question_id')
            and rec.get('kind') not in ('inline_authored', 'inline_mcq')
        ):
            return ""

        # Resolve student_status from the most-recent grade verdict
        # if any. _pending_bank_grade is set by
        # _grade_against_last_bank_question whenever the student replies
        # to a bank question (BankGradeResult with .is_correct).
        status = 'awaiting_answer'
        grade = getattr(self, '_pending_bank_grade', None)
        if grade is not None and getattr(grade, 'is_correct', None) is not None:
            status = 'answered_correct' if grade.is_correct else 'answered_wrong'

        kind = rec.get('kind')
        question_id = rec['question_id']

        # Look up the bank entry for the verified key + explanation.
        try:
            if kind == 'lesson_step':
                from apps.curriculum.models import LessonStep
                step = LessonStep.objects.filter(id=question_id).first()
                if step is None:
                    return ""
                stem = (step.question or step.teacher_script or '')[:600]
                expected = (step.expected_answer or '').strip()[:200]
                lines = [
                    "[ACTIVE BANK QUESTION]",
                    "A bank question is awaiting an answer. The student",
                    "sees the question rendered in the side artifact panel.",
                    "DO NOT re-author the question stem in your reply.",
                    "",
                    f"  question_id: {step.id}",
                    f"  kind: lesson_step",
                    f"  stem: {stem}",
                    f"  expected_answer: {expected}",
                    f"  student_status: {status}",
                ]
            elif kind == 'exit_ticket_question':
                from apps.tutoring.models import ExitTicketQuestion
                q = ExitTicketQuestion.objects.filter(id=question_id).first()
                if q is None:
                    return ""
                stem = (q.question_text or '')[:600]
                lines = [
                    "[ACTIVE BANK QUESTION]",
                    "A bank question is awaiting an answer. The student",
                    "sees the question rendered in the side artifact panel.",
                    "DO NOT re-author the question stem in your reply.",
                    "",
                    f"  question_id: {q.id}",
                    f"  kind: exit_ticket_question",
                    f"  question_type: {q.question_type or 'mcq'}",
                    f"  stem: {stem}",
                ]
                if q.question_type == 'mcq':
                    lines += [
                        f"  options:",
                        f"    A: {(q.option_a or '')[:200]}",
                        f"    B: {(q.option_b or '')[:200]}",
                        f"    C: {(q.option_c or '')[:200]}",
                        f"    D: {(q.option_d or '')[:200]}",
                        f"  correct_answer: {q.correct_answer or '(none)'}",
                    ]
                if q.explanation:
                    lines.append(f"  explanation: {q.explanation[:400]}")
                lines.append(f"  student_status: {status}")
            elif kind == 'inline_authored':
                # The tutor authored this question via
                # pose_inline_question. Question text + answer_key
                # live on the previous tutor turn's metadata. Pull
                # them so the LLM sees what was asked + the verdict.
                from apps.tutoring.models import SessionTurn
                _last_t = (
                    SessionTurn.objects
                    .filter(session=self.session, role='tutor')
                    .order_by('-created_at')
                    .first()
                )
                ia = (
                    ((_last_t.metadata or {}).get(
                        'inline_authored_question', {}
                    ) or {})
                    if _last_t else {}
                )
                if not ia:
                    return ""
                lines = [
                    "[ACTIVE INLINE-AUTHORED QUESTION]",
                    "You authored this question on the previous turn",
                    "via pose_inline_question. Use the answer_key +",
                    "student_status below to scaffold the next turn.",
                    "",
                    f"  kind: inline_authored",
                    f"  question_type: {ia.get('question_type', 'short_answer')}",
                    f"  stem: {(ia.get('question') or '')[:600]}",
                    f"  answer_key: {(ia.get('answer_key') or '')[:200]}",
                    f"  student_status: {status}",
                ]
                if ia.get('working'):
                    lines.append(
                        f"  reference_working: {(ia.get('working') or '')[:300]}"
                    )
            elif kind == 'inline_mcq':
                # Task #173 (2026-05-17). The LLM authored MCQ options
                # inline (no pose_question, no answer key in the bank).
                # We don't know the canonical answer programmatically;
                # the chat-authored grader's LLM judges each reply. All
                # this block does is surface the wrong_attempts +
                # student_status to the tutor so the hint-vs-reveal
                # gating still works.
                lines = [
                    "[ACTIVE INLINE-AUTHORED MCQ]",
                    "You authored an MCQ in chat narrative on a prior",
                    "turn (no answer key in the bank). The chat-authored",
                    "grader's LLM judges each reply; wrong_attempts is",
                    "tracked on this record so the hint-vs-reveal threshold",
                    "still applies.",
                    "",
                    f"  kind: inline_mcq",
                    f"  question_type: mcq",
                    f"  stem: {(rec.get('authored_question_text') or '')[:600]}",
                    f"  student_status: {status}",
                ]
            else:
                return ""
        except Exception as exc:
            logger.warning(
                f"[ActiveBankQuestion] resolve failed for {kind}#{question_id}: "
                f"{type(exc).__name__}: {exc}"
            )
            return ""

        # Status-driven scaffolding rules. Tight wording so the LLM
        # internalises them without a separate behavioural prompt.
        #
        # CRITICAL HINT POLICY (2026-05-17, pilot lesson 540 session 44):
        # The correct_answer letter + explanation are above so the
        # tutor can scaffold against TRUTH, NOT so it can reveal them.
        # When awaiting / answered_wrong, the tutor MUST give a HINT
        # (a clue that narrows the answer space or surfaces the
        # underlying concept) and let the student retry. Reveal is
        # permitted ONLY after the student has attempted twice and
        # explicitly asked for the answer, or after wrong_attempts >= 3
        # on the same question. Otherwise the tutor learns nothing
        # about the student's misconception and the student doesn't
        # struggle productively.
        wrong_attempts = int(rec.get('wrong_attempts', 0) or 0)
        _reveal_at = self._reveal_threshold()
        reveal_allowed = (status == 'answered_wrong' and wrong_attempts >= _reveal_at)
        rules = {
            'awaiting_answer': (
                "Scaffolding: HINT ONLY — never reveal the correct "
                "option letter or the answer text in this turn. "
                "Don't ask the student to explain their reasoning "
                "yet — let them answer first. If they're stuck, "
                "give ONE hint that narrows the choices or names "
                "the concept being tested (e.g. 'Think about which "
                "feature explains what symbols mean'). Reference "
                "options indirectly without re-stating the stem."
            ),
            'answered_correct': (
                "Scaffolding: the student got it RIGHT. Confirm "
                "briefly + explain WHY using the explanation field "
                "above. NEVER ask 'let's check that', NEVER ask the "
                "student to show working, NEVER probe their reasoning. "
                "The verified answer key tells you it's correct."
            ),
            'answered_wrong': (
                "Scaffolding: the student answered INCORRECTLY "
                f"(wrong_attempts: {wrong_attempts}). DO NOT REVEAL "
                "the correct option letter or paraphrase the correct "
                "answer text. Acknowledge gently ('not quite' / "
                "'close, but think about…'), point at the concept "
                "they missed (use the explanation field to inform "
                "the hint, but rephrase as a clue not a giveaway), "
                "and invite them to try again. ONE short probe is OK "
                "('what made you pick that?'). Let them attempt the "
                "question again."
                + (
                    f"\n  ↳ MOVE-ON ALLOWED this turn: student has "
                    f"missed this question {_reveal_at}+ times — do "
                    "NOT reveal the answer. Re-explain the underlying "
                    "concept in 1-2 sentences (no answer text, no "
                    "option letter), then pose a DIFFERENT question "
                    "on the same concept (or an easier related one) "
                    "via pose_question."
                    if reveal_allowed else ""
                )
            ),
        }
        lines.append("")
        lines.append(rules.get(status, rules['awaiting_answer']))

        # W2 — append FORBIDDEN/ACCEPTABLE list + difficulty-tiered
        # obviousness when in awaiting_answer or answered_wrong AND
        # reveal is NOT yet allowed.
        if status in ('awaiting_answer', 'answered_wrong'):
            # Pull the correct-option text for MCQ so the forbidden
            # list can quote the actual canonical text.
            _opt_letter: Optional[str] = None
            _opt_text: Optional[str] = None
            try:
                if kind == 'exit_ticket_question':
                    from apps.tutoring.models import ExitTicketQuestion
                    _q = ExitTicketQuestion.objects.filter(id=question_id).first()
                    if _q and (_q.question_type or 'mcq') == 'mcq':
                        _opt_letter = (_q.correct_answer or '').strip().upper()
                        _opt_text = {
                            'A': _q.option_a, 'B': _q.option_b,
                            'C': _q.option_c, 'D': _q.option_d,
                        }.get(_opt_letter)
                elif kind == 'lesson_step':
                    from apps.curriculum.models import LessonStep
                    _s = LessonStep.objects.filter(id=question_id).first()
                    if _s:
                        _opt_text = (_s.expected_answer or '').strip()
            except Exception:
                pass
            calib = self._build_hint_calibration_block(
                correct_option_letter=_opt_letter,
                correct_option_text=_opt_text,
                reveal_allowed=reveal_allowed,
            )
            if calib:
                lines.append(calib)
        return "\n".join(lines)

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
            blocks.append(f"""[DIFFICULTY ADJUSTMENT — EASY MODE]
Student is in easy mode (struggle level: {abs(level)}/2). Lower-
performing students need MCQ / fill-in-blank formats with visual
anchors, NOT open-text working-required questions.

QUESTION FORMAT:
- Prefer MCQ or fill-in-blank bank questions over open-ended.
  Use the pose_question tool to render bank entries — many bank
  entries are MCQ and the tool renders the options on screen.
- For math: STILL show worked examples first (math memory rules),
  but the student's PRACTICE response is a letter or single number.
- Always include or reference a visual/diagram when one helps anchor
  the question (use the lesson's step media, or describe a diagram in
  the question).

SCAFFOLDING TONE ({intensity} application):
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

    def _build_pretest_diagnostic_block(self) -> str:
        """If the student took a diagnostic pre-test before starting
        this lesson and didn't pass, surface the per-EO sub-skill map
        so the tutor focuses on the gaps and skips what they showed
        they already know."""
        diag = getattr(self, 'pretest_diagnostic', None)
        if not diag:
            return ""
        achieved = [eo for eo in (diag.get('achieved_eos') or []) if eo]
        failed = [eo for eo in (diag.get('failed_eos') or []) if eo]
        if not achieved and not failed:
            return ""
        score = diag.get('score', 0)
        total = diag.get('total', 0)
        score_str = f"{score}/{total}" if total else "partial"
        lines = [
            f"[PRE-TEST RESULT — {score_str}]",
            "The student took a diagnostic pre-test before starting this lesson. "
            "Use the breakdown below to focus the tutoring:",
        ]
        if achieved:
            lines.append("• Already demonstrated competency on these sub-skills (DON'T re-teach unless they ask):")
            for eo in achieved[:8]:
                lines.append(f"  - {eo}")
        if failed:
            lines.append("• Got these wrong on the pre-test — PRIORITIZE these in the lesson:")
            for eo in failed[:8]:
                lines.append(f"  - {eo}")
        lines.append("[/PRE-TEST RESULT]")
        return "\n".join(lines)

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
        *,
        omit_history: bool = False,
    ) -> str:
        """Build the LLM user prompt for generating a tutoring response.

        Shared by _generate_contextual_response() and respond_stream()
        to prevent the two copies from diverging.

        When ``omit_history=True``, the CONVERSATION CONTEXT and STUDENT
        JUST SAID sections are dropped — the caller is passing
        conversation history as a structured ``messages`` array instead
        of embedding it in this prompt. This is the right shape for
        models that follow turn-array semantics (Sonnet, gpt-4o, Gemini)
        and stops the looping behaviour where the model treats the
        explicit STEP DIRECTIVE as the latest instruction and ignores
        the embedded history.
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
        pretest_block = self._build_pretest_diagnostic_block()
        if pretest_block:
            difficulty_block = (difficulty_block + "\n\n" + pretest_block).strip()

        # R2 (2026-05-15): when a bank question is awaiting an answer
        # we inject a context block so the tutor can scaffold without
        # re-authoring the stem (which trips NO_AUTHORING). The block
        # carries the verified answer key + explanation so the tutor
        # can confirm + explain WHY rather than asking the student to
        # show working on a known-correct answer. Read the docstring
        # on _build_active_bank_question_block for the scaffolding
        # rules this enables.
        active_question_block = self._build_active_bank_question_block()
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

        # When omit_history=True, the conversation history flows
        # through as a proper messages array. The directive below
        # references the student's latest turn implicitly (the model
        # already sees it as the most recent user message).
        history_block = (
            ""
            if omit_history
            else f"CONVERSATION CONTEXT:\n{self._format_recent_conversation(5)}\n\n"
            f'STUDENT JUST SAID: "{student_input}"\n\n'
        )

        return f"""{history_block}LESSON CONTEXT:
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

{active_question_block}

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
        """Generate a response based on student input and context.

        Conversation history flows through as a proper ``messages``
        array (set via ``self.conversation``). The per-turn directive
        block becomes a system-prompt suffix. This is the structural
        fix for the looping behaviour where models treat the explicit
        STEP DIRECTIVE as the latest instruction and ignore embedded
        history.
        """
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

        directive_block = self._build_response_prompt(
            student_input, kb_context, visual_instructions, omit_history=True,
        )
        return self._generate_response(directive_block)
    
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

                # For fill_in_blank failures, surface per-blank detail
                # so the tutor can target the specific blank that
                # failed instead of re-explaining the whole question.
                # Empty for non-fill-in-blank or older failed-question
                # records that predate per-blank tracking.
                per_blank_block = ""
                if failed_q:
                    bc = failed_q.get('blanks_correct') or []
                    br = failed_q.get('blanks_reasoning') or []
                    sa = failed_q.get('student_answer')
                    if bc and isinstance(sa, list):
                        rows = []
                        for idx, ok in enumerate(bc):
                            student_v = sa[idx] if idx < len(sa) else ''
                            reason = br[idx] if idx < len(br) else ''
                            verdict = '✓ accepted' if ok else '✗ wrong'
                            rows.append(
                                f"  Blank {idx + 1}: \"{student_v}\" {verdict}"
                                + (f" ({reason})" if reason else "")
                            )
                        per_blank_block = (
                            "\n\nPer-blank verdict (from auto-grader):\n"
                            + "\n".join(rows)
                            + "\nFocus your remediation on the WRONG blanks "
                            "only — don't re-teach the ones that were "
                            "accepted."
                        )

                return f"""🎯 REMEDIATION FOCUS - This is a concept the student got WRONG on the exit ticket:

Question they missed: "{concept['question']}"
Their wrong answer was: "{failed_q.get('student_answer', '?') if failed_q else '?'}"
Correct answer: "{concept['correct_text']}"
Why it's correct: "{concept.get('explanation', 'This is the key concept to understand')}"{per_blank_block}

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
        """Call the LLM to generate a response.

        On math turns, routes through the pose_question tool so the LLM
        cannot author numerical questions in free prose. On non-math
        turns, falls back to plain text generation.
        """
        self._last_response_was_fallback = False

        if not self.llm_client:
            logger.warning(
                f"No LLM client available for session={self.session.id} "
                f"lesson='{self.lesson.title}'"
            )
            return self._fallback_response(fallback_context)

        try:
            # Conversation history → proper messages array. The caller's
            # `prompt` is the per-turn directive (step task, kb context,
            # response rules) which now becomes a system-prompt suffix
            # rather than embedded text in a single user message. This
            # is structural fix (A) — see the architecture map: prior
            # behaviour put history in the user prompt as text, which
            # gpt-4o (and to a lesser extent Sonnet) ignored in favour
            # of the most concrete directive, causing TEACH-step loops.
            messages = list(self.conversation)
            # Edge: if there's no history yet (first turn / start path),
            # prepend a placeholder so the API doesn't reject an empty
            # messages array. The directive in the system prompt drives
            # the response.
            if not messages:
                messages = [{"role": "user", "content": "Begin the lesson."}]
            # If the last turn is from the assistant (rare — happens on
            # certain resume paths), append a marker user turn so the
            # API has a proper user→assistant alternation.
            if messages and messages[-1].get("role") == "assistant":
                messages.append({"role": "user", "content": "Continue."})

            # VISION: when the current step has an attached figure,
            # convert the latest user message to a multimodal content
            # block so the LLM can SEE the figure rather than relying
            # on text metadata alone. Catches cases like the function-
            # machine where the model misread "×4" as "2x" because it
            # only had the abstract description, not the image.
            #
            # The image attaches FIRST in the content list (Anthropic
            # best practice for grounding the model on a visual). The
            # student's text follows so the model treats the visual as
            # context for what's being asked.
            vision_block = self._get_step_vision_block()
            if vision_block:
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "user":
                        existing_text = messages[i].get("content", "")
                        # If content is already a list (rare — only on
                        # subsequent turns within the same response),
                        # don't double-attach.
                        if isinstance(existing_text, list):
                            break
                        messages[i] = {
                            "role": "user",
                            "content": [
                                vision_block,
                                {"type": "text", "text": str(existing_text or "")},
                            ],
                        }
                        logger.info(
                            "[Vision] attached step image to user msg #%d "
                            "for tutor turn",
                            i,
                        )
                        break

            # _build_system_prompt populates self._question_id_map as a
            # side effect — must be called BEFORE _build_pose_question_tool.
            base_system_prompt = self._build_system_prompt()
            system_prompt = (
                base_system_prompt
                + "\n\n<turn_directive>\n"
                + (prompt or "").strip()
                + "\n</turn_directive>"
            )
            # Fingerprint the EXACT prompt the LLM will see, so the
            # benchmark can record WHICH prompt revision produced a
            # given response. Stashed for the surrounding respond()
            # to pick up into turn_metadata['prompt_pack'].
            from apps.tutoring.judges._prompt_meta import prompt_fingerprint
            _ph, _pc = prompt_fingerprint(system_prompt)
            self._last_tutor_prompt_meta = {'hash': _ph, 'chars': _pc}

            is_math = False
            try:
                is_math = bool(self.lesson.unit.course.is_math)
            except Exception:
                pass

            # Bank + pose_question tool is universal (2026-05-05).
            # Any subject's lesson with a populated bank gets the
            # tool — geography MCQ, science fill-in-blank, history
            # short-answer all benefit from verified-question grounding.
            # _build_pose_question_tool() returns None when the
            # id_map is empty (no bank for this lesson), in which
            # case we fall through to the plain text path.
            tool = (
                self._build_pose_question_tool()
                if hasattr(self.llm_client, 'generate_with_tools')
                else None
            )
            # pose_inline_question was tried 2026-05-16 to allow the
            # tutor to author check/scaffolding questions with an
            # answer key. REVERTED same day: the LLM supplied bad
            # answer keys (numeric '85' for a conceptual "set up the
            # equation" question), grader graded against the bad key,
            # student marked wrong, downstream gates fired → empty
            # turn. Net: tutor-authored questions are unreliable. Stick
            # with bank-only. When the bank has no fit AND the LLM
            # still authors in chat, the chat-authored fallback grader
            # (no-key, LLM-derived) handles it without the bad-key
            # trap. Per user direction 2026-05-16: "this is why I did
            # not want to allow the tutor to pose its own question."
            inline_tool = None

            if tool is not None or inline_tool is not None:
                # Tool-capable client → tool-use path. The LLM can pose
                # via pose_question (bank) OR pose_inline_question
                # (authored-with-key) depending on context.
                self._pending_pose_question_meta = {}
                tools_to_offer = [t for t in (tool, inline_tool) if t is not None]
                # tool_choice="auto" — LLM picks whether to call
                # pose_question (when a bank slot fits) or generate
                # free-prose narrative. The forced tool_choice="any"
                # was reverted 2026-05-16 alongside the
                # pose_inline_question removal: with only the bank
                # tool available, forcing would push the LLM to call
                # it even when no slot fits, which produced
                # off-curriculum questions.
                _force_tool_choice = None
                try:
                    message = self.llm_client.generate_with_tools(
                        messages=messages,
                        system_prompt=system_prompt,
                        tools=tools_to_offer,
                        max_tokens=2048,
                        tool_choice=_force_tool_choice,
                    )
                    # The handler iterates message.content as a list of
                    # blocks. If we got back something that doesn't
                    # quack like an Anthropic Message (e.g. a test
                    # MagicMock without proper structure), fall through
                    # to the plain-text path below.
                    if not hasattr(message, 'content') or not isinstance(
                        getattr(message, 'content', None), list
                    ):
                        raise TypeError(
                            f"generate_with_tools returned non-Message: "
                            f"{type(message).__name__}"
                        )
                    final_text = self._handle_pose_question_message(
                        message, self._pending_pose_question_meta,
                    )
                    return final_text.strip()
                except (NotImplementedError, AttributeError, TypeError) as e:
                    logger.warning(
                        "[QuestionTool] tool path unavailable, "
                        "falling back to text path: %s", e,
                    )

            # Non-math (or non-Anthropic) → plain text generation.
            logger.info(
                "[QuestionTool] generate: TEXT_PATH is_math=%s has_tool_method=%s",
                is_math, hasattr(self.llm_client, 'generate_with_tools'),
            )
            response = self.llm_client.generate(
                messages=messages,
                system_prompt=system_prompt,
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
            "\n   the student gave a one-line answer with no explanation. Use a"
            "\n   neutral, specific acknowledgment (e.g. \"yes — 8 is right.\")"
            "\n   and ADVANCE to the next step. Do NOT ask for reasoning, working,"
            "\n   or 'how did you get there' on a correct answer — bare or"
            "\n   otherwise. Probing on every correct reply reads as interrogation,"
            "\n   not teaching."
            "\n"
            "\n2. ALWAYS END WITH A QUESTION. The tutor leads the session —"
            "\n   you are not waiting for the student to drive the next move."
            "\n   EVERY turn must end with a question that moves the student"
            "\n   forward. No exceptions except the final wrap-up turn after the"
            "\n   student has demonstrated mastery of the lesson objective."
            "\n   Two tools are available for posing questions:"
            "\n     - pose_question(slot): pull a CANONICAL question from the"
            "\n       lesson bank (curriculum-aligned practice + exit-ticket items)."
            "\n       Prefer this when a fitting slot exists."
            "\n     - pose_inline_question(question, answer_key, type, ...):"
            "\n       AUTHOR your own check / scaffolding question with an"
            "\n       answer key the grader will use. Use this when no bank"
            "\n       slot fits — quick comprehension checks, simpler"
            "\n       sub-steps, rephrased versions for struggling students."
            "\n   For non-numerical free-prose questions (\"why does that"
            "\n   work?\"), it's still OK to end with the question in your"
            "\n   text — never end with a colon or fragment like \"Quick"
            "\n   check:\" without a question following."
            "\n"
            "\n3. ONE NEW IDEA AT A TIME. Do not list 5 facts in a single turn."
            "\n   Introduce one concept, ask the student to engage with it, then"
            "\n   layer the next concept based on their response."
            "\n"
            "\n4. BE HONEST WHEN YOU'RE UNCERTAIN. If you're stating a specific"
            "\n   number, comparison, or named fact, only do so when it's grounded"
            "\n   in the curriculum context provided to you below. If you're not"
            "\n   sure, say so and ask the student to look it up — never invent."
            "\n"
            "\n5. NEVER NARRATE WHAT YOU ARE ABOUT TO DO. Do not start a"
            "\n   response with 'I need to…', 'Let me first…', 'First, I'll…',"
            "\n   'My plan is…', 'I'm going to…'. Just write the response."
            "\n   The student does not need a preamble — they need the answer."
            "\n</socratic_rules>"
        )

        # Append math-specific instructions. Four orthogonal rules —
        # each covers a distinct concern with no overlap, so the LLM
        # treats them as a checklist instead of a debate. Examples
        # are kept short and concrete; long prose dilutes the signal.
        if self.lesson.unit.course.is_math:
            system_prompt += (
                "\n\n<math_teaching>"
                "\nThis is a MATHEMATICS lesson. Your job is to scaffold the "
                "student through verified questions — never to author or solve."
                "\n"
                "\n=== R1: BANK IS THE SOURCE OF TRUTH ==="
                "\nFor any question with numerical values:"
                "\n  • POSE — call the pose_question tool with a slot from "
                "the <question_bank> below. The tool is the ONLY way to "
                "ask a numerical question. Do NOT type questions in your "
                "text response — the system will strip them."
                "\n  • GRADE — read the verdict from <bank_evaluation_signal> "
                "or <math_evaluation_signal>. The server already checked "
                "against the bank's stored answer; do NOT recompute."
                "\n  • EXPLAIN — quote the canonical_working from the bank "
                "verbatim. Never paraphrase, never re-derive."
                "\n  • If you must reference numerical examples in prose "
                "(rule recital, conceptual framing), they MUST satisfy the "
                "lesson's rule (e.g. \"angles around a point sum to 360°\" → "
                "any \"around a point\" list you state MUST sum to 360°)."
                "\n"
                "\n=== R2: ADVANCE ON CORRECT ANSWERS — NEVER PROBE ==="
                "\nWhen the student's answer is CORRECT (with OR without "
                "working shown), CONFIRM briefly + ADVANCE to the next "
                "step or sub-question. Do NOT ask 'how did you get "
                "there?', 'what was your reasoning?', 'walk me through "
                "your working', 'what made you identify X?', or any "
                "other reasoning probe. This applies whether the answer "
                "is bare (\"8\") or has working (\"25 × 8 = 200\"). "
                "Probing on a correct answer is interrogation, not "
                "teaching — kill it.\n"
                "When the student's answer is INCORRECT and the reply "
                "contains working, name the specific wrong step in one "
                "sentence and give a short hint. Do not ask 'walk me "
                "through your steps' — they already did. Diagnose, "
                "don't interrogate."
                "\n"
                "\nMCQ value-form acceptance: when the bank pulled an "
                "MCQ question and the student answered with the correct "
                "VALUE (e.g. \"88\" when option A is \"88°\") rather "
                "than the letter, ACCEPT IT and advance. Do NOT ask "
                "them to translate to a letter — that's pedantic, not "
                "pedagogical. The bank grader already maps value→letter "
                "for you in <bank_evaluation_signal>."
                "\n"
                "\n=== R3: SCAFFOLD, DON'T SOLVE ==="
                "\nNever do the math FOR the student. Three concrete forms:"
                "\n  • Partial-correct working → ask 'what comes next?', "
                "don't compute the next step yourself."
                "\n  • Wrong answer → give a TARGETED HINT pointing at the "
                "misconception and ask retry. Never reveal the answer in "
                "the same turn as catching the error."
                "\n  • Reveal the answer ONLY after 5+ wrong attempts on "
                "the SAME step OR explicit 'I give up' / 'show me'. When "
                "you do reveal, quote the canonical_working from "
                "<bank_evaluation_signal>; never re-derive."
                "\n"
                "\nExample (catches error, hints, asks retry):"
                "\n  ❌ 'Hold on — 360 − 295 isn't 55. It's 65, so x = 65°.'"
                "\n  ✓  'Hold on — let's redo 360 − 295. Try thinking of "
                "it as 360 − 300, then add back. What do you get?'"
                "\n"
                "\n=== R4: SUBSKILLS + RUNG LADDER ==="
                "\nFor each problem, name the subskills first ('this needs "
                "(1) substitution, (2) BIDMAS'). When the student fails, "
                "diagnose WHICH subskill broke and drop one rung — give a "
                "simpler problem isolating that subskill, then return."
                "\nLadder: whole-number → decimal/fraction/negative → "
                "word problem → inverse. Don't skip for a struggler; don't "
                "linger when they've shown mastery."
                "\n"
                "\n=== NOTATION + COMMON TRAPS ==="
                "\nUse LaTeX: $\\frac{1}{2}$, $3x + 5 = 20$. Show every line. "
                "Ask for an estimate before calculating. Watch for: BIDMAS "
                "(3+4×2=11 not 14), sign-flips on negatives, adding fraction "
                "denominators, distributing negatives across brackets, "
                "percentage 'of' vs 'is'. Name the misconception by name "
                "when you spot it."
                "\n</math_teaching>"
            )

        # Append group-session block when there is more than one active
        # participant (G3). The tutor addresses the group collectively.
        system_prompt += self._build_group_session_block()

        # Append Seychelles context library (P1.5)
        system_prompt += self._build_seychelles_context_block()

        # Append media catalog so the LLM knows what figures are available
        system_prompt += self._build_media_catalog()

        # Append figure_facts — structured visual ground truth for any
        # figure attached to the current step. Replaces "imagine" with
        # "look at the figure". See memory/figure_facts_plan.md.
        system_prompt += self._build_figure_facts_block()

        # Append question bank — math-only. Forces the tutor to pose
        # only verified questions, never author its own. See
        # memory/tutor_no_authoring_plan.md.
        system_prompt += self._build_question_bank_block()

        # A.4 — walkthrough hint guidance. When the student is in
        # remediation walkthrough and has just answered a question
        # WRONG, the tutor MUST give a hint or short explanation
        # (not the answer) and ask them to try again. This block
        # forces that behaviour and is the prompt-side counterpart
        # to the retry-counter cap in _maybe_advance_walkthrough.
        system_prompt += self._build_walkthrough_hint_block()

        # Regeneration constraint (V3) — appended when retrying after a
        # validator hard-fail. Highest-priority block.
        regen = getattr(self, '_pending_regen_constraint', None)
        if regen:
            system_prompt += regen

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
            current_step_type = ''
            if self.current_topic_index < len(self.steps):
                current_step_type = (
                    self.steps[self.current_topic_index].step_type or ''
                )
            system_prompt += self._build_math_eval_signal_block(
                pending_check,
                student_input,
                bare_answer=bare,
                bare_answer_count_for_step=bare_count,
                step_type=current_step_type,
            )

        # Time-awareness block — per-turn, tells the tutor how much
        # tutoring time remains and prescribes a pace adjustment.
        # The exit-ticket reserve is held back so the tutor never
        # burns the final ~5 minutes that belong to the exit ticket.
        try:
            system_prompt += self._build_time_awareness_block()
        except Exception as e:
            logger.warning("[TimeAwareness] block failed: %s", e)

        # P3 — bank grading verdict. When the previous turn rendered a
        # bank question, the deterministic grader has already judged
        # the student's reply. This block is the signal the LLM reads
        # to decide what to say. The platform-wide rule is enforced
        # here: the LLM is told the verdict, not asked for one.
        bank_grade = getattr(self, '_pending_bank_grade', None)
        if bank_grade is not None and bank_grade.is_correct is not None:
            system_prompt += self._build_bank_grade_signal_block(bank_grade)

        if pending_check is None and getattr(self, '_pending_bare_answer', False):
            # Bare numeric answer on a math practice/quiz step but no
            # expected_answer to check against (i.e. the tutor invented
            # an interim arithmetic question on the fly). Without this
            # block the LLM falls back to its own judgement and praises
            # what it independently knows is right — violating Rule 1.
            student_input = getattr(self, '_pending_math_student_input', '') or ''
            bare_count = self.bare_answer_counts_by_step.get(
                self.current_topic_index, 0,
            )
            system_prompt += self._build_bare_answer_only_block(
                student_input,
                bare_answer_count_for_step=bare_count,
            )

        # Layer S — student-working analysis block. Appended AFTER
        # the math eval signal so it's the very last thing the LLM
        # reads before generating. The two blocks are complementary:
        # eval-signal verdicts the FINAL answer; working-analysis
        # diagnoses each STEP and tells the LLM whether the student
        # is partway through (PARTIAL_CORRECT) or wrong on a
        # specific step (PARTIAL_WRONG, with FIRST_ERROR pointer).
        working_analysis = getattr(self, '_pending_working_analysis', None)
        if working_analysis is not None and working_analysis.steps:
            from apps.tutoring.student_working_analyzer import (
                build_working_analysis_block,
            )
            system_prompt += "\n\n" + build_working_analysis_block(
                working_analysis,
            )

        # Mobile clients pass X-Client-Form-Factor: mobile so we can keep
        # responses short (small screens, typing on a phone). The view
        # sets self.client_form_factor before calling start/respond/resume.
        if getattr(self, 'client_form_factor', 'web') == 'mobile':
            system_prompt += (
                "\n\n<mobile_response_format>"
                "\nThe student is on a phone. Keep responses SHORT:"
                "\n- 1–3 short sentences per paragraph, max 2 paragraphs."
                "\n- One question at a time, never multi-part."
                "\n- Skip preamble like \"Great question!\" or restating what the student said."
                "\n- Use simple plain prose. Avoid headers, long bullet lists, and tables."
                "\n- ~80 words total per turn unless the student explicitly asks for more."
                "\n</mobile_response_format>"
            )

        # Per-turn structural reminder — math only. Repeated last so it
        # sits in the highest-salience position of the prompt every turn,
        # regardless of how long the conversation has grown. The prose
        # rules above dilute as context expands; this block is the
        # short, machine-checked line of defense the validator enforces.
        try:
            is_math_final = self.lesson.unit.course.is_math
        except Exception:
            is_math_final = False
        if is_math_final:
            system_prompt += (
                "\n\n<final_reminder>"
                "\nMATH RULE — to ask a numerical question this turn,"
                "\ncall a TOOL. You have two options:"
                "\n  1. pose_question(slot): pull from the canonical"
                "\n     lesson bank. Use this when a bank slot fits."
                "\n  2. pose_inline_question(question, answer_key, type):"
                "\n     AUTHOR your own question with an answer key the"
                "\n     grader will use. Use this for check / scaffolding"
                "\n     questions that aren't in the bank."
                "\n"
                "\nDo NOT type a numerical question in your text response"
                "\nwithout calling a tool. If you author a question in"
                "\nfree prose without supplying an answer_key, the grader"
                "\ncan't verify the student's response — the post-response"
                "\njudge will flag NO_AUTHORING and regen. Use"
                "\npose_inline_question if you want to author."
                "\nEmit tool calls as real tool_use blocks, never as text."
                "\n"
                "\nIf the bank has no question that fits (e.g. you want"
                "\na warmup recap from the previous lesson and no"
                "\n\"previous lesson recap\" slot is listed), do a"
                "\nCONCEPTUAL warmup instead — \"What rule did we learn"
                "\nlast time about angles around a point?\" — never"
                "\ninvent specific numerical values."
                "\n</final_reminder>"
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

        Per-course gate: when Course.tutoring_images_enabled is False,
        return an empty catalog and clear the id_map so the LLM has
        nothing to emit |||MEDIA:N||| against. This is the primary
        choke point for the "disable images for this course" feature
        — the system prompt sees no media block, so the model cannot
        reference figures.
        """
        # Per-course image gate
        try:
            if self.lesson and self.lesson.unit and self.lesson.unit.course:
                if not self.lesson.unit.course.tutoring_images_enabled:
                    self._media_id_map = {}
                    return ""
        except Exception:
            pass   # If the chain fails, fall through to default behaviour

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
        # NB: previously this loop withheld practice/quiz step
        # images until the student answered correctly, to prevent
        # answer-revealing figures leaking the answer. That guard
        # is now handled UPSTREAM at content-generation time —
        # figures attached to practice/quiz steps must depict the
        # question setup without showing the answer (see
        # content_generator.py figure-spec guidelines + image_service
        # prompt enhancer). Showing the figure during the question
        # is pedagogically useful for weaker learners.
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
                # Generated images store the descriptive text under
                # 'description' (the LLM emits {description, type} when
                # planning step media; image_service adds url + source
                # but doesn't populate alt/caption). Without this
                # fallback, every generated figure was being skipped
                # from the catalog ("steps_with_media=[]" in logs even
                # when the dashboard clearly showed them).
                description = img.get('description', '')
                label = alt or caption or description
                if not label:
                    continue
                media_items.append((label, {
                    'type': img.get('type', 'image'),
                    'url': url,
                    'alt': alt or description,
                    'caption': caption,
                    'description': description or alt or caption,
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
        # Observability: surface what the catalog has so we can debug
        # "no figure shown" cases without crawling the LLM prompt.
        try:
            steps_with_media = sorted(step_media_positions.keys())
            current_step_has_media = bool(
                step_media_positions.get(self.current_topic_index)
            )
            logger.info(
                "[MediaCatalog] lesson_id=%s items=%d steps_with_media=%s "
                "current_step=%d current_step_has_media=%s",
                self.lesson.id, len(media_items), steps_with_media,
                self.current_topic_index, current_step_has_media,
            )
        except Exception:
            pass
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
        catalog += "\nIf no item in the catalog fits what you want to show, omit"
        catalog += "\nthe signal entirely and teach with text."
        catalog += "\n</media_catalog>"
        # Cache the rendered catalog so the regen ensemble can reuse
        # it without rebuilding (the regen prompt needs the same
        # numbered list so the rewrite-LLM can emit |||MEDIA:N|||).
        self._last_media_catalog_text = catalog
        return catalog

    def _build_figure_facts_block(self) -> str:
        """Build the <figure_facts> block for the current step.

        For each figure attached to the current step that carries
        structured `figure_facts` metadata, render the scene
        description, labelled features, verified relationships, and
        anchor prompts. Followed by usage rules forbidding "imagine X"
        when the figure is visible. See memory/figure_facts_plan.md.

        Universal across subjects (was math-only). Geography maps,
        science diagrams, and history primary-source images all
        benefit from the same anchor-and-don't-imagine guidance.
        Empty string when there is no current step, no attached
        figure, or no figure_facts on the attached MediaAsset.
        """
        if self.current_topic_index >= len(self.steps):
            return ''
        current_step = self.steps[self.current_topic_index]

        # Collect MediaAssets referenced by the current step. A step's
        # `media` JSONField may reference assets by URL; we pull
        # MediaAsset rows whose file URL matches.
        from apps.media_library.models import MediaAsset

        urls = set()
        if current_step.media:
            for img in (current_step.media.get('images') or []):
                u = img.get('url')
                if u:
                    urls.add(u)
        if not urls:
            return ''

        # Match by file URL — Django's FileField.url is a string we can
        # compare. We only iterate institution-scoped assets to keep
        # the query bounded.
        institution = self.session.institution
        candidate_qs = MediaAsset.objects.filter(
            asset_type=MediaAsset.AssetType.IMAGE,
            figure_facts__isnull=False,
        )
        if institution is not None:
            candidate_qs = candidate_qs.filter(institution=institution)

        matched = []
        for asset in candidate_qs.iterator():
            try:
                if asset.file and asset.file.url in urls:
                    matched.append(asset)
            except Exception:
                continue

        if not matched:
            return ''

        parts: List[str] = []
        for asset in matched:
            facts = asset.figure_facts or {}
            if not isinstance(facts, dict):
                continue
            block = [f"\n<figure_facts source=\"{asset.title[:80]}\">"]
            gen_prompt = (facts.get('generation_prompt') or '').strip()
            if gen_prompt:
                block.append(f"  Original generation prompt: {gen_prompt[:280]}")
            scene = (facts.get('scene_description') or '').strip()
            if scene:
                block.append(f"  Scene: {scene}")
            features = facts.get('labelled_features') or []
            if features:
                block.append("  Labelled features:")
                for f in features[:20]:
                    label = f.get('label', '?')
                    location = f.get('location', '?')
                    color = f.get('color')
                    suffix = f" ({color})" if color else ""
                    block.append(f"    - \"{label}\" — {location}{suffix}")
            relationships = facts.get('angle_relationships') or []
            if relationships:
                block.append("  Verified relationships:")
                for r in relationships[:20]:
                    pair = r.get('pair') or [None, None]
                    rel = (r.get('relationship') or '').upper().replace('_', ' ')
                    if r.get('equal') is True:
                        block.append(
                            f"    - Angles {pair[0]} and {pair[1]} are {rel} (equal)"
                        )
                    elif r.get('sum') is not None:
                        block.append(
                            f"    - Angles {pair[0]} and {pair[1]} are {rel} "
                            f"(sum to {r.get('sum')}°)"
                        )
                    else:
                        block.append(
                            f"    - Angles {pair[0]} and {pair[1]} are {rel}"
                        )
            extra = facts.get('extra_facts') or []
            if extra:
                block.append("  Other facts:")
                for fact in extra[:10]:
                    block.append(f"    - {fact}")
            anchors = facts.get('anchor_prompts') or []
            if anchors:
                block.append("  Anchor prompts you may use VERBATIM to direct attention:")
                for a in anchors[:6]:
                    block.append(f"    - \"{a}\"")
            block.append("</figure_facts>")
            parts.append("\n".join(block))

        if not parts:
            return ''

        rules = (
            "\n\nRULES FOR USING FIGURES:"
            "\n1. PROMPT VISUALISATION, NOT IMAGINATION. The figure"
            " above is ALREADY VISIBLE to the student. Say \"look at"
            " the figure\" / \"find angle 5 on the diagram\" — NEVER"
            " \"imagine two parallel lines\" or \"picture this\". The"
            " student sees the figure; you must reference it as a"
            " present, visible object."
            "\n2. ANCHOR YOUR SCAFFOLDING. Reference labelled features"
            " by their actual labels and positions (\"the blue angle"
            " at the top-left of the upper intersection\"), not vague"
            " gestures (\"an angle up here\")."
            "\n3. VERIFY CLAIMS AGAINST <figure_facts>. When the"
            " student names a relationship (\"are 1 and 5"
            " corresponding?\"), consult the verified relationships"
            " list before answering. Do not interpret the geometry"
            " yourself — the data above is authoritative."
            "\n4. PREFER ANCHOR PROMPTS. The anchor prompts above are"
            " pre-verified scaffolds you can use verbatim — no"
            " authoring required."
            "\n5. HONEST UNCERTAINTY. If the student asks about"
            " something not in <figure_facts>, say so. Do not guess"
            " about visual features that aren't enumerated above."
        )
        return "\n" + "\n".join(parts) + rules + "\n"

    def _build_question_bank_block(self) -> str:
        """Build the <question_bank> block for math sessions.

        The tutor MUST NOT author its own arithmetic questions during
        ANY phase (engage, explain, worked_example, practice, quiz,
        summary). Every question with numerical values comes from
        this bank:
          - slot 0 — current step's teacher_script (canonical)
          - slots 1..N — exit-ticket bank candidates for the lesson
          - slots N+1..M — all OTHER lesson step teacher_scripts so
            warmup / review / engage phases can pose questions from
            steps not yet reached

        Without the "all lesson steps" pool, engage/warmup phases
        had nothing in the bank → LLM defaulted to authoring. Even
        when the LLM tried to copy verified numbers it paraphrased
        and used questions out of step order.

        See memory/tutor_no_authoring_plan.md.

        Side effect: populates self._question_id_map = {N: entry} for
        O(1) lookup when parsing |||QUESTION:N||| signals.
        """
        # Reset every turn so a stale map can't bleed across responses.
        self._question_id_map = {}

        # Universal across subjects (2026-05-05): the bank + tool was
        # math-only, but verified question grounding is just as
        # valuable for geography MCQ ("which is the largest island?"),
        # science fill-in-blank ("photosynthesis converts ___ into
        # ___"), etc. Now any subject's lesson with published
        # exit-ticket questions gets the bank + pose_question tool.
        if self.current_topic_index >= len(self.steps):
            return ''
        current_step = self.steps[self.current_topic_index]

        # Per-session pool — sample once at first need, cache the IDs
        # in engine_state so reloads reconstruct the same pool. Same
        # pattern the exit-ticket randomisation already uses.
        from apps.tutoring.question_bank import (
            pick_candidates_for_step,
            render_bank_block,
            sample_session_pool,
        )
        from apps.tutoring.models import ExitTicketQuestion

        pool_ids = (self.session.engine_state or {}).get('question_pool_ids')
        if pool_ids is None:
            # P5 — bias the per-session pool toward the student's weak
            # EOs (failed=5x, unattempted=3x, mastered=1x). See
            # memory/curriculum_tutor_v2_plan.md item 4.
            pool = sample_session_pool(
                self.lesson, seed=self.session.id, student=self.student,
            )
            pool_ids = [q.id for q in pool]
            # Audit log: surface lessons that ship with an empty
            # published bank early, so we don't have to mine chat
            # transcripts to find them. The runtime is graceful
            # (random fallback in pick_candidates_for_step), but the
            # content gap should be fixed at the source — this log is
            # the signal that triggers that work.
            if not pool:
                logger.warning(
                    "[BankAudit] bank_empty_for_lesson lesson_id=%s "
                    "title=%r — no published ExitTicketQuestion rows. "
                    "Tutor will run with no verified bank to ground on.",
                    self.lesson.id,
                    (self.lesson.title or '')[:80],
                )
            state = self.session.engine_state or {}
            state['question_pool_ids'] = pool_ids
            self.session.engine_state = state
            self.session.save(update_fields=['engine_state'])
        else:
            # Preserve the sampled order so the bank block is stable
            # turn-to-turn (otherwise Django's PK-ordered reload would
            # change the candidate ranking each request).
            qs = ExitTicketQuestion.objects.filter(id__in=pool_ids)
            by_id = {q.id: q for q in qs}
            pool = [by_id[i] for i in pool_ids if i in by_id]

        # Drop questions already posed in this session so the tutor
        # doesn't recycle the same question after the student answered
        # it. If the filter empties the pool, fall back to the full
        # pool — better to repeat than to ship an empty bank block.
        shown = getattr(self, 'shown_question_ids', None) or set()
        if shown:
            unshown = [q for q in pool if q.id not in shown]
            if unshown:
                pool = unshown

        # Match the bank candidates to the current step's enabling
        # objective (preferred), falling back to concept_tag, then to
        # a random sample of the lesson's pool. EO is the structured
        # curriculum primitive — exit-ticket questions and lesson
        # steps both carry it, and it's what drives the rest of the
        # competency / remediation system. concept_tag is kept as a
        # legacy fallback for older content.
        candidates = pick_candidates_for_step(
            pool,
            enabling_objective=getattr(current_step, 'enabling_objective', '') or '',
            concept_tag=current_step.concept_tag or '',
            difficulty_level=int(getattr(self, 'difficulty_level', 0) or 0),
        )

        # Slot 0 is the current step's teacher_script. For practice /
        # quiz / worked_example steps, that's a posable canonical
        # question. For TEACH / SUMMARY / engage steps, the
        # teacher_script is teaching content (delivered via the system
        # prompt's CONTENT TO TEACH block) — posing it via the tool
        # produces a duplicated intro, so we drop it from the bank.
        question_shaped_step_types = {'practice', 'quiz', 'worked_example'}
        include_step_slot = (
            (current_step.step_type or '') in question_shaped_step_types
        )

        # Engage / warmup turns: include questions from prerequisite
        # lessons' published exit-ticket banks so the LLM can pose a
        # verified previous-lesson question instead of authoring one.
        # Without this, the warmup directive ("a quick warm-up from
        # last week") had no source and the LLM was inventing numbers.
        prereq_questions = []
        is_engage_or_warmup = (
            (current_step.phase or '').lower() in ('engage', 'warmup')
            or self.current_topic_index == 0
        )
        if is_engage_or_warmup:
            prereq_questions = self._pull_prerequisite_recap_questions(
                max_per_lesson=3, max_total=6,
            )

        block, id_map = render_bank_block(
            current_step, candidates,
            include_step_slot=include_step_slot,
            prereq_questions=prereq_questions,
            is_engage_or_warmup=is_engage_or_warmup,
        )

        self._question_id_map = id_map
        logger.info(
            "[QuestionTool] build_bank: step=%d step_type=%s phase=%s "
            "eo='%s' tag='%s' slots=%s prereq_count=%d include_slot_0=%s",
            self.current_topic_index,
            current_step.step_type or '',
            current_step.phase or '',
            (getattr(current_step, 'enabling_objective', '') or '')[:60],
            current_step.concept_tag or '',
            sorted(id_map.keys()),
            len(prereq_questions),
            include_step_slot,
        )
        return block

    def _pull_prerequisite_recap_questions(
        self, *, max_per_lesson: int = 3, max_total: int = 6,
    ) -> List:
        """Pull a small sample of published exit-ticket questions from
        each prerequisite lesson, scoped to the current course.

        Used for engage / warmup turns so the LLM can pose a verified
        previous-lesson question via the pose_question tool instead of
        inventing one in prose ("Three angles around a point measure
        78°, 102°, and 115°. What is the fourth?" — that came from the
        previous lesson but the bank had no slot for it).

        Returns [] when:
          - the lesson has no prerequisites configured (legitimate —
            first lesson in a unit, foundational topics, intro lessons)
          - prerequisites exist but none of them have published bank
            questions yet (curriculum still being built)
          - the prerequisite-tracking models can't be imported
            (defensive — never crash the turn)
        Empty list flows naturally through render_bank_block, which
        emits an explicit "no recap available — use a conceptual hook"
        note for the LLM on engage/warmup turns.

        Bounded to per_lesson + total caps so the system prompt doesn't
        balloon. Returns ExitTicketQuestion instances ordered by
        prerequisite strength (essential first).
        """
        try:
            from apps.tutoring.skills_models import LessonPrerequisite
            from apps.tutoring.models import ExitTicketQuestion
        except Exception as e:
            logger.info(
                "[QuestionTool] prereq pull skipped — import failure: %s", e,
            )
            return []
        try:
            prereqs = (
                LessonPrerequisite.objects
                .filter(lesson=self.lesson)
                .select_related('prerequisite')
                .order_by('-strength', '-is_direct')
            )
        except Exception as e:
            logger.info(
                "[QuestionTool] prereq pull skipped — query failure: %s", e,
            )
            return []
        # Recap questions get rendered WITHOUT the original lesson's
        # diagram (we don't pull the previous lesson's media — the
        # auto-attach uses CURRENT-step media which would be the
        # wrong picture). Two-layer filter to keep recap questions
        # purely numeric / verbal:
        #   1. Restrict to question_type in {short_numeric, fill_in_blank} —
        #      these are the bank's text-only formats. MCQ + matching
        #      tend to reference "the diagram" or option visuals.
        #   2. Drop any remaining stem that references a figure /
        #      diagram (defense in depth).
        diagram_required_re = re.compile(
            r'\b(in the diagram|in the figure|see the diagram|look at|'
            r'shown below|the diagram|the figure|the image|the picture|'
            r'pictured|labelled|labeled)\b',
            re.IGNORECASE,
        )
        NUMERIC_RECAP_TYPES = ('short_numeric', 'fill_in_blank')

        out: List = []
        for lp in prereqs:
            prev = lp.prerequisite
            if not prev or not getattr(prev, 'is_published', False):
                continue
            # is_published is summative-only — see question_bank.py
            # sample_session_pool. Filter by assessment_type instead so
            # lesson-level recap banks aren't silently hidden.
            from apps.tutoring.models import ExitTicket
            qs = list(
                ExitTicketQuestion.objects.filter(
                    exit_ticket__lesson=prev,
                    exit_ticket__assessment_type=ExitTicket.AssessmentType.EXIT_TICKET,
                    question_type__in=NUMERIC_RECAP_TYPES,
                ).order_by('?')[: max_per_lesson * 3]  # over-fetch then filter
            )
            kept_for_lesson = 0
            for q in qs:
                stem = (getattr(q, 'question_text', '') or '')
                if diagram_required_re.search(stem):
                    continue
                out.append(q)
                kept_for_lesson += 1
                if kept_for_lesson >= max_per_lesson:
                    break
                if len(out) >= max_total:
                    break
            if len(out) >= max_total:
                break
        logger.info(
            "[QuestionTool] prereq_pull: lesson=%s prereq_count=%d → %d question(s)",
            self.lesson.id, prereqs.count(), len(out),
        )
        return out

    # =========================================================================
    # POSE-QUESTION TOOL (Anthropic tool use) — see memory/pose_question_tool_plan.md
    # =========================================================================

    POSE_QUESTION_TOOL_NAME = "pose_question"

    def _build_pose_question_tool(self) -> Optional[dict]:
        """Build the Anthropic tool definition for pose_question.

        The tool is the ONLY way for the LLM to ask a numerical question.
        Its only data parameters are `slot` (an integer index into the
        bank) and `lead_in` (an optional one-sentence framing). There is
        NO `question_text` parameter — the LLM cannot pass arbitrary text
        because the API will reject inputs that don't match the schema.

        Returns the tool dict, or None when there is no bank to pose
        from (e.g. non-math lesson, or empty id_map). When None the
        caller falls back to plain text generation.
        """
        id_map = getattr(self, '_question_id_map', {}) or {}
        if not id_map:
            logger.info(
                "[QuestionTool] build_tool: SKIP — empty id_map (non-math or empty bank)"
            )
            return None

        slot_keys = sorted(id_map.keys())
        max_slot = max(slot_keys)

        # Slot menu — short, factual. The LLM already has the full
        # <question_bank> block in the system prompt; this menu is
        # just a quick reference inside the tool description.
        menu_lines = []
        for slot in slot_keys:
            entry = id_map[slot]
            stem = ''
            if hasattr(entry, 'teacher_script'):
                stem = (getattr(entry, 'teacher_script', '') or '').strip()
            else:
                stem = (getattr(entry, 'question_text', '') or '').strip()
            menu_lines.append(f"  {slot}: {stem[:120]}")
        menu = "\n".join(menu_lines)

        tool = {
            "name": self.POSE_QUESTION_TOOL_NAME,
            "description": (
                "Pose a verified question from the bank to the student. "
                "Use this when you want to ask a question that has a "
                "known correct answer — multiple choice, fill-in-blank, "
                "short answer, numerical, etc. The slot index refers "
                "to the <question_bank> in the system prompt. Slot 0 "
                "is the current step's canonical question; slots 1+ "
                "are exit-ticket bank questions tagged to this step's "
                "concept; later slots labelled 'previous lesson recap' "
                "are for warmup turns.\n\n"
                "For math turns specifically, this tool is the ONLY "
                "legal way to ask a numerical question — never type "
                "such a question in your text response. For other "
                "subjects, prefer this tool for verified-answer "
                "questions, but free-prose conceptual questions "
                "(\"What do you think causes…?\") remain fine.\n\n"
                "IMPORTANT: emit a real tool_use block — do NOT type "
                "the call as text in your response. The student "
                "literally sees raw text characters.\n\n"
                "Available slots:\n" + menu
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "slot": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": max_slot,
                        "description": (
                            "Bank slot to pose. Must be one of: "
                            f"{slot_keys}"
                        ),
                    },
                    "lead_in": {
                        "type": "string",
                        "description": (
                            "Optional SHORT TRANSITION shown before the "
                            "bank question. This is a CONNECTOR, NOT a "
                            "question.\n"
                            "Hard constraints:\n"
                            "  - At most ONE short sentence (≤80 chars).\n"
                            "  - MUST NOT end with '?'.\n"
                            "  - MUST NOT contain any numerical setup "
                            "('48 km', 'three equal legs', 'x km', "
                            "'75 SCR per kg', etc.).\n"
                            "  - MUST NOT contain question prompts "
                            "('solve', 'find x', 'write the equation', "
                            "'what is the').\n"
                            "GOOD lead_ins: \"Try this:\", "
                            "\"Now apply that.\", \"Here's another:\", "
                            "\"Let's check your understanding.\".\n"
                            "BAD lead_ins: \"A boat travels 48 km in 3 "
                            "legs — write the equation.\" (that's a "
                            "question; put the question in the BANK slot "
                            "instead). \"Solve for x.\" (instruction → "
                            "let the bank question carry the verb).\n"
                            "If unsure, leave empty — the bank question "
                            "stands alone."
                        ),
                    },
                },
                "required": ["slot"],
            },
        }
        logger.info(
            "[QuestionTool] build_tool: slots=%s max_slot=%d",
            slot_keys, max_slot,
        )
        return tool

    # =========================================================================
    # POSE-INLINE-QUESTION TOOL (Anthropic tool use, 2026-05-16 A/B)
    # =========================================================================
    # The tutor sometimes wants to author its own check / scaffolding
    # question rather than pull from the bank ("Now let's try a simpler
    # version..." / "Quick check before we move on..."). The strip+regen
    # path tried to prevent this and produced incoherent turns. Per
    # pilot directive 2026-05-16, the user wants the tutor to be ALLOWED
    # to author — but ONLY if it provides an answer key so the grader
    # can still reliably verify the student's response.
    #
    # This tool requires (question, answer_key, type) and renders the
    # question inline in chat (text is in the chat bubble). The answer
    # key + working go to turn_metadata so the next student reply gets
    # graded via grade_written_responses_batch (same LLM grader the
    # exit ticket + bank use).

    POSE_INLINE_QUESTION_TOOL_NAME = "pose_inline_question"

    def _build_pose_inline_question_tool(self) -> Optional[dict]:
        """Build the Anthropic tool definition for pose_inline_question.

        Returns the tool dict, or None if tool offering is disabled
        (currently never None — this tool is always available since
        it doesn't depend on a bank). The LLM must supply an answer
        key alongside the question so the grader has ground truth.
        """
        return {
            "name": self.POSE_INLINE_QUESTION_TOOL_NAME,
            "description": (
                "Use this tool when you want to author your OWN check "
                "question — a quick comprehension probe, a simpler "
                "scaffolding step, or any question that isn't in the "
                "lesson's bank. You MUST supply the answer key so the "
                "system can reliably grade the student's response.\n\n"
                "When to PREFER this over pose_question:\n"
                "  - You want to ask a quick check before moving on "
                "(\"What's 360 - 90?\") that isn't in the bank.\n"
                "  - You're scaffolding a multi-step problem and need a "
                "sub-question.\n"
                "  - You're echoing/rephrasing for a struggling student.\n"
                "When to use pose_question INSTEAD:\n"
                "  - The lesson bank has a question that fits — pull from "
                "the bank for canonical curriculum coverage.\n"
                "  - Practice / exit-ticket items belong to the bank.\n\n"
                "The question text goes INTO the chat bubble (the student "
                "answers in the regular chat input). The answer_key is "
                "kept server-side and fed to the grader, never shown to "
                "the student."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "The full question text shown to the student "
                            "in the chat bubble. Include any setup + the "
                            "ask in one sentence or two. Examples: "
                            "'Two angles sum to 360°; one is 120°. What's "
                            "the other?' / 'In your own words, why do "
                            "angles around a point add to 360°?'"
                        ),
                    },
                    "answer_key": {
                        "type": "string",
                        "description": (
                            "The canonical correct answer. For numeric "
                            "questions give the value with unit "
                            "('240°' or '240'). For short-answer give "
                            "the key concept/keywords the student must "
                            "convey ('full rotation' / 'sum equals 360'). "
                            "Used as ground truth by the LLM grader."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "enum": ["short_numeric", "short_answer", "concept"],
                        "description": (
                            "short_numeric = expects a number ('240°', "
                            "'90'). short_answer = expects a 1-2 sentence "
                            "written response. concept = expects "
                            "explanation of a concept (grader is generous "
                            "on phrasing)."
                        ),
                    },
                    "working": {
                        "type": "string",
                        "description": (
                            "Optional step-by-step solution. Used by the "
                            "tutor's next turn to scaffold remediation if "
                            "the student gets it wrong. Not shown to the "
                            "student until needed."
                        ),
                    },
                    "alternatives": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of acceptable equivalent "
                            "answers ('two hundred forty', '240 degrees'). "
                            "The grader is already generous on phrasing; "
                            "leave empty unless there are multiple "
                            "genuinely-different correct answers."
                        ),
                    },
                },
                "required": ["question", "answer_key", "type"],
            },
        }

    def _record_inline_authored_question(
        self,
        turn_metadata: Dict,
        question: str,
        answer_key: str,
        question_type: str,
        working: str = '',
        alternatives: Optional[List[str]] = None,
    ) -> None:
        """Stash the tutor's authored question + answer key on the
        upcoming turn so the next student reply gets graded.

        Mirrors `_record_bank_question_on_turn` but for the
        inline-authored path. Sets engine_state.awaiting_answer with
        kind='inline_authored' so `_grade_against_last_bank_question`
        knows to pull the answer key from turn_metadata instead of
        the bank tables.
        """
        turn_index = len(getattr(self, 'conversation', []))
        from django.utils import timezone as _tz
        posed_at_iso = _tz.now().isoformat()

        turn_metadata['inline_authored_question'] = {
            'question': (question or '')[:1000],
            'answer_key': (answer_key or '')[:300],
            'question_type': question_type or 'short_answer',
            'working': (working or '')[:1000],
            'alternatives': list(alternatives or [])[:8],
            'turn_index': turn_index,
            'posed_at': posed_at_iso,
        }
        # Mirror bank-question-ref shape so downstream code (artifact
        # panel resume, grade dispatch) can branch on `kind`.
        turn_metadata['bank_question_ref'] = {
            'kind': 'inline_authored',
            'id': None,  # no DB row — the question lives only on this turn
            'question_type': question_type or 'short_answer',
        }
        # Engine-state awaiting_answer for the grader to find.
        record = {
            'kind': 'inline_authored',
            'question_id': None,
            'question_type': question_type or 'short_answer',
            'turn_index': int(turn_index),
            'posed_at': posed_at_iso,
        }
        if not hasattr(self, '_turn_questions') or self._turn_questions is None:
            self._turn_questions = {}
        self._turn_questions[str(turn_index)] = record
        self._awaiting_answer = record

    # NOTE: text-block defense-strip REMOVED (2026-05-04). Editing the
    # tutor's text after generation produced robotic-feeling output
    # ("Quick check:" with no question following) when the heuristic
    # caught a benign question. Per user direction: never edit the
    # tutor message in place. If the LLM authors a numerical question
    # in a text block instead of calling pose_question, the combined
    # judge flags NO_AUTHORING and the V3 regen path handles it —
    # one clean retry, then ship the regen output as-is. No surgery.

    # Defensive strip for leaked tool-call syntax in text blocks.
    # Some turns the LLM types the literal `pose_question(slot=N)`
    # syntax instead of emitting a real tool_use block — the student
    # then sees those characters as raw text. We strip them.
    # Conservative: only matches the exact pose_question(slot=...)
    # shape, not other text containing parens or "pose_question".
    _LEAKED_TOOL_CALL_RE = re.compile(
        r"\bpose_question\s*\(\s*slot\s*=\s*\d+\s*(?:,\s*lead_in\s*=\s*[\"'][^\"']*[\"']\s*)?\)",
        re.IGNORECASE,
    )

    # NOTE: a `_VISUAL_REFERENCE_RE` lived here briefly to gate an
    # auto-attach fallback when the LLM mentioned a figure without
    # emitting |||MEDIA:N|||. That fallback was removed 2026-05-06:
    # we now trust the explicit signal exclusively. If you need to
    # re-add inference, do it as a defense-in-depth, not a primary path.

    def _strip_leaked_tool_call_syntax(self, text: str) -> Tuple[str, int]:
        """Remove `pose_question(slot=N)` shaped text from a text block.
        Returns (cleaned, chars_removed)."""
        if not text or 'pose_question' not in text.lower():
            return text, 0
        cleaned = self._LEAKED_TOOL_CALL_RE.sub('', text)
        # Tidy stray whitespace + dangling punctuation left behind.
        cleaned = re.sub(r' {2,}', ' ', cleaned)
        cleaned = re.sub(r'\s+([.!?])', r'\1', cleaned)
        cleaned = cleaned.strip()
        return cleaned, len(text) - len(cleaned)

    def _handle_pose_question_message(
        self,
        message,
        turn_metadata: Dict,
    ) -> str:
        """Process an Anthropic Message returned by generate_with_tools.

        Walks content blocks in order:
          - text block → append text to the response
          - tool_use block (pose_question) → resolve slot in id_map,
            render the bank entry verbatim, append to the response,
            and record bank_question_ref on turn_metadata so the next
            student reply gets graded deterministically.
          - any other tool_use → ignored (the tool is the only one
            we expose).

        Returns the final response string the student will see.

        Heavy logging at every branch — by the time we hit production,
        the logs should make it obvious which step in this pipeline
        failed if the response is wrong.
        """
        from apps.tutoring.question_bank import render_question_to_prose

        id_map = getattr(self, '_question_id_map', {}) or {}
        text_parts: List[str] = []
        tool_use_count = 0
        bank_rendered = False

        # PRE-SCAN: extract the bank-rendered text from any pose_question
        # tool_use so the text-block strip (below) can drop sentences
        # that overlap with it. Without this 2-pass, text blocks are
        # processed BEFORE the tool block in the loop order, and we'd
        # have no bank text to compare against. The e2e pilot
        # 2026-05-16 showed the LLM repeatedly emitting the bank
        # question stem inside the text block AND then calling
        # pose_question with the same slot — two copies on screen.
        # Stash on engine so the regen path (which runs later, in
        # _respond_impl) can apply the same strip to regen output.
        bank_rendered_text_for_strip = ''
        for _blk in (message.content or []):
            if (
                getattr(_blk, 'type', None) == 'tool_use'
                and getattr(_blk, 'name', '') == self.POSE_QUESTION_TOOL_NAME
            ):
                _ti = getattr(_blk, 'input', {}) or {}
                _slot = _ti.get('slot') if isinstance(_ti.get('slot'), int) else 0
                _entry = id_map.get(_slot) or id_map.get(0)
                if _entry is not None:
                    try:
                        bank_rendered_text_for_strip = (
                            render_question_to_prose(_entry) or ''
                        )
                    except Exception as _exc:
                        logger.warning(
                            "[QuestionTool] pre-scan render failed: %s",
                            _exc,
                        )
                break  # only one pose_question per turn
        # Stash on engine for the regen finalize path
        self._last_bank_rendered_text = bank_rendered_text_for_strip

        for block in (message.content or []):
            btype = getattr(block, 'type', None)
            if btype == 'text':
                # Pass text blocks through verbatim — no in-place
                # editing. If the LLM authors a numerical question in
                # prose, the combined judge flags NO_AUTHORING and the
                # V3 regen path takes one clean retry. Editing in place
                # produced robotic output and is no longer used.
                #
                # ONE exception: leaked tool-call syntax. Sometimes
                # the LLM types `pose_question(slot=N)` as literal
                # text instead of emitting a real tool_use block. The
                # student sees those characters and is confused. This
                # is clearly garbage output (not teaching content) so
                # we strip it. Coupled with the prompt update telling
                # the LLM to "emit it as a real tool_use call" not
                # type the syntax.
                raw_text = (getattr(block, 'text', '') or '')
                cleaned, leaked = self._strip_leaked_tool_call_syntax(raw_text)
                if leaked:
                    logger.warning(
                        "[QuestionTool] LEAKED_TOOL_SYNTAX in text block — "
                        "stripped %d chars. The LLM typed the call instead "
                        "of emitting a tool_use block.",
                        leaked,
                    )
                # STRIP REMOVED 2026-05-16 per pilot directive: the
                # text-block strip was producing incoherent tutor turns
                # (cut a sentence mid-flow, left the surrounding
                # narrative awkward). The grader is now robust enough
                # to handle a tutor-authored question without an answer
                # key — see grade_chat_authored fallback in
                # _grade_against_last_bank_question — so we don't need
                # to surgically remove authored questions from the
                # chat. If the validator detects an authoring violation
                # it triggers regen via the existing path; that's
                # cheaper coherence-wise than mid-sentence editing.
                if cleaned.strip():
                    text_parts.append(cleaned.strip())
            elif btype == 'tool_use':
                tool_use_count += 1
                tool_name = getattr(block, 'name', '')
                # NEW (2026-05-16): pose_inline_question branch.
                # Tutor authored its own question + supplied answer key.
                # Render the question inline in chat (append the question
                # text to text_parts) and stash the answer key on turn
                # metadata so the grader can verify.
                if tool_name == self.POSE_INLINE_QUESTION_TOOL_NAME:
                    inline_input = getattr(block, 'input', {}) or {}
                    q_text = (inline_input.get('question') or '').strip()
                    a_key = (inline_input.get('answer_key') or '').strip()
                    q_type = (
                        inline_input.get('type') or 'short_answer'
                    ).strip()
                    working = (inline_input.get('working') or '').strip()
                    alternatives = inline_input.get('alternatives') or []
                    if not q_text or not a_key:
                        logger.warning(
                            "[QuestionTool] pose_inline_question: "
                            "MISSING_FIELDS question=%r answer_key=%r — "
                            "skipping render",
                            bool(q_text), bool(a_key),
                        )
                        continue
                    logger.info(
                        "[QuestionTool] inline_authored: type=%s q=%r "
                        "key=%r",
                        q_type, q_text[:80], a_key[:40],
                    )
                    self._record_inline_authored_question(
                        turn_metadata,
                        question=q_text,
                        answer_key=a_key,
                        question_type=q_type,
                        working=working,
                        alternatives=alternatives,
                    )
                    text_parts.append(q_text)
                    self._bank_signal_used_this_turn = True  # suppresses authoring gate
                    bank_rendered = True  # for TurnSummary metrics
                    continue
                if tool_name != self.POSE_QUESTION_TOOL_NAME:
                    logger.warning(
                        "[QuestionTool] tool_call: UNEXPECTED tool_name='%s' — ignoring",
                        tool_name,
                    )
                    continue
                # Stay-on-question-during-remediation gate (2026-05-16):
                # when the student's most recent answer was WRONG, the
                # tutor must not advance the artifact to a new bank
                # question — the existing one stays in flight so the
                # student can retry.
                # Only applies when the PREVIOUS question had an
                # artifact representation (bank kinds:
                # lesson_step / exit_ticket_question). For
                # inline_authored questions there's no artifact to
                # keep alive, and blocking leaves the student with
                # an empty response (pilot 2026-05-16: session 32
                # turn 12 stranded after a conceptual question with
                # a bad numeric answer_key was marked wrong).
                pending_grade = getattr(self, '_pending_bank_grade', None)
                _awaiting = getattr(self, '_awaiting_answer', None) or {}
                _prev_kind = _awaiting.get('kind') if isinstance(_awaiting, dict) else None
                _prev_was_bank = _prev_kind in ('lesson_step', 'exit_ticket_question')
                if (
                    pending_grade is not None
                    and getattr(pending_grade, 'is_correct', None) is False
                    and _prev_was_bank
                ):
                    logger.warning(
                        "[QuestionTool] BLOCKED_ROTATE_ON_WRONG slot=%r — "
                        "last verdict was wrong; keeping existing artifact "
                        "question in flight for retry instead of posing a "
                        "new one.",
                        (getattr(block, 'input', {}) or {}).get('slot'),
                    )
                    turn_metadata.setdefault(
                        'blocked_rotates_on_wrong', 0,
                    )
                    turn_metadata['blocked_rotates_on_wrong'] += 1
                    continue
                tool_input = getattr(block, 'input', {}) or {}
                slot = tool_input.get('slot')
                lead_in = (tool_input.get('lead_in') or '').strip()
                # Server-side defense: the tool schema tells the LLM
                # "lead_in is a transition, not a question" but in
                # practice the LLM still drops an authored question
                # in here (production session 252, 2026-05-12 — boat
                # word problem in lead_in + MCQ from bank → two
                # questions to the student). Detect + drop the bad
                # lead_in so the rendered turn carries only the bank
                # question.
                if lead_in and _looks_like_authored_question(lead_in):
                    logger.warning(
                        "[QuestionTool] lead_in: DROPPED (looks like authored "
                        "question) chars=%d preview=%r",
                        len(lead_in), lead_in[:120],
                    )
                    turn_metadata.setdefault('dropped_lead_ins', []).append(
                        lead_in[:200],
                    )
                    lead_in = ''
                logger.info(
                    "[QuestionTool] tool_call: slot=%s lead_in=%r",
                    slot, lead_in[:80],
                )
                if not isinstance(slot, int):
                    logger.error(
                        "[QuestionTool] tool_call: NON_INT slot=%r — falling back to slot 0",
                        slot,
                    )
                    slot = 0
                if slot not in id_map:
                    logger.error(
                        "[QuestionTool] resolve_slot: slot=%d NOT_IN_ID_MAP keys=%s — falling back to slot 0",
                        slot, sorted(id_map.keys()),
                    )
                    slot = 0
                entry = id_map.get(slot)
                if entry is None:
                    logger.error(
                        "[QuestionTool] resolve_slot: id_map[%d] is None — skipping render",
                        slot,
                    )
                    continue
                kind = 'lesson_step' if hasattr(entry, 'teacher_script') else 'exit_ticket_question'
                logger.info(
                    "[QuestionTool] resolve_slot: slot=%d → %s(id=%s)",
                    slot, kind, getattr(entry, 'id', '?'),
                )
                rendered = render_question_to_prose(entry)
                if not rendered:
                    logger.error(
                        "[QuestionTool] render: EMPTY for slot=%d", slot,
                    )
                    continue
                logger.info(
                    "[QuestionTool] render: chars=%d slot=%d", len(rendered), slot,
                )
                # AUTHORED-vs-BANK CONFLICT GUARD (2026-05-16). If the
                # tutor's text block already authored a question
                # (esp. an MCQ with A/B/C/D options), rendering an
                # UNRELATED bank Q here would put two questions on
                # screen — the student answers the authored one, but
                # awaiting_answer points at the bank Q so the grader
                # judges against the wrong question (false negative,
                # student stranded). Pilot 2026-05-16 lesson 538
                # session 39 turn 9: text authored sea-turtle MCQ
                # while pose_question pulled Q3711 ('Compose a
                # definition of geography'). Fix: when conflict
                # detected, SKIP the bank render — let the authored
                # question stay in chat and route the next student
                # input through the chat-authored grounded grader
                # (no awaiting_answer set means
                # _grade_against_last_bank_question takes the
                # tail-question fallback path). Must run BEFORE
                # _record_bank_question_on_turn so awaiting_answer
                # isn't written.
                existing_text = "\n\n".join(text_parts)
                if _text_block_has_authored_question(existing_text):
                    logger.warning(
                        "[QuestionTool] AUTHORED_VS_BANK_CONFLICT: text "
                        "block already contains an authored question — "
                        "SKIPPING bank render (slot=%d, %s id=%s) so "
                        "awaiting_answer doesn't mis-point. Student's "
                        "next reply will route through the chat-authored "
                        "grader.",
                        slot, kind, getattr(entry, 'id', '?'),
                    )
                    turn_metadata.setdefault(
                        'authored_vs_bank_conflicts', 0,
                    )
                    turn_metadata['authored_vs_bank_conflicts'] += 1
                    continue
                self._record_bank_question_on_turn(turn_metadata, entry)
                # Render the bank question text inline in the chat AND
                # to the artifact panel (pilot directive 2026-05-16):
                # showing it in chat makes the question readable by TTS
                # (audio mode) and gives the conversation history a
                # complete reading transcript. The artifact still
                # renders the interactive UI controls (radios for MCQ,
                # blanks for FIB, etc) so the student can ANSWER there.
                if lead_in:
                    text_parts.append(lead_in)
                text_parts.append(rendered)
                bank_rendered = True
                # Track for the validator's authoring gate so it knows
                # a verified bank pull happened on this turn.
                self._bank_signal_used_this_turn = True
            else:
                logger.info(
                    "[QuestionTool] block: ignoring type=%r", btype,
                )

        final = "\n\n".join(text_parts).strip()
        # Surface tool-use rate to the structured per-turn log
        # ([TurnSummary] in _save_turn). Sonnet's 3% compliance was
        # only visible after scraping these lines individually before.
        turn_metadata['tool_use_count'] = tool_use_count
        turn_metadata['bank_rendered'] = bank_rendered
        logger.info(
            "[QuestionTool] final: chars=%d tool_use_count=%d bank_rendered=%s "
            "stop_reason=%s",
            len(final), tool_use_count, bank_rendered,
            getattr(message, 'stop_reason', '?'),
        )
        return final

    # NOTE: _force_inject_bank_question REMOVED (2026-05-04). The
    # tool-use path makes it impossible for the LLM to author a
    # numerical question, so post-hoc truncate-and-replace surgery
    # is no longer needed. The helper produced half-baked tutor
    # responses ("Quick check before we apply it:" with no question
    # following) when the LLM's question shape didn't match the
    # truncation heuristic.

    def _parse_question_signal(
        self, text: str,
    ) -> Tuple[str, Optional[object]]:
        """Parse |||QUESTION:N||| from the LLM response.

        Returns (clean_text, chosen_entry_or_None). chosen_entry is the
        LessonStep (slot 0) or ExitTicketQuestion (slots 1..N) the LLM
        picked. Caller renders it verbatim and appends to the response.
        """
        from apps.tutoring.question_bank import parse_question_signal
        clean_text, n = parse_question_signal(text)
        if n is None:
            return clean_text, None
        id_map = getattr(self, '_question_id_map', {}) or {}
        entry = id_map.get(n)
        if entry is None:
            logger.warning(
                "[QuestionBank] LLM emitted |||QUESTION:%d||| "
                "but no entry exists at that slot (id_map keys: %s)",
                n, sorted(id_map.keys()),
            )
        return clean_text, entry

    def _parse_question_eo_signal(
        self, text: str,
    ) -> Tuple[str, Optional[object]]:
        """Parse |||QUESTION_EO:N||| — EO-targeted bank pull.

        N is 1-based into ``self.enabling_objectives`` (the same order
        the tutor sees in the [ENABLING OBJECTIVES] block). Resolves
        to a published ExitTicketQuestion tagged with that EO.
        """
        from apps.tutoring.question_bank import (
            parse_question_eo_signal, pick_question_for_eo,
        )
        clean_text, n = parse_question_eo_signal(text)
        if n is None:
            return clean_text, None
        eos = self.enabling_objectives or []
        if not (1 <= n <= len(eos)):
            logger.warning(
                "[QuestionBank] LLM emitted |||QUESTION_EO:%d||| "
                "but lesson only has %d EO(s)",
                n, len(eos),
            )
            return clean_text, None
        eo_text = (eos[n - 1].get('objective') or '').strip()
        if not eo_text:
            return clean_text, None
        # Exclude any bank question we've already rendered this session
        # so the tutor doesn't re-pose the same item back-to-back.
        already = (self.session.engine_state or {}).get('rendered_bank_ids', [])
        question = pick_question_for_eo(
            self.lesson, eo_text, exclude_ids=already,
        )
        if question is None:
            logger.info(
                "[QuestionBank] no published bank question for EO %r",
                eo_text[:80],
            )
            return clean_text, None
        # Track for future exclusion
        state = self.session.engine_state or {}
        rendered = list(state.get('rendered_bank_ids') or [])
        rendered.append(question.id)
        state['rendered_bank_ids'] = rendered[-30:]  # cap so JSON stays small
        self.session.engine_state = state
        return clean_text, question

    def _current_bank_stems(self) -> List[str]:
        """Flatten the active question_id_map to a list of stem strings.

        Used by the rule-compliance validator (P5) so the judge knows
        which question stems are bank-approved (anything else from the
        tutor is an authoring violation).
        """
        id_map = getattr(self, '_question_id_map', {}) or {}
        stems: List[str] = []
        for entry in id_map.values():
            ts = getattr(entry, 'teacher_script', None)
            if ts:
                stems.append(ts.strip())
                continue
            qt = getattr(entry, 'question_text', '') or ''
            if qt:
                stems.append(qt.strip())
        return stems

    def _current_bank_full_render(self) -> List[str]:
        """Like _current_bank_stems but includes the rendered options
        for MCQs / blanks for FIB / pairs for matching — i.e. the full
        student-facing text the bank entry would produce.

        Use this for the regen ensemble: when the rewrite LLM picks a
        bank question to pose, it needs the OPTIONS too. Bare stems
        cause the regen to emit the question without A/B/C/D and
        strands the student (pilot 2026-05-16, lesson 538 session 38 —
        bank Q 3724 'How do tourist resorts modify the environment?'
        rendered in chat as a bare stem after regen, options dropped).
        """
        from apps.tutoring.question_bank import render_question_to_prose
        id_map = getattr(self, '_question_id_map', {}) or {}
        out: List[str] = []
        for entry in id_map.values():
            try:
                full = render_question_to_prose(entry)
            except Exception as exc:
                logger.warning(
                    "[BankFullRender] render failed for entry=%r: %s",
                    getattr(entry, 'id', '?'), exc,
                )
                continue
            if full and full.strip():
                out.append(full.strip())
        return out

    def _record_bank_question_on_turn(self, turn_metadata: Dict, entry) -> None:
        """When a tutor turn renders a bank question, write the entry's
        identity onto turn_metadata so the NEXT respond() call can
        grade the student's reply against the bank's stored answer.

        Uses {kind, id, question_type} so the grader doesn't need the
        Pydantic id_map (which only lives in memory for the current turn).

        R2 (2026-05-15): also writes engine_state.awaiting_answer +
        engine_state.turn_questions so:
          - The artifact panel (R3) can render the question by id
            without depending on inline prose.
          - The resume system (R5) knows what to re-pose when the
            student returns mid-question.
          - The active_bank_question system-prompt context block
            (R2.2 below) can scaffold without the tutor re-authoring.
        """
        if entry is None:
            return

        # Compute the upcoming tutor turn index (matches the
        # _turn_media[turn_index] convention: conversation hasn't been
        # appended yet at this point; the upcoming index = len + 1
        # because the student turn was appended before respond() was
        # called but the tutor turn isn't appended yet).
        turn_index = len(getattr(self, 'conversation', []))
        from django.utils import timezone as _tz
        posed_at_iso = _tz.now().isoformat()

        # LessonStep — slot 0 in the bank block. Graded against
        # LessonStep.expected_answer via the existing
        # _deterministic_math_check; we still record it for forensics.
        if hasattr(entry, 'teacher_script'):
            turn_metadata['bank_question_ref'] = {
                'kind': 'lesson_step',
                'id': entry.id,
                'question_type': 'short_numeric',
            }
            self._set_awaiting_answer(
                kind='lesson_step',
                question_id=entry.id,
                question_type='short_numeric',
                turn_index=turn_index,
                posed_at_iso=posed_at_iso,
            )
            return

        # ExitTicketQuestion — bank slots 1..N. Mark as shown so the
        # session-pool picker excludes it on subsequent turns; this
        # stops the tutor recycling the same question after a correct
        # answer (the canonical-step slot 0 above is intentionally
        # NOT tracked — it's the step's own question).
        if not hasattr(self, 'shown_question_ids'):
            self.shown_question_ids = set()
        try:
            self.shown_question_ids.add(int(entry.id))
        except (TypeError, ValueError, AttributeError):
            pass

        qtype = getattr(entry, 'question_type', 'mcq') or 'mcq'
        turn_metadata['bank_question_ref'] = {
            'kind': 'exit_ticket_question',
            'id': entry.id,
            'question_type': qtype,
        }
        self._set_awaiting_answer(
            kind='exit_ticket_question',
            question_id=entry.id,
            question_type=qtype,
            turn_index=turn_index,
            posed_at_iso=posed_at_iso,
        )

    def _set_awaiting_answer(
        self,
        *,
        kind: str,
        question_id: int,
        question_type: str,
        turn_index: int,
        posed_at_iso: str,
    ) -> None:
        """Persist 'we're awaiting an answer to question X' state for
        the artifact panel + resume system. Reads-after-writes go via
        the engine instance vars; full state lands in engine_state on
        the next _save_state.
        """
        record = {
            'kind': kind,
            'question_id': int(question_id),
            'question_type': question_type,
            'turn_index': int(turn_index),
            'posed_at': posed_at_iso,
        }
        if not hasattr(self, '_turn_questions') or self._turn_questions is None:
            self._turn_questions = {}
        self._turn_questions[str(turn_index)] = record
        self._awaiting_answer = record

    def _clear_awaiting_answer(self) -> None:
        """Drop the awaiting_answer flag — call when the student has
        submitted an answer (via the artifact UI in R4, or via a
        free-text reply that the grader processes).
        """
        self._awaiting_answer = None

    def _build_pending_question_payload(self) -> Optional[Dict]:
        """Render the active awaiting_answer record into the dict
        shape the frontend artifact panel (R3) consumes.

        DISABLED 2026-05-16 per pilot directive: "I think the artifact
        question layer is bringing a layer of confusion and chaos. Let
        us remove it for now." Returning None unconditionally so the
        frontend never renders the artifact question card. The bank
        question text already appears in the chat narrative (via
        pose_question handler), and the student answers in the chat
        textbox. `_grade_against_last_bank_question` runs against the
        most-recent bank_question_ref on the previous tutor turn, so
        grading still works without the artifact UI.

        The code below is kept (commented out indirectly via the early
        return) so re-enabling the artifact panel is a one-line revert
        when we decide to bring it back.
        """
        return None
        rec = getattr(self, '_awaiting_answer', None)
        if not rec or not rec.get('question_id'):
            return None
        kind = rec.get('kind')
        question_id = rec['question_id']

        try:
            if kind == 'lesson_step':
                from apps.curriculum.models import LessonStep
                from apps.tutoring.question_bank import render_question_to_prose
                step = LessonStep.objects.filter(id=question_id).first()
                if step is None:
                    return None
                # Use the canonical renderer so the artifact stem
                # carries the full setup+ask (e.g. "Look at the
                # diagram... four angles meet at a central point...
                # Find y") — not just one field. Matches what the
                # tool-render path produces.
                return {
                    'kind': 'lesson_step',
                    'question_id': step.id,
                    'question_type': step.answer_type or 'short_numeric',
                    'turn_index': rec.get('turn_index'),
                    'posed_at': rec.get('posed_at'),
                    'stem': render_question_to_prose(step),
                    'expected_answer': step.expected_answer or '',
                    'choices': step.choices or [],
                }
            if kind == 'exit_ticket_question':
                from apps.tutoring.models import ExitTicketQuestion
                q = ExitTicketQuestion.objects.filter(id=question_id).first()
                if q is None:
                    return None
                payload = {
                    'kind': 'exit_ticket_question',
                    'question_id': q.id,
                    'question_type': q.question_type or 'mcq',
                    'turn_index': rec.get('turn_index'),
                    'posed_at': rec.get('posed_at'),
                    'stem': q.question_text or '',
                    'explanation': q.explanation or '',
                }
                if q.question_type == 'mcq':
                    payload['options'] = {
                        'A': q.option_a or '',
                        'B': q.option_b or '',
                        'C': q.option_c or '',
                        'D': q.option_d or '',
                    }
                    payload['correct_answer'] = q.correct_answer or ''
                else:
                    # Non-MCQ types (fill_in_blank, matching, short_answer,
                    # data_interpretation) carry their own answer_data
                    # shape — surface verbatim for the frontend to dispatch.
                    payload['answer_data'] = q.answer_data or {}
                return payload
        except Exception as exc:
            logger.warning(
                f"[PendingQuestion] resolve failed for {kind}#{question_id}: "
                f"{type(exc).__name__}: {exc}"
            )
        return None

    def _grade_against_last_bank_question(self, student_input: str):
        """If the most recent tutor turn rendered a bank question, run
        the deterministic grader (apps.tutoring.bank_grader) on the
        student's reply. Sets self._pending_bank_grade for the
        upcoming LLM call's evaluation_signal block.

        Returns the BankGradeResult, or None when no bank question was
        in flight or grading was skipped.
        """
        from apps.tutoring.models import SessionTurn
        last_tutor_turn = (
            SessionTurn.objects
            .filter(session=self.session, role='tutor')
            .order_by('-created_at')
            .first()
        )
        if last_tutor_turn is None:
            return None
        ref = (last_tutor_turn.metadata or {}).get('bank_question_ref') or {}
        kind = ref.get('kind')
        ref_id = ref.get('id')

        # 2026-05-17 (task #171) — bank link persistence across hint
        # turns. The previous logic only looked at the most-recent
        # tutor turn's bank_question_ref. But pose_question only fires
        # on the turn that posts the question; subsequent hint turns
        # (after wrong#1, wrong#2, regen-rewrites...) carry no
        # bank_question_ref, so the grader fell through to the
        # chat-authored path and LLM-guessed the single-letter reply.
        # Lesson 540 session 47 turn 828: 'D' graded True against a
        # regen-rewritten stem despite correct=B.
        #
        # Fix: when the bank question is still active (self._awaiting_answer
        # set with a bank-Q kind), the BANK grader is authoritative —
        # regardless of which tutor turn rendered it. The last-turn
        # metadata is the cheap path; self._awaiting_answer is the
        # cross-turn anchor.
        aa = getattr(self, '_awaiting_answer', None) or {}
        aa_kind = aa.get('kind')
        aa_qid = aa.get('question_id')
        if (
            not kind  # no bank_question_ref on the last turn
            and aa_kind in ('lesson_step', 'exit_ticket_question')
            and aa_qid
        ):
            kind = aa_kind
            ref_id = aa_qid
            logger.info(
                "[BankGrade] reusing _awaiting_answer link (kind=%s id=%s) "
                "— last tutor turn had no bank_question_ref (hint/regen turn)",
                kind, ref_id,
            )
        # Fallback path 2026-05-16: no bank_question_ref but the previous
        # tutor turn ended in a question — likely a tutor-authored
        # question without an answer key. Use the no-key LLM grader so
        # the student isn't stranded. This is what makes removing the
        # strip safe: we don't need to enforce "no authoring" because
        # we can grade the authored question anyway.
        #
        # 2026-05-17 (task #173) — also fire when the previous tutor
        # turn rendered an inline MCQ pattern (A) ... B) ... C) ... D))
        # even without a ?-terminator. Lesson 540 session 48: tutor
        # authored "Which feature explains symbols? A) Scale B) Legend
        # C) Title D) Grid" inline, ending in "D) Grid" (no ?). Fallback
        # didn't fire, no wrong_attempts tracking, tutor revealed on
        # attempt #2. Detect the MCQ pattern + grade the whole stem+
        # options block so the LLM grader has options context for bare
        # letter replies.
        if not kind:
            tutor_content = (last_tutor_turn.content or '').strip()
            mcq_match = _INLINE_MCQ_RE.search(tutor_content) if tutor_content else None
            has_q = bool(tutor_content) and (
                tutor_content.endswith('?') or mcq_match is not None
            )
            if has_q:
                if mcq_match is not None:
                    # Extract the MCQ block — find the last sentence
                    # before the options that ends with '?', then take
                    # everything from there to the end of the message.
                    mcq_start = mcq_match.start()
                    preamble = tutor_content[:mcq_start].rstrip()
                    pre_sentences = re.split(r'(?<=[.!?])\s+', preamble)
                    stem = next(
                        (s for s in reversed(pre_sentences) if s.strip().endswith('?')),
                        preamble.split('\n')[-1] if preamble else '',
                    ).strip()
                    options_block = tutor_content[mcq_start:].strip()
                    question_text = (
                        f"{stem}\n\n{options_block}"
                        if stem else options_block
                    )
                else:
                    # Plain ?-terminated text — old behavior.
                    sentences = re.split(r'(?<=[.!?])\s+', tutor_content)
                    question_text = next(
                        (s for s in reversed(sentences) if s.strip().endswith('?')),
                        tutor_content,
                    ).strip()
                from apps.tutoring.bank_grader import (
                    grade_chat_authored_question,
                )
                is_math = (
                    self.lesson.unit.course.is_math
                    if self.lesson.unit and self.lesson.unit.course
                    else False
                )
                # Pull relevant KB chunks so the grader has the
                # curriculum context (not just LLM parametric
                # knowledge). Pilot 2026-05-16: niche / local facts
                # (Seychelles geography, etc.) need curriculum
                # grounding to be judged correctly. The grader also
                # uses Google search grounding on top via the
                # two-call pattern.
                kb_ctx = ''
                try:
                    kb_ctx = self._get_knowledge_context(student_input) or ''
                except Exception as _exc:
                    logger.debug(
                        f"[ChatAuthored] KB context fetch failed: {_exc}"
                    )
                result = grade_chat_authored_question(
                    question_text=question_text,
                    student_response=student_input,
                    llm_client=self.judge_client,
                    is_math=is_math,
                    kb_context=kb_ctx,
                    use_grounding=True,
                )
                self._pending_bank_grade = result
                self._pending_bank_question = None
                # Track wrong_attempts on the awaiting_answer record
                # so the hint-vs-reveal threshold also fires on
                # chat-authored questions. Without this, attempts
                # via chat-authored grading don't accumulate and
                # the reveal-after-N gate never opens.
                #
                # Task #173 (2026-05-17): bootstrap _awaiting_answer
                # for chat-authored / inline-MCQ questions when the
                # LLM didn't call pose_question. Otherwise the
                # transient record never exists and wrong_attempts
                # never accumulates across hint turns.
                if (
                    result is not None
                    and getattr(result, 'is_correct', None) is False
                ):
                    if not isinstance(getattr(self, '_awaiting_answer', None), dict):
                        from django.utils import timezone as _tz
                        self._awaiting_answer = {
                            'kind': 'inline_mcq' if mcq_match else 'inline_authored',
                            'question_id': None,
                            'question_type': 'mcq' if mcq_match else 'short_answer',
                            'turn_index': len(self.conversation),
                            'posed_at': _tz.now().isoformat(),
                            'wrong_attempts': 0,
                            'authored_question_text': question_text[:600],
                        }
                    cur = int(self._awaiting_answer.get('wrong_attempts', 0) or 0)
                    self._awaiting_answer['wrong_attempts'] = cur + 1
                if result is not None and getattr(result, 'is_correct', None) is True:
                    self._clear_awaiting_answer()
                logger.info(
                    "[BankGrade/ChatAuthored] session=%s is_correct=%s "
                    "q=%r reply=%r",
                    self.session.id, result.is_correct,
                    question_text[:80], student_input[:60],
                )
                return result
            return None
        if not ref_id and kind not in ('inline_authored',):
            return None

        # Resolve the question record + grade with the right shape.
        # LessonStep and ExitTicketQuestion have different field shapes
        # (LessonStep uses answer_type/expected_answer; ExitTicketQuestion
        # uses question_type/correct_answer/option_a..d). Dispatch by
        # kind so we don't silently fall into the MCQ default — that's
        # what produced [BankGrade] is_correct=None expected=None
        # student=None for every slot-0 turn in Martin's session.
        from apps.tutoring.bank_grader import (
            grade_bank_response,
            grade_lesson_step_response,
        )
        question = None
        if kind == 'lesson_step':
            from apps.curriculum.models import LessonStep
            question = LessonStep.objects.filter(id=ref_id).first()
            if question is None:
                return None
            result = grade_lesson_step_response(question, student_input)
        elif kind == 'exit_ticket_question':
            from apps.tutoring.models import ExitTicketQuestion
            question = ExitTicketQuestion.objects.filter(id=ref_id).first()
            if question is None:
                return None
            # Pass the judge_client so text-content question types
            # (short_answer, non-numeric FIB, matching) route through
            # the same LLM batch grader the exit ticket uses.
            # Deterministic types (MCQ, numeric short_numeric) still
            # short-circuit. Pilot directive 2026-05-16: mid-lesson
            # grading must match exit-ticket grading exactly.
            is_math = (
                self.lesson.unit.course.is_math
                if self.lesson.unit and self.lesson.unit.course
                else False
            )
            result = grade_bank_response(
                question, student_input,
                llm_client=self.judge_client,
                is_math=is_math,
            )
        elif kind == 'inline_authored':
            # NEW 2026-05-16: tutor authored its own question via
            # pose_inline_question. The question + answer_key are on
            # the previous tutor turn's metadata. Build a duck-typed
            # object with the same shape ExitTicketQuestion has so
            # grade_bank_response can grade it the same way.
            ia = (last_tutor_turn.metadata or {}).get(
                'inline_authored_question', {},
            ) or {}
            if not ia.get('answer_key'):
                logger.warning(
                    "[BankGrade] inline_authored: no answer_key on "
                    "previous turn metadata; skipping grade"
                )
                return None
            q_type = ia.get('question_type') or 'short_answer'
            answer_key = ia.get('answer_key', '')
            alternatives = ia.get('alternatives', []) or []
            keywords_for_grader = [answer_key] + list(alternatives)
            duck = _InlineAuthoredQuestion(
                question_text=ia.get('question', ''),
                question_type=q_type,
                correct_answer=answer_key,
                answer_data={
                    'model_answer': answer_key,
                    'keywords': keywords_for_grader,
                },
            )
            is_math = (
                self.lesson.unit.course.is_math
                if self.lesson.unit and self.lesson.unit.course
                else False
            )
            result = grade_bank_response(
                duck, student_input,
                llm_client=self.judge_client,
                is_math=is_math,
            )
            question = duck
        else:
            return None
        self._pending_bank_grade = result
        # Increment wrong_attempts on the awaiting_answer record when
        # this verdict is False. The active_bank_question system block
        # reads wrong_attempts to decide whether the tutor may finally
        # reveal the answer (>= 3 attempts) or must keep giving hints.
        if (
            result is not None
            and getattr(result, 'is_correct', None) is False
            and isinstance(getattr(self, '_awaiting_answer', None), dict)
        ):
            cur = int(self._awaiting_answer.get('wrong_attempts', 0) or 0)
            self._awaiting_answer['wrong_attempts'] = cur + 1
        # Stash the full question so the next-turn prompt builder can
        # surface the canonical explanation / step-by-step working —
        # the tutor uses it to scaffold remediation after a wrong
        # answer (rather than re-deriving the math itself).
        self._pending_bank_question = question
        # Keep awaiting_answer populated through the NEXT tutor turn so
        # the system prompt's <active_bank_question> block renders with
        # student_status='answered_correct' / 'answered_wrong' — that's
        # how the LLM knows the student replied and the result. Without
        # this, the LLM saw no signal that the previous question got
        # answered, and re-asked the same question on the next turn
        # (e2e 2026-05-16, session 32 turn 6 repeated "If angles around
        # a point are 45°, 90°, 135°, and x, what is x?"). The next
        # pose_question / pose_inline_question call naturally overwrites
        # _awaiting_answer with the new question, so it doesn't
        # accumulate across many turns.
        logger.info(
            "[BankGrade] session=%s ref=%s:%s is_correct=%s expected=%r student=%r",
            self.session.id, kind, ref_id,
            result.is_correct, result.expected, result.student_parsed,
        )
        return result

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
            # CRITICAL: respond to the student's last answer FIRST. Without
            # this guidance the model just re-delivers the teaching content
            # turn after turn (gpt-4o was looping "Welcome, Edward! Today
            # we're learning…" with each rephrasing).
            already_delivered = self.step_exchange_count > 0
            if already_delivered:
                parts.append(
                    "YOUR TASK: The teaching content has ALREADY been delivered "
                    "in a previous turn (see CONVERSATION CONTEXT above). DO NOT "
                    "re-deliver or rephrase the same content. Instead:"
                )
                parts.append(
                    "  1. RESPOND to what the student JUST SAID — acknowledge "
                    "their answer (correct / partially correct / not quite) "
                    "with specific feedback referring to their words."
                )
                parts.append(
                    "  2. If their answer is correct, advance the discussion "
                    "with the next idea OR signal completion (the engine will "
                    "move to the next step)."
                )
                parts.append(
                    "  3. If wrong/partial, give a TARGETED hint pointing at "
                    "the misconception and ask them to retry."
                )
                parts.append(
                    "  4. End with a question that moves forward — never with "
                    "the SAME comprehension check you already asked."
                )
            else:
                parts.append(
                    "YOUR TASK: Deliver this teaching content. Explain clearly, "
                    "then ask a comprehension check that the student has not "
                    "answered yet."
                )
                parts.append(
                    "IMPORTANT: Your comprehension check must be a complete, "
                    "self-contained question. Never say 'which of these' or "
                    "reference options you haven't listed."
                )
            parts.append(f"\nCONTENT TO TEACH (for reference, do NOT re-recite verbatim):\n{teacher_script}")
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
            # Same anti-loop guidance as TEACH: after the question has
            # been posed once, switch from "ask the question" to
            # "respond to the student's answer." gpt-4o was looping
            # the same True/False question after the student answered.
            already_posed = self.step_exchange_count > 0
            if already_posed:
                parts.append(
                    "YOUR TASK: The question has ALREADY been posed in a "
                    "previous turn (see CONVERSATION CONTEXT above). DO NOT "
                    "re-pose the same question. Instead:"
                )
                parts.append(
                    "  1. Read the EXPECTED ANSWER below and read what the "
                    "student JUST SAID. Decide: correct / partially correct "
                    "/ wrong."
                )
                parts.append(
                    "  2. If correct: confirm briefly using the canonical "
                    "explanation, then signal completion (the engine will "
                    "advance to the next step). Do NOT pose another question "
                    "from this step."
                )
                parts.append(
                    "  3. If wrong: give a TARGETED hint pointing at the "
                    "misconception (do NOT reveal the answer until 5+ wrong "
                    "attempts), and ask them to retry."
                )
                parts.append(
                    "  4. Never re-state the same question stem the student "
                    "already saw. End with a question that moves forward."
                )
            else:
                parts.append("YOUR TASK: Ask the EXACT question below verbatim, then grade the student's answer against the expected answer.")
            if step.question:
                parts.append(f"\nQUESTION (for reference): {step.question}")
            if step.expected_answer:
                parts.append(f"EXPECTED ANSWER: {step.expected_answer}")

            # Format-aware presentation. Lesson content is generated
            # rich (mixed answer_types); the tutor is responsible for
            # presenting each format correctly. See
            # memory/course_regeneration_for_slow_learners.md
            # "Revised Phase 2 Layer A".
            atype = (step.answer_type or 'none').lower()
            if atype == 'multiple_choice':
                choices = step.choices or []
                # Render choices verbatim; if they aren't already
                # prefixed A) / B) / ..., add the letter labels.
                rendered = []
                for i, c in enumerate(choices[:4]):
                    label = chr(ord('A') + i)
                    if isinstance(c, str) and c.strip().upper().startswith(f"{label})"):
                        rendered.append(c)
                    else:
                        rendered.append(f"{label}) {c}")
                parts.append(
                    "\nFORMAT — multiple choice:\n"
                    "Present the question, then list ALL the choices below. End with: "
                    "'Which letter is your answer — A, B, C, or D?'\n"
                    f"CHOICES (read all to the student):\n" + "\n".join(rendered)
                )
                parts.append(
                    "GRADING: The student's answer is correct only if it matches the expected_answer letter. "
                    "Accept the letter alone (e.g. 'B') or letter + value ('B, 12 cm'). "
                    "If they answer with the value only, infer the letter from the choices."
                )
            elif atype == 'true_false':
                parts.append(
                    "\nFORMAT — true/false:\n"
                    "Frame the question as: 'True or False: <statement>'. "
                    "Ask the student to answer with the word True or False."
                )
                parts.append(
                    "GRADING: Correct only if the student says exactly True or False matching the expected_answer. "
                    "Accept 'T' / 'F' as well. If they explain their reasoning instead of choosing, "
                    "ask them to commit to True or False before grading."
                )
            elif atype == 'short_numeric':
                parts.append(
                    "\nFORMAT — short numeric:\n"
                    "Ask for a number. If the expected_answer includes a unit (e.g. '12 cm'), "
                    "ask the student to include the unit too."
                )
                parts.append(
                    "GRADING: Strip units and whitespace, then compare numerically. "
                    "Accept ±5% tolerance for non-trivial calculations. "
                    "If the answer is right but the unit is missing, accept it and gently remind them about the unit."
                )
            elif atype == 'free_text':
                parts.append(
                    "\nFORMAT — free response:\n"
                    "Open-ended question. Encourage the student to explain in their own words."
                )
                parts.append(
                    "GRADING: Compare conceptually against the expected_answer; accept paraphrases that capture "
                    "the same key points. Don't penalize spelling or minor word differences."
                )
            elif atype != 'none':
                parts.append(f"ANSWER TYPE: {step.answer_type}")
            if step.choices and atype != 'multiple_choice':
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

        # Hint ladder (for practice/quiz steps, or any step with hints).
        # Hint POLICY adapts to the session's difficulty_level — the
        # existing -1/0/+1 signal already tracks whether this student is
        # a slower or stronger learner (per skills_snapshot, see
        # _derive_initial_difficulty). Per memory/course_regeneration_for_slow_learners.md
        # "Revised Phase 2 Layer B".
        hints = [h for h in [step.hint_1, step.hint_2, step.hint_3] if h]
        if hints:
            difficulty = getattr(self, 'difficulty_level', 0) or 0
            if difficulty <= -1:
                # High scaffolding (slow learner / no signal yet).
                # Offer hints proactively on the first wrong answer; if
                # the student has been silent on this step for >1
                # exchange, surface Hint 1 unprompted.
                policy = (
                    "HINT POLICY (this student needs more scaffolding):\n"
                    "  • On the FIRST wrong answer, give Hint 1 + a fresh attempt.\n"
                    "  • On the SECOND wrong answer, give Hint 2 + a fresh attempt.\n"
                    "  • On the THIRD wrong answer, give Hint 3 along with the worked-out answer.\n"
                    "  • If the student stalls (no answer for 2 exchanges), volunteer Hint 1 unprompted."
                )
            elif difficulty >= 1:
                # Low scaffolding (advanced student). Withhold hints
                # until they've genuinely tried twice.
                policy = (
                    "HINT POLICY (this student is performing strongly — challenge them):\n"
                    "  • Do NOT volunteer hints. Wait for the student to ask or attempt.\n"
                    "  • Give Hint 1 only after TWO wrong attempts.\n"
                    "  • Give Hint 2 only after THREE wrong attempts.\n"
                    "  • Reserve Hint 3 + worked answer for genuine surrender."
                )
            else:
                # Standard.
                policy = (
                    "HINT POLICY (use progressively if student is stuck):\n"
                    "  • Give a hint after one wrong attempt or if the student asks.\n"
                    "  • Walk up the ladder Hint 1 → Hint 2 → Hint 3 across attempts.\n"
                    "  • Don't reveal the answer outright; scaffold them to it."
                )
            parts.append("\n" + policy)
            parts.append("HINT LADDER:")
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

    def _build_regen_constraint_block(
        self, validation, previous_response: str = '',
    ) -> str:
        """Build a system-prompt block injected on regeneration (V3).

        The regen LLM is given:
          1. The list of violations the judge flagged
          2. The PREVIOUS response text it produced (the violating one)
          3. An instruction to EDIT that text — preserve the teaching
             value, fix only the flagged issues. Editing-with-context
             is more reliable than blind regeneration because the LLM
             can see what was good and target the fix.

        Triggers on contradicted curriculum claims OR rule-compliance
        violations (P5 — NO_AUTHORING / ARITHMETIC / RULE_1).
        """
        meta = validation.metadata or {}
        contradicted = meta.get('factual_claims_contradicted', []) or []
        unverified = meta.get('factual_claims_unverified', []) or []
        rule_violations = meta.get('rule_violations', []) or []
        parts = ["\n\n<regeneration_required>"]
        parts.append(
            "Your previous response had violations flagged by the"
            " post-response judge. Rewrite the response — keep the"
            " teaching value, fix the issues. Do NOT repeat the"
            " mistakes."
        )

        # Show the previous response as the editing context. The LLM
        # gets to see what it wrote so it can preserve the salvageable
        # parts (greeting, framing, conceptual scaffold) while
        # surgically replacing the violating bits.
        prev = (previous_response or '').strip()
        if prev:
            # Cap to keep prompt size sane; 2000 chars covers any
            # realistic tutor turn (typical ~300-800 chars).
            parts.append("PREVIOUS RESPONSE (your earlier output for THIS turn — edit it, do not start over):")
            parts.append("---")
            parts.append(prev[:2000])
            parts.append("---")

        if contradicted:
            parts.append(
                "Curriculum CONTRADICTS these claims (do not restate them):"
            )
            for c in contradicted[:5]:
                parts.append(f"  - {c}")
        if unverified:
            parts.append(
                "These claims have NO curriculum support — only state them"
                " if you can ground them in the curriculum context, otherwise"
                " avoid the specific number / fact:"
            )
            for c in unverified[:5]:
                parts.append(f"  - {c}")
        if rule_violations:
            parts.append("Rule violations the reviewer flagged:")
            for v in rule_violations[:5]:
                rule = v.get('rule', '?')
                evidence = (v.get('evidence') or '').strip()
                fix = (v.get('suggested_fix') or '').strip()
                line = f"  - [{rule}]"
                if evidence:
                    line += f" evidence: \"{evidence[:120]}\""
                if fix:
                    line += f" → fix: {fix[:160]}"
                parts.append(line)
            parts.append(
                "EDITING GUIDANCE:"
            )
            parts.append(
                "If NO_AUTHORING was flagged: REMOVE the numerical"
                " question from your prose. If you still want to ask"
                " one this turn, call the pose_question tool with a"
                " slot from <question_bank> instead. If no fitting"
                " bank slot exists, replace the numerical question"
                " with a CONCEPTUAL one (\"What rule applies?\")."
            )
            parts.append(
                "If ARITHMETIC was flagged: rewrite the number-claim"
                " with the correct value, or omit the calculation"
                " entirely."
            )
            parts.append(
                "If RULE_1 was flagged: drop the praise. Then either"
                " ask one focused question that probes the student's"
                " reasoning, OR transition forward without restating"
                " their answer. Phrase the question naturally — do"
                " NOT default to a stock phrase like \"walk me through"
                " your steps\" or \"show me how you got there\". Vary"
                " your wording across turns."
            )
        # Figure-reference-without-signal — text said "the diagram" /
        # "in the figure" but no |||MEDIA:N||| was emitted. Two valid
        # fixes: signal the matching media OR remove the deictic
        # reference and explain in plain prose.
        from apps.tutoring.validator import (
            ISSUE_FIGURE_MISMATCH,
            ISSUE_FIGURE_REF_WITHOUT_SIGNAL,
            ISSUE_TUTOR_INCOHERENT,
            ISSUE_TUTOR_UNSAFE,
            ISSUE_VERDICT_MISMATCH,
        )
        if ISSUE_FIGURE_REF_WITHOUT_SIGNAL in (validation.issues or []):
            # Surface the specific phrases the figure_ref judge found.
            ref_issues = (meta.get('figure_ref_issues') or [])[:3]
            ref_block = ""
            if ref_issues:
                ref_block = "\n  Specific phrases flagged:" + "".join(
                    f"\n    - {x}" for x in ref_issues
                )
            parts.append(
                "FIGURE_REF_WITHOUT_SIGNAL was flagged: your previous"
                " response referenced a visual (\"the diagram\","
                " \"looking at the figure\", \"shown above\") but did"
                " NOT emit |||MEDIA:N||| — the student saw the"
                " reference but no figure." + ref_block + " Two valid fixes:"
                "\n  (a) If a matching media item exists in the"
                " <media_catalog>, append |||MEDIA:N||| as the LAST"
                " line of your response so the student sees the"
                " figure you referenced."
                "\n  (b) If no matching figure is available, REMOVE"
                " every \"the diagram / figure / image / picture /"
                " shown above\" reference from your text and explain"
                " the concept in plain prose instead."
                " Pick one — do not leave the reference dangling."
            )

        # Coherence — tutor self-contradicted within the same turn.
        if ISSUE_TUTOR_INCOHERENT in (validation.issues or []):
            coh_violations = (meta.get('coherence_violations') or [])[:3]
            v_block = ""
            if coh_violations:
                v_block = "\n  Contradictions flagged:" + "".join(
                    f"\n    - {x}" for x in coh_violations
                )
            parts.append(
                "TUTOR_INCOHERENT was flagged: your previous response"
                " contradicts itself (e.g. introduces a setup with N"
                " items, then poses a question with M items; or praises"
                " the student then immediately corrects them)." + v_block +
                " Pick ONE consistent framing and rewrite. If you"
                " posed a question that doesn't match the setup you"
                " just described, fix the question to match the setup,"
                " or fix the setup to match the question — not both."
            )

        # Safety — tutor response flagged HARMFUL or INAPPROPRIATE.
        # This clause comes FIRST in the regen prompt because safety
        # dominates: even if the rewrite has other issues, the rewrite
        # must be safe. The score function further enforces this by
        # making safety:critical / safety:warning impossible to pick.
        if ISSUE_TUTOR_UNSAFE in (validation.issues or []):
            sev = (meta.get('safety_severity') or '').strip()
            cats = meta.get('safety_categories') or []
            reasoning = (meta.get('safety_reasoning') or '').strip()
            cat_str = ", ".join(str(c) for c in cats) if cats else "(unspecified)"
            parts.append(
                f"TUTOR_UNSAFE was flagged: the original response "
                f"contained content classified as [{cat_str}] "
                f"(severity={sev or 'warning'}). "
                + (f"Reviewer reason: {reasoning}. " if reasoning else "")
                + "The rewrite MUST be age-appropriate for a 13–16-year-old "
                "student in a school setting, free of any harmful, "
                "violent, sexual, or self-harm content, and must not "
                "promote drugs / alcohol / gambling. If the original "
                "topic itself is unsafe to teach, redirect the student "
                "to ask a trusted adult or counsellor and pivot back "
                "to the lesson with one focused, on-topic question."
            )

        # Verdict-mismatch — tutor's text contradicts the deterministic
        # verdict (e.g. student said B for an MCQ, B is correct,
        # deterministic verdict True, but tutor said "not quite").
        if ISSUE_VERDICT_MISMATCH in (validation.issues or []):
            direction = (meta.get('verdict_mismatch_direction') or '').strip()
            if direction == 'tutor_said_wrong_was_right':
                parts.append(
                    "VERDICT_MISMATCH was flagged: the deterministic "
                    "check (numeric / MCQ-letter) confirms the student's "
                    "answer is CORRECT, but your previous response said "
                    "it was wrong (\"not quite\", \"that's not right\"). "
                    "Rewrite to confirm the student got it right and "
                    "transition forward. Do NOT add a 'walk me through "
                    "your working' interrogation — they answered correctly."
                )
            elif direction == 'tutor_said_right_was_wrong':
                parts.append(
                    "VERDICT_MISMATCH was flagged: the deterministic "
                    "check confirms the student's answer is INCORRECT, "
                    "but your previous response praised them as right "
                    "(\"exactly\", \"perfect\"). Rewrite to point at "
                    "the actual error and ask one focused question "
                    "to help them find the right answer."
                )

        # Figure-vision mismatch — attached figure doesn't match
        # the question. Two valid fixes: rewrite the question to match
        # the figure, OR pick a different figure from the catalog.
        if ISSUE_FIGURE_MISMATCH in (validation.issues or []):
            mismatch = (meta.get('figure_mismatch_reason') or '').strip()
            summary = (meta.get('figure_summary') or '').strip()
            parts.append(
                "FIGURE_MISMATCH was flagged: the figure you attached"
                " (|||MEDIA:N|||) does NOT match the question you"
                " posed."
                + (f"\n  What the figure actually shows: {summary}"
                   if summary else "")
                + (f"\n  Mismatch reason: {mismatch}"
                   if mismatch else "")
                + " Two valid fixes:"
                "\n  (a) Rewrite the question so it matches what the"
                " figure depicts."
                "\n  (b) Pick a different |||MEDIA:N||| from"
                " <media_catalog> that actually shows what your"
                " question describes — or drop the figure entirely"
                " and remove any \"in the diagram\" reference."
            )

        parts.append(
            "Edit the previous response to fix these violations. Keep"
            " whatever was good (greeting, framing, conceptual scaffold)."
            " End with a question. One new idea at a time."
        )
        parts.append("</regeneration_required>")
        return "\n".join(parts)

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

    # A CHAINED arithmetic expression IS the working — at least two
    # operations (or an op followed by '='). "180-62-30=88" is working;
    # "x=88" is just an assignment (still bare); "5 1/4" is a single
    # fractional answer (still bare). Single binary ops like "5×4"
    # without a result also stay bare — the student should write the
    # computed value.
    _ARITHMETIC_EXPRESSION_RE = re.compile(
        r"\d\s*[+\-*/×÷]\s*\d.*[+\-*/×÷=]"
    )

    def _is_bare_math_answer(self, student_input: str) -> bool:
        """Return True when the student's reply looks like a naked numeric
        answer with no working/explanation (M9 pedagogy layer).

        Heuristic:
          - Input has content and parses as a single number
          - Input is short (<= 40 chars)
          - Input contains none of the 'working' marker words
          - Input contains no arithmetic expression (operators between
            digits or an '=' sign — that's the student showing working
            in compact form, e.g. "180-62-30=88")
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
        if self._ARITHMETIC_EXPRESSION_RE.search(text):
            return False
        return True

    def _build_math_eval_signal_block(
        self,
        check: MathCheckResult,
        student_input: str,
        bare_answer: bool = False,
        bare_answer_count_for_step: int = 0,
        step_type: str = '',
    ) -> str:
        """Render the <evaluation_signal> block appended to the system prompt
        when a deterministic math check produced a definite result.

        ``step_type`` and ``bare_answer_count_for_step`` together scope
        how aggressively the bare-answer "show working" rule is applied:

        - Guided steps (teach / worked_example / summary): elementary
          sub-questions like "what is 200 ÷ 25?" don't deserve a
          show-working interrogation. The tutor is walking the student
          through the steps; a one-number answer to a one-operation
          prompt is the working. Use light-touch guidance.

        - Independent practice (practice / quiz): enforce show-working
          on the FIRST bare answer per step (matches the principle's
          "ask ONCE"). On the second+ bare answer in the same step,
          accept the value and continue — repeated probing is
          interrogation, not teaching.
        """
        verdict = "CORRECT" if check.is_correct else "INCORRECT"
        guided_step = (step_type or '').strip().lower() in (
            'teach', 'worked_example', 'summary',
        )
        if bare_answer:
            if guided_step:
                # Guided sub-question — the calculation IS the working.
                if check.is_correct:
                    guidance = (
                        "This is a sub-step inside a guided walkthrough"
                        f" (step_type={step_type}). The student's elementary"
                        " calculation is correct. Confirm briefly (one"
                        " short phrase, no over-praise) and move to the"
                        " NEXT conceptual question. DO NOT ask them to"
                        " 'show working' for a single-operation answer —"
                        " the calculation IS the working at this scale."
                    )
                else:
                    guidance = (
                        "This is a sub-step inside a guided walkthrough"
                        f" (step_type={step_type}). The student's"
                        " calculation is incorrect. Point at the specific"
                        " arithmetic error, give a short hint, and let"
                        " them retry the same sub-step. Don't escalate to"
                        " 'walk me through your working' for a one-"
                        "operation calculation."
                    )
            else:
                # All bare-answer branches now collapse to "confirm or
                # diagnose, then advance" — never probe. Pilot feedback
                # 2026-05-12: probing on every bare answer (even with
                # the "ask once" caps) was reading as interrogation.
                # Per-pilot directive: "the system should just move on
                # when a correct answer is provided."
                if check.is_correct:
                    guidance = (
                        "The student's bare answer matches the expected"
                        " value. Per math_teaching Rule 1, do NOT use"
                        " 'correct', 'right', 'brilliant', 'you got it',"
                        " 'exactly', or 'perfect' on a bare answer.\n"
                        "Acknowledge the value briefly (one short"
                        f" specific line — e.g. \"yes — {check.student_parsed}"
                        " is right.\") and ADVANCE to the next step or"
                        " sub-question. The probe_frequency principle"
                        " applies — no reasoning probes on correct"
                        " answers."
                    )
                else:
                    guidance = (
                        "The student's bare answer does NOT match. Name"
                        " the specific error in one short sentence and"
                        " give a brief hint, then let them retry. Do"
                        " NOT ask 'walk me through your working' or"
                        " 'how did you get there' — diagnose, don't"
                        " interrogate.\n"
                        "Example: BEFORE \"How did you arrive at 95?\""
                        " AFTER \"95 isn't quite right — check the sum"
                        " of the three angles. What do you get?\""
                    )
        elif check.is_correct:
            guidance = (
                "The student's answer is correct. CONFIRM briefly (one"
                " short specific line — e.g. \"yes — 8 is right.\","
                " \"that's it — dividing by 25 worked.\") and ADVANCE"
                " to the next step. The probe_frequency principle"
                " applies — no reasoning probes on correct answers."
            )
        else:
            guidance = (
                "The student's numeric answer does NOT match the expected"
                " answer. You MUST NOT say 'correct', 'right', 'brilliant',"
                " 'well done', 'you got it', 'exactly', 'perfect', or any"
                " equivalent praise. Do not state the correct answer yet.\n"
                "Name the specific step that went wrong in one sentence"
                " and give a short hint pointing at the fix. Do NOT ask"
                " 'walk me through your working' — they already showed"
                " it (or didn't). Diagnose, don't interrogate."
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

    def _build_bank_grade_signal_block(self, grade) -> str:
        """Render the <bank_evaluation_signal> block when the previous
        turn rendered a bank question and the deterministic grader
        produced a verdict. Tells the LLM the verdict and forbids it
        from disagreeing — platform-wide rule (LLM never calculates).

        Includes the bank's stored explanation / canonical working so
        the LLM can scaffold remediation against it instead of
        re-deriving (and risking new errors).
        """
        verdict = "CORRECT" if grade.is_correct else "INCORRECT"
        # Read wrong_attempts from the awaiting_answer record (bank Qs)
        # or fall back to the grade detail for chat-authored fallbacks.
        # Reveal is gated at >= 3 attempts on the same question; before
        # then the tutor MUST give a hint and let the student retry,
        # so they actually learn to recognise the misconception. Pilot
        # 2026-05-17 lesson 540: tutor revealed "physical geography"
        # on the first wrong attempt, so the student never had a chance
        # to self-correct.
        _aa = getattr(self, '_awaiting_answer', None) or {}
        wrong_attempts = int(_aa.get('wrong_attempts', 0) or 0)
        _reveal_at = self._reveal_threshold()
        reveal_allowed = wrong_attempts >= _reveal_at

        if grade.is_correct:
            guidance = (
                "The bank's stored answer matches the student's response. "
                "Confirm correctness briefly, then move forward — either "
                "advance to the next step OR pose the next bank question "
                "by calling the pose_question tool. DO NOT re-derive the "
                "answer yourself; the bank is the source of truth. If the "
                "student wants to see the working, QUOTE the "
                "canonical_working / explanation below verbatim — never "
                "compute a fresh derivation."
            )
        elif reveal_allowed:
            guidance = (
                f"The student has now missed this question "
                f"{wrong_attempts} times (move-on threshold: "
                f"{_reveal_at}). MOVE ON — do NOT reveal the correct "
                "answer. Acknowledge their attempt gently ('that's a "
                "tricky one — let's try a different angle'). In ONE "
                "or TWO short sentences, RE-EXPLAIN the underlying "
                "concept the question was testing WITHOUT naming the "
                "correct option letter and WITHOUT paraphrasing the "
                "canonical answer text. Then immediately pose a "
                "DIFFERENT question on the SAME concept (or an "
                "easier related one) via the pose_question tool — "
                "ideally one tagged easy or with simpler wording. "
                "NEVER state 'the correct answer is X' or describe "
                "the canonical option."
            )
        else:
            guidance = (
                f"The student's response does NOT match the bank's "
                f"stored answer (wrong_attempts: {wrong_attempts}, "
                f"reveal at: {_reveal_at}). "
                "DO NOT REVEAL the correct answer, the correct option "
                "letter, or paraphrase the canonical text. You MUST "
                "NOT say 'correct', 'right', 'exactly', or any "
                "equivalent praise. Instead: (1) acknowledge gently "
                "('not quite' / 'close, but think about…'), (2) name "
                "the underlying concept they need to consider — use "
                "the canonical_explanation below to inform a CLUE, "
                "but rephrase it so it narrows the answer space "
                "without giving it away, (3) invite them to try again. "
                "ONE short probe ('what made you pick that?') is OK. "
                f"Reveal is only permitted after {_reveal_at} wrong "
                "attempts on this question."
            )

        # Pull the stored explanation from the cached question. Limit
        # to ~600 chars so the prompt stays compact.
        question = getattr(self, '_pending_bank_question', None)
        canonical_block = ""
        if question is not None:
            explanation = (getattr(question, 'explanation', '') or '').strip()
            if explanation:
                canonical_block = (
                    f"\nCanonical explanation / working (from the bank):"
                    f"\n  {explanation[:600]}"
                )
            # Some templated entries store extended working under
            # answer_data['canonical_working'] (short_answer + a few
            # other types). Surface that too when present.
            adata = getattr(question, 'answer_data', None) or {}
            if isinstance(adata, dict):
                cw = (adata.get('canonical_working') or '').strip()
                if cw and cw != explanation:
                    canonical_block += (
                        f"\nCanonical step-by-step:\n  {cw[:600]}"
                    )

        # W2 — append FORBIDDEN/ACCEPTABLE phrase list + difficulty-tiered
        # obviousness directive when reveal is NOT yet allowed.
        # Resolves the canonical option letter + text when MCQ so the
        # forbidden list quotes the actual canonical text.
        calibration_block = ""
        if not grade.is_correct and not reveal_allowed:
            _opt_letter: Optional[str] = None
            _opt_text: Optional[str] = None
            try:
                if question is not None:
                    qt = getattr(question, 'question_type', '') or 'mcq'
                    if qt == 'mcq':
                        _opt_letter = (
                            getattr(question, 'correct_answer', '') or ''
                        ).strip().upper()
                        _opt_text = {
                            'A': getattr(question, 'option_a', None),
                            'B': getattr(question, 'option_b', None),
                            'C': getattr(question, 'option_c', None),
                            'D': getattr(question, 'option_d', None),
                        }.get(_opt_letter)
                    else:
                        _opt_text = (
                            getattr(question, 'expected_answer', '') or ''
                        ).strip()
            except Exception:
                pass
            calib = self._build_hint_calibration_block(
                correct_option_letter=_opt_letter,
                correct_option_text=_opt_text,
                reveal_allowed=False,
            )
            if calib:
                calibration_block = "\n" + calib

        return (
            "\n\n<bank_evaluation_signal>"
            f"\nStudent's response (parsed): {grade.student_parsed!r}"
            f"\nExpected (from the bank): {grade.expected!r}"
            f"\nVerdict: {verdict}"
            f"\nDetail: {grade.detail}"
            f"{canonical_block}"
            f"\n{guidance}"
            f"{calibration_block}"
            "\n</bank_evaluation_signal>"
        )

    def _build_bare_answer_only_block(
        self,
        student_input: str,
        bare_answer_count_for_step: int = 0,
    ) -> str:
        """Render a slim guidance block when the student gave a bare
        numeric answer on a math practice/quiz step but the deterministic
        check produced no result (no expected_answer recorded — the case
        when the tutor invents interim arithmetic mid-conversation).

        Without this block the LLM, lacking the full <evaluation_signal>
        guidance, falls back to its own judgement and praises bare
        correct answers — violating math_teaching Rule 1 and triggering
        the praise filter's awkward "Let's check this together" rewrite.
        """
        echo = (student_input or "").strip()[:80]
        guidance = (
            "The student replied with a BARE numeric answer with no"
            " working shown, on a math practice/quiz step. There is no"
            " pre-recorded expected answer for this turn (you asked an"
            " interim arithmetic question on the fly), so DO NOT decide"
            " correctness on your own.\n"
            "Per math_teaching Rule 1, you MUST NOT say 'correct',"
            " 'right', 'brilliant', 'perfect', 'exactly', 'spot on',"
            " 'great work', 'you got it', '✓', or any equivalent praise"
            " — even if their answer looks right to you.\n"
            f"Echo the student's answer back verbatim ('You said {echo}'),"
            " then ask them to walk you through how they got it. Only"
            " after seeing the working may you affirm or correct."
        )
        if bare_answer_count_for_step >= 2:
            guidance += (
                "\nNOTE: This is the third+ bare answer on this step. Be"
                " patient — gently model what 'showing working' looks"
                " like by writing one example step yourself, then invite"
                " them to try the next step that way."
            )
        return (
            "\n\n<evaluation_signal>"
            f"\nStudent's answer: {echo}"
            "\nVerdict: BARE — no expected answer to check against"
            "\nBare answer (no working shown): True"
            f"\n{guidance}"
            "\n</evaluation_signal>"
        )

    # ------------------------------------------------------------------
    # Step-eval context (consumed by combined_judge as CHECK 4)
    # ------------------------------------------------------------------

    # Step types whose teacher_script is a question (slot-0-posable).
    _QUESTION_SHAPED_STEPS = ('practice', 'quiz', 'worked_example')
    # Min exchanges before step-eval fires for each step type. Below
    # the floor we skip step eval entirely (no judge step verdict, no
    # advancement). Stops the model from rushing teach/worked_example
    # off the very first turn before the student has actually engaged.
    _STEP_EVAL_MIN_EXCHANGES = {
        'teach': 2,
        'worked_example': 2,
        'practice': 1,
        'quiz': 1,
        'summary': 1,
    }
    # Hard cap per step type: force advance after this many exchanges
    # regardless of LLM verdict. Prevents the tutor from getting stuck
    # on a single step. teach / worked_example / summary are short
    # Hard-cap exchanges per step. Reduced from 30 → 10 (2026-05-06)
    # for practice/quiz: production transcripts showed sessions running
    # 14+ exchanges on the final quiz step without the exit ticket
    # firing because step_complete=null came back from the judge every
    # turn. 10 is enough wrestling time for a single question with
    # retries, and forces advancement so the exit ticket actually
    # triggers.
    _STEP_HARD_CAP_EXCHANGES = {
        'teach': 10,
        'worked_example': 10,
        'practice': 10,
        'quiz': 10,
        'summary': 10,
    }
    _STEP_HARD_CAP_DEFAULT = 10

    # Conceptual non-answers — student inputs that aren't math attempts
    # and shouldn't trigger an answer_correct verdict. Production
    # transcripts (Samanthi / Trent / Francis pilot, 2026-05-06) showed
    # Sonnet returning bogus true/false on these one-word inputs even
    # though the judge prompt says "null". Programmatic guard.
    _NON_ANSWER_INPUTS: frozenset = frozenset({
        "yes", "yes.", "yeah", "yep", "y",
        "no", "no.", "nope", "n",
        "ok", "ok.", "okay", "okay.", "k",
        "help", "?", "??", "huh", "hmm",
        "i don't know", "idk", "i dont know", "dunno",
        "not sure", "no idea",
    })

    def _is_non_answer_input(self, student_input: str) -> bool:
        """Return True for student inputs that aren't math attempts."""
        text = (student_input or "").strip().lower()
        if not text:
            return True
        # Exact match against the conceptual-non-answer set.
        if text in self._NON_ANSWER_INPUTS:
            return True
        # Very short non-numeric inputs ("yes please", "ok then") also
        # qualify if they contain only non-answer phrases.
        if len(text) <= 20:
            for phrase in self._NON_ANSWER_INPUTS:
                if text == phrase or text.startswith(phrase + " "):
                    return True
        return False

    @staticmethod
    def _extract_last_question(text: str) -> str:
        """Best-effort: return the last question-like sentence from a tutor
        message — sentences ending in `?` or fill-in markers like
        `___°.` / `___.`. Used by `_build_step_eval_context` as a
        fallback when `step.teacher_script` is empty so the step_eval
        judge always has the actual posed question to anchor on,
        instead of guessing from the tutor's CURRENT response (which
        may contain a freshly-authored next question).
        Returns "" when no question found.
        """
        if not text:
            return ""
        import re as _re_q
        sentences = _re_q.split(r'(?<=[\.\!\?])\s+|\n+', text.strip())
        for sent in reversed(sentences):
            s = (sent or "").strip()
            if not s:
                continue
            if (
                s.endswith("?")
                or s.endswith("___°.") or s.endswith("___°")
                or s.endswith("___.") or s.endswith("___")
            ):
                return s[:400]
        return ""

    def _build_step_eval_context(
        self, student_input: str, tutor_response: str,
        math_check: Optional[MathCheckResult] = None,
    ) -> Optional[dict]:
        """Build the step_context payload for combined_judge CHECK 4.

        Returns None when step eval should be skipped:
          - no step in flight / exit-ticket phase / below min-exchange floor
          - the current step has NO expected_answer (engage/teach/summary
            steps without an expected — Sonnet can't evaluate against
            nothing and ends up vibes-grading)
          - the student's input is a conceptual non-answer ("yes",
            "help", "ok") — these aren't math attempts, no verdict
            should be issued

        When None, the judge sees an empty step_context and reports
        answer_correct=null, step_complete=false (skipping the eval).
        """
        if self.session_state != SessionState.TUTORING:
            return None
        if self.current_topic_index >= len(self.steps):
            return None
        step = self.steps[self.current_topic_index]
        step_type = (step.step_type or 'teach').strip()
        floor = self._STEP_EVAL_MIN_EXCHANGES.get(step_type, 1)
        if self.step_exchange_count < floor:
            logger.info(
                "[CombinedJudge] step_eval: SKIPPED step=%d type=%s "
                "exchanges=%d < floor=%d",
                self.current_topic_index, step_type,
                self.step_exchange_count, floor,
            )
            return None

        # No expected_answer → the judge has nothing to compare against.
        # Production showed Sonnet hallucinating true/false on engage /
        # teach steps where expected_answer was empty.
        expected = (step.expected_answer or '').strip()
        if not expected:
            logger.info(
                "[CombinedJudge] step_eval: SKIPPED step=%d type=%s "
                "no expected_answer — verdict=null",
                self.current_topic_index, step_type,
            )
            return None

        # Conceptual non-answers ("yes", "help", "ok") aren't math
        # attempts; force null verdict.
        if self._is_non_answer_input(student_input):
            logger.info(
                "[CombinedJudge] step_eval: SKIPPED step=%d "
                "non-answer input=%r — verdict=null",
                self.current_topic_index, (student_input or '')[:40],
            )
            return None

        criteria_map = {
            # Loosened (2026-05-05): old criteria required BOTH content
            # delivery AND a perfect comprehension check, which made
            # the LLM judge return step_complete=False even when
            # students were clearly engaging. New criteria: complete
            # when the student has shown they understand the concept,
            # via correct or substantively-engaged answers. Hard cap
            # of 10 exchanges still backstops anything that drags.
            'teach': (
                "Complete when the student has shown they understand "
                "the concept — either by correctly answering a "
                "comprehension check OR by accurately paraphrasing / "
                "extending the idea in their own words. Don't require "
                "a perfect textbook answer; a meaningful demonstration "
                "of understanding is enough."
            ),
            'worked_example': (
                "Complete when the student has explained at least one "
                "step of the example back correctly OR demonstrated "
                "they can apply the same method to a similar setup."
            ),
            'practice': (
                "Complete when the student gives the FINAL correct "
                "answer to the posed question. Partial working / "
                "intermediate steps that are correct so far do NOT "
                "complete the step — wait for the final answer."
            ),
            'quiz': (
                "Complete when the student gives the FINAL correct "
                "answer to the posed question. Partial working that "
                "is correct so far does NOT complete the step."
            ),
            'summary': (
                "Complete when the student acknowledges the key points "
                "(any 'got it' / 'makes sense' / paraphrase counts)."
            ),
        }
        # Last 4 conversation turns for context (kept short — judge
        # already gets the full turn pair via student_input + tutor_response).
        recent = self.conversation[-(4 * 2):] if len(self.conversation) > 4 else self.conversation
        recent_text = "\n".join(
            f"{'TUTOR' if m['role'] == 'assistant' else 'STUDENT'}: "
            f"{(m.get('content') or '')[:200]}"
            for m in recent
        )

        atype = (step.answer_type or '').lower()
        format_hint = ''
        if atype == 'multiple_choice':
            format_hint = "Answer format: multiple_choice. Correct iff student's letter matches expected_answer (A/B/C/D)."
        elif atype == 'true_false':
            format_hint = "Answer format: true_false. Correct iff student's response (True/False) matches expected_answer exactly."
        elif atype == 'short_numeric':
            format_hint = "Answer format: short_numeric. Strip units, compare numerically with ±5% tolerance."
        elif atype == 'free_text':
            format_hint = "Answer format: free_text. Compare conceptually; accept paraphrases that capture key points."

        # First-layer deterministic verdict — feeds into the LLM's
        # judgment so it anchors on arithmetic / MCQ ground truth.
        # Edward, 2026-05-07: "deterministic eval [is] the first
        # layer ... LLM is always executed, but it can use the
        # deterministic eval as the first layer of eval to avoid
        # arithmetic errors."
        deterministic_verdict: Optional[bool] = None
        deterministic_source: str = ''
        if math_check is not None and math_check.is_correct is not None:
            deterministic_verdict = bool(math_check.is_correct)
            deterministic_source = 'numeric'
        elif atype == 'multiple_choice' and expected:
            text = (student_input or '').strip()
            import re as _re_mcq
            m = _re_mcq.match(
                r'^[\(\[]?\s*([A-D])\s*[\)\]\.]*\s*$', text, _re_mcq.IGNORECASE,
            )
            if m:
                student_letter = m.group(1).upper()
                expected_letter = expected.upper()
                if expected_letter in ('A', 'B', 'C', 'D'):
                    deterministic_verdict = (student_letter == expected_letter)
                    deterministic_source = 'mcq_letter'

        # The QUESTION the student was actually answering. Without this
        # explicit anchor, step_eval has been observed to read the
        # CURRENT tutor_response (which may contain a freshly-authored
        # next question) and grade the student's answer against the
        # WRONG question. Production transcript example: tutor asked
        # "find missing angle if one is 65°", student said "subtract
        # 65 from 180", tutor's next response posed a new 50° question
        # — judge graded the answer against the 50° question. With
        # posed_question explicitly carried, the judge anchors on
        # what the student was actually asked.
        prior_tutor_text = ""
        for m in reversed(self.conversation[:-1] if self.conversation else []):
            if m.get('role') == 'assistant':
                prior_tutor_text = (m.get('content') or '').strip()
                break
        # Priority: the immediately-prior tutor turn's last question
        # is what the student actually answered. Anchor here FIRST.
        # teacher_script is often a walkthrough narrative (for
        # worked_example) or a setup directive (for practice/quiz)
        # — neither is the specific sub-question that prompted the
        # student's reply. Previously we anchored on teacher_script,
        # which made step_eval mis-grade replies in guided steps:
        # production session 255 (2026-05-12) — tutor asked "How
        # many groups of 25 can you make from 200?", student said
        # "8", step_eval anchored on the long worked-example
        # narrative, fell through to the NEW tutor_response (which
        # had pivoted to "5y = 70") and marked "8" wrong.
        prior_q = self._extract_last_question(prior_tutor_text)
        posed_question = (
            prior_q
            or (step.question or '').strip()
            or (step.teacher_script or '').strip()
        )[:400]

        # Surface MCQ option CONTENT when a bank question is in flight,
        # so step_eval can recognize equivalence between a free-text
        # student answer ("x = 8", "8") and the correct option's text
        # ("C) 8"). Without this, the deterministic letter check fails
        # any free-text answer and the engine never advances. See
        # production transcript on 2026-05-12 — session 251.
        mcq_options = None
        correct_option_text = ''
        bank_q = getattr(self, '_pending_bank_question', None)
        if bank_q is not None:
            # ExitTicketQuestion path
            if hasattr(bank_q, 'option_a'):
                opts = {
                    'A': (bank_q.option_a or '').strip(),
                    'B': (bank_q.option_b or '').strip(),
                    'C': (bank_q.option_c or '').strip(),
                    'D': (bank_q.option_d or '').strip(),
                }
                if any(opts.values()):
                    mcq_options = opts
                    correct_letter = (
                        getattr(bank_q, 'correct_answer', '') or ''
                    ).upper()
                    correct_option_text = opts.get(correct_letter, '')
            # LessonStep path — MCQ choices stored as a list
            elif hasattr(bank_q, 'choices') and getattr(bank_q, 'choices', None):
                choices = list(bank_q.choices)[:4]
                if choices:
                    mcq_options = {
                        chr(ord('A') + i): str(c).strip()
                        for i, c in enumerate(choices)
                        if str(c).strip()
                    }
                    # For LessonStep, expected_answer carries either the
                    # letter or the content. Try letter resolution first.
                    expected_raw = (
                        getattr(bank_q, 'expected_answer', '') or ''
                    ).strip()
                    if (expected_raw.upper() in mcq_options
                            and len(expected_raw) == 1):
                        correct_option_text = mcq_options[expected_raw.upper()]
                    else:
                        correct_option_text = expected_raw

        return {
            "step_type": step_type,
            "step_index": self.current_topic_index,
            "exchanges_on_this_step": self.step_exchange_count,
            "completion_criteria": criteria_map.get(step_type, criteria_map['teach']),
            "expected_answer": (step.expected_answer or '')[:300],
            "teacher_script_excerpt": (step.teacher_script or '')[:400],
            "posed_question": posed_question,
            "format_hint": format_hint,
            "recent_conversation": recent_text[:1500],
            # First-layer deterministic check. The judge anchors here
            # for arithmetic / MCQ; only override on equivalent forms,
            # working-with-typo, or when the deterministic check
            # missed context the broader LLM judgment can see.
            "deterministic_verdict": deterministic_verdict,
            "deterministic_source": deterministic_source,
            # MCQ equivalence context (2026-05-12). None when the in-
            # flight bank question isn't MCQ or has no options.
            "mcq_options": mcq_options,
            "correct_option_text": correct_option_text or None,
        }

    def _evaluate_step(self, student_input: str, tutor_response: str) -> Optional[StepEvaluationResult]:
        """Merged LLM evaluator: answer correctness + step completion in one call.

        Step-type-specific prompts:
        - teach: complete when content delivered + comprehension check answered correctly
        - worked_example: complete when example walked through + student explained a step back
        - practice/quiz: complete when answered correctly OR exhausted max_attempts
        - summary: complete when key points stated + student acknowledged

        NOTE (2026-05-05): on math turns, step eval is now folded into
        run_combined_judge (CHECK 4) — see _build_step_eval_context.
        This method remains as the fallback for non-math sessions where
        the combined judge isn't running.
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
        # Format-aware grading hints — the evaluator should apply
        # answer-type-specific rules (e.g. true_false → exact match,
        # short_numeric → ±5% tolerance, mcq → letter match).
        atype = (step.answer_type or '').lower()
        if atype == 'multiple_choice':
            step_context_parts.append(
                "Answer format: multiple_choice. Correct iff student's letter matches expected_answer (A/B/C/D)."
            )
        elif atype == 'true_false':
            step_context_parts.append(
                "Answer format: true_false. Correct iff student's response (True/False) matches expected_answer exactly."
            )
        elif atype == 'short_numeric':
            step_context_parts.append(
                "Answer format: short_numeric. Strip units, compare numerically with ±5% tolerance."
            )
        elif atype == 'free_text':
            step_context_parts.append(
                "Answer format: free_text. Compare conceptually; accept paraphrases that match expected_answer's key points."
            )
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

        # System prompt holds the step context + completion criteria.
        # Conversation history flows through as a proper messages array
        # (structural fix A — same shape as the main tutor call) so the
        # evaluator reads turn structure natively instead of parsing
        # "TUTOR: …\nSTUDENT: …" embedded text.
        eval_system = (
            "You are a tutoring step evaluator. Assess answer correctness "
            "and step completion based on the conversation that follows.\n\n"
            f"STEP CONTEXT:\n{chr(10).join(step_context_parts)}\n\n"
            f"COMPLETION CRITERIA: {completion_criteria}\n\n"
            "INSTRUCTIONS FOR answer_correct:\n"
            "- Return TRUE if the student gave a clear, demonstrably correct "
            "answer to a specific question.\n"
            "- Return FALSE only when the student gave a clear, demonstrably "
            "wrong answer to a specific question.\n"
            "- Return NULL when none of these apply: the student is "
            "acknowledging the teaching ('ok', 'got it', 'interesting'), "
            "asking their own question, expressing confusion, sharing "
            "tangential thoughts, or there was no expected_answer to "
            "compare against. Default to NULL when uncertain — never "
            "penalize a student for engaging conversationally.\n\n"
            "INSTRUCTIONS FOR step_complete:\n"
            "- Apply the COMPLETION CRITERIA above. A step is NOT complete "
            "just because the student is engaged; it is complete when the "
            "criteria are met or the student has demonstrated mastery of "
            "this step's objective."
        )

        # Build messages from conversation history. Cap to last ~10
        # turns to keep the eval call cheap; the latest turn pair
        # (student_input → tutor_response) is what the verdict mostly
        # rests on, but the evaluator can use prior turns to judge
        # whether teaching content was already delivered.
        history = self.conversation[-10:] if len(self.conversation) > 10 else list(self.conversation)
        # Append the freshly-generated tutor response since it's not
        # yet in self.conversation at this point in the flow.
        eval_messages = list(history)
        if (not eval_messages) or eval_messages[-1].get('role') != 'assistant':
            eval_messages.append({"role": "assistant", "content": tutor_response[:1500]})
        # Final user marker pinning what the evaluator should answer.
        eval_messages.append({
            "role": "user",
            "content": (
                "Based on the conversation above, evaluate the latest "
                "tutor turn against this step's completion criteria. "
                "Return: answer_correct (true|false|null), "
                "step_complete (true|false), reasoning (one sentence)."
            ),
        })

        try:
            create_kwargs = dict(
                response_model=StepEvaluationResult,
                messages=[
                    {"role": "system", "content": eval_system},
                    *eval_messages,
                ],
                max_retries=2,
            )
            if getattr(self, '_instructor_provider', None) == 'google':
                create_kwargs['generation_config'] = {'max_tokens': 1024}
            else:
                # 1024 not 150 — Opus and other extended-thinking models
                # produce verbose reasoning before the tool_use block;
                # 150 truncated mid-reasoning and broke instructor parsing.
                create_kwargs['max_tokens'] = 1024
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

        # Safety valve: max 30 exchanges in remediation
        if self.exchange_count >= 30:
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
        combined_judge_result=None,
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

        # Belt-and-braces against the production bug where Sonnet
        # returns true/false on conceptual non-answers ("yes", "help",
        # "ok"). _build_step_eval_context already returns None on
        # these inputs so the judge skips them — but if a verdict
        # somehow lands, force null here too.
        non_answer = self._is_non_answer_input(student_input)
        if non_answer:
            logger.info(
                "[Eval] forcing is_correct=None — non-answer input=%r",
                (student_input or '')[:40],
            )

        # Edward, 2026-05-07: deterministic check is a FIRST LAYER, not
        # a replacement. We always let the LLM eval run — the
        # deterministic verdict is fed into combined_judge as
        # `deterministic_verdict` and the judge anchors on it for the
        # arithmetic/MCQ axis while still applying broader judgment
        # (equivalent forms, working-with-typo, partial credit). Final
        # is_correct comes from the judge.
        if non_answer:
            # No verdict — student wasn't answering a math question.
            is_correct = None
            eval_layer = 'non_answer_skip'
            eval_reasoning = 'student input is a conceptual non-answer'
        elif combined_judge_result is not None and not getattr(
            combined_judge_result, 'step_eval_skipped', False,
        ):
            # Step eval was folded into the combined judge — use its
            # verdict instead of calling _evaluate_step (saves 1 LLM
            # call per math turn).
            ac = getattr(combined_judge_result, 'answer_correct', None)
            sc = bool(getattr(combined_judge_result, 'step_complete', False))
            reasoning = getattr(combined_judge_result, 'step_eval_reasoning', '') or ''
            # Build a StepEvaluationResult-shape adapter for downstream
            # callers (_should_advance_step expects it).
            step_eval_result = StepEvaluationResult(
                answer_correct=ac,
                step_complete=sc,
                reasoning=reasoning[:280],
            )
            is_correct = ac
            # Source label — when the step_eval judge short-circuited via
            # deterministic verdict (numeric / MCQ), surface that as the
            # eval_layer so telemetry + tests can distinguish deterministic
            # outcomes from LLM verdicts.
            src = getattr(combined_judge_result, 'step_eval_source', '') or ''
            if src.startswith('deterministic'):
                eval_layer = src  # 'deterministic_numeric' | 'deterministic_mcq' | 'deterministic'
            else:
                eval_layer = 'combined_judge'
            eval_reasoning = reasoning
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

        # Bank grader override: when the student replied to a bank-pulled
        # question, the deterministic bank verdict is more authoritative
        # than the LLM step evaluator (which sometimes returns
        # answer_correct=False for a verbatim-correct MCQ letter, e.g.
        # lesson 538 session 40 turn 10: Q3725 'B' graded True by bank
        # but step-eval said False, blocking advancement). Trust the
        # bank for bank-backed turns.
        _bank_grade = getattr(self, '_pending_bank_grade', None)
        _bank_verdict = getattr(_bank_grade, 'is_correct', None) if _bank_grade else None
        if _bank_verdict is True and is_correct is not True:
            logger.info(
                "[StepEval] BANK_OVERRIDE: is_correct=%s → True "
                "(bank grader said True, eval_layer=%s)",
                is_correct, eval_layer,
            )
            is_correct = True
            eval_layer = 'bank_grader_override'
        elif _bank_verdict is False and is_correct is not False:
            logger.info(
                "[StepEval] BANK_OVERRIDE: is_correct=%s → False "
                "(bank grader said False, eval_layer=%s)",
                is_correct, eval_layer,
            )
            is_correct = False
            eval_layer = 'bank_grader_override'

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

        # First-try-correct counter (separate from consecutive_correct_streak
        # which counts any correct verdict). "First try" = correct AND the
        # awaiting_answer record showed 0 wrong attempts on this Q. Used by
        # the auto-difficulty bumper below.
        if verdict_correct:
            _aa_for_first_try = getattr(self, '_awaiting_answer', None) or {}
            _prior_wrongs = int(_aa_for_first_try.get('wrong_attempts', 0) or 0)
            # _awaiting_answer is updated AFTER grading by the bank/chat
            # grader to either clear (on correct) or increment (on wrong).
            # On a correct verdict we read the count BEFORE that update,
            # so 0 here means truly first-try.
            if _prior_wrongs == 0:
                self.consecutive_first_try_correct = getattr(
                    self, 'consecutive_first_try_correct', 0,
                ) + 1
            else:
                # Correct, but only after wrong attempts — streak resets.
                self.consecutive_first_try_correct = 0
        elif verdict_wrong:
            self.consecutive_first_try_correct = 0

        # Auto-difficulty adjustment (2026-05-17 pilot directive).
        # Personalize per-student per-lesson without manual button mash:
        #   - 2 consecutive wrong answers → drop to easy (-1) if not lower
        #   - 2 consecutive first-try-correct → bump to hard (+1) if not higher
        # Manual Too hard? / Too easy? buttons still override and clamp.
        # Once the adjustment fires, reset the counter so we don't bump
        # repeatedly on the same streak (re-arm only after the opposite
        # outcome breaks the streak).
        if (
            verdict_wrong
            and getattr(self, 'consecutive_wrong', 0) >= 2
            and int(getattr(self, 'difficulty_level', 0) or 0) > -1
        ):
            _prev = self.difficulty_level
            self.difficulty_level = -1
            self.consecutive_wrong = 0  # re-arm
            logger.info(
                "[AutoDifficulty] session=%s dropping difficulty %+d → -1 "
                "(2 consecutive wrong)",
                self.session.id, _prev,
            )
        elif (
            verdict_correct
            and getattr(self, 'consecutive_first_try_correct', 0) >= 2
            and int(getattr(self, 'difficulty_level', 0) or 0) < 1
        ):
            _prev = self.difficulty_level
            self.difficulty_level = 1
            self.consecutive_first_try_correct = 0  # re-arm
            logger.info(
                "[AutoDifficulty] session=%s bumping difficulty %+d → +1 "
                "(2 consecutive first-try-correct)",
                self.session.id, _prev,
            )

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
            # Track whether is_correct came from a deterministic source.
            # The fast-path advances on is_correct=True ONLY when it's
            # deterministic (the student's reply matches expected_answer
            # exactly). LLM-judged is_correct could be a sub-step
            # working line — we don't want to advance prematurely.
            #
            # A bank grader verdict (mid-lesson MCQ / FIB / etc) ALSO
            # counts as deterministic: the bank entry has a verified
            # correct_answer, the student's reply matched it (either
            # directly or via the MCQ LLM fallback against the option
            # text). Without this, 'teach' steps with a bank
            # comprehension check sat indefinitely because the
            # LLM step evaluator kept returning step_complete=False
            # ("teaching content not fully delivered" — pilot
            # 2026-05-16, lesson 538 session 38, step 0 stuck for
            # 5+ correct bank answers).
            bank_grade_now = getattr(self, '_pending_bank_grade', None)
            bank_correct_now = (
                bank_grade_now is not None
                and getattr(bank_grade_now, 'is_correct', None) is True
            )
            is_correct_was_deterministic = (
                eval_layer == 'deterministic_numeric'
                or bank_correct_now
            )
            should_advance = self._should_advance_step(
                student_input, tutor_response, is_correct, step_eval_result,
                is_correct_was_deterministic=is_correct_was_deterministic,
            )
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

            # When the step advances, any pending bank-Q awaiting answer
            # is now stale — the engine has moved past it (either via
            # correct answer which already cleared, or via reveal-on-
            # threshold which doesn't clear). Without this clear, the
            # next student reply gets graded against the OLD question
            # via the bank-link-preservation fallback (task #171), even
            # though the tutor has authored a new MCQ on this turn.
            # Pilot 2026-05-17 lesson 540 session 48 turn 847: 'C' to
            # scale Q graded against legend-evaluate Q3113 (correct=B).
            if getattr(self, '_step_just_advanced', False):
                _aa = getattr(self, '_awaiting_answer', None) or {}
                if _aa and int(_aa.get('wrong_attempts', 0) or 0) > 0:
                    logger.info(
                        "[ClearAwaiting] step advanced — clearing stale "
                        "awaiting_answer (kind=%s id=%s wrong=%d)",
                        _aa.get('kind'), _aa.get('question_id'),
                        int(_aa.get('wrong_attempts', 0) or 0),
                    )
                    self._clear_awaiting_answer()

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

        # Layer S — persist working-analysis state to metadata so the
        # teacher monitor can render chips (state, step count,
        # first-error pointer, propagation set). Independent of
        # whether the math_check fired — Layer S runs on every math
        # practice/quiz/worked_example turn.
        wa = getattr(self, '_pending_working_analysis', None)
        if wa is not None:
            metadata['working_state'] = wa.state.value
            metadata['working_steps_count'] = len(wa.steps)
            metadata['working_first_error_idx'] = wa.first_error_idx
            metadata['working_propagated_idxs'] = sorted(wa.propagated_idxs)
            if wa.final_claim is not None:
                metadata['working_final_claim'] = wa.final_claim
            if wa.expected_answer is not None:
                metadata['working_expected'] = wa.expected_answer
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
                # See _evaluate_step note — Opus needs headroom for the
                # tool_use reasoning block; 50 was Sonnet-tuned.
                create_kwargs['max_tokens'] = 1024
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

    def _should_advance_step(
        self,
        student_input: str,
        tutor_response: str,
        is_correct: bool,
        eval_result=None,
        *,
        is_correct_was_deterministic: bool = False,
    ) -> bool:
        """Determine if the current step is complete.

        Safety valves (hard rules, not LLM):
        | Rule                              | Threshold                              |
        |-----------------------------------|----------------------------------------|
        | Per-step-type hard cap            | 10 exchanges (all step types)          |
        | Min exchanges before eval         | teach / worked_example: 2 ; others: 1  |
        | Deterministic-correct fast-path   | practice/quiz + is_correct=True from deterministic check |
        | step_complete fast-path           | LLM judge says step_complete=True      |

        2026-05-06: caps reduced to a uniform 10 across all step types.
        Production showed practice/quiz at 30 → step_complete null →
        exit ticket never triggered. 10 forces advancement so sessions
        complete in finite time even when the judge returns no signal.

        2026-05-05 (later): tightened the correct-answer fast-path.
        Originally fired on ANY is_correct=True, which could advance
        practice steps prematurely when the LLM judge marks a correct
        SUB-STEP ("first I subtract 5 → 3x = 15") as answer_correct=True.
        Now: fast-path on correct only fires when the verdict is
        DETERMINISTIC (student's reply matches expected_answer exactly).
        For LLM-judged correctness on partial work, defer to
        step_complete — the LLM is in a better position to judge
        whether the WHOLE problem was solved.
        """
        if self.current_topic_index >= len(self.steps):
            return False

        step = self.steps[self.current_topic_index]
        step_type = step.step_type or 'teach'
        exchanges = self.step_exchange_count

        # 1. Per-step-type hard cap. Forces advance regardless of LLM
        # verdict so a stuck judge / stuck student can't trap the
        # session indefinitely on one step.
        hard_cap = self._STEP_HARD_CAP_EXCHANGES.get(
            step_type, self._STEP_HARD_CAP_DEFAULT,
        )
        if exchanges >= hard_cap:
            logger.info(
                "[StepAdvance] HARD_CAP step=%d type=%s exchanges=%d >= cap=%d → advance",
                self.current_topic_index, step_type, exchanges, hard_cap,
            )
            return True

        # 1.5 — Reveal-on-threshold advance (2026-05-17). If the bank
        # Q on the current step reached the difficulty-tiered reveal
        # threshold AND the just-graded verdict was wrong, the tutor
        # has revealed the canonical answer — the step is "done" via
        # reveal, not via a correct verdict. Without this, the step
        # gets stuck because is_correct=False can't trip the
        # deterministic fast-path, and the LLM step_complete judge
        # tends to stay False after a wrong verdict.
        # Pilot 2026-05-17 lesson 540 session 49: step 0 stuck after
        # reveal on the population-density Q; tutor authored a new
        # MCQ inline but engine didn't advance, so the next reply
        # routed back to qid=3103 via the bank-link fallback.
        _aa_advance = getattr(self, '_awaiting_answer', None) or {}
        _wrong_attempts_advance = int(_aa_advance.get('wrong_attempts', 0) or 0)
        _reveal_at = self._reveal_threshold()
        if (
            is_correct is False
            and _wrong_attempts_advance >= _reveal_at
        ):
            logger.info(
                "[StepAdvance] REVEAL_ON_THRESHOLD step=%d type=%s "
                "wrong_attempts=%d >= reveal_threshold=%d → advance",
                self.current_topic_index, step_type,
                _wrong_attempts_advance, _reveal_at,
            )
            return True

        # 2. Min exchange floor before any advancement decision fires.
        min_exchanges = self._STEP_EVAL_MIN_EXCHANGES.get(step_type, 1)
        if exchanges < min_exchanges:
            return False

        # 3. Deterministic-correct fast-path. Fires when the is_correct
        # verdict came from a deterministic source:
        #   - numeric expected_answer match (practice/quiz)
        #   - bank grader verdict (any step type — MCQ/FIB/etc.)
        # For practice/quiz: final answer = step done (original rule).
        # For teach/worked_example/summary: a CORRECT bank-backed
        # comprehension check is the step's evaluation criterion — once
        # met (and the min exchange floor is past), advance instead of
        # waiting for the LLM step-eval to say "teaching content
        # delivered" (which can stall the step indefinitely — pilot
        # 2026-05-16 lesson 538 step 0 stuck for 5+ correct answers).
        if (
            is_correct is True
            and is_correct_was_deterministic
        ):
            logger.info(
                "[StepAdvance] DETERMINISTIC_CORRECT step=%d type=%s → advance",
                self.current_topic_index, step_type,
            )
            return True

        # 4. Use pre-computed eval result. When math turns flow through
        # combined_judge, the step verdict is already computed in CHECK
        # 4 of that single call — eval_result is the adapter built by
        # _analyze_student_response and we DO NOT make a second
        # instructor _evaluate_step call. Fallback to instructor only
        # for non-math sessions or when combined_judge skipped step
        # eval (below the min-exchange floor).
        if eval_result is None:
            eval_result = self._evaluate_step(student_input, tutor_response)
        if eval_result is not None:
            sc = bool(getattr(eval_result, 'step_complete', False))
            if sc:
                logger.info(
                    "[StepAdvance] LLM_VERDICT step=%d type=%s step_complete=True → advance",
                    self.current_topic_index, step_type,
                )
            return sc

        # 6. Evaluator failed (network / parse error). Don't force
        # advancement — wait for the 30-exchange hard cap above
        # OR for the evaluator to recover on the next turn.
        # The old per-step-type fallback rules (3/4/1/2 exchanges)
        # were removed 2026-05-01 because they fired earlier than
        # the hard cap and contradicted the "stay until they get it"
        # principle.
        logger.info(
            f"Evaluator failed for step {self.current_topic_index} "
            f"({step_type}); deferring to hard cap"
        )
        return False

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
                # See _evaluate_step note — Opus needs headroom for the
                # tool_use reasoning block; 100 was Sonnet-tuned.
                create_kwargs['max_tokens'] = 1024
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
        passing = self._exit_ticket_passing_score()
        total = len(exit_questions)
        # Explicit "this is the exit ticket" framing so the student
        # knows the conversation has moved out of tutoring and into
        # assessment. Soft "let's check your understanding" wasn't
        # signalling the mode change clearly enough (pilot 2026-05-12).
        content = (
            "📋 **Exit Ticket** — time to check what you've learned.\n\n"
            f"You'll see **{total} question{'s' if total != 1 else ''}** "
            f"one at a time. You need **{passing}/{total}** correct to "
            "pass. Take your time — there's no time pressure on the "
            "ticket itself."
        )
        return TutorMessage(
            content=content,
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
        was_mastered = progress.mastery_level == 'mastered'
        progress.mastery_level = 'mastered'
        progress.save()

        # Permanent transcript entry — see record_lesson docstring. The
        # idempotency guard means re-completing a lesson is a no-op.
        if not was_mastered:
            from apps.tutoring.skills_models import StudentCompetencyRecord
            StudentCompetencyRecord.record_lesson(progress, session=self.session)

        return TutorMessage(
            content=f"🎉 Congratulations! You've completed {self.lesson.title}! You showed great understanding. Keep up the excellent work!",
            phase="completed",
            is_complete=True,
        )
    
    # ------------------------------------------------------------------
    # Exit-ticket grading helpers (split: deterministic vs LLM-batch)
    # ------------------------------------------------------------------

    def _grade_mcq_deterministic(self, question, student_answer) -> bool:
        """Pure letter compare for MCQ. No LLM."""
        ans = student_answer if isinstance(student_answer, str) else str(student_answer or '')
        return ans.upper().strip() == (question.correct_answer or '').upper().strip()

    def _try_numeric_fast_path(
        self, question, student_answer,
    ) -> Optional[bool]:
        """Numeric tolerance grading for fill_in_blank with numeric blanks.
        Returns True/False when the verdict is unambiguous, or None to
        defer to the LLM batch."""
        q_type = getattr(question, 'question_type', 'mcq') or 'mcq'
        if q_type != 'fill_in_blank':
            return None
        is_math = self.lesson.unit.course.is_math if self.lesson.unit and self.lesson.unit.course else False
        if not is_math:
            return None
        data = question.answer_data or {}
        blanks = data.get('blanks', []) or []
        if not blanks:
            return None
        student_blanks = student_answer if isinstance(student_answer, list) else [student_answer]
        from apps.tutoring.math_tools import safe_eval_expression
        correct_count = 0
        evaluated_count = 0
        for idx, expected in enumerate(blanks):
            given = str(student_blanks[idx] if idx < len(student_blanks) else '').strip()
            expected_val = safe_eval_expression(str(expected))
            given_val = safe_eval_expression(given)
            if expected_val is not None and given_val is not None:
                evaluated_count += 1
                if abs(expected_val - given_val) < 0.01:
                    correct_count += 1
                continue
            # Plain string match fallback (case-insensitive)
            if given.lower() == str(expected).lower():
                correct_count += 1
                evaluated_count += 1
        if evaluated_count == 0:
            return None  # nothing parseable; defer to LLM
        threshold = max(1, len(blanks) // 2 + 1)
        if correct_count >= threshold:
            return True
        if correct_count == 0:
            return False
        return None  # partial — let LLM decide

    def _build_batch_grade_item(
        self, index: int, question, student_answer,
    ):
        """Serialise one exit-ticket question into a BatchGradeItem.

        Thin wrapper around the top-level
        `exit_ticket_grader.build_batch_grade_item` so mid-lesson
        artifact grading (bank_grader) can reuse the exact same
        builder. The only thing this wrapper adds is the lesson-aware
        is_math flag.
        """
        from apps.tutoring.exit_ticket_grader import build_batch_grade_item
        is_math = (
            self.lesson.unit.course.is_math
            if self.lesson.unit and self.lesson.unit.course
            else False
        )
        return build_batch_grade_item(
            index=index,
            question=question,
            student_answer=student_answer,
            is_math=is_math,
        )

    def _grade_exit_question(self, question, student_answer) -> bool:
        """Per-question grader entry point.

        Lookup order:
          1. _exit_ticket_batch_cache (set by _submit_exit_ticket_inner
             after running the batched LLM grader). Returns the cached
             verdict for LLM-graded questions in O(1) — no extra call.
          2. MCQ → deterministic letter match.
          3. Numeric fast-path for fill_in_blank with numeric blanks.
          4. Per-question LLM grading (legacy / fallback path used by
             remediation walkthrough or when the batch was skipped).

        Tests can still patch this method; the patch overrides every
        path including the cache.
        """
        # 1. Batch cache check (set during exit-ticket submission).
        # Cache values are BatchGradeResult; pull the boolean verdict
        # off `.correct`. Older code paths stored just the bool, so
        # tolerate that shape too.
        cache = getattr(self, '_exit_ticket_batch_cache', None)
        if cache and getattr(question, 'id', None) in cache:
            cached = cache[question.id]
            return bool(getattr(cached, 'correct', cached))

        q_type = getattr(question, 'question_type', 'mcq') or 'mcq'
        data = question.answer_data or {}

        # Safety: if student_answer is a list/dict, it's NOT an MCQ answer
        if isinstance(student_answer, (list, dict)):
            if q_type == 'mcq':
                q_type = 'fill_in_blank' if isinstance(student_answer, list) else 'matching'

        # 2. MCQ deterministic
        if q_type == 'mcq':
            return self._grade_mcq_deterministic(question, student_answer)

        # 3. Numeric fast-path
        fast = self._try_numeric_fast_path(question, student_answer)
        if fast is not None:
            return fast

        # 4. Legacy per-question LLM grading
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

        # short_answer / data_interpretation fallback. Math-symbol
        # normalise BOTH sides so "38°" and "38" are equivalent —
        # students rarely type Unicode glyphs from a phone keyboard.
        # See apps/tutoring/summative_grading.py::_math_norm for the
        # canonical implementation.
        from apps.tutoring.summative_grading import _math_norm
        keywords = [_math_norm(kw) for kw in (data.get('keywords') or [])]
        min_kw = data.get('min_keywords', 2)
        text = _math_norm(student_answer if isinstance(student_answer, str) else '')
        # Numeric-aware match: pure numbers extracted from the student
        # answer are compared with tolerance against numeric keywords;
        # text keywords use substring match.
        import re as _re
        student_numbers = _re.findall(r'-?\d+(?:\.\d+)?', text)
        matched = 0
        for kw in keywords:
            if not kw:
                continue
            if _re.fullmatch(r'-?\d+(?:\.\d+)?', kw):
                target = float(kw)
                if any(abs(float(sn) - target) < 0.01 for sn in student_numbers):
                    matched += 1
            elif kw in text:
                matched += 1
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

        # Pre-grade pass: identify LLM-needing questions and grade
        # them all in ONE batched LLM call. Deterministic types (MCQ,
        # numeric fill_in_blank) are NOT included in the batch — they
        # get scored by _grade_exit_question's fast-path during the
        # main loop. Results land on _exit_ticket_batch_cache keyed
        # by question.id so the per-question loop can read them
        # without re-calling the LLM.
        #
        # The main loop still calls _grade_exit_question once per
        # question — preserves the test seam (tests patch that
        # method), and _grade_exit_question reads the cache before
        # falling through to its legacy per-question path.
        student_answers: Dict[int, object] = {}
        batch_items: List = []
        for i, q in enumerate(questions):
            raw_answer = answers[i] if i < len(answers) else ''
            if isinstance(raw_answer, dict) and 'answer' in raw_answer:
                student_answer = raw_answer['answer']
            elif isinstance(raw_answer, dict) and 'type' not in raw_answer and len(raw_answer) > 0:
                student_answer = raw_answer
            else:
                student_answer = raw_answer
            student_answers[i] = student_answer

            q_type = getattr(q, 'question_type', 'mcq') or 'mcq'
            if isinstance(student_answer, (list, dict)) and q_type == 'mcq':
                q_type = 'fill_in_blank' if isinstance(student_answer, list) else 'matching'

            if q_type == 'mcq':
                continue  # deterministic — handled in the loop
            if self._try_numeric_fast_path(q, student_answer) is not None:
                continue  # numeric fast-path — handled in the loop
            batch_items.append(self._build_batch_grade_item(i, q, student_answer))

        # Run the batch (if any items need it). Cache the full
        # BatchGradeResult per question.id so downstream callers can
        # pull both the boolean verdict AND any per-blank breakdown
        # (fill_in_blank items carry .blanks: List[BlankVerdict]).
        from apps.tutoring.exit_ticket_grader import (
            BatchGradeResult, grade_written_responses_batch,
        )
        self._exit_ticket_batch_cache: Dict[int, BatchGradeResult] = {}
        if batch_items:
            batch_results = grade_written_responses_batch(
                batch_items, llm_client=self.judge_client,
            )
            for r in batch_results:
                # Map index → question.id so _grade_exit_question can
                # look it up by question identity.
                qid = questions[r.index].id if 0 <= r.index < len(questions) else None
                if qid is not None:
                    self._exit_ticket_batch_cache[qid] = r

        correct = 0
        results = []
        failed_questions = []

        for i, q in enumerate(questions):
            student_answer = student_answers.get(i, '')
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

            # Track tags for competency reporting + remediation
            # targeting. concept_tag is the BROAD learning-objective
            # grouping; enabling_objective is the NARROW sub-objective
            # remediation actually targets.
            eo_tag = getattr(q, 'concept_tag', '') or ''
            sub_eo = getattr(q, 'enabling_objective', '') or ''

            # Pull per-element verdicts out of the batch cache so the
            # frontend can colour each blank/pair individually AND the
            # tutor's remediation directive can target the specific
            # element that failed (not re-explain the whole question).
            # Same shape works for fill_in_blank (per blank) and
            # matching (per pair); we expose with q_type-specific
            # payload keys for clarity downstream.
            blanks_correct: list = []
            blanks_reasoning: list = []
            pairs_correct: list = []
            pairs_reasoning: list = []
            cached = (self._exit_ticket_batch_cache or {}).get(q.id)
            cached_parts = getattr(cached, 'parts', []) if cached else []
            if cached_parts:
                q_type_now = getattr(q, 'question_type', 'mcq') or 'mcq'
                if q_type_now == 'fill_in_blank':
                    blanks_correct = [bool(b.is_correct) for b in cached_parts]
                    blanks_reasoning = [str(b.reasoning) for b in cached_parts]
                elif q_type_now == 'matching':
                    pairs_correct = [bool(b.is_correct) for b in cached_parts]
                    pairs_reasoning = [str(b.reasoning) for b in cached_parts]

            if is_correct:
                correct += 1
            else:
                q_type = getattr(q, 'question_type', 'mcq') or 'mcq'
                failed_questions.append({
                    'id': q.id,
                    'index': i,
                    'question': q.question_text,
                    'concept_tag': eo_tag,
                    'enabling_objective': sub_eo,
                    'student_answer': student_answer,
                    'correct_answer': q.correct_answer if q_type == 'mcq' else str(q.answer_data or {}),
                    'correct_text': getattr(q, f'option_{(q.correct_answer or "a").lower()}', '') if q_type == 'mcq' else '',
                    'explanation': q.explanation,
                    # fill_in_blank only — empty list otherwise.
                    'blanks_correct': blanks_correct,
                    'blanks_reasoning': blanks_reasoning,
                    # matching only — empty list otherwise.
                    'pairs_correct': pairs_correct,
                    'pairs_reasoning': pairs_reasoning,
                })

            results.append({
                'index': i,
                'question': q.question_text,
                'question_type': getattr(q, 'question_type', 'mcq') or 'mcq',
                'concept_tag': eo_tag,
                'enabling_objective': sub_eo,
                'selected': student_answer,
                'correct_answer': q.correct_answer if (getattr(q, 'question_type', 'mcq') or 'mcq') == 'mcq' else q.answer_data,
                'is_correct': is_correct,
                'explanation': q.explanation,
                # Surfaced to the frontend so the exit-ticket review
                # modal can colour each blank/pair individually instead
                # of painting them all the same colour as the question.
                'blanks_correct': blanks_correct,
                'blanks_reasoning': blanks_reasoning,
                'pairs_correct': pairs_correct,
                'pairs_reasoning': pairs_reasoning,
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
            # ``best_score`` is now LATEST score, not historical best.
            # Competency reads should reflect where the student is RIGHT
            # NOW, not where they peaked. Permanent transcript
            # (StudentCompetencyRecord) preserves the high-water mark
            # for longitudinal reporting; this field tracks current
            # state for the catalog / dashboard.
            progress.best_score = score_pct
            progress.attempts_count = (progress.attempts_count or 0) + 1
            progress.last_attempt_at = now
            progress.last_completion_session = self.session
            progress.last_completion_was_group = was_group
            newly_mastered = False
            if passed:
                if progress.mastery_level != 'mastered':
                    progress.mastery_level = 'mastered'
                    newly_mastered = True
            else:
                # Failed this attempt → demote mastery so the catalog
                # shows the student as needing more work. Was promote-
                # only; per the 2026-05 reset, mastery reflects current
                # competency, not ever-achieved status.
                if progress.attempts_count > 0:
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

            # Permanent transcript entry — survives course re-parse /
            # deletion via SET_NULL + snapshot fields. Best-effort; the
            # helper swallows its own failures so live grading is never
            # blocked. See memory/student_competency_persistence_plan.md.
            if newly_mastered:
                from apps.tutoring.skills_models import StudentCompetencyRecord
                StudentCompetencyRecord.record_lesson(progress, session=self.session)

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
                # Refresh denormalized skills snapshot on StudentProfile
                # so the tutor + recommendation engine see the new data.
                try:
                    from apps.tutoring.competency_tracker import refresh_student_snapshot
                    course = self.lesson.unit.course if self.lesson.unit else None
                    if course:
                        for participant in participants:
                            refresh_student_snapshot(participant, course)
                except Exception as e:
                    logger.warning(f"snapshot refresh failed after exit ticket: {e}")
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
    
    def _build_eo_competency_map(
        self, results: List[Dict],
    ) -> Dict[str, Dict]:
        """Build a per-EO competency map from exit-ticket results.

        For each enabling_objective the questions tested, count
        how many were asked, how many the student got right, and
        which question ids they failed. Used by the remediation
        flow to name EOs got/missed and queue failed questions
        in lesson-EO order. See P4 of
        memory/curriculum_tutor_v2_plan.md.

        Returns:
          {
            "<eo text>": {
              "asked": int, "correct": int,
              "failed_question_ids": [int, ...],
              "is_mastered": bool,  # all asked == correct
            },
            ...
          }
        """
        eo_map: Dict[str, Dict] = {}
        for r in results:
            # Prefer the SPECIFIC enabling_objective; fall back to
            # the broader concept_tag for older content where the
            # sub-objective wasn't populated.
            eo = (r.get('enabling_objective') or r.get('concept_tag') or '').strip()
            if not eo:
                continue
            bucket = eo_map.setdefault(eo, {
                'asked': 0, 'correct': 0, 'failed_question_ids': [],
            })
            bucket['asked'] += 1
            if r.get('is_correct'):
                bucket['correct'] += 1
            else:
                qid = r.get('question_id') or r.get('index')
                if qid is not None:
                    bucket['failed_question_ids'].append(qid)
        for eo, b in eo_map.items():
            b['is_mastered'] = (b['asked'] > 0 and b['correct'] == b['asked'])
        return eo_map

    def _ordered_failed_questions(
        self,
        failed_questions: List[Dict],
        eo_map: Dict[str, Dict],
    ) -> List[Dict]:
        """Order failed questions for the remediation walkthrough.

        Per the v2 plan locked decision: walk in LESSON-EO order
        (the order EOs appear on the lesson, not ad-hoc grouping).
        For each EO in lesson order, walk through ALL failed questions
        tagged to that EO before moving to the next EO.
        """
        lesson_eos = list(self.lesson.enabling_objectives or [])
        # Bucket failed_questions by EO for lookup
        by_eo: Dict[str, List[Dict]] = {}
        for fq in failed_questions:
            eo = (fq.get('enabling_objective') or fq.get('concept_tag') or '').strip()
            by_eo.setdefault(eo, []).append(fq)

        ordered: List[Dict] = []
        seen_ids = set()
        # First pass — iterate lesson EOs in order
        for lesson_eo in lesson_eos:
            for fq in by_eo.get(lesson_eo, []):
                fid = fq.get('id')
                if fid not in seen_ids:
                    ordered.append(fq)
                    seen_ids.add(fid)
        # Second pass — append any failed questions whose EO didn't
        # match a lesson EO (drift, untagged) at the end so they don't
        # get dropped from the walkthrough.
        for fq in failed_questions:
            fid = fq.get('id')
            if fid not in seen_ids:
                ordered.append(fq)
                seen_ids.add(fid)
        return ordered

    def _start_remediation(
        self,
        results: List[Dict],
        score: int,
        failed_questions: List[Dict]
    ) -> TutorMessage:
        """
        Start targeted remediation based on failed ENABLING OBJECTIVES.

        Builds a per-EO competency map (got vs missed) and queues the
        failed questions in lesson-EO order. The remediation walkthrough
        then steps through every failed question, using the bank's
        canonical explanation as scaffolding. See P4 of
        memory/curriculum_tutor_v2_plan.md.
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

        # Record ExitTicketAttempt per active participant (G3: group-aware).
        # Persist the EO competency map under answers['eo_competency']
        # so the dashboard + future sampling can read it (P4).
        eo_competency_for_attempt = self._build_eo_competency_map(results)
        try:
            from apps.tutoring.models import ExitTicket, ExitTicketAttempt
            exit_ticket = ExitTicket.objects.filter(lesson=self.lesson).first()
            if exit_ticket:
                per_question = []
                for r in results:
                    per_question.append({
                        'concept_tag': r.get('concept_tag', ''),
                        'enabling_objective': r.get('enabling_objective', ''),
                        'correct': r.get('is_correct', False),
                        'selected': r.get('selected', ''),
                        'question_type': r.get('question_type', 'mcq'),
                    })
                answer_data = {
                    'per_question': per_question,
                    'eo_competency': {
                        eo: {
                            'asked': b['asked'],
                            'correct': b['correct'],
                            'failed_question_ids': b['failed_question_ids'],
                            'is_mastered': b['is_mastered'],
                        }
                        for eo, b in eo_competency_for_attempt.items()
                    },
                }
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

        # Extract failed enabling objectives. Prefer the question's
        # specific `enabling_objective` (sub-objective) — that's what
        # remediation should target. Fall back to `concept_tag` for
        # older questions where the field hasn't been backfilled, then
        # to the question text as a last resort.
        failed_eos = set()
        from apps.tutoring.models import ExitTicketQuestion
        for fq in failed_questions:
            eo = (fq.get('enabling_objective') or '').strip()
            if eo:
                failed_eos.add(eo)
                continue
            # Re-fetch from DB if the dict didn't carry it
            q = ExitTicketQuestion.objects.filter(id=fq.get('id')).first() if fq.get('id') else None
            if q and (q.enabling_objective or '').strip():
                failed_eos.add(q.enabling_objective.strip())
                continue
            # Fallback to concept_tag
            tag = (fq.get('concept_tag') or '').strip()
            if tag:
                failed_eos.add(tag)
                continue
            if q and q.concept_tag:
                failed_eos.add(q.concept_tag.strip())

        # Last resort: use question text
        if not failed_eos:
            failed_eos = {fq.get('question', '')[:100] for fq in failed_questions[:5]}

        self.failed_exit_questions = failed_questions
        self._failed_eos = list(failed_eos)

        # P4 — build per-EO competency map + ordered failed-question
        # queue. Persist to engine_state so the walkthrough can drive
        # the next-question selection across turns. Also persist the
        # competency map to ExitTicketAttempt.answers so the dashboard
        # + future sampling can read it. See v2 plan items 4 + 6.
        eo_competency = self._build_eo_competency_map(results)
        ordered_failed = self._ordered_failed_questions(failed_questions, eo_competency)

        state = self.session.engine_state or {}
        state['remediation_eo_competency'] = {
            eo: {
                'asked': b['asked'],
                'correct': b['correct'],
                'failed_question_ids': b['failed_question_ids'],
                'is_mastered': b['is_mastered'],
            }
            for eo, b in eo_competency.items()
        }
        state['remediation_walkthrough_queue'] = [
            {'id': fq.get('id'), 'eo': (fq.get('enabling_objective') or fq.get('concept_tag') or '')[:200]}
            for fq in ordered_failed
        ]
        state['remediation_walkthrough_index'] = 0
        state['remediation_phase'] = 'walkthrough'
        self.session.engine_state = state

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

        # Generate EO-focused remediation message. The opener returns
        # turn_metadata carrying bank_question_ref for the first
        # walkthrough question, so the next student reply is graded
        # by the bank grader (P3) and _maybe_advance_walkthrough fires.
        message, turn_metadata = self._generate_remediation_opening(
            score, len(results), failed_questions,
        )

        # Save the message + bank ref
        self._save_turn("tutor", message, metadata=turn_metadata)
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
    ) -> Tuple[str, Dict]:
        """Build the remediation opening + pose the first failed question.

        DETERMINISTIC (no LLM call). Per pilot directive 2026-05-12:
        "the remediation should start with a recap of the exit ticket.
        It should state how many questions the student got correct
        and got wrong and list the questions one after the other for
        us to work on."

        Structure:
          1. Header: "Exit ticket review" + clear mode signal.
          2. Score line: "X of Y correct. Z to work through together."
          3. Numbered list of failed question stems (the "agenda").
          4. First failed question rendered verbatim from the bank.

        Returns (message, turn_metadata). The metadata carries
        ``bank_question_ref`` for the first walkthrough question so
        the next student reply is graded against it by the bank
        grader (P3 plumbing).
        """
        from apps.tutoring.models import ExitTicketQuestion
        from apps.tutoring.question_bank import render_question_to_prose

        state = self.session.engine_state or {}
        walkthrough_queue = state.get('remediation_walkthrough_queue') or []

        # Resolve the actual question stems in queue order so we can
        # show the student which problems we're about to work through.
        queue_ids = [q.get('id') for q in walkthrough_queue if q.get('id')]
        question_lookup = {
            q.id: q for q in
            ExitTicketQuestion.objects.filter(id__in=queue_ids)
        }
        n_failed = len(queue_ids)
        n_correct = max(0, total - n_failed)

        # Build the recap header (deterministic, no LLM).
        lines: List[str] = []
        lines.append("📋 **Exit ticket review**")
        lines.append("")
        if n_failed == 0:
            # Edge case — somehow we're in remediation with no failed
            # queue. Fall back to a simple message.
            lines.append(
                f"You scored {score} out of {total}. Let's revisit "
                "anything you'd like to lock in."
            )
            return "\n".join(lines), {}

        lines.append(
            f"You scored **{score} of {total}** "
            f"({n_correct} right, **{n_failed} to revisit**). "
            "We'll walk through each missed question one at a time — "
            "this is the fastest way to fix the gap."
        )
        lines.append("")
        lines.append(f"**Questions we'll work on ({n_failed}):**")
        for idx, qid in enumerate(queue_ids[:10], start=1):
            q = question_lookup.get(qid)
            if not q:
                continue
            stem = (q.question_text or '').strip()
            # One line per question: number + first 140 chars of stem
            preview = stem[:140] + ('…' if len(stem) > 140 else '')
            lines.append(f"{idx}. {preview}")
        if n_failed > 10:
            lines.append(f"…and {n_failed - 10} more.")
        lines.append("")
        lines.append("Let's start with question 1:")

        # Pose the first failed question verbatim from the bank.
        first_question = question_lookup.get(queue_ids[0]) if queue_ids else None
        turn_metadata: Dict = {}
        if first_question is not None:
            rendered = render_question_to_prose(first_question)
            if rendered:
                turn_metadata['bank_question_ref'] = {
                    'kind': 'exit_ticket_question',
                    'id': first_question.id,
                    'question_type': first_question.question_type or 'mcq',
                }
                lines.append("")
                lines.append(f"**Question 1 of {n_failed}:**")
                lines.append("")
                lines.append(rendered)

        return "\n".join(lines), turn_metadata

    def _exit_ticket_passing_score(self) -> int:
        """Lookup the lesson's exit-ticket passing score, with a
        sensible fallback when no ExitTicket exists."""
        from apps.tutoring.models import ExitTicket
        et = ExitTicket.objects.filter(lesson=self.lesson).first()
        return et.passing_score if et else 8

    def _maybe_advance_walkthrough(
        self, clean_response: str, turn_metadata: Dict,
    ) -> str:
        """Drive the remediation walkthrough forward.

        On each turn: if the student just answered the previously-posed
        bank question (``_pending_bank_grade`` is set), advance the
        walkthrough queue. When the walkthrough ends, hand off to a
        FRESH exit ticket (requiz phase was removed 2026-05-12).

        ``turn_metadata['bank_question_ref']`` is set whenever a new
        question is posed so the next student turn grades against it.
        """
        if not getattr(self, 'is_remediation', False):
            return clean_response
        state = self.session.engine_state or {}
        phase = state.get('remediation_phase')
        # 'walkthrough' is the only active phase. 'requiz' may appear
        # on legacy in-flight sessions from before the requiz removal
        # — drain them straight through _finish_remediation.
        if phase not in ('walkthrough', 'requiz'):
            return clean_response
        bank_grade = getattr(self, '_pending_bank_grade', None)
        if bank_grade is None or bank_grade.is_correct is None:
            return clean_response

        if phase == 'requiz':
            # Legacy session: drop straight to the fresh exit ticket.
            return self._finish_remediation(state, clean_response)

        if phase == 'walkthrough':
            queue = list(state.get('remediation_walkthrough_queue') or [])
            current_idx = state.get('remediation_walkthrough_index', 0)
            attempts = int(state.get('walkthrough_attempts_on_current', 0)) + 1

            # A.4 (2026-05-08): retry-then-advance with a cap.
            # Per Edward's spec: tutor must NOT give the answer
            # directly. On wrong, give a hint and let the student
            # retry — up to MAX_RETRIES times per question. After
            # the cap, log the question for later review and move on
            # without revealing the answer.
            MAX_RETRIES = 10
            if not bank_grade.is_correct and attempts < MAX_RETRIES:
                # Stay on the same question — increment retry count
                # and let the tutor LLM (with hint guidance from the
                # system prompt block built in _build_walkthrough_hint_block)
                # ask the student to try again.
                state['walkthrough_attempts_on_current'] = attempts
                self.session.engine_state = state
                self._save_state()
                return clean_response

            # Either correct OR cap exhausted → advance.
            if not bank_grade.is_correct and attempts >= MAX_RETRIES:
                # Cap hit without success — log for teacher review.
                unresolved = state.setdefault(
                    'walkthrough_unresolved_question_ids', [],
                )
                if 0 <= current_idx < len(queue):
                    qid = queue[current_idx].get('id')
                    if qid is not None and qid not in unresolved:
                        unresolved.append(qid)
                logger.info(
                    "[Walkthrough] cap reached on q=%s after %d attempts; advancing",
                    queue[current_idx].get('id') if 0 <= current_idx < len(queue) else '?',
                    attempts,
                )

            # Advance to next question + reset retry counter.
            idx = current_idx + 1
            state['remediation_walkthrough_index'] = idx
            state['walkthrough_attempts_on_current'] = 0

            if idx >= len(queue):
                # Walkthrough complete — hand off straight to a FRESH
                # exit ticket. The requiz phase was removed per pilot
                # 2026-05-12 ("remove the requiz from the remediation.
                # It is actually too long, so let us just focus on what
                # the students got wrong and go back to the exit ticket
                # after the questions have been walked through").
                return self._finish_remediation(state, clean_response)
            return self._pose_next_remediation_question(
                state, queue, idx, label='Question',
                clean_response=clean_response, turn_metadata=turn_metadata,
            )

        # Defensive: legacy sessions may have phase == 'requiz' from
        # before the requiz removal (2026-05-12). Treat as done.
        return self._finish_remediation(state, clean_response)

    def _build_walkthrough_hint_block(self) -> str:
        """A.4 (2026-05-08): when the student is mid-walkthrough and
        just answered the active question wrong, force the tutor LLM
        to give a HINT (not the answer) and invite a retry. Returns
        empty string when not in walkthrough or when the previous
        turn wasn't a wrong answer."""
        if not getattr(self, 'is_remediation', False):
            return ''
        state = self.session.engine_state or {}
        if state.get('remediation_phase') != 'walkthrough':
            return ''
        attempts = int(state.get('walkthrough_attempts_on_current', 0) or 0)
        if attempts <= 0:
            # First attempt on this question, or just advanced —
            # no hint context to inject.
            return ''
        # The retry counter is incremented in _maybe_advance_walkthrough
        # AFTER this turn is generated. So `attempts` here = number
        # of wrong attempts already made on the active question.
        return (
            "\n\n<walkthrough_hint_required>\n"
            "The student is in REMEDIATION WALKTHROUGH on a previously-"
            f"failed exit-ticket question. They have answered wrong "
            f"{attempts} time(s) so far. Hard rules for this turn:\n"
            "  - You MUST NOT give the answer directly.\n"
            "  - You MUST NOT reveal the correct option letter (A/B/C/D)\n"
            "    or the correct numeric answer.\n"
            "  - Give a SHORT (1-2 sentences) explanation of why their\n"
            "    answer was wrong and ONE targeted hint that guides them\n"
            "    toward the correct reasoning.\n"
            "  - End by asking them to try again.\n"
            "  - Be encouraging — this is review, not a test.\n"
            "  - Do NOT pose a NEW question; the platform re-poses the\n"
            "    same one automatically. Just respond with the hint.\n"
            "</walkthrough_hint_required>\n"
        )

    def _pose_next_remediation_question(
        self,
        state: Dict,
        queue: List[Dict],
        idx: int,
        label: str,
        clean_response: str,
        turn_metadata: Dict,
    ) -> str:
        """Render queue[idx] verbatim from the bank, append to the
        tutor response, and record bank_question_ref so the next
        student reply gets graded. Persists ``state`` back to the
        session (caller has already mutated the index)."""
        from apps.tutoring.models import ExitTicketQuestion
        from apps.tutoring.question_bank import render_question_to_prose
        self.session.engine_state = state
        next_id = queue[idx].get('id')
        next_q = ExitTicketQuestion.objects.filter(id=next_id).first()
        if next_q is None:
            return clean_response
        rendered = render_question_to_prose(next_q)
        if not rendered:
            return clean_response
        turn_metadata['bank_question_ref'] = {
            'kind': 'exit_ticket_question',
            'id': next_q.id,
            'question_type': next_q.question_type or 'mcq',
        }
        return (
            clean_response.rstrip()
            + f"\n\n**{label} {idx + 1} of {len(queue)}:**\n\n"
            + rendered
        )

    def _finish_remediation(self, state: Dict, clean_response: str) -> str:
        """Walkthrough done — hand off to a fresh exit ticket.

        Per pilot 2026-05-12, the requiz phase was removed. After the
        student walks through every failed exit-ticket question, we go
        straight back to the exit ticket with a FRESH question sample
        (the sampler excludes any IDs already posed during tutoring +
        the prior failed attempt) — the new attempt is the evaluation.

        This method:
          - Clears remediation engine_state keys
          - Resamples the in-memory ``exit_ticket_concepts`` list
          - Sets ``session_state = EXIT_TICKET`` + ``is_remediation = False``
          - Returns a closing message + exit-ticket framing
        """
        # Clear remediation state.
        state['remediation_phase'] = 'done'
        state.pop('remediation_walkthrough_queue', None)
        state.pop('remediation_walkthrough_index', None)
        state.pop('remediation_requiz_queue', None)
        state.pop('remediation_requiz_index', None)
        state.pop('remediation_requiz_results', None)
        state.pop('walkthrough_attempts_on_current', None)
        # Resample on retake: clear the previously-selected exit-ticket
        # IDs + reset covered flags so the next attempt draws fresh
        # questions from the bank.
        state.pop('selected_exit_ticket_ids', None)
        state['covered_concept_ids'] = []
        self.session.engine_state = state
        # Rebuild the in-memory concept list from a fresh draw so
        # the next _save_state() doesn't rewrite the stale IDs back
        # into engine_state.
        self.exit_ticket_concepts = self._load_exit_ticket_concepts()
        # Transition out of remediation; caller picks up the EXIT_TICKET
        # state and fires _handle_exit_ticket() on this same turn.
        self.is_remediation = False
        self.session_state = SessionState.EXIT_TICKET

        closing = (
            "\n\n---\n"
            "✅ **Review complete.** You've walked through every "
            "question you missed.\n\n"
            "📋 **Fresh exit ticket coming up** — these are NEW "
            "questions on the same topics, so we can see what stuck."
        )
        return clean_response.rstrip() + closing
    
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

        Per-judge breakdown — when present under the private key
        ``_judge_outputs`` — is pulled out and persisted on the
        SessionTurn.judge_outputs column so it doesn't get duplicated
        inside metadata. See memory/eval_benchmark_v2_simplified.md.
        """
        md = dict(metadata) if metadata else {}
        judge_outputs = md.pop('_judge_outputs', None) or {}

        # Attach the figure the LLM signalled this turn (|||MEDIA:N|||
        # parser stores it on self._turn_media keyed by conversation
        # index). Persisted on SessionTurn.metadata so the benchmark
        # snapshot can render the figure in the annotation UI — needed
        # to validate FIGURE_MISMATCH labels.
        # Conversation hasn't been appended yet at _save_turn time, so
        # the upcoming index for THIS turn equals len(self.conversation).
        if role == 'tutor' and 'attached_media' not in md:
            turn_idx = len(getattr(self, 'conversation', []) or [])
            media_for_turn = (
                getattr(self, '_turn_media', {}) or {}
            ).get(str(turn_idx))
            if media_for_turn:
                md['attached_media'] = [media_for_turn]

        turn = SessionTurn.objects.create(
            session=self.session,
            role=role,
            content=content,
            metadata=md,
            judge_outputs=judge_outputs,
        )
        # Structured per-turn log line for offline analysis (Phase 5 of
        # memory/martin_session_fix_plan.md). Single line covers the
        # signals we previously had to scrape across multiple log
        # entries: eval_layer, is_correct, bank state, tool use,
        # validator issues, regen count, bare-answer flag. Skipping
        # student turns keeps the log volume halved without losing
        # the verdict-side information.
        if role == "tutor":
            # Flush accumulated tracing spans to this tutor turn — see
            # apps.tutoring.tracing and Phase 1 of
            # memory/agentic_platform_architecture_plan.md.
            from apps.tutoring.tracing import flush_spans
            flush_spans(turn.id)
            self._emit_turn_summary_log(content, md)
        return turn

    def _emit_turn_summary_log(self, content: str, metadata: Dict) -> None:
        """Emit one [TurnSummary] structured log line per tutor turn.

        Pulls fields that are otherwise spread across:
          - turn_metadata (eval_layer, is_correct, validator_issues,
            regenerated, bare_answer, praise_stripped, tool_use_count)
          - engine state (current step index, step type/phase,
            session_state, bank pool size for empty-bank detection)
          - bank id_map (whether tools were offered this turn)

        Format: key=value pairs so it's grep-friendly AND parseable.
        """
        try:
            import json as _json
            step_idx = self.current_topic_index
            step = (
                self.steps[step_idx]
                if 0 <= step_idx < len(self.steps)
                else None
            )
            id_map = getattr(self, '_question_id_map', None) or {}
            engine_state = self.session.engine_state or {}
            pool_ids = engine_state.get('question_pool_ids')

            payload = {
                "session": self.session.id,
                "turn": len(self.conversation),
                "step_index": step_idx,
                "step_type": getattr(step, 'step_type', '') if step else '',
                "step_phase": getattr(step, 'phase', '') if step else '',
                "session_state": str(getattr(self, 'session_state', '')),
                "is_remediation": bool(getattr(self, 'is_remediation', False)),
                "content_chars": len(content or ''),
                # Eval layer & correctness
                "eval_layer": metadata.get('eval_layer'),
                "is_correct": metadata.get('is_correct'),
                "bare_answer": bool(metadata.get('bare_answer')),
                "praise_stripped": bool(metadata.get('praise_stripped')),
                # Verdict source diagnostics (A.0 — Edward's pilot)
                "expected_answer_present": bool(
                    step and (getattr(step, 'expected_answer', '') or '').strip()
                ),
                "answer_type": (getattr(step, 'answer_type', '') or '') if step else '',
                "non_answer_input": bool(
                    self._is_non_answer_input(metadata.get('student_input_excerpt', ''))
                ) if metadata.get('student_input_excerpt') else False,
                # Bank + tool state
                "bank_pool_size": len(pool_ids) if pool_ids is not None else None,
                "bank_offered": bool(id_map),
                "bank_slot_count": len(id_map),
                "bank_signal_used": bool(getattr(self, '_bank_signal_used_this_turn', False)),
                # Media availability for current step (debugging
                # "no figure shown" complaints — was the catalog
                # populated AND did the LLM use it?).
                "step_has_media": bool(
                    (getattr(self, '_step_media_ids', {}) or {})
                    .get(step_idx)
                ),
                "media_emitted_this_turn": bool(
                    (self._turn_media or {}).get(str(len(self.conversation) - 1))
                ),
                # Tool use (populated by _handle_pose_question_message)
                "tool_use_count": metadata.get('tool_use_count', 0),
                # Validator + regen
                "validator_issues": list(metadata.get('validator_issues', []) or []),
                "regenerated": bool(metadata.get('regenerated')),
                "regeneration_reason": list(metadata.get('regeneration_reason', []) or []),
                # Step eval (combined judge CHECK 4)
                "step_complete": metadata.get('step_complete'),
            }
            # JSON-safe single line. Use logger.info — Azure picks it up.
            logger.info("[TurnSummary] %s", _json.dumps(payload, default=str))
        except Exception as e:
            # Logging must never break the turn.
            logger.warning("[TurnSummary] emit failed: %s", e)
    
    # NOTE (2026-05-05): _parse_probe_signal REMOVED. The
    # |||PROBE:{json}||| inline-widget channel is gone. MCQ rendering
    # now happens via the pose_question tool resolving an
    # ExitTicketQuestion bank slot — render_question_to_prose puts
    # the options on screen verbatim. The probe regex was also
    # fragile (broke on stems containing '{' or '}' such as LaTeX
    # \frac{1}{2}), which leaked raw JSON into the chat.

    def _parse_media_signal(self, text: str) -> Tuple[str, Optional[Dict]]:
        """Parse |||MEDIA:N||| from LLM output.

        Returns (clean_text, media_dict or None). The signal is always
        stripped so nothing leaks into DB or student chat.

        NOTE (2026-05-05): the |||GENERATE:category:description|||
        on-the-fly image-generation channel was REMOVED. The only
        media path is now showing existing entries from the catalog.
        Defensive cleanup of any historical GENERATE tag in saved
        content lives in _create_message.
        """
        match = re.search(r'\|\|\|MEDIA\s*:\s*(\d+)\s*\|\|\|', text)
        if match:
            clean_text = text[:match.start()].rstrip()
            media_id = int(match.group(1))
            if media_id == 0:
                return clean_text, None
            media_id_map = getattr(self, '_media_id_map', {})
            return clean_text, media_id_map.get(media_id)
        return text, None

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

    # NOTE (2026-05-05): _parse_artifact_signal + _sanitize_artifact_html
    # REMOVED. The |||ARTIFACT:html|||...|||/ARTIFACT||| inline-HTML
    # channel is gone. Tables / diagrams now belong in lesson media
    # (uploaded ahead of time) and are surfaced via |||MEDIA:N|||.
    # Defensive cleanup of any historical ARTIFACT tag in saved
    # content lives in _create_message.

    def _create_message(
        self, content: str, media: List[Dict] = None,
    ) -> TutorMessage:
        """Create a TutorMessage from content.

        NOTE (2026-05-05): the optional `artifact_html` and `probe`
        parameters were REMOVED — those signal channels are gone. The
        only output channels are text + an optional media attachment.
        """
        # Defense-in-depth: strip legacy + current signal tags from the
        # content before saving. Even though the LLM is no longer
        # instructed to emit GENERATE / ARTIFACT / PROBE, we still
        # strip them from any historical content loaded from the DB.
        content = re.sub(r'\[SHOW_MEDIA\s*:[^\]]*\]', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\|\|\|MEDIA\s*:\s*\d+\s*\|\|\|', '', content)
        content = re.sub(r'\|\|\|GENERATE\s*:\s*\w+\s*:.+?\|\|\|', '', content)
        content = re.sub(r'\|\|\|ARTIFACT:html\|\|\|.*?\|\|\|/ARTIFACT\|\|\|', '', content, flags=re.DOTALL)
        content = re.sub(r'\|\|\|PROBE\s*:\s*\{.+?\}\s*\|\|\|', '', content, flags=re.DOTALL)
        content = re.sub(r'\|\|\|QUESTION\s*:\s*\d+\s*\|\|\|', '', content)
        # Strip leaked planning narration. The LLM sometimes verbalises
        # its own plan as the first sentence ("I need to address the
        # student's incorrect warmup answer first..."). That's internal
        # monologue — the student should never see it.
        content = _THINKING_LEAK_RE.sub('', content, count=1)
        content = re.sub(r' {2,}', ' ', content)
        content = re.sub(r'\n{3,}', '\n\n', content)
        content = content.strip()
        step_num = min(self.current_topic_index + 1, len(self.steps)) if self.steps else 0
        total = len(self.steps)
        return TutorMessage(
            content=content,
            phase=self._get_display_phase(),
            media=media or [],
            expects_response=self.session_state != SessionState.COMPLETED,
            step_number=step_num,
            total_steps=total,
            is_correct=getattr(self, 'last_answer_correct', False),
            streak_count=getattr(self, '_correct_streak', 0),
            practice_score=f"{self.practice_correct}/{self.practice_total}" if self.practice_total > 0 else "",
            milestone=self._check_milestone(),
        )