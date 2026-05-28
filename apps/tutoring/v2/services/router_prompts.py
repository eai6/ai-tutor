"""Prompt artefacts for the LLM Move Router.

Post-prune Commit D §4.2 — rewritten per v2-prune-plan Appendix A.

Key shape changes from the legacy router prompt:
  - No principle table, no focus_note, no principle_emphasis schema.
  - Three named TURN CLASSIFICATION cases: ANSWER_ATTEMPT,
    HELP_REQUEST, OPENING_TURN (forced_close folds in as a routing
    rule on top of the OPENING_TURN family).
  - Explicit if-then routing rules that reference NAMED counter fields
    from the CONTEXT block. The router does NOT count from the
    transcript — the engine has already done that work.
  - Strict JSON output schema, conditional on case:
    * non-answer-attempt: {case, move, verdict_needed: false, reason}
    * answer-attempt: {case: "answer_attempt", verdict_needed: true,
                       moves_by_verdict: {correct, partial, wrong},
                       reason}

Companion to ``apps/tutoring/v2/services/move_router.py``. The system
prompt is stable across all turns (eligible for prompt caching with a
1-hour TTL — same shape as ``MATH_DSL_SYSTEM``); only the user prompt
varies per turn.

Per-prompt prompting-skills compliance (CLAUDE.md non-negotiable):
  - Direct task statement, no flowery role priming.
  - Positive if-then rules; counters referenced by name.
  - Closed output schema with strict JSON; no markdown fences.
"""

from __future__ import annotations

from apps.tutoring.v2.contracts import RouterRequest


# ──────────────────────────────────────────────────────────────────────
# 1. The router's system prompt (static, cacheable)
# ──────────────────────────────────────────────────────────────────────


SHARED_ROUTER_SYSTEM = """\
You are a move classifier for a one-to-one tutoring session.

Pick the next tutor move based on the student's last turn, the open
question state, and the recent attempt history. Return one move from
the closed set below, plus whether a verdict is needed before the
tutor responds.

MOVES (closed set — pick exactly one per branch):

1. confirm_and_advance — the student gave a correct answer; affirm
   what they got right and pose the next question.
   Examples: student wrote "9 SCR"; student wrote "x = 8".

2. confirm_and_extend — the student gave a rich correct answer that
   named the mechanism; affirm and pose a single twist on the same
   concept.
   Examples: student wrote "5² + 12² = 169 and 13² = 169, so it's a
   right triangle" (full reasoning shown).

3. scaffold_hint — the student was wrong or partially right; credit
   any partial they named, name the slip without revealing the
   answer, ask ONE smaller step on the SAME open question.
   Examples: student wrote "is it 90?" on a bearing question
   expecting 045°; student wrote "profit is 9" on a multi-slot
   question.

4. name_misconception — the student has been wrong several times on
   the same item AND named their faulty reasoning ("because it's
   halfway"); name the specific misconception, give one more attempt.
   Examples: student wrote "is north east 180 because its halfway";
   student wrote "x = 21 because i added 3 to both sides" on 3x = 18.

5. worked_example — the student explicitly asked for help, an
   example, or said they don't understand; walk through ONE example
   with labelled subgoals anchored to the open question.
   Examples: "show me how to do this"; "I don't get it, can you walk
   me through it?"; "give me an example".

6. explain — the student asked for a definition or concept, or this
   is an opening turn before any question has been posed; deliver the
   rule in 2-4 short sentences, end with one action.
   Examples: "what's a bearing?"; opening turn of a lesson.

7. pivot — the student has been wrong ≥4 times on the same item;
   acknowledge the difficulty, pose a different question on the SAME
   concept at the SAME rigor (do not lower the bar).
   Examples: 4 wrong attempts in a row on the same Pythagoras
   prove-or-disprove problem.

8. close_topic — objective evidence has saturated (≥2 unscaffolded
   correct retrievals), OR a session-level safety cap has been
   reached; name what was done in one sentence and signal the
   transition.
   Examples: student just gave the 2nd consecutive unscaffolded
   correct answer on this objective.

────────────────────────────────────────────────────────────────────
TURN CLASSIFICATION — three cases the router must distinguish:

1. ANSWER_ATTEMPT — the student is attempting to answer the open
   question. verdict_needed: true. You emit a `moves_by_verdict`
   object enumerating the final move for each possible grader
   outcome (correct / partial / wrong) using the named counter
   fields. The engine looks up the matching row after the grader
   returns.

2. HELP_REQUEST — the student is asking the tutor to teach, explain,
   give an example, or otherwise hand the work back to the tutor.
   verdict_needed: false. Return worked_example (if there is an open
   question) or explain (if not).
   Signal phrases: "I don't understand", "I don't know", "I'm
   stuck", "explain", "show me", "what does X mean", "what is X",
   "how do I", "tell me how", "can you walk me through", "I'm
   lost", "I forgot how".

3. OPENING_TURN — no open question yet, no prior student answer
   attempt on this objective. verdict_needed: false. Return explain.

────────────────────────────────────────────────────────────────────
ROUTING RULES — non-answer-attempt cases. Each rule references named
counters from the CONTEXT block — do not count from the transcript,
the engine has already done the counting.

- If the student's turn matches a HELP_REQUEST signal phrase →
  case: "help_request"
  move: worked_example (if `open_question_present` is true) or
        explain (if not)
  verdict_needed: false

- If `open_question_present` is false AND
  `prior_answer_attempts_on_objective` is 0 →
  case: "opening_turn"
  move: explain
  verdict_needed: false

- If `objective_turn_count` ≥ 12 AND `correct_on_objective` is 0 →
  case: "forced_close"
  move: close_topic
  verdict_needed: false

- Otherwise → case: "answer_attempt"; see ANSWER-ATTEMPT block below.

────────────────────────────────────────────────────────────────────
ROUTING RULES — answer-attempt case. You enumerate the final move
for EACH possible grader outcome (correct, partial, wrong) using the
named counter fields. The engine looks up the matching row after the
grader returns. The verdict→move logic lives in YOUR output, not in
the engine.

For each verdict, pick from the closed move set above:

- correct:
    * if `unscaffolded_correct_on_objective` ≥ 1 already (this would
      be the 2nd unscaffolded correct, saturation reached)
                                              → close_topic
    * if the current student turn is rich (names the mechanism,
      formula, or full chain of reasoning explicitly)
                                              → confirm_and_extend
    * otherwise                               → confirm_and_advance

- partial:
    * always                                  → scaffold_hint
      (partial-credit branch — credit what the student named, ask
      one step on the SAME open question)

- wrong:
    * if `wrong_attempts_on_open_question` ≥ 4 (counting THIS
      attempt)                                → pivot
    * if `wrong_attempts_on_open_question` ∈ [2, 3] (counting THIS
      attempt) AND the current student turn named their reasoning
      ("because…", "I think…", explicit faulty rule)
                                              → name_misconception
    * otherwise                               → scaffold_hint

The "named their reasoning" judgment is a content read of the
current student turn — THAT is your job. The attempt counts are
fields — do not re-derive them.

────────────────────────────────────────────────────────────────────
OUTPUT SCHEMA — strict JSON, no prose before or after.

For NON-answer-attempt cases (help_request, opening_turn,
forced_close):
{
  "case": "help_request | opening_turn | forced_close",
  "move": "<one of the 8 moves>",
  "verdict_needed": false,
  "reason": "one sentence — names the case and the rule that fired"
}

For the ANSWER_ATTEMPT case:
{
  "case": "answer_attempt",
  "verdict_needed": true,
  "moves_by_verdict": {
    "correct": "<move from closed set>",
    "partial": "<move from closed set>",
    "wrong":   "<move from closed set>"
  },
  "reason": "one sentence — names the counter values that drove each branch"
}

────────────────────────────────────────────────────────────────────
HARD RULES — non-negotiable.

- Emit JSON only. No prose preface, no markdown fences, no comments.
- Every `move` / `moves_by_verdict` value must be one of the 8 move
  names above. Any other value is rejected.
- `reason` ≤ 400 characters.
- Do NOT include the canonical answer or grader internals in any
  field. The tutor LLM works from its own grounding; leaking
  internals into `reason` defeats the redaction layer.
"""


# ──────────────────────────────────────────────────────────────────────
# 2. Per-turn user prompt rendering
# ──────────────────────────────────────────────────────────────────────


def render_router_user_prompt(request: RouterRequest) -> str:
    """Render the dynamic per-turn payload the router sees.

    Long-context query-last shape: lesson + counters first, transcript
    in the middle, latest student input + the decision ask at the END
    so the model's recency bias steers toward the actual task.
    """
    transcript_block = _render_transcript_block(request.last_n_turns)
    counters_block = _render_counters_block(request)
    lesson_block = _render_lesson_block(request)
    open_q_block = _render_open_q_block(request)
    profile_block = (
        (request.profile_summary or "").strip()
        or "(no profile summary yet)"
    )

    return (
        f"{lesson_block}\n\n"
        f"{open_q_block}\n\n"
        f"{counters_block}\n\n"
        f"=== Student profile summary ===\n{profile_block}\n\n"
        f"=== Recent transcript (last {len(request.last_n_turns)} turns; "
        f"for qualitative context only — do NOT count from this) ===\n"
        f"{transcript_block}\n\n"
        f"=== CURRENT STUDENT TURN ===\n"
        f"{(request.student_input or '').strip() or '(no input — opening / transitional turn)'}\n\n"
        f"---\n"
        f"Classify the turn (answer_attempt / help_request / "
        f"opening_turn / forced_close) and return strict JSON per the "
        f"schema in the system prompt. No prose, no fences."
    )


def _render_lesson_block(request: RouterRequest) -> str:
    title = (request.lesson_title or "(this lesson)").strip()
    subject = (request.lesson_subject or "(see title)").strip()
    objective = (request.objective or "(no objective set)").strip()
    position = (
        "final step of the lesson" if request.is_final_step
        else "more steps remain after this one"
    )
    teacher_script = (request.lesson_step_teacher_script or "").strip()
    worked_example = (request.lesson_step_worked_example or "").strip()
    media = (
        (request.media_catalog_summary or "").strip()
        or "(no figures available)"
    )
    parts = [
        "=== Lesson context ===",
        f"Title: {title}",
        f"Subject: {subject}",
        f"Objective: {objective}",
        f"Lesson position: {position}",
        f"Media catalog: {media}",
    ]
    if teacher_script:
        parts.append(f"Authored direct-instruction draft: {_clip(teacher_script, 400)}")
    if worked_example:
        parts.append(f"Authored worked example: {_clip(worked_example, 400)}")
    return "\n".join(parts)


def _render_counters_block(request: RouterRequest) -> str:
    """Named counter fields the routing rules reference.

    Every counter the router's routing rules use is here, named the
    same way as in the system prompt. The router MUST NOT count from
    the transcript — these are authoritative.
    """
    recent_moves = request.move_history[-5:] if request.move_history else []
    recent_verdicts = (
        request.recent_verdicts[-10:] if request.recent_verdicts else []
    )
    return (
        "=== Runtime counters (authoritative — do not re-derive) ===\n"
        f"open_question_present: {request.open_question_has_pending}\n"
        f"wrong_attempts_on_open_question: {request.wrong_attempts_on_open_question}\n"
        f"partial_attempts_on_open_question: {request.partial_attempts_on_open_question}\n"
        f"consecutive_wrong_on_open_question: {request.consecutive_wrong_on_open_question}\n"
        f"objective_turn_count: {request.objective_turn_count}\n"
        f"prior_answer_attempts_on_objective: {request.prior_answer_attempts_on_objective}\n"
        f"correct_on_objective: {request.correct_on_objective}\n"
        f"unscaffolded_correct_on_objective: {request.unscaffolded_correct_on_objective}\n"
        f"recent_verdicts (oldest first, up to last 10): {recent_verdicts}\n"
        f"recent_moves (oldest first, up to last 5): {recent_moves}\n"
        f"pose_tool_available: {request.pose_tool_available}"
    )


def _render_open_q_block(request: RouterRequest) -> str:
    if not request.open_question_has_pending:
        return "=== Open question ===\n(none — no question in flight)"
    stem = _clip(request.open_question_stem or "", 400)
    return (
        "=== Open question ===\n"
        f"In flight: {stem!r}"
    )


def _render_transcript_block(turns: list[dict]) -> str:
    if not turns:
        return "(empty — fresh session or no recent turns retained)"
    lines: list[str] = []
    for turn in turns:
        role = (turn.get("role") or "?").strip()
        content = (turn.get("content") or "").strip()
        lines.append(f"[{role}] {_clip(content, 500)}")
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


__all__ = [
    "SHARED_ROUTER_SYSTEM",
    "render_router_user_prompt",
]
