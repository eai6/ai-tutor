"""Question-bank helpers for the no-authoring tutor (P1 of
memory/tutor_no_authoring_plan.md).

The runtime tutor must NEVER author its own questions. Every question
posed during a session comes from the published, teacher-verified bank:

  - LessonStep.teacher_script + LessonStep.expected_answer (canonical
    practice question per step)
  - ExitTicketQuestion rows where parent ExitTicket.is_published=True

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
    from apps.tutoring.models import ExitTicketQuestion
    bank_qs = ExitTicketQuestion.objects.filter(
        exit_ticket__lesson=lesson,
        exit_ticket__is_published=True,
    ).order_by('order_index')
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
    concept_tag: str,
    max_candidates: int = CANDIDATES_PER_STEP,
) -> List:
    """Return up to N pool questions whose concept_tag matches.

    STRICT: no fallback. If the current step's concept_tag has no
    matches in the pool, returns []. The previous "any same-lesson
    question" fallback was leaking later-step questions onto earlier
    steps (e.g. step 1 surfacing a step-4 question because the pool
    is sampled across the whole lesson).

    The caller is expected to fall back to slot 0 (the current
    step's own teacher_script) when the bank is empty for this step.
    """
    if not pool:
        logger.info("[QuestionTool] pick_candidates_for_step: empty pool")
        return []
    tag = (concept_tag or '').strip()
    if not tag:
        logger.info("[QuestionTool] pick_candidates_for_step: no concept_tag — returning []")
        return []
    matches = [q for q in pool if (q.concept_tag or '').strip() == tag]
    logger.info(
        "[QuestionTool] pick_candidates_for_step: tag='%s' pool=%d matches=%d",
        tag, len(pool), len(matches),
    )
    return matches[:max_candidates]


def pick_published_for_concept_tag(
    lesson,
    concept_tag: str,
    max_candidates: int = 1,
):
    """Query the published bank directly for matches by tag.

    STRICT: no cross-tag fallback. Returns only questions matching
    the requested EO or concept_tag. If neither matches, returns [].

    The previous fallback ("any published bank question for this
    lesson") leaked later-step questions onto earlier steps. Callers
    that want a cross-step bank must explicitly pass each step's tag.

    Match precedence:
      1. enabling_objective exact match — narrow sub-objective targeting
      2. concept_tag exact match — broad learning-objective grouping
    """
    from apps.tutoring.models import ExitTicketQuestion
    base = ExitTicketQuestion.objects.filter(
        exit_ticket__lesson=lesson,
        exit_ticket__is_published=True,
    )
    tag = (concept_tag or '').strip()
    if not tag:
        logger.info(
            "[QuestionTool] pick_published_for_concept_tag: no tag → []"
        )
        return []
    matches = list(
        base.filter(enabling_objective=tag).order_by('order_index')[:max_candidates]
    )
    if matches:
        logger.info(
            "[QuestionTool] pick_published_for_concept_tag: eo='%s' matches=%d",
            tag, len(matches),
        )
        return matches
    matches = list(
        base.filter(concept_tag=tag).order_by('order_index')[:max_candidates]
    )
    logger.info(
        "[QuestionTool] pick_published_for_concept_tag: concept_tag='%s' matches=%d",
        tag, len(matches),
    )
    return matches


def build_remediation_requiz_queue(
    lesson,
    failed_eos: List[str],
    walkthrough_question_ids: Optional[List[int]] = None,
    seed: int = 0,
    per_eo: int = 2,
) -> List:
    """Build the post-walkthrough re-quiz queue (P5).

    For each failed EO (in lesson-EO order — caller controls ordering),
    pick ``per_eo`` fresh published-bank questions tagged to that EO,
    excluding any the student has already walked through. Within each
    EO, the pick is randomised by ``seed`` so retakes draw a different
    sample each time — gives the student a VARIETY of questions per EO
    across attempts (per the explicit user requirement).

    Falls back to walked questions when no fresh ones exist for an EO,
    rather than skipping the EO entirely. The re-quiz must consistently
    cover every failed EO.

    Returns a flat list of ``ExitTicketQuestion`` rows in EO-then-pick
    order (all EO-1 picks, then all EO-2 picks, ...). Empty list when
    no failed EOs supplied.
    """
    from django.db.models import Q
    from apps.tutoring.models import ExitTicketQuestion
    if not failed_eos:
        return []
    rng = random.Random(seed)
    walked = set(walkthrough_question_ids or [])
    queue: List = []
    seen: set = set()
    for eo in failed_eos:
        eo_key = (eo or '').strip()
        if not eo_key:
            continue
        base = ExitTicketQuestion.objects.filter(
            exit_ticket__lesson=lesson,
            exit_ticket__is_published=True,
        ).filter(
            Q(enabling_objective=eo_key) | Q(concept_tag=eo_key),
        )
        fresh = [q for q in base if q.id not in walked and q.id not in seen]
        if not fresh:
            # Allow re-using walked questions before skipping the EO —
            # consistent EO coverage matters more than freshness.
            fresh = [q for q in base if q.id not in seen]
        if not fresh:
            continue
        chosen = rng.sample(fresh, min(per_eo, len(fresh)))
        for q in chosen:
            queue.append(q)
            seen.add(q.id)
    return queue


def render_bank_block(
    step,
    candidates: List,
) -> Tuple[str, Dict[int, object]]:
    """Render the <question_bank> XML block for the system prompt.

    The block is the LLM's *only* allowed source for new questions:
      [0] is the current step's own teacher_script (canonical practice
          question for this step)
      [1..N] are the candidate bank questions for this step's concept

    The LLM picks one by emitting |||QUESTION:N||| as the LAST line of
    its response. The server intercepts and renders the bank entry's
    text verbatim — the LLM never speaks the question stem itself.

    Returns (block_text, id_map). id_map is {1-indexed-id: object} where
    object is either the LessonStep (for id 0) or an ExitTicketQuestion
    (for ids 1..N). Used by parse_question_signal to look up the chosen
    entry.
    """
    id_map: Dict[int, object] = {SENTINEL_NO_QUESTION: step}
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
        "  ONLY path is: invoke pose_question(slot=N) with N from the\n"
        "  list below. Slot 0 = current step's canonical question.\n"
        "  Slots 1+ = exit-ticket bank questions for this step's concept."
    )

    # Slot 0 — the step's own canonical practice question.
    teacher_script = (getattr(step, 'teacher_script', '') or '').strip()
    expected = (getattr(step, 'expected_answer', '') or '').strip()
    lines.append(f"  [0] (current step) {teacher_script[:300]}")
    if expected:
        lines.append(f"      expected_answer: {expected[:120]}")

    # Slots 1..N — bank candidates.
    for i, q in enumerate(candidates, start=1):
        id_map[i] = q
        stem = (getattr(q, 'question_text', '') or '').strip()
        correct = _correct_answer_for_log(q)
        tag = (getattr(q, 'concept_tag', '') or '').strip()
        line = f"  [{i}] {stem[:300]}"
        meta_bits = []
        if tag:
            meta_bits.append(f"concept={tag}")
        if correct:
            meta_bits.append(f"answer={correct}")
        if meta_bits:
            line += f"   ({', '.join(meta_bits)})"
        lines.append(line)
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
    """
    if entry is None:
        return ''
    # LessonStep — pose the canonical practice question.
    teacher_script = getattr(entry, 'teacher_script', None)
    if teacher_script is not None:
        return teacher_script.strip()

    # ExitTicketQuestion — render stem + options if MCQ.
    stem = (getattr(entry, 'question_text', '') or '').strip()
    qtype = getattr(entry, 'question_type', 'mcq') or 'mcq'
    if qtype != 'mcq':
        return stem
    options = []
    for letter in ('A', 'B', 'C', 'D'):
        opt = (getattr(entry, f'option_{letter.lower()}', '') or '').strip()
        if opt:
            options.append(f"  {letter}) {opt}")
    if not options:
        return stem
    return stem + "\n\n" + "\n".join(options)
