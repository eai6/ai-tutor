"""SessionDriver — runs one synthetic-student session end-to-end.

Wires the StudentClient (LLM-as-student) to ConversationalTutor.respond().
Creates a TutorSession tagged is_synthetic=True, loops until termination,
returns a SimResult with transcript + cost breakdown.

See memory/llm_student_simulator_plan.md (Phase 2). Cost ceiling lives
in Phase 3 (apps/llm/cost_estimator.py); for v1 we trust max_turns to
bound cost.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from django.contrib.auth.models import User
from django.db import transaction

from apps.accounts.models import Institution
from apps.curriculum.models import Lesson
from apps.tutoring.models import TutorSession
from apps.tutoring.student_sim.client import StudentClient


logger = logging.getLogger(__name__)


# Username for the dedicated simulator-bot user. Idempotently created
# per institution. NOT a real user — just satisfies the FK on
# TutorSession.student. Filtered out of dashboards by the is_synthetic
# flag on the session, not by the user identity.
SIM_BOT_USERNAME_PREFIX = 'simulator-bot'


@dataclass
class TranscriptTurn:
    """One exchange in the synthetic session."""
    turn_number: int
    role: str  # 'tutor' or 'student'
    content: str
    is_correct: Optional[bool] = None
    phase: str = ''
    show_exit_ticket: bool = False
    is_complete: bool = False


@dataclass
class SimResult:
    """Result of one simulated session."""
    session_id: int
    persona: str
    lesson_id: int
    reason: str  # 'completed' | 'exit_ticket' | 'max_turns' | 'deadlock' | 'error'
    turns: int
    transcript: list[TranscriptTurn] = field(default_factory=list)
    student_tokens_in: int = 0
    student_tokens_out: int = 0
    error: str = ''


def _get_or_create_sim_user(institution: Institution) -> User:
    """Idempotent: one simulator-bot User per institution.

    The user is a real Django User row (it has to be, given the FK on
    TutorSession.student NOT NULL). It's filtered out of dashboards by
    `TutorSession.is_synthetic`, not by user identity. Username pattern:
    ``simulator-bot-<institution.slug>``.
    """
    slug = (institution.slug or 'global').replace('/', '-')
    username = f"{SIM_BOT_USERNAME_PREFIX}-{slug}"[:150]
    user, created = User.objects.get_or_create(
        username=username,
        defaults={
            'email': f'{username}@simulator.local',
            'first_name': 'Simulator',
            'last_name': 'Bot',
            'is_active': False,  # cannot log in via the web UI
        },
    )
    if created:
        # Unusable password — bot has no login path.
        user.set_unusable_password()
        user.save()
        logger.info("Created simulator-bot user %s", username)
    return user


def _normalize_for_deadlock(text: str) -> str:
    """Lowercase + collapse whitespace + drop trailing punctuation. Used to
    compare two tutor responses for the deadlock detector. We don't need
    semantic equivalence — just catch literal repeats from regen loops.
    """
    text = (text or '').lower().strip()
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[.!?]+$', '', text)
    return text


def _is_deadlock(transcript: list[TranscriptTurn]) -> bool:
    """Detect tutor saying the same thing twice in a row.

    Looks at the last two TUTOR turns. If their normalized content
    matches, we're stuck — student can't make progress and the tutor
    isn't varying its move. Loops here would burn budget without
    producing useful signal.
    """
    tutor_turns = [t for t in transcript if t.role == 'tutor']
    if len(tutor_turns) < 2:
        return False
    a = _normalize_for_deadlock(tutor_turns[-1].content)
    b = _normalize_for_deadlock(tutor_turns[-2].content)
    return bool(a) and a == b


def simulate_session(
    *,
    lesson_id: int,
    persona: str,
    max_turns: int = 40,
    institution_id: Optional[int] = None,
    student_user: Optional[User] = None,
) -> SimResult:
    """Drive one full synthetic-student session.

    Args:
        lesson_id: The Lesson to teach. Must exist and have content_status='ready'
            (the tutor will misbehave on empty lessons).
        persona: Persona key from `apps.tutoring.student_sim.personas.PERSONAS`.
        max_turns: Hard cap on tutor↔student exchanges. Counts ONE pair
            (tutor speaks, student replies) as one turn.
        institution_id: Optional; defaults to the lesson's institution
            (or Global if the lesson is platform-wide).
        student_user: Optional User to use as `session.student`. Defaults
            to the auto-created simulator-bot user for the institution.

    Returns:
        SimResult — populated even on error (reason='error', error=str(exc)).
    """
    lesson = Lesson.objects.select_related(
        'unit__course__institution',
    ).get(id=lesson_id)

    # Resolve institution: caller's override > lesson's institution > global
    if institution_id is not None:
        institution = Institution.objects.get(id=institution_id)
    else:
        institution = lesson.unit.course.institution or Institution.get_global()

    if student_user is None:
        student_user = _get_or_create_sim_user(institution)

    with transaction.atomic():
        session = TutorSession.objects.create(
            student=student_user,
            lesson=lesson,
            institution=institution,
            status=TutorSession.Status.ACTIVE,
            is_synthetic=True,
            sim_persona=persona,
        )

    student = StudentClient(persona, institution_id=institution.id)
    transcript: list[TranscriptTurn] = []
    student_tokens_in = 0
    student_tokens_out = 0

    def _result(reason: str, turns: int) -> SimResult:
        return SimResult(
            session_id=session.id, persona=persona, lesson_id=lesson_id,
            reason=reason, turns=turns, transcript=transcript,
            student_tokens_in=student_tokens_in,
            student_tokens_out=student_tokens_out,
        )

    try:
        # Engine dispatch (mirrors views.py): honor SIMPLE_TUTOR_ENGINE
        # so eval / synthetic-traffic runs against the same engine the
        # live web app serves. Wraps simple_tutor's dict payloads into
        # a TutorMessage-shaped namespace so the driver doesn't need
        # to know which engine produced the turn.
        from apps.tutoring import simple_tutor as _simple_tutor
        _use_simple = _simple_tutor.is_enabled()

        if _use_simple:
            from apps.tutoring.simple_tutor.engine import (
                start_for_view as _simple_start,
                respond_for_view as _simple_respond,
            )
            from types import SimpleNamespace as _NS

            def _wrap(d: dict):
                return _NS(
                    content=d.get('message', ''),
                    phase=d.get('phase', ''),
                    show_exit_ticket=d.get('show_exit_ticket', False),
                    is_complete=d.get('is_complete', False),
                    is_correct=d.get('is_correct'),
                )

            class _SimpleTutorAdapter:
                def __init__(self, session):
                    self.session = session

                def start(self):
                    return _wrap(_simple_start(self.session))

                def respond(self, msg, **_kwargs):
                    return _wrap(_simple_respond(self.session, msg))

            tutor = _SimpleTutorAdapter(session)
        else:
            # Imported here, not at module scope: this is the only use, and it
            # runs only when SIMPLE_TUTOR_ENGINE is off. A module-level import
            # made every simple_tutor eval load the legacy engine it does not
            # use — enough to break `pytest evals/` at COLLECTION time whenever
            # conversational_tutor.py cannot be parsed or imported.
            from apps.tutoring.conversational_tutor import ConversationalTutor
            tutor = ConversationalTutor(session)

        # Tutor speaks first (mirrors production: views.py:820 calls .start()).
        opening = tutor.start()
        transcript.append(TranscriptTurn(
            turn_number=0, role='tutor', content=opening.content,
            phase=opening.phase, show_exit_ticket=opening.show_exit_ticket,
            is_complete=opening.is_complete,
        ))

        if opening.is_complete:
            return _result('completed', 0)
        if opening.show_exit_ticket:
            return _result('exit_ticket', 0)

        for n in range(1, max_turns + 1):
            last_tutor_content = transcript[-1].content
            student_reply = student.next_reply(last_tutor_content)
            if student.last_response is not None:
                student_tokens_in += student.last_response.tokens_in
                student_tokens_out += student.last_response.tokens_out
            transcript.append(TranscriptTurn(
                turn_number=n, role='student', content=student_reply,
            ))

            tutor_msg = tutor.respond(student_reply)
            transcript.append(TranscriptTurn(
                turn_number=n, role='tutor', content=tutor_msg.content,
                is_correct=tutor_msg.is_correct, phase=tutor_msg.phase,
                show_exit_ticket=tutor_msg.show_exit_ticket,
                is_complete=tutor_msg.is_complete,
            ))

            if tutor_msg.is_complete:
                return _result('completed', n)
            if tutor_msg.show_exit_ticket:
                return _result('exit_ticket', n)
            if _is_deadlock(transcript):
                logger.warning(
                    "Deadlock detected in session %s after turn %s",
                    session.id, n,
                )
                return _result('deadlock', n)

        return _result('max_turns', max_turns)

    except Exception as exc:
        logger.exception("simulate_session failed for session %s", session.id)
        result = _result('error', len([t for t in transcript if t.role == 'student']))
        result.error = str(exc)
        return result
