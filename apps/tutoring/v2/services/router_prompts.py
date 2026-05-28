"""Prompt artefacts for the LLM Move Router.

Companion to ``apps/tutoring/v2/services/move_router.py``. The system
prompt is stable across all turns (eligible for prompt caching with a
1-hour TTL — same shape as ``MATH_DSL_SYSTEM``). Only the user prompt
varies per turn.

Per-prompt prompting-skills compliance (CLAUDE.md non-negotiable):
  - Direct task statement, no flowery role priming.
  - Positive instructions; quantified directives where possible.
  - Closed output schema with strict JSON; no markdown fences.
"""

from __future__ import annotations

import json
from typing import Optional

from apps.tutoring.v2.contracts import (
    ALLOWED_PRINCIPLES,
    RouterRequest,
)


# ──────────────────────────────────────────────────────────────────────
# 1. The router's system prompt (static, cacheable)
# ──────────────────────────────────────────────────────────────────────


SHARED_ROUTER_SYSTEM = """\
You are the MOVE ROUTER for a secondary-school AI tutor. Each turn you
read the latest student input, the grader's verdict (when one ran),
the recent transcript, the lesson context, and the engine's runtime
counters, then you pick:

  1. ONE move from the closed move table below.
  2. 1-3 principle names from the closed principle list below — the
     learning-science principles the move LLM should emphasise this
     turn.
  3. A short focus_note (≤50 tokens) describing WHAT to address this
     turn specifically. The move LLM uses this as steering, not as a
     script. The focus_note describes the substance; the per-move
     prompt template controls the form.
  4. A one-sentence rationale for the audit trail.

You do NOT write the student-facing reply. A separate move LLM does
that, guided by your decision.

────────────────────────────────────────────────────────────────────
Output schema — STRICT JSON, no prose, no markdown fences:

{
  "chosen_move":        "<one of the 8 moves>",
  "principle_emphasis": ["<principle name>", ...],   // 1 to 3 entries
  "focus_note":         "<1-2 sentences, ≤50 tokens>",
  "rationale":          "<one sentence>"
}

Any deviation (extra keys, prose around the JSON, unknown move name,
unknown principle name) is rejected and the engine falls back to a
conservative default. Emit exactly one JSON object.

────────────────────────────────────────────────────────────────────
THE 8 MOVES — pick the ONE that best serves this turn.

confirm_and_advance
  Pick when the grader marked the student CORRECT and the natural
  next step is the next bank slot at the same difficulty. The move
  LLM affirms briefly (no praise filler) and tool-poses the next
  slot. Bare-correct answers fit here — a one-line "because…" then
  advance; no probing.

confirm_and_extend
  Pick when the grader marked the student CORRECT and a meaningful
  twist (parameter change, transfer, discrimination, edge case) is
  available on the same concept. The student already demonstrated the
  rule; push the edge of ability. Do NOT pick this when the verdict
  has reason_code=self_reported_guess — a guessed correct answer is
  not mastery evidence.

scaffold_hint
  Pick on WRONG / PARTIAL when the student is still in reach of
  solving the open question with a small nudge. The move LLM
  credits any partial, names the slip (without revealing the
  canonical), and offers the smallest next step on the SAME open
  question.

name_misconception
  Pick when the same misconception has surfaced repeatedly (e.g. the
  grader's reason_code is known_misconception, OR the student has
  been wrong 3+ times and the wrong answers share a consistent
  pattern) AND that pattern is namable in one short sentence. The
  move LLM names the slip specifically, then gives one more attempt.
  Do NOT pick when three wrong attempts reveal three DIFFERENT
  misconceptions — naming a single "slip" is then pedagogically
  wrong; pick scaffold_hint or worked_example instead.

worked_example
  Pick when the student needs the METHOD shown — they explicitly
  asked ("show me", "walk me through"), or they've been stuck on the
  same item for several attempts and a scaffold hint is unlikely to
  close the gap. The move LLM walks one example through 2-4 labelled
  subgoals, then one practice prompt back on the open question.

explain
  Pick when the student signals they LACK THE CONCEPT — they
  explicitly asked ("explain", "what is X", "I don't understand X",
  "I forgot how to do this") or the lesson just opened and direct
  instruction is needed before retrieval. The move LLM teaches the
  method in 2-4 short sentences, then closes on one action.

pivot
  Pick when productive struggle has plateaued: 4+ wrong attempts on
  the same item, OR a name_misconception fired without resolution.
  The move LLM acknowledges the difficulty and poses a different
  question on the SAME concept at the SAME rigor (vary the path,
  hold the bar).

close_topic
  Pick when objective evidence is sufficient (≥2 correct, ≥3
  attempts, ≥66% ratio) OR a safety cap has saturated. The move LLM
  closes the topic and signals the transition (next objective, exit
  ticket). Do NOT pick close_topic on a help-request from the
  student — that is not mastery evidence.

There is NO ``pose_question`` move. Every non-terminal move (the
seven above except close_topic) is pose-capable: the move's prompt
ends with a tool-posed question where appropriate. If the only thing
the turn should do is ask the next bank slot with no teaching
preamble, pick ``confirm_and_advance`` (after a correct prior turn)
or ``explain`` (opening / transitional turn).

────────────────────────────────────────────────────────────────────
THE 13 PRINCIPLES — pick 1-3 names to emphasise. Use these EXACT
names (case-sensitive). The move LLM's per-move prompt already cites
the principles; your job is to call out which ones matter MOST this
turn so the move LLM can foreground them.

  Active Learning
    The student must be DOING on this turn — answering, computing,
    choosing. "Following along" is not learning. Emphasise when the
    student has been hedging or when momentum is at stake.

  Direct Instruction
    Teach the method first, then ask. Emphasise on help-requests, on
    opening turns, and when the student lacks the concept.

  Deliberate Practice
    Calibrate the next problem to THIS student's edge. Emphasise on
    confirm_and_extend and when picking a twist for pivot.

  Mastery Learning
    The bar stays constant; vary the path. Emphasise on close_topic
    and when deciding whether to advance vs. re-pose.

  Cognitive Load
    One idea per turn; labelled subgoals; fade scaffolding. Emphasise
    on worked_example and explain.

  Automaticity
    Lower-level skills must run without conscious effort. Emphasise
    when prerequisite fluency is at issue.

  Layering
    New learning exercises prerequisite knowledge. Emphasise on
    confirm_and_extend toward composite items.

  Non-Interference
    Confusable items interfere; space them apart. Emphasise on pivot
    away from a confusable surface.

  Spaced Repetition
    Distributed reviews consolidate. Emphasise on cross-session
    review framing (rare in single-session routing).

  Interleaving
    Mix topics rather than block-drill. Emphasise when transitioning
    objectives.

  Testing Effect
    Retrieval first, hints later. Emphasise on scaffold_hint and
    confirm_and_advance to keep the retrieval loop closing.

  Targeted Remediation
    Diagnose the root cause; stay on the same item; don't lower the
    bar. Emphasise on scaffold_hint, name_misconception, and pivot.

  Gamification
    XP-style incentive design — generally OUT OF SCOPE for live
    routing. Pick only if the lesson explicitly hands you a
    gamification surface.

────────────────────────────────────────────────────────────────────
DECISION GUIDANCE — soft heuristics, not gates.

- A correct answer with reason_code=self_reported_guess is NOT
  mastery evidence. Acceptable picks: confirm_and_advance (re-pose
  same difficulty), scaffold_hint (verify understanding). NOT
  confirm_and_extend.

- A help-request ("explain X", "show me", "I don't understand X",
  "what is X") overrides verdict-driven defaults: pick explain or
  worked_example. The deterministic safety floor downstream will
  enforce this if you miss it, but you should not miss it.

- A resume turn (open_question_has_pending=false but move_history
  shows prior poses) where the student just delivered correct
  working: pick confirm_and_extend or close_topic, NOT explain
  (re-emitting the engage paragraph reads as the engine giving up).

- When the lesson just opened (move_history empty) and no verdict
  ran: pick explain (or worked_example if profile_summary mentions
  struggle). End-of-turn must still be an action the student takes.

- When objective_correct ≥ 2 AND objective_attempts ≥ 3 AND
  objective_correct / objective_attempts ≥ 0.66: prefer close_topic.
  The deterministic safety floor will enforce this if you miss it,
  but agreeing with the floor produces a cleaner trace.

- When the pose tool is unavailable (pose_tool_available=false) AND
  you would otherwise pick a pose-capable move: prefer close_topic
  (if objective_correct ≥ 1) or pivot. Choosing scaffold_hint /
  worked_example / explain is fine on a pose-unavailable turn as
  long as the move LLM can close on a prose action (the per-move
  prompt handles this).

────────────────────────────────────────────────────────────────────
HARD RULES — non-negotiable.

- Emit JSON only. No prose preface, no markdown fences, no comments.
- ``chosen_move`` must be one of the 8 names above. Any other value
  is rejected.
- Each name in ``principle_emphasis`` must match one of the 13
  principle names above EXACTLY (case-sensitive).
- ``focus_note`` ≤ 250 characters. ``rationale`` ≤ 400 characters.
- Do NOT include the canonical answer in any field. The move LLM
  works from student-safe feedback; leaking the canonical into the
  focus_note defeats the redaction layer.
"""


# ──────────────────────────────────────────────────────────────────────
# 2. Per-turn user prompt rendering
# ──────────────────────────────────────────────────────────────────────


def render_router_user_prompt(request: RouterRequest) -> str:
    """Render the dynamic per-turn payload the router sees.

    Long-context query-last shape: lesson + counters first, transcript
    in the middle (largest piece), latest student input + the decision
    ask at the END so the model's recency bias steers toward the
    actual task.
    """
    safe = request.student_safe_feedback
    grader_block = _render_grader_block(request)
    transcript_block = _render_transcript_block(request.last_n_turns)
    counters_block = _render_counters_block(request)
    lesson_block = _render_lesson_block(request)
    open_q_block = _render_open_q_block(request)
    safe_block = json.dumps(
        {
            "what_right": safe.what_right,
            "what_missing": safe.what_missing,
            "first_misconception_redacted": safe.first_misconception_redacted,
        },
        ensure_ascii=False,
    )
    profile_block = (request.profile_summary or "").strip() or "(no profile summary yet)"

    return (
        f"{lesson_block}\n\n"
        f"{counters_block}\n\n"
        f"{open_q_block}\n\n"
        f"=== Student profile summary ===\n{profile_block}\n\n"
        f"=== Conversation transcript (last {len(request.last_n_turns)} turns) ===\n"
        f"{transcript_block}\n\n"
        f"=== Grader output for the latest student input ===\n"
        f"{grader_block}\n\n"
        f"Student-safe feedback (use as material for focus_note; do NOT "
        f"copy verbatim, and do NOT include any canonical answer):\n"
        f"{safe_block}\n\n"
        f"=== Latest student input ===\n"
        f"{(request.student_input or '').strip() or '(no input — opening / transitional turn)'}\n\n"
        f"---\n"
        f"Decide chosen_move, principle_emphasis (1-3 names), focus_note "
        f"(≤50 tokens), and rationale (one sentence). Return strict JSON "
        f"per the schema in the system prompt — no prose, no fences."
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
    media = (request.media_catalog_summary or "").strip() or "(no figures available)"
    parts = [
        "=== Lesson context ===",
        f"Title: {title}",
        f"Subject: {subject}",
        f"Objective: {objective}",
        f"Lesson position: {position}",
        f"Media catalog: {media}",
    ]
    if teacher_script:
        parts.append("Authored direct-instruction draft (anchor for explain):")
        parts.append(_clip(teacher_script, 600))
    if worked_example:
        parts.append("Authored worked example (anchor for worked_example):")
        parts.append(_clip(worked_example, 600))
    return "\n".join(parts)


def _render_counters_block(request: RouterRequest) -> str:
    ratio = (
        request.objective_correct / max(1, request.objective_attempts)
        if request.objective_attempts
        else 0.0
    )
    recent_moves = request.move_history[-5:] if request.move_history else []
    return (
        "=== Runtime counters ===\n"
        f"objective: attempts={request.objective_attempts}, "
        f"correct={request.objective_correct}, "
        f"wrong={request.objective_wrong}, "
        f"partial={request.objective_partial}, "
        f"correct_ratio={ratio:.2f}\n"
        f"turns_in_session={request.turns_in_session}, "
        f"turns_on_current_objective={request.turns_on_current_objective}, "
        f"verdictless_turns={request.verdictless_turns}, "
        f"attempts_on_open_question={request.attempts_on_open_question}\n"
        f"recent_moves (most recent last): {recent_moves}\n"
        f"pose_tool_available={request.pose_tool_available}"
    )


def _render_open_q_block(request: RouterRequest) -> str:
    if not request.open_question_has_pending:
        return "=== Open question ===\n(none — no question in flight)"
    stem = _clip(request.open_question_stem or "", 400)
    return (
        "=== Open question ===\n"
        f"In flight: {stem!r}"
    )


def _render_grader_block(request: RouterRequest) -> str:
    if request.grader_verdict is None:
        return (
            "verdict: (none — no graded attempt this turn; either the "
            "input is a help-request / meta input, or the turn is "
            "opening / transitional)"
        )
    reason = request.grader_reason_code or ""
    return (
        f"verdict: {request.grader_verdict.value}\n"
        f"reason_code: {reason or '(none)'}"
    )


def _render_transcript_block(turns: list[dict]) -> str:
    if not turns:
        return "(empty — fresh session or no recent turns retained)"
    lines: list[str] = []
    for turn in turns:
        role = (turn.get("role") or "?").strip()
        content = (turn.get("content") or "").strip()
        lines.append(f"[{role}] {_clip(content, 600)}")
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# Re-export ALLOWED_PRINCIPLES so callers that import this module can
# reference the canonical list without reaching into contracts.
__all__ = [
    "ALLOWED_PRINCIPLES",
    "SHARED_ROUTER_SYSTEM",
    "render_router_user_prompt",
]
