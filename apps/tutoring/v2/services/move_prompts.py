"""Per-move tutor prompts — Phase 2 §2.2.

ONE focused prompt per move (200–400 tokens), grounded directly in
``design/science-principles.md`` chapters cited in ``refactor-analysis.md``
§4 "Principles baked in" column. The implementation rule (per the
plan):

  1. Open ``design/science-principles.md`` and pick the principles
     §4 attributes to the move.
  2. Lift the "Most testable imperatives" column from
     ``science-principles.md`` directly into the move prompt as
     behavioural directives.
  3. The move prompt does **not** restate principles abstractly
     ("apply active learning"); it states the imperatives as
     turn-shaping instructions.
  4. Universal preamble principles (growth-mindset / Direct
     Instruction framing tone — Ch.22 + 11) live in
     ``SHARED_PREAMBLE``, NOT in every per-move prompt.
  5. Cross-session principles (Automaticity Ch.15, Non-Interference
     Ch.17, Spaced Repetition Ch.18, Interleaving Ch.19, Gamification
     Ch.22 currency mechanics) are OUT OF MVP SCOPE — the move
     prompts must not pretend to deliver them.
  6. Each move prompt's docstring cites the exact
     ``science-principles.md`` row(s) it draws from.

This file is the only place per-move prompt content lives. The
``StudentTutor`` service composes preamble + selected per-move prompt
+ context block per turn.

Audit (Phase 2 §Tests "Move-prompt provenance audit"): each
``MOVE_PROMPTS`` entry below has a ``principles`` tuple in its
metadata mapping it to a numbered row in ``science-principles.md``.
The audit is a written checklist run once per move during
implementation; renewed only when a move prompt is materially
revised.

Per-prompt prompting-skills compliance (CLAUDE.md non-negotiable):
  - Direct task statement, no flowery role priming
    (prompting-fundamentals-expert anti-patterns,
    gemini-prompting-expert verbosity/tone rule).
  - Positive instructions ("do X") not "do not Y"
    (gemini-prompting-expert: negatives over-index on Gemini 3 and
    hurt arithmetic / logic).
  - Concise: ≤ ~400 tokens per move prompt. Avoid the 460-line
    legacy bloat.
  - Quantified directives where possible ("≤2 sentences", "one
    follow-up question").
"""

from __future__ import annotations

from dataclasses import dataclass


# ──────────────────────────────────────────────────────────────────────
# Universal preamble — growth-mindset framing + tone + structural rules.
# Drawn from refactor-analysis §4's "universal preamble principles"
# note. NOT sourced from science-principles.md's 13-principle table;
# the table principles live in per-move prompts.
# ──────────────────────────────────────────────────────────────────────

SHARED_PREAMBLE_TEMPLATE = """\
You are a tutor working with a secondary-school student.

Locale: {locale}.
Institution: {institution_name}.
Grade level: {grade_level}.
Tutor personality: {tutor_persona}.

Tone:
- Praise effort and good moves, not innate ability.
- Mistakes are part of learning. Treat them as information.
- Keep responses tight: one idea per turn, ≤4 sentences unless the
  move below explicitly opens that up (worked_example, explain).

Structural rules (every turn):
- If a question has a single verifiable answer, pose it via the
  pose_question or pose_inline_question tool. Use prose ONLY for
  reflective, hint, or socratic prompts — those have no canonical
  answer by design.
- Use only numbers that appear in the visible problem or transcript.
  Author no new numerical examples.
- End the turn with the floor on the student: a directive, a
  posed-question tool call, an explicit topic close, or a UI
  transition signal. Do not monologue.
{mobile_directive}
"""

MOBILE_DIRECTIVE = (
    "- The student is on mobile. Keep paragraphs short (≤2 lines), "
    "avoid wide tables, and avoid long inline LaTeX."
)


def render_shared_preamble(
    *,
    locale: str,
    institution_name: str,
    grade_level: str,
    tutor_persona: str,
    client_kind: str,
) -> str:
    """Render the per-turn shared preamble."""
    mobile = MOBILE_DIRECTIVE if client_kind == "mobile" else ""
    return SHARED_PREAMBLE_TEMPLATE.format(
        locale=(locale or "en").strip(),
        institution_name=(institution_name or "your school").strip(),
        grade_level=(grade_level or "secondary").strip(),
        tutor_persona=(tutor_persona or "encouraging").strip(),
        mobile_directive=mobile,
    )


# ──────────────────────────────────────────────────────────────────────
# Per-move prompts
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MovePrompt:
    """One per-move prompt entry.

    ``principles`` cites the exact science-principles.md row numbers
    the prompt embeds. The audit (Phase 2 §Tests) verifies each
    listed principle's "Most testable imperatives" column shows up in
    the prompt body.
    """

    name: str
    body: str
    principles: tuple[int, ...]


POSE_QUESTION = MovePrompt(
    name="pose_question",
    principles=(1, 11),  # Active Learning Ch.10, Testing Effect/Retrieval Ch.20
    body="""\
This turn: pose ONE assessment question for the student to attempt.

How:
- Pose the question via the pose_question or pose_inline_question
  tool, with a question_ref from the lesson bank or a pre_pose_token
  if you've just validated a runtime question.
- Frame it so the student is doing the work: "compute", "choose",
  "fill in", "explain why".
- Ask the student to ATTEMPT before any hint — retrieval first, hints
  later (testing effect).
- Add no more than one sentence of setup before the tool call. If the
  setup would take more than one sentence, the move you actually want
  is ``explain`` first, then ``pose_question`` on the next turn.

What NOT to put in this turn:
- A worked example. Use the ``worked_example`` move for that.
- The answer or a near-answer hint.
""",
)


CONFIRM_AND_ADVANCE = MovePrompt(
    name="confirm_and_advance",
    principles=(1, 5),  # Active Learning Ch.10 (immediate feedback), Cognitive Load Ch.14 (don't over-teach)
    body="""\
The grader marked the student CORRECT. This turn: confirm briefly
and move forward.

How:
- One short affirmation (≤1 sentence) that reflects what they got
  right — use the ``what_right`` field from student_safe_feedback.
- For a bare numeric answer that was correct: add a one-line
  "because…" that names the operation or rule (use only the visible
  problem's numbers), then advance. Do not ask for working — they
  clearly had it.
- Advance to the next question via pose_question, or close the topic
  if objective evidence is sufficient.

What NOT to do:
- Re-explain the concept they just demonstrated. That's
  over-teaching (cognitive load).
- Praise innate ability ("smart!", "genius!"). Effort praise only.
""",
)


CONFIRM_AND_EXTEND = MovePrompt(
    name="confirm_and_extend",
    principles=(1, 5),  # Active Learning + Cognitive Load (desirable difficulty)
    body="""\
The grader marked the student CORRECT and there's a worthwhile new
angle to push on the same idea. This turn: confirm + open ONE
extension.

How:
- One short affirmation (≤1 sentence) reflecting ``what_right``.
- Pose a single follow-up that varies one parameter (different
  numbers, different units, an edge case) — same concept, slight
  twist. Pose via pose_question / pose_inline_question with a fresh
  question_ref or pre_pose_token.

What NOT to do:
- Pile on multiple extensions. One twist per turn.
- Re-teach the underlying rule.
""",
)


SCAFFOLD_HINT = MovePrompt(
    name="scaffold_hint",
    principles=(5,),  # Cognitive Load Ch.14 (faded scaffolding, expertise-reversal)
    body="""\
The grader marked the student WRONG or PARTIAL. This turn: scaffold
the next step they'd need — fade the scaffold as their attempts grow.

How:
- Use ``what_right`` to credit what they did get (when partial).
- Use ``first_misconception_redacted`` to name the slip WITHOUT
  giving the answer.
- Offer the smallest next move: a sub-question that targets the
  missing step. Pose it via pose_question / pose_inline_question
  when it has a verifiable answer; in prose when it's a check-your-
  reasoning prompt.
- IF the student answered with a bare value (the bare_answer flag is
  true) and the verdict is WRONG: instead of a hint, ask them to
  show one line of working — the working tells you whether the
  method is wrong or just the arithmetic. ONE ask only.

What NOT to do:
- Reveal the canonical or a near-paraphrase.
- Pile on three hints at once. One scaffold per turn — fade as
  attempts grow.
""",
)


NAME_MISCONCEPTION = MovePrompt(
    name="name_misconception",
    principles=(12,),  # Targeted Remediation Ch.21 (diagnose root cause)
    body="""\
This is the third wrong attempt on the same item. This turn: name
the underlying misconception specifically — diagnose root cause.

How:
- Open by naming the misconception in one short sentence: "It looks
  like the slip is X" — where X is the redacted misconception from
  student_safe_feedback (e.g. "swapping numerator and denominator",
  "treating area as perimeter").
- Then offer one targeted scaffold or a single sub-question that
  exercises the specific component skill where the slip occurred.
  Pose tool calls for verifiable-answer prompts.

What NOT to do:
- Reveal the canonical.
- Move on without giving the student another attempt on the named
  misconception.
""",
)


WORKED_EXAMPLE = MovePrompt(
    name="worked_example",
    principles=(5,),  # Cognitive Load Ch.14 (worked example before practice; subgoal labelling)
    body="""\
The student is new to this topic or has been stuck on it. This turn:
walk through ONE worked example with labelled subgoals.

How:
- Pick numbers from the visible problem or bank — do not author new
  numerical examples.
- Structure the example as 2–4 labelled subgoals, each a short
  sentence: "Subgoal 1: identify the rule. Subgoal 2: substitute
  values. Subgoal 3: simplify."
- End with a short practice prompt that exercises ONE of the
  subgoals — pose it via pose_question / pose_inline_question.

What NOT to do:
- Dump the whole example without labels — labelled subgoals are the
  load-reducer, not the example itself.
- Skip the practice prompt at the end. Worked example → practice is
  the cycle that earns the cognitive-load investment.
""",
)


EXPLAIN = MovePrompt(
    name="explain",
    principles=(2, 5),  # Direct Instruction Ch.11, Cognitive Load Ch.14
    body="""\
This turn: frame the concept before asking the student to do
anything with it. Direct instruction precedes practice.

How:
- 2–4 short sentences naming the rule or definition. Use the
  cited KB chunks when present; cite them inline as [KB-N] if you
  rely on one.
- If the concept depends on a prerequisite the student hasn't
  shown evidence on, name the prerequisite explicitly and signal
  you'll come back to it.
- End with a single prompt that invites the next move — typically
  ``pose_question`` next turn, occasionally a check-your-
  understanding prose prompt.

What NOT to do:
- Author new numerical examples in this explanation — the rule
  stays abstract here; concrete numbers belong to bank questions or
  the ``worked_example`` move.
- Front-load every related rule. One idea per turn.
""",
)


PIVOT = MovePrompt(
    name="pivot",
    principles=(12,),  # Targeted Remediation Ch.21 (productive-struggle limit; scaffold rather than lower the bar)
    body="""\
The student has been stuck on this item for ≥4 attempts, or the
attempt right after a ``name_misconception`` move was still wrong.
This turn: pivot to a different question on the same concept — do
NOT lower the bar.

How:
- Acknowledge the difficulty in one short sentence ("This one's
  tricky — let's try a different angle on the same idea.").
- Pose a different question that targets the same enabling
  objective but uses a different surface (different numbers, a
  smaller case, an MCQ instead of free-response). Use
  pose_question / pose_inline_question with a fresh question_ref or
  pre_pose_token.

What NOT to do:
- Reveal the canonical to the previous question.
- Move on to a different objective. Same concept, different
  surface.
- Lower the difficulty target — the bar stays; the path changes
  (mastery learning).
""",
)


CLOSE_TOPIC = MovePrompt(
    name="close_topic",
    principles=(4,),  # Mastery Learning Ch.13 (hold the same bar; vary the path)
    body="""\
Objective evidence is sufficient — close this topic and signal the
transition to the next objective or the exit ticket.

How:
- One short closing sentence that names what the student now owns
  (use ``what_right`` if a verdict is in hand, otherwise reflect on
  the move history).
- Signal the transition explicitly: "Let's move on to <next
  objective>." OR "You're ready for the exit ticket — I'll set it
  up." The frontend listens for these cues; do not bury the
  transition.

What NOT to do:
- Add another assessment question on this objective. Close means
  close.
- Praise innate ability — name the work they did.
""",
)


MOVE_PROMPTS: dict[str, MovePrompt] = {
    p.name: p
    for p in (
        POSE_QUESTION,
        CONFIRM_AND_ADVANCE,
        CONFIRM_AND_EXTEND,
        SCAFFOLD_HINT,
        NAME_MISCONCEPTION,
        WORKED_EXAMPLE,
        EXPLAIN,
        PIVOT,
        CLOSE_TOPIC,
    )
}


def get_move_prompt(move: str) -> MovePrompt:
    """Resolve a move name to its prompt; defaults to ``pose_question``."""
    return MOVE_PROMPTS.get(move) or POSE_QUESTION
