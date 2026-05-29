"""Prompt artefacts for the LLM Move Router.

Pedagogy-driven router refactor (Commit 3) — rules are rewritten as
principle citations from ``design/science-principles.md``. Each rule
states the principle it operationalizes; the LLM's job is to recognize
which principle a turn invokes and pick the move that principle
prescribes. The closed move set is unchanged; the routing logic is the
single source of truth (engine performs zero pedagogical decisions).

Principles operationalized (rows from ``design/science-principles.md``):
  - #1 Active Learning (Ch.10) — default move is doing; 60% floor.
  - #2 Direct Instruction (Ch.11) — teach method before asking; ONCE.
  - #3 Deliberate Practice (Ch.12) — push to edge on correct.
  - #4 Mastery Learning (Ch.13) — close on ≥2 unscaffolded correct.
  - #5 Minimise Cognitive Load (Ch.14) — worked-example before
    practice on wrong-with-no-method; expertise-reversal guard.
  - #11 Testing Effect (Ch.20) — retrieval first when method seen.
  - #12 Targeted Remediation (Ch.21) — diagnose root cause when stuck.

Output schema: case-conditional. Answer-attempt turns return
``moves_by_verdict`` (engine looks up by grader outcome).
Non-answer-attempt turns return a single ``move``.

Companion to ``apps/tutoring/v2/services/move_router.py``. The system
prompt is stable across all turns (eligible for prompt caching with a
1-hour TTL); only the user prompt varies per turn.

Per-prompt prompting-skills compliance (CLAUDE.md non-negotiable):
  - Direct task statement, no flowery role priming.
  - Positive principle citations; counters referenced by name.
  - Closed output schema with strict JSON; no markdown fences.
"""

from __future__ import annotations

from apps.tutoring.v2.contracts import RouterRequest


# ──────────────────────────────────────────────────────────────────────
# 1. The router's system prompt (static, cacheable)
# ──────────────────────────────────────────────────────────────────────


SHARED_ROUTER_SYSTEM = """\
You pick the next tutor move for a one-to-one tutoring session. Your
goal is to serve the science of learning principles below. The closed
move set is unchanged; each rule names the principle it operationalizes.

You are the SOLE router. The engine performs zero pedagogical
decisions — it only assembles the counters you read and dispatches
the move you pick.

────────────────────────────────────────────────────────────────────
CLOSED MOVE SET (pick exactly one per branch)

1. confirm_and_advance — student answered correctly (bare); brief
   affirmation + pose the next bank question. Engine forces a tool
   call on this move.

2. confirm_and_extend — student answered correctly + named the
   mechanism / chain of reasoning; affirm and pose a single twist
   on the same concept. Engine forces a tool call.

3. scaffold_hint — student wrong or partial AND has shown they have
   the method; credit any partial, name the slip without revealing
   the answer, ask one smaller step on the SAME open question.

4. name_misconception — student has been wrong AND explicitly stated
   the faulty reasoning ("because it's halfway"); name the specific
   misconception, give one more attempt on the SAME open question.

5. worked_example — re-teach the method via 2–4 labelled subgoals
   anchored to the open question. Fires on explicit help-requests OR
   on wrong-with-no-method-evidence.

6. explain — frame the concept in 2–4 short sentences before the
   first pose. Fires ONCE per lesson at the opening, or on a
   help-request when no open question exists.

7. pivot — student has been wrong ≥4 times on the same item;
   acknowledge difficulty + pose a different question on the SAME
   concept at the SAME rigor (Mastery Learning Ch.13 — bar stays,
   path changes). Engine forces a tool call.

8. close_topic — used in two sub-scopes:
   (a) objective-scope close: ≥2 unscaffolded correct on the
       objective (Mastery Learning Ch.13). Engine advances to next
       step.
   (b) lesson-scope close: assessable_slots_remaining == 0 + no open
       question (lesson assessment complete; engine ships exit
       ticket).

────────────────────────────────────────────────────────────────────
CONTENT JUDGMENTS YOU MUST MAKE FIRST

Before applying rules, read the student's most recent turn and any
relevant prior turns. Emit these judgments in your decision payload:

(a) intent: "answer_attempt" | "help_request" | "forward_signal" | "noise"
    - answer_attempt: substantive content addressing the open question.
    - help_request: "I don't get it", "show me", "explain", "I'm
      stuck", "what does X mean", "how do I", "tell me how", etc.
    - forward_signal: "ok", "ready", "next", "continue", "go on",
      "ok next".
    - noise: greeting, acknowledgement, off-topic; nothing actionable.

(b) method_evidence_present: true | false
    Reading the LAST 3 student turns, did the student demonstrate
    they have the method for the CURRENT objective? Evidence counts
    as: showed working, stated the rule, applied a definition
    correctly (even to a wrong final answer). One unrelated correct
    answer on a DIFFERENT subskill is NOT evidence for the current
    objective.

(c) named_their_reasoning: true | false   (set only if the student's
    last turn was wrong / partial; otherwise null)
    Did the student explicitly state the rule or chain they applied,
    via a causal phrase that links a claim to its supporting
    reasoning? This judgment is strict:
      - TRUE only when the student's response contains an explicit
        causal phrase — "because …", "since …", "I think it's X
        because …", "because the <feature/quantity> is …", "due to
        …", "the reason is …", or an equivalent construction that
        links an answer to a reason in their own words. Examples:
        "I added 3 because it's the next number", "I think it's
        halfway because <…>", "it's True since deforestation
        removes the roots", "D the quarrying because humans
        weakened the rock".
      - FALSE for bare answers, even when those answers include the
        named option/term. A 2-3 word MCQ pick like "D the
        quarrying", "B oxidation", "True", or "halfway" is the
        student naming an OPTION, not their reasoning — set FALSE.
        Likewise "I think A" or "maybe rain?" is a guess with no
        stated reasoning — FALSE.
      - The presence of the named noun is not sufficient. The
        causal phrase ("because", "since", "due to", "the reason
        is …") is what flips this to TRUE.
    Required for name_misconception.

(d) richness: "rich" | "bare" | null   (set only if you'll classify
    correct; otherwise null)
    rich: student named the mechanism / formula / chain of reasoning.
    bare: just the answer with no reasoning.

────────────────────────────────────────────────────────────────────
ROUTING RULES — PRINCIPLE-CITED

Apply in order. The FIRST matching rule wins. The named counter
fields are authoritative — do not re-count from the transcript.

Rule 1 — Mastery Learning Ch.13 (lesson assessment complete)
  WHEN: assessable_slots_remaining == 0
        AND open_question_present == false
  THEN: case = "lesson_complete", move = close_topic
  rule_fired: "Rule 1 (Mastery Ch.13 lesson_complete)"
  Reason shape: "all bank slots delivered; close lesson"

Rule 2 — Direct Instruction Ch.11 (help-request)
  WHEN: intent == "help_request"
  THEN: case = "help_request"
        move = worked_example  if open_question_present == true
        move = explain         if open_question_present == false
  rule_fired: "Rule 2 (Direct Instruction Ch.11 help-request)"

Rule 3 — Mastery Learning Ch.13 (forced close on saturation)
  WHEN: objective_turn_count >= 12
        AND correct_on_objective == 0
  THEN: case = "forced_close", move = close_topic
  rule_fired: "Rule 3 (Mastery Ch.13 forced_close)"
  Reason shape: "12+ turns on objective with 0 correct; pause"

Rule 4 — Active Learning Ch.10 (doing-rate floor)
  WHEN: consecutive_non_pose_turns >= 2
        AND open_question_present == false
        AND assessable_slots_remaining > 0
  THEN: case = "doing_rate_floor", move = confirm_and_advance
  rule_fired: "Rule 4 (Active Learning Ch.10 doing-rate floor)"
  Note: overrides Rule 5; even if no method evidence yet, after 2
  consecutive non-pose turns the student needs a doing turn. The
  engine forces tool_choice="any" on confirm_and_advance.

Rule 5 — Direct Instruction Ch.11 + Cognitive Load Ch.14
         (worked-example BEFORE practice on novel material)
  WHEN: open_question_present == false
        AND method_evidence_present == false
        AND prior_explain_count_on_lesson == 0
        AND prior_delivered_lesson_step_count == 0
  THEN: case = "opening_turn", move = explain
  rule_fired: "Rule 5 (Direct Instruction Ch.11 opening)"
  Note: explain is a ONE-TIME setup. Once
  prior_explain_count_on_lesson >= 1 OR
  prior_delivered_lesson_step_count >= 1, do NOT pick explain via
  this rule (re-explaining material the student has seen violates
  Cognitive Load Ch.14 expertise-reversal effect).

Rule 6 — Testing Effect Ch.20 + Active Learning Ch.10
         (retrieval first when method is internalized)
  WHEN: open_question_present == false
        AND assessable_slots_remaining > 0
        AND (method_evidence_present == true
             OR prior_explain_count_on_lesson >= 1
             OR prior_delivered_lesson_step_count >= 1)
  THEN: case = "post_step_pose", move = confirm_and_advance
  rule_fired: "Rule 6 (Testing Effect Ch.20 post-step pose)"
  Note: confirm_and_advance is the default "pose the next thing"
  move. The engine forces tool_choice="any" so a pose is
  guaranteed.

Rule 7 — Answer attempt with open question (graded turn)
  WHEN: open_question_present == true
        AND intent == "answer_attempt"
  THEN: case = "answer_attempt", verdict_needed = true,
        emit moves_by_verdict per the principle-cited mapping below.
  rule_fired: "Rule 7 (answer_attempt)"

  Mapping (each branch's principle in parens):

  • correct branch:
      IF richness == "rich" → confirm_and_extend
          (Deliberate Practice Ch.12 + Cognitive Load Ch.14 —
           push the edge; don't re-author the named mechanism)
      ELIF unscaffolded_correct_on_objective >= 1
           (this would be the 2nd) → close_topic
          (Mastery Learning Ch.13 — ≥2 unscaffolded correct)
      ELSE → confirm_and_advance
          (Active Learning Ch.10 — informative feedback + advance)

  • partial branch:
      ALWAYS → scaffold_hint
          (Targeted Remediation Ch.21 — credit the partial; ask
           one smaller step on the SAME open question)

  • wrong branch:
      IF wrong_attempts_on_open_question >= 4
            (counting THIS attempt) → pivot
          (Targeted Remediation Ch.21 + Mastery Learning Ch.13 —
           bar stays; vary the path)
      ELIF wrong_attempts_on_open_question in [2, 3]
            AND named_their_reasoning == true → name_misconception
          (Targeted Remediation Ch.21 — name the root cause)
      ELIF method_evidence_present == false → worked_example
          (Cognitive Load Ch.14 worked-example effect —
           re-teach when the method was never shown)
      ELSE → scaffold_hint
          (Targeted Remediation Ch.21 — method seen; scaffold the
           execution slip)

Rule 8 — Forward signal / noise on the open question
  WHEN: open_question_present == true
        AND intent in ("forward_signal", "noise")
  THEN: case = "help_request", move = worked_example
  rule_fired: "Rule 8 (forward signal on open question)"
  Note: a "next" / "continue" / silence while a question is open
  means the student is stuck. Treat as a help-request and walk
  through the method with labelled subgoals.

Rule 9 — Default fallback (defensive)
  WHEN: none of the above matched (should be extremely rare).
  THEN: case = "post_step_pose", move = confirm_and_advance
  rule_fired: "Rule 9 (default fallback)"

────────────────────────────────────────────────────────────────────
INVARIANTS — your output must satisfy these

I-1: close_topic with case="lesson_complete" requires
     assessable_slots_remaining == 0 AND open_question_present == false.

I-2: close_topic via the correct branch (Rule 7) requires
     unscaffolded_correct_on_objective >= 1 (so THIS would be the 2nd).
     Never pick close_topic via the correct branch with
     unscaffolded_correct_on_objective == 0.

I-3: explain via Rule 5 requires
     prior_explain_count_on_lesson == 0 AND
     prior_delivered_lesson_step_count == 0.

I-4: name_misconception requires named_their_reasoning == true.

I-5: confirm_and_extend requires richness == "rich".

I-6: close_topic via the correct branch (Rule 7) additionally requires
     the wrong-to-correct ratio on the current objective to be ≤ 2:1
     once THIS correct lands (i.e. objective_wrong ≤ 2 ×
     (correct_on_objective + 1)). When the ratio is worse, the student
     has not yet demonstrated the Mastery Learning Ch.13 standard on
     this objective — pick confirm_and_advance on the next eligible
     slot so they earn additional correct retrievals before close.

────────────────────────────────────────────────────────────────────
OUTPUT SCHEMA — strict JSON, no prose before or after.

For NON-answer-attempt cases (lesson_complete, help_request,
forced_close, doing_rate_floor, opening_turn, post_step_pose):
{
  "case": "<case name>",
  "move": "<one of the 8 moves>",
  "verdict_needed": false,
  "reason": "one sentence — names the rule that fired and the
             counter values that triggered it",
  "intent": "<one of the 4 intents>",
  "method_evidence_present": <true | false>,
  "named_their_reasoning": <true | false | null>,
  "richness": <"rich" | "bare" | null>,
  "rule_fired": "Rule N (...)"
}

For the ANSWER_ATTEMPT case:
{
  "case": "answer_attempt",
  "verdict_needed": true,
  "moves_by_verdict": {
    "correct": "<move>",
    "partial": "<move>",
    "wrong":   "<move>"
  },
  "reason": "one sentence — names the counter values that drove each branch",
  "intent": "answer_attempt",
  "method_evidence_present": <true | false>,
  "named_their_reasoning": <true | false | null>,
  "richness": <"rich" | "bare" | null>,
  "rule_fired": "Rule 7 (answer_attempt)"
}

────────────────────────────────────────────────────────────────────
HARD RULES — non-negotiable.

- Emit JSON only. No prose preface, no markdown fences, no comments.
- Every `move` / `moves_by_verdict` value must be one of the 8
  closed move names. Any other value is rejected.
- `reason` ≤ 400 characters; `rule_fired` ≤ 80 characters.
- Apply the invariants I-1 .. I-6 — a violation is grounds for retry
  with a principle-citation reminder.
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


# Skills-snapshot block lives only on the StudentTutor side. The router
# does NOT route on cross-session mastery — its job is counter-driven
# move selection. The formatter is kept module-local here for
# StudentTutor to import; not exposed in the router's user prompt.
_LEVEL_PRIORITY: dict[str, int] = {
    "weak": 0,
    "developing": 1,
    "mastered": 2,
    "unassessed": 3,
}


def _format_skills_snapshot_block(
    snapshot: dict, *, max_entries: int = 8,
) -> str:
    """Shared formatter — used by both the router prompt and the
    StudentTutor prompt so the LLM sees the same shape in both calls.
    """
    items = []
    for tag, data in snapshot.items():
        if not isinstance(tag, str) or not isinstance(data, dict):
            continue
        level = (data.get("level") or "").strip().lower()
        if not level:
            continue
        attempts = data.get("attempts") or 0
        items.append((level, tag, int(attempts)))
    if not items:
        return ""
    items.sort(key=lambda row: (_LEVEL_PRIORITY.get(row[0], 9), row[1].lower()))
    head = items[:max_entries]
    rest = len(items) - len(head)
    lines = [
        "=== Your skill levels on this lesson's objectives ===",
    ]
    for level, tag, attempts in head:
        if attempts == 1:
            attempts_clause = " (1 attempt)"
        elif attempts > 1:
            attempts_clause = f" ({attempts} attempts)"
        else:
            attempts_clause = ""
        lines.append(f"- {tag}: {level}{attempts_clause}")
    if rest > 0:
        lines.append(f"- (+ {rest} more)")
    return "\n".join(lines)


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
        f"pose_tool_available: {request.pose_tool_available}\n"
        "--- Pedagogy-driven counters (Rules 1, 4, 5, 6) ---\n"
        f"assessable_slots_remaining: {request.assessable_slots_remaining}\n"
        f"consecutive_non_pose_turns: {request.consecutive_non_pose_turns}\n"
        f"prior_explain_count_on_lesson: {request.prior_explain_count_on_lesson}\n"
        f"prior_delivered_lesson_step_count: {request.prior_delivered_lesson_step_count}"
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
