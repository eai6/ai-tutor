"""Sampling helpers — build BenchmarkItem snapshots from production SessionTurns.

Separated from the management command for testability. Pure-ish functions:
queries the DB, but the snapshot construction itself takes no I/O.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from django.db.models import Q, QuerySet

from apps.benchmark.autopopulate import derive_suggested_labels
from apps.tutoring.models import SessionTurn


# Map of Course.subject_type → BenchmarkItem.Subject.value
_SUBJECT_TYPE_MAP = {
    'mathematics': 'math',
    'math': 'math',
    'humanities': 'geography',
    'geography': 'geography',
    'science': 'science',
}


def map_subject(course_subject_type: str, course_title: str = '') -> str:
    """Resolve a course's subject_type field to the benchmark subject enum.

    Falls back to 'other' when the subject doesn't match a known mapping.
    Heuristic on title for empty subject_type (legacy rows).
    """
    if course_subject_type:
        mapped = _SUBJECT_TYPE_MAP.get(course_subject_type.lower())
        if mapped:
            return mapped
    # Fallback heuristic for empty subject_type rows
    title_lower = (course_title or '').lower()
    if any(k in title_lower for k in ('math', 'algebra', 'angles', 'geometry', 'arithmetic')):
        return 'math'
    if any(k in title_lower for k in ('geography', 'map', 'climate', 'region')):
        return 'geography'
    return 'other'


# Minimal anonymizer — replaces obvious names/identifiers in free text.
# Production traces from the Seychelles pilot may contain student names in
# tutor responses or student inputs; this is a basic defensive strip.
# Phase 2.x can extend this with a more thorough PII scrubber if needed.
_NAME_RE = re.compile(
    r'\b(?:Hi|Hello|Hey|Welcome,?)\s+([A-Z][a-z]+)(?:\s+[A-Z][a-z]+)?\b'
)


def anonymize(text: str) -> str:
    """Replace 'Hi <Name>' / 'Hello <Name> <Surname>' with placeholders.

    Best-effort only. Free-text mentions deeper in the conversation are
    left alone — the more invasive scrub belongs in a separate PII pass
    when we're ready to share benchmark items externally.
    """
    if not text:
        return text
    return _NAME_RE.sub(r'\1 [STUDENT]', text)


def build_item_snapshot(tutor_turn: SessionTurn) -> Dict:
    """Build the frozen snapshot dict for one tutor turn.

    Shape per memory/eval_benchmark_v2_simplified.md:

        {
          "item": {
            "item_id", "subject", "lesson_id", "lesson_objective",
            "current_step_type", "conversation_history": [...],
            "student_turn": "..."
          },
          "production": {
            "tutor_response": "...",
            "pipeline_trace": { ...metadata + judge_outputs subset... },
            "suggested_labels": [...]   # auto-derived from pipeline_trace
          }
        }
    """
    session = tutor_turn.session
    lesson = session.lesson
    course = lesson.unit.course

    # Find the student turn immediately preceding this tutor turn.
    student_turn = SessionTurn.objects.filter(
        session=session,
        role='student',
        created_at__lt=tutor_turn.created_at,
    ).order_by('-created_at').first()

    # Conversation history: every prior turn (student + tutor) BEFORE the
    # student turn that triggered this tutor response. Excludes the
    # immediate student turn itself (that lives in `student_turn`).
    if student_turn:
        history_qs = SessionTurn.objects.filter(
            session=session,
            created_at__lt=student_turn.created_at,
        ).order_by('created_at')
    else:
        history_qs = SessionTurn.objects.none()

    conversation_history = [
        {
            'turn_id': t.id,
            'role': t.role,
            'text': anonymize(t.content or ''),
        }
        for t in history_qs
    ]

    metadata = tutor_turn.metadata or {}
    judge_outputs = tutor_turn.judge_outputs or {}

    # Trim pipeline_trace to the high-signal subset used by the rubric.
    pipeline_trace = {
        'is_correct': metadata.get('is_correct'),
        'eval_layer': metadata.get('eval_layer'),
        'eval_reasoning': metadata.get('eval_reasoning'),
        'step_index': metadata.get('step_index'),
        'step_type': metadata.get('step_type'),
        'working_state': metadata.get('working_state'),
        'working_first_error_idx': metadata.get('working_first_error_idx'),
        'working_final_claim': metadata.get('working_final_claim'),
        'working_expected': metadata.get('working_expected'),
        'validator_passed': metadata.get('validator_passed'),
        'validator_issues': list(metadata.get('validator_issues') or []),
        'rule_violations': list(metadata.get('rule_violations') or []),
        'regenerated': metadata.get('regenerated'),
        'regen_reason': metadata.get('regeneration_reason'),
        'regen_cycles': metadata.get('regen_cycles'),
        'bare_answer_flagged': metadata.get('bare_answer_flagged'),
        'praise_stripped': metadata.get('praise_stripped'),
        # Phase 2.2.5 — bounded history window the judges saw (0 on
        # legacy turns). Lets the benchmark slice agreement by
        # history-aware vs not.
        'judge_history_turns': metadata.get('judge_history_turns', 0),
        # Per-cycle regen audit (Phase 2.x). When regenerated=True,
        # carries one entry per cycle with per-candidate text preview
        # + judge_outputs so the annotation UI can show what each
        # regen attempt looked like. {} on turns that didn't regen.
        'regen_audit': metadata.get('regen_audit', {}),
        # Prompt-pack fingerprints (Phase 2.x) — tutor system prompt
        # hash + per-judge prompt hashes. Lets the annotation UI show
        # WHICH prompt revisions produced a given response/verdict so
        # behaviour shifts can be attributed to specific changes.
        # {} on legacy turns predating the prompt_pack capture.
        'prompt_pack': metadata.get('prompt_pack', {}),
        # Full per-judge breakdown — for benchmark scoring + auto-population
        'judge_outputs': judge_outputs,
    }

    suggested_labels = derive_suggested_labels(metadata, judge_outputs)

    # Resolve step info — best-effort because SessionTurn.step is often NULL.
    step_type = metadata.get('step_type') or ''
    step_id = None
    if tutor_turn.step_id is not None:
        step_id = tutor_turn.step_id

    item = {
        'item_id': make_item_id(course.subject_type, course.title, session.id, tutor_turn.id),
        'session_id': session.id,
        'turn_id': tutor_turn.id,
        'subject': map_subject(course.subject_type or '', course.title or ''),
        'lesson_id': lesson.id,
        'lesson_title': lesson.title or '',
        'lesson_objective': lesson.objective or '',
        'current_step_id': step_id,
        'current_step_type': step_type,
        'conversation_history': conversation_history,
        'student_turn': {
            'turn_id': student_turn.id if student_turn else None,
            'text': anonymize(student_turn.content) if student_turn else '',
        },
    }

    # Attached figure for this turn (Phase 2.4.x) — picked up from
    # SessionTurn.metadata['attached_media'], itself populated centrally
    # in ConversationalTutor._save_turn. Lets the annotation UI render
    # the figure when validating FIGURE_MISMATCH labels.
    attached_media = list(metadata.get('attached_media') or [])

    production = {
        'tutor_response': anonymize(tutor_turn.content or ''),
        'pipeline_trace': pipeline_trace,
        'attached_media': attached_media,
        'suggested_labels': suggested_labels,
    }

    return {'item': item, 'production': production}


def make_item_id(course_subject_type: str, course_title: str,
                 session_id: int, turn_id: int) -> str:
    """Stable, human-readable item ID: e.g. 'MATH_S18_T458'."""
    subj = map_subject(course_subject_type, course_title).upper()
    return f"{subj}_S{session_id}_T{turn_id}"


# ---------------------------------------------------------------------------
# Stratification
# ---------------------------------------------------------------------------

def candidate_tutor_turns(*, subject: Optional[str] = None,
                          lesson_id: Optional[int] = None,
                          since=None,
                          until=None,
                          exclude_sampled: bool = True,
                          require_full_tracking: bool = True,
                          include_synthetic: bool = False) -> QuerySet:
    """Tutor turns eligible for sampling.

    Filters:
    - role='tutor'
    - has non-empty content
    - session has a lesson + course (filters out test/incomplete sessions)
    - optionally restricted to a subject, a specific lesson, and/or a
      created_at time window
    - optionally excludes turns already in the benchmark
    - by default, excludes legacy turns missing Phase 2.2.5 instrumentation
      (see ``require_full_tracking``)
    - by default, excludes synthetic-student sessions (see ``include_synthetic``)

    Args:
        subject: 'math' / 'geography' / 'science' — maps back to
            Course.subject_type. None = all subjects.
        lesson_id: limit to one Lesson PK. Useful for prompt-iteration
            workflows that re-sample the same lesson before/after a
            change.
        since: datetime — only include tutor turns created AT or AFTER
            this timestamp. Use to focus on traffic produced after a
            specific commit / pipeline change.
        until: datetime — only include tutor turns created STRICTLY
            BEFORE this timestamp. Pair with ``since`` for a window.
        require_full_tracking: when True (default), only return turns
            that have both ``judge_outputs`` populated (per-judge
            breakdown, shipped 2026-05-11) AND ``metadata`` carrying
            the ``judge_history_turns`` key (history-aware judges,
            shipped 2026-05-12). This is the cohort with complete
            quality-tracking data; pre-Phase-2.2.5 turns are skipped.
            Pass False to include legacy traces (useful as a control
            cohort or for backfilling historical baselines).
        include_synthetic: when False (default), excludes turns from
            sessions tagged ``is_synthetic=True`` by the simulator
            (apps/tutoring/student_sim/). Synthetic-distribution may
            not match real-student distribution, so we don't dilute
            the manual annotation queue by default. Pass True to
            include them as a separate cohort — they're stratified
            into ``synthetic_<persona>`` buckets in
            :func:`stratify`. See memory/llm_student_simulator_plan.md.
    """
    from apps.benchmark.models import BenchmarkItem

    qs = SessionTurn.objects.filter(
        role='tutor',
    ).exclude(
        content='',
    ).select_related(
        'session__lesson__unit__course',
    )

    if not include_synthetic:
        qs = qs.filter(session__is_synthetic=False)

    if subject:
        # Map benchmark subject → Course.subject_type. Canonical values
        # in the SubjectType enum are: math, science, humanities,
        # language, other (geography is part of humanities — there's no
        # separate enum value for it). For math + geography we ALSO
        # include courses whose subject_type is blank but whose title
        # carries the relevant keyword — this matches Course.is_math's
        # title-fallback behavior at apps/curriculum/models.py:96 and
        # rescues legacy / un-classified courses.
        from django.db.models import Q as _Q
        if subject == 'math':
            qs = qs.filter(
                _Q(session__lesson__unit__course__subject_type='math')
                | (_Q(session__lesson__unit__course__subject_type='')
                   & (
                       _Q(session__lesson__unit__course__title__icontains='math')
                       | _Q(session__lesson__unit__course__title__icontains='algebra')
                       | _Q(session__lesson__unit__course__title__icontains='geometry')
                       | _Q(session__lesson__unit__course__title__icontains='calculus')
                   ))
            )
        elif subject == 'geography':
            qs = qs.filter(
                _Q(session__lesson__unit__course__subject_type='humanities')
                | _Q(session__lesson__unit__course__title__icontains='geography')
            )
        elif subject == 'science':
            qs = qs.filter(session__lesson__unit__course__subject_type='science')

    if lesson_id is not None:
        qs = qs.filter(session__lesson_id=lesson_id)

    if since is not None:
        qs = qs.filter(created_at__gte=since)
    if until is not None:
        qs = qs.filter(created_at__lt=until)

    if require_full_tracking:
        # Two-part filter — both must hold:
        # 1. judge_outputs is non-empty (Phase 2.0+ per-judge breakdown)
        # 2. metadata carries judge_history_turns key (Phase 2.2.5
        #    history-aware judges; key present even when value is 0
        #    on the first turn of a session)
        qs = qs.exclude(
            Q(judge_outputs__isnull=True) | Q(judge_outputs={})
        ).filter(metadata__has_key='judge_history_turns')

    if exclude_sampled:
        already = BenchmarkItem.objects.values_list('source_turn_id', flat=True)
        qs = qs.exclude(id__in=list(already))

    # Deterministic ordering — when combined with --seed, this makes
    # sampling fully reproducible (else QuerySet order is DB-dependent).
    return qs.order_by('id')


def stratify(qs: QuerySet) -> Dict[str, List[SessionTurn]]:
    """Bucket tutor turns into stratification groups for sampling.

    v1 buckets (simplified from the v2 plan's 4-bucket scheme):
    - 'wrong_answer'      → metadata.is_correct is False
    - 'validator_flagged' → metadata.validator_passed=False OR regenerated=True
    - 'random'            → everything else
    - 'synthetic_<persona>' → tutor turns from a synthetic-student
      session (TutorSession.is_synthetic=True). Carved out so synthetic
      and real cohorts can be analysed side-by-side without pooling.
      Persona suffix matches `TutorSession.sim_persona`.

    Each turn goes into exactly one bucket (first-match wins so the
    target proportions are recoverable). v2 plan's 'step_transition'
    bucket is deferred — requires cross-turn comparison.
    """
    wrong: List[SessionTurn] = []
    flagged: List[SessionTurn] = []
    random_bucket: List[SessionTurn] = []
    synthetic: Dict[str, List[SessionTurn]] = {}

    for turn in qs:
        session = turn.session
        if getattr(session, 'is_synthetic', False):
            persona = (session.sim_persona or 'unknown').lower()
            key = f'synthetic_{persona}'
            synthetic.setdefault(key, []).append(turn)
            continue

        meta = turn.metadata or {}
        if meta.get('is_correct') is False:
            wrong.append(turn)
        elif (meta.get('validator_passed') is False
              or meta.get('regenerated') is True):
            flagged.append(turn)
        else:
            random_bucket.append(turn)

    out: Dict[str, List[SessionTurn]] = {
        'wrong_answer': wrong,
        'validator_flagged': flagged,
        'random': random_bucket,
    }
    out.update(synthetic)
    return out


def create_benchmark_items(
    *,
    limit: int,
    subject: Optional[str] = None,
    lesson_id: Optional[int] = None,
    since=None,
    until=None,
    require_full_tracking: bool = True,
    include_synthetic: bool = False,
    seed: Optional[int] = None,
    created_by=None,
) -> Dict:
    """One-call helper for sampling N items and persisting them.

    Shared between the management command and the dashboard UI. Returns
    a dict summarising what happened — caller picks how to render it
    (CLI prints; UI flashes).

    Args:
        include_synthetic: when False (default), excludes synthetic-
            student sessions from the candidate pool. Pass True to
            include synthetic turns; they appear in the breakdown as
            ``synthetic_<persona>`` strata.

    Returns:
        ``{
            'created': int,                 # rows newly inserted
            'skipped': int,                 # rows already existed (idempotent)
            'eligible': int,                # candidate pool size before sampling
            'sampled': int,                 # items returned by sample_proportional
            'stratum_breakdown': {name: count},  # of newly created items
        }``
    """
    from apps.benchmark.models import BenchmarkItem

    candidates = candidate_tutor_turns(
        subject=subject,
        lesson_id=lesson_id,
        since=since,
        until=until,
        require_full_tracking=require_full_tracking,
        include_synthetic=include_synthetic,
    )
    eligible = candidates.count()
    strata = stratify(candidates)
    picks = sample_proportional(strata, limit=limit, seed=seed)

    created = 0
    skipped = 0
    stratum_breakdown: Dict[str, int] = {}
    for stratum_name, turn in picks:
        snapshot = build_item_snapshot(turn)
        defaults = {
            'source_turn': turn,
            'subject': snapshot['item']['subject'],
            'lesson_id': snapshot['item']['lesson_id'],
            'snapshot': snapshot,
            'stratum': stratum_name,
        }
        if created_by is not None:
            defaults['created_by'] = created_by

        _obj, was_created = BenchmarkItem.objects.update_or_create(
            item_id=snapshot['item']['item_id'],
            defaults=defaults,
        )
        if was_created:
            created += 1
            stratum_breakdown[stratum_name] = (
                stratum_breakdown.get(stratum_name, 0) + 1
            )
        else:
            skipped += 1

    return {
        'created': created,
        'skipped': skipped,
        'eligible': eligible,
        'sampled': len(picks),
        'stratum_breakdown': stratum_breakdown,
    }


def sample_proportional(strata: Dict[str, List[SessionTurn]],
                        limit: int,
                        mix: Optional[Dict[str, float]] = None,
                        seed: Optional[int] = None) -> List[tuple[str, SessionTurn]]:
    """Sample from each stratum proportional to ``mix`` (default: 50/30/20).

    Returns a list of (stratum_name, turn) tuples. Total length ≤ limit.
    When a stratum has fewer turns than its share, the deficit is
    redistributed to the other strata in order.
    """
    import random as _random

    if seed is not None:
        rng = _random.Random(seed)
    else:
        rng = _random.Random()

    mix = mix or {
        'wrong_answer': 0.50,
        'validator_flagged': 0.30,
        'random': 0.20,
    }
    assert abs(sum(mix.values()) - 1.0) < 0.01, "mix must sum to 1.0"

    out: List[tuple[str, SessionTurn]] = []
    remaining = limit
    leftover_pool: List[tuple[str, SessionTurn]] = []

    # First pass: take floor(share * limit) from each stratum named in mix.
    for name, share in mix.items():
        target = int(round(share * limit))
        pool = list(strata.get(name, []))
        rng.shuffle(pool)
        take = pool[:target]
        for extra in pool[target:]:
            leftover_pool.append((name, extra))
        for t in take:
            out.append((name, t))
        remaining -= len(take)

    # Synthetic / persona buckets aren't in the default mix, so they
    # never get picked in the first pass. Add their entire content to
    # the leftover pool so the second pass can dip into them when the
    # mix-defined strata can't fill the limit.
    for name, pool in strata.items():
        if name in mix:
            continue
        for t in pool:
            leftover_pool.append((name, t))

    # Second pass: fill any deficit from the leftover pool (random across
    # all strata, including synthetic). Each entry already carries its
    # source stratum name so we don't have to re-scan.
    if remaining > 0 and leftover_pool:
        rng.shuffle(leftover_pool)
        for stratum_name, t in leftover_pool[:remaining]:
            out.append((stratum_name, t))

    return out
