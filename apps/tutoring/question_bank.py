"""Question-bank helpers for the no-authoring tutor (P1 of
memory/tutor_no_authoring_plan.md).

The runtime tutor must NEVER author its own questions. Every question
posed during a session comes from the published, teacher-verified bank:

  - LessonStep.teacher_script + LessonStep.expected_answer (canonical
    practice question per step)
  - ExitTicketQuestion rows whose parent ExitTicket.assessment_type ==
    'exit_ticket' (i.e. lesson-level, not summative). is_published is a
    summative-only flag and is NOT used to gate lesson banks here.

This module is pure helpers — no engine state, no LLM calls. The
conversational_tutor wires them in; the helpers are independently
testable.

Pieces:
  - sample_session_pool: per-session deterministic subset of the bank
  - pick_candidates_for_step: filter the pool by concept_tag, fallback
    to any same-lesson question
  - render_bank_block: produce the <question_bank> XML for the system
    prompt
  - parse_question_signal: extract |||QUESTION:N||| from LLM output
  - render_question_to_prose: server-side verbatim render of the chosen
    bank entry, replacing whatever stem the LLM might have authored
"""

from __future__ import annotations

import logging
import random
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Defaults sized for one session. Small enough to fit the prompt context
# comfortably; large enough that the LLM has variety per step.
POOL_SIZE_PER_LESSON = 12
CANDIDATES_PER_STEP = 5

# 1-indexed catalog starts at 1; we reserve 0 as "no question / use the
# step's own teacher_script" sentinel, mirroring the |||MEDIA:0|||
# convention.
SENTINEL_NO_QUESTION = 0

# Sampling weights per item 4 of memory/curriculum_tutor_v2_plan.md.
# Failed EOs get 5x weight, unattempted 3x, mastered 1x — biases the
# session pool toward sub-skills the student is weak on.
EO_WEIGHT_FAILED = 5
EO_WEIGHT_UNATTEMPTED = 3
EO_WEIGHT_MASTERED = 1


def compute_student_eo_competency(student, lesson) -> Dict[str, str]:
    """Build a per-EO competency status map for one student on one lesson.

    Reads past ``ExitTicketAttempt.answers['eo_competency']`` rows for
    the lesson's exit ticket. Latest attempt wins per EO so that a
    student who fails an EO once and then masters it on a retake is
    correctly tagged 'mastered'.

    Returns ``{eo_text: status}`` where status is one of:
      - ``'mastered'``   — most recent attempt for this EO had every
                           question correct
      - ``'failed'``     — most recent attempt had at least one wrong
      - ``'unattempted'``— EO has never been seen on a graded attempt
                           (or student has no past attempts)

    EOs that don't appear in any past attempt are NOT in the returned
    dict — callers should treat missing keys as ``'unattempted'``.
    """
    from apps.tutoring.models import ExitTicket, ExitTicketAttempt
    if student is None or lesson is None:
        return {}
    et = ExitTicket.objects.filter(lesson=lesson).first()
    if et is None:
        return {}
    # Latest first by completion time, falling back to start time so
    # in-progress attempts still slot in correctly.
    attempts = list(
        ExitTicketAttempt.objects
        .filter(exit_ticket=et, student=student)
        .order_by('-completed_at', '-started_at')
        .values_list('answers', flat=True)
    )
    status: Dict[str, str] = {}
    for answers in attempts:
        if not isinstance(answers, dict):
            continue
        eo_block = answers.get('eo_competency') or {}
        if not isinstance(eo_block, dict):
            continue
        for eo, bucket in eo_block.items():
            eo_key = (eo or '').strip()
            if not eo_key or eo_key in status:
                # Latest attempt wins — earlier attempts don't override
                continue
            if not isinstance(bucket, dict):
                continue
            if bucket.get('is_mastered'):
                status[eo_key] = 'mastered'
            elif (bucket.get('asked') or 0) > 0:
                status[eo_key] = 'failed'
    return status


def _eo_weight(eo: str, competency: Dict[str, str]) -> int:
    """Look up the sampling weight for one EO."""
    if not eo:
        return EO_WEIGHT_UNATTEMPTED
    s = competency.get(eo.strip())
    if s == 'failed':
        return EO_WEIGHT_FAILED
    if s == 'mastered':
        return EO_WEIGHT_MASTERED
    return EO_WEIGHT_UNATTEMPTED


def _question_eo_key(q) -> str:
    """Pick the EO tag we sample by — prefer the specific
    enabling_objective, fall back to the broader concept_tag."""
    return (
        getattr(q, 'enabling_objective', None)
        or getattr(q, 'concept_tag', None)
        or ''
    ).strip()


def _weighted_sample_without_replacement(
    population: List,
    weights: List[int],
    k: int,
    rng: random.Random,
) -> List:
    """Draw ``k`` items from ``population`` weighted by ``weights``,
    without replacement. ``random.sample`` doesn't support weights
    and ``random.choices`` is with-replacement, so we roll our own.
    O(k * n); banks are small (~35) so this is fine."""
    pop = list(population)
    w = list(weights)
    chosen: List = []
    while pop and len(chosen) < k:
        # All-zero weights would crash random.choices — fall back to
        # uniform when nothing has positive mass.
        if not any(w):
            idx = rng.randrange(len(pop))
        else:
            idx = rng.choices(range(len(pop)), weights=w, k=1)[0]
        chosen.append(pop.pop(idx))
        w.pop(idx)
    return chosen


def sample_session_pool(
    lesson,
    seed: int,
    pool_size: int = POOL_SIZE_PER_LESSON,
    student=None,
) -> List:
    """Sample a deterministic per-session pool from the lesson's
    published exit-ticket bank.

    Mirrors the exit-ticket randomisation pattern: the bank is fixed
    per lesson, but each session draws a different subset. Deterministic
    given the seed, so reloading the session reconstructs the same
    pool from engine_state.

    When ``student`` is provided, the draw is biased by the student's
    per-EO competency (failed=5x, unattempted=3x, mastered=1x). When
    ``student`` is None, the draw is uniform — preserves the prior
    behaviour for callers that don't track student state.

    Returns a list of ExitTicketQuestion objects. Empty list if the
    lesson has no published bank yet.
    """
    from apps.tutoring.models import ExitTicket, ExitTicketQuestion
    # ExitTicket.is_published is "Summatives only — when False, students
    # can't take the exam." Lesson-level exit tickets default to
    # is_published=False; using that filter here meant the runtime
    # never saw lesson-level banks even when the teacher dashboard
    # showed them populated. Filter by assessment_type instead.
    #
    # 'matching' questions are excluded from the in-chat tutoring pool —
    # they render awkwardly inline ("70° → ___, choose from: …") and
    # confuse students. Matching stays in the EXIT TICKET (post-lesson
    # modal where the UI can render proper select boxes). Tutoring
    # uses MCQ / short_numeric / short_answer / fill_in_blank only.
    bank_qs = ExitTicketQuestion.objects.filter(
        exit_ticket__lesson=lesson,
        exit_ticket__assessment_type=ExitTicket.AssessmentType.EXIT_TICKET,
    ).exclude(question_type='matching').order_by('order_index')
    bank = list(bank_qs)
    if not bank:
        return []
    if len(bank) <= pool_size:
        return bank
    rng = random.Random(seed)
    if student is None:
        return rng.sample(bank, pool_size)
    competency = compute_student_eo_competency(student, lesson)
    weights = [_eo_weight(_question_eo_key(q), competency) for q in bank]
    return _weighted_sample_without_replacement(bank, weights, pool_size, rng)


def pick_candidates_for_step(
    pool: List,
    enabling_objective: str = '',
    concept_tag: str = '',
    max_candidates: int = CANDIDATES_PER_STEP,
) -> List:
    """Return up to N candidate bank questions for the current step.

    Match precedence (specific → general):
      1. enabling_objective exact match — the structured curriculum
         primitive. ExitTicketQuestions are tagged with the EO they
         test; LessonSteps are tagged with the EO they teach. Match
         these and the bank slot is exactly the right scope.
      2. concept_tag exact match — legacy / coarser grouping. Used
         only when no EO is set (older content that pre-dates the
         EO field on LessonStep).
      3. Random fallback — when the step has no EO/tag or no match,
         return up to N from the session pool. The pool is already
         lesson-scoped and session-seeded, so this is a stable
         random sample of the lesson's published bank, NOT a global
         leak. Bank is never empty if the lesson has any published
         exit-ticket questions.
    """
    if not pool:
        logger.info("[QuestionTool] pick_candidates_for_step: empty pool")
        return []

    eo = (enabling_objective or '').strip()
    if eo:
        matches = [q for q in pool if (q.enabling_objective or '').strip() == eo]
        if matches:
            logger.info(
                "[QuestionTool] pick_candidates_for_step: EO='%s' pool=%d matches=%d (EO_MATCH)",
                eo[:60], len(pool), len(matches),
            )
            return matches[:max_candidates]

    tag = (concept_tag or '').strip()
    if tag:
        matches = [q for q in pool if (q.concept_tag or '').strip() == tag]
        if matches:
            logger.info(
                "[QuestionTool] pick_candidates_for_step: tag='%s' pool=%d matches=%d (TAG_MATCH)",
                tag, len(pool), len(matches),
            )
            return matches[:max_candidates]

    # Random fallback — pool is already lesson-scoped + session-seeded.
    logger.info(
        "[QuestionTool] pick_candidates_for_step: no EO/tag match — "
        "using random pool sample of %d (RANDOM_FALLBACK)",
        min(len(pool), max_candidates),
    )
    return pool[:max_candidates]


def pick_published_for_concept_tag(
    lesson,
    concept_tag: str,
    max_candidates: int = 1,
):
    """Query the published bank directly for matches by tag.

    Match precedence (specific → general):
      1. enabling_objective exact match — preferred curriculum primitive
      2. concept_tag exact match — legacy / coarser grouping
      3. Random fallback — when nothing matches, return up to N
         random questions from the lesson's published bank. Bank is
         never empty if the lesson has any published questions.

    Used by the remediation flow which doesn't go through the per-
    session pool, so we hit the published bank directly.
    """
    from apps.tutoring.models import ExitTicket, ExitTicketQuestion
    # See sample_session_pool — is_published is summative-only.
    base = ExitTicketQuestion.objects.filter(
        exit_ticket__lesson=lesson,
        exit_ticket__assessment_type=ExitTicket.AssessmentType.EXIT_TICKET,
    )
    tag = (concept_tag or '').strip()
    if tag:
        matches = list(
            base.filter(enabling_objective=tag).order_by('order_index')[:max_candidates]
        )
        if matches:
            logger.info(
                "[QuestionTool] pick_published_for_concept_tag: eo='%s' matches=%d (EO_MATCH)",
                tag[:60], len(matches),
            )
            return matches
        matches = list(
            base.filter(concept_tag=tag).order_by('order_index')[:max_candidates]
        )
        if matches:
            logger.info(
                "[QuestionTool] pick_published_for_concept_tag: tag='%s' matches=%d (TAG_MATCH)",
                tag[:60], len(matches),
            )
            return matches

    # Random fallback — order_by('?') is DB-agnostic random.
    matches = list(base.order_by('?')[:max_candidates])
    logger.info(
        "[QuestionTool] pick_published_for_concept_tag: tag='%s' "
        "no match — using %d random from lesson bank (RANDOM_FALLBACK)",
        tag[:60] if tag else "(none)", len(matches),
    )
    return matches


def render_bank_block(
    step,
    candidates: List,
    *,
    include_step_slot: bool = True,
    prereq_questions: Optional[List] = None,
    is_engage_or_warmup: bool = False,
) -> Tuple[str, Dict[int, object]]:
    """Render the <question_bank> XML block for the system prompt.

    Slot inventory:
      [0] = current step's teacher_script when ``include_step_slot=True``
            AND step_type is question-shaped (practice / quiz /
            worked_example). For TEACH/SUMMARY steps the teacher_script
            is teaching content (delivered via the system prompt as
            content-to-deliver, not a posable question), so the
            caller passes include_step_slot=False and slot 0 is
            omitted.
      [1..N] = exit-ticket bank candidates matching this step's EO.
      [N+1..M] = prerequisite-lesson exit-ticket questions
                 (when ``prereq_questions`` is provided, typically only
                 on engage / warmup turns) — labelled clearly so the
                 LLM knows they're for "previous lesson recap" use.

    The LLM picks one by calling the pose_question tool with a slot
    index. Server resolves the slot via id_map and renders the bank
    entry verbatim.
    """
    id_map: Dict[int, object] = {}
    lines: List[str] = ["<question_bank>"]
    lines.append(
        "  HARD RULE — questions you pose MUST come from this bank.\n"
        "  To ask any numerical question, you MUST call the\n"
        "  pose_question tool with a slot index from this bank.\n"
        "  Do NOT type questions in your text response — the tool\n"
        "  is the only legal channel.\n"
        "  Allowed in your text response (no tool needed):\n"
        "    • Pure conceptual scaffolding (\"which rule applies?\",\n"
        "      \"what do you do first?\") — no specific numbers\n"
        "    • Reciting the lesson rule (\"angles around a point\n"
        "      sum to 360°\") — no question being posed\n"
        "  NOT allowed in your text response:\n"
        "    • Inventing a numerical example (\"if angles are 100°,\n"
        "      120°, and 80°…\")\n"
        "    • Hypothetical premises with specific numbers\n"
        "    • Any sentence ending in '?' that contains digits\n"
        "  If you need to pose a question with numerical values, the\n"
        "  ONLY path is to invoke the pose_question tool with a slot\n"
        "  number from the list below. Do NOT type the tool call as\n"
        "  text — the system will not parse it; emit it as a real\n"
        "  tool_use call.\n"
        "  Slot 0 (when listed) = current step's canonical question;\n"
        "  slots 1+ = exit-ticket bank questions for this step's\n"
        "  concept; later slots labelled 'previous lesson recap' are\n"
        "  warmup material from a prerequisite lesson — use those\n"
        "  ONLY for warmup / engage turns."
    )

    # Slot 0 — the step's own canonical practice question.
    # Skipped for step types where teacher_script is teaching content
    # (delivered via the system prompt's CONTENT TO TEACH block) rather
    # than a posable question.
    if include_step_slot:
        id_map[SENTINEL_NO_QUESTION] = step
        teacher_script = (getattr(step, 'teacher_script', '') or '').strip()
        expected = (getattr(step, 'expected_answer', '') or '').strip()
        lines.append(f"  [0] (current step) {teacher_script[:300]}")
        if expected:
            lines.append(f"      expected_answer: {expected[:120]}")

    # Slots 1..N — current-lesson bank candidates.
    next_slot = 1
    for q in candidates:
        id_map[next_slot] = q
        stem = (getattr(q, 'question_text', '') or '').strip()
        correct = _correct_answer_for_log(q)
        tag = (getattr(q, 'concept_tag', '') or '').strip()
        line = f"  [{next_slot}] {stem[:300]}"
        meta_bits = []
        if tag:
            meta_bits.append(f"concept={tag}")
        if correct:
            meta_bits.append(f"answer={correct}")
        if meta_bits:
            line += f"   ({', '.join(meta_bits)})"
        lines.append(line)
        next_slot += 1

    # Prerequisite-lesson questions (warmup recap material).
    if prereq_questions:
        lines.append("")
        lines.append(
            "  Previous-lesson recap questions (use ONLY for warmup /"
            " engage turns to review prior content):"
        )
        for q in prereq_questions:
            id_map[next_slot] = q
            stem = (getattr(q, 'question_text', '') or '').strip()
            correct = _correct_answer_for_log(q)
            prev_lesson_title = ''
            try:
                prev_lesson_title = (
                    q.exit_ticket.lesson.title or ''
                ).strip()
            except Exception:
                pass
            line = f"  [{next_slot}] (previous lesson — {prev_lesson_title[:60]}) {stem[:300]}"
            if correct:
                line += f"   (answer={correct})"
            lines.append(line)
            next_slot += 1
    elif is_engage_or_warmup:
        # Engage / warmup turn but no recap material is available
        # (lesson has no prerequisites, OR the prereqs have no
        # published bank questions). Tell the LLM explicitly so it
        # doesn't try to invent "from last week" warmup questions.
        # Some lessons legitimately have no prereqs — first lesson
        # in a unit, foundational topics, intro lessons.
        lines.append("")
        lines.append(
            "  No previous-lesson recap available for this lesson —"
            " do NOT do a 'from last week' warmup with invented numbers."
            " Open the lesson directly with a CONCEPTUAL hook ('What"
            " do you already know about angles?') or dive into the"
            " step content."
        )

    lines.append("</question_bank>")
    return "\n" + "\n".join(lines) + "\n", id_map


def _correct_answer_for_log(question) -> str:
    """Best-effort one-line correct-answer summary for the bank block.

    For MCQ this is the option letter. For other types we read
    answer_data with a few common shapes. Used only inside the system
    prompt — the student never sees this string.
    """
    qtype = getattr(question, 'question_type', '') or 'mcq'
    if qtype == 'mcq':
        return getattr(question, 'correct_answer', '') or ''
    data = getattr(question, 'answer_data', None) or {}
    if not isinstance(data, dict):
        return ''
    for key in ('model_answer', 'computed', 'correct_answer'):
        v = data.get(key)
        if v is not None:
            return str(v)[:60]
    blanks = data.get('blanks')
    if blanks:
        return ' / '.join(str(b) for b in blanks)[:60]
    return ''


_QUESTION_SIGNAL = re.compile(r'\|\|\|QUESTION\s*:\s*(\d+)\s*\|\|\|')

# New: EO-targeted bank pull. The LLM emits the EO index it wants a
# question for; the server resolves to a published bank question
# tagged with that EO. Lets the LLM ask "give me a question on EO 3"
# without preloading a fixed candidate slate per turn — frees up the
# system prompt size budget AND lets remediation walk through any
# EO without the engine having to predict candidates per step.
_QUESTION_EO_SIGNAL = re.compile(r'\|\|\|QUESTION_EO\s*:\s*(\d+)\s*\|\|\|')


def parse_question_signal(
    text: str,
) -> Tuple[str, Optional[int]]:
    """Extract |||QUESTION:N||| from the response text.

    Returns (clean_text, n_or_None). The signal is always stripped
    so it never reaches the student. n is the integer the LLM picked;
    the caller looks it up in the id_map returned by render_bank_block.
    """
    match = _QUESTION_SIGNAL.search(text)
    if not match:
        return text, None
    clean = (text[:match.start()] + text[match.end():]).rstrip()
    return clean, int(match.group(1))


def parse_question_eo_signal(
    text: str,
) -> Tuple[str, Optional[int]]:
    """Extract |||QUESTION_EO:N||| from the response text.

    Returns (clean_text, eo_index_or_None). Index is 1-based and
    refers to the lesson's enabling_objectives list (the same order
    the tutor sees in the [ENABLING OBJECTIVES] block). The caller
    resolves the index → EO text → published bank question.
    """
    match = _QUESTION_EO_SIGNAL.search(text)
    if not match:
        return text, None
    clean = (text[:match.start()] + text[match.end():]).rstrip()
    return clean, int(match.group(1))


def pick_question_for_eo(
    lesson, eo_text: str, *, exclude_ids: Optional[List[int]] = None,
):
    """Resolve the EO signal — return one published ExitTicketQuestion
    matching the EO, optionally excluding ids the tutor has already
    posed this session. Falls through to ``pick_published_for_concept_tag``
    so it inherits the same EO → concept_tag → fallback chain."""
    candidates = pick_published_for_concept_tag(
        lesson, eo_text, max_candidates=10,
    )
    if not candidates:
        return None
    excl = set(exclude_ids or [])
    for q in candidates:
        if q.id not in excl:
            return q
    # Everything was excluded → return the first anyway so the tutor
    # still has SOMETHING; the caller can dedupe semantically.
    return candidates[0]


def render_question_to_prose(entry) -> str:
    """Render a bank entry to the student-facing prose stem.

    Verbatim — no paraphrasing. For MCQ, includes the lettered options.
    The caller substitutes this string in place of any LLM-authored
    question stem in the response.

    LessonStep field choice:
      - For practice/quiz steps, render LessonStep.question (the
        student-facing question) and append choices for MCQ. The
        teacher_script for those steps is the tutor's setup directive
        ("Now try a similar problem…") — not what should be posed to
        the student.
      - For worked_example, render teacher_script (it's the example
        walkthrough).
      - For other types or when question is empty, fall back to
        teacher_script.
    """
    if entry is None:
        return ''
    # LessonStep — pose the canonical practice question.
    if hasattr(entry, 'teacher_script'):
        step_type = (getattr(entry, 'step_type', '') or '').strip()
        question = (getattr(entry, 'question', '') or '').strip()
        teacher_script = (getattr(entry, 'teacher_script', '') or '').strip()
        if step_type in ('practice', 'quiz') and question:
            atype = (getattr(entry, 'answer_type', '') or '').lower()
            if atype == 'multiple_choice':
                choices = list(getattr(entry, 'choices', None) or [])
                rendered_choices = []
                for i, c in enumerate(choices[:4]):
                    label = chr(ord('A') + i)
                    s = str(c).strip()
                    if s.upper().startswith(f"{label})"):
                        rendered_choices.append(f"  {s}")
                    else:
                        rendered_choices.append(f"  {label}) {s}")
                if rendered_choices:
                    return question + "\n\n" + "\n".join(rendered_choices)
            return question
        if teacher_script:
            return teacher_script
        return question

    # ExitTicketQuestion — render stem with type-appropriate scaffolding.
    stem = (getattr(entry, 'question_text', '') or '').strip()
    qtype = (getattr(entry, 'question_type', 'mcq') or 'mcq').lower()
    answer_data = getattr(entry, 'answer_data', None) or {}
    if not isinstance(answer_data, dict):
        answer_data = {}

    if qtype == 'fill_in_blank':
        # The actual sentence-with-blanks lives in
        # answer_data.text_template — question_text is often just a
        # short label like "Complete the sentence:". Without the
        # template we render a truncated stub the student can't answer.
        # Match the exit-modal frontend, which already pulls
        # text_template (see _partials/exit_modal.html).
        template = (answer_data.get('text_template') or '').strip()
        if template:
            if stem and stem.lower() not in template.lower():
                return f"{stem}\n\n{template}"
            return template
        return stem

    if qtype == 'matching':
        # Render left → right pairing rows so the student sees the
        # actual matching prompt instead of a bare "Match each angle…".
        pairs = answer_data.get('pairs') or []
        if pairs:
            lines = [stem] if stem else []
            lines.append("")
            for p in pairs:
                left = str(p.get('left', '')).strip()
                if left:
                    lines.append(f"  • {left}  →  ___")
            distractors = [
                str(r).strip()
                for r in answer_data.get('distractor_rights', []) or []
                if str(r).strip()
            ]
            right_pool = [str(p.get('right', '')).strip() for p in pairs
                          if str(p.get('right', '')).strip()]
            choices = [c for c in (right_pool + distractors) if c]
            if choices:
                lines.append("")
                lines.append("Choose from: " + ", ".join(choices))
            return "\n".join(lines).strip()
        return stem

    if qtype in ('short_answer', 'data_interpretation', 'short_numeric'):
        # Most short-answer banks store the full prompt in question_text;
        # some carry a data_description / figure_description that adds
        # context. Append when present so the student sees the same
        # framing the exit-ticket modal would show.
        extras = []
        for key in ('data_description', 'figure_description'):
            val = (answer_data.get(key) or '').strip()
            if val and val not in stem:
                extras.append(val)
        if extras:
            return stem + "\n\n" + "\n".join(extras)
        return stem

    if qtype == 'mcq':
        options = []
        for letter in ('A', 'B', 'C', 'D'):
            opt = (getattr(entry, f'option_{letter.lower()}', '') or '').strip()
            if opt:
                options.append(f"  {letter}) {opt}")
        if not options:
            return stem
        return stem + "\n\n" + "\n".join(options)

    # Unknown type — fall back to the bare stem.
    return stem
