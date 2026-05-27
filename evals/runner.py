"""Eval harness runner.

Loads scenario YAMLs, drives `ConversationalTutor.respond()` against each,
scores the result through three layers, and writes a per-run JSON blob to
``evals/runs/``.

Scoring layers (memory/eval_harness_plan.md):
- Layer 1 — deterministic phrase / structural checks
- Layer 2 — judge-derived label set via apps.benchmark.autopopulate
- Layer 3 — LLM-as-judge rubric (Haiku 4.5 @ temp=0 by default)

A scenario passes iff layers 1+2 all pass AND the rubric mean (if present)
meets ``pass_threshold``.

Multi-turn mode and cost tracking land in Phase 4+.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml
from django.contrib.auth.models import User

from apps.accounts.models import Institution
from apps.curriculum.models import Lesson
from apps.tutoring.conversational_tutor import ConversationalTutor
from apps.tutoring.models import SessionTurn, TutorSession

from evals.scorers import AssertionResult
from evals.scorers import deterministic as deterministic_scorer
from evals.scorers import llm_rubric
from evals.scorers import trajectory as trajectory_scorer

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_ROOT = REPO_ROOT / 'evals' / 'dataset'
RUNS_ROOT = REPO_ROOT / 'evals' / 'runs'

# Set by the fixture extractor. See evals/fixtures/extract.py.
EVAL_INSTITUTION_PK = 999001
EVAL_USER_PK = 999001


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    """One loaded scenario YAML."""
    id: str
    description: str
    persona: str
    subject: str
    lesson_id: int
    tags: list[str]
    mode: str  # 'single_turn' | 'multi_turn'
    seed_history: list[dict]
    student_turn: str
    max_turns: int  # multi_turn only; ignored for single_turn
    assertions: dict[str, Any]
    rubric: list[str]
    pass_threshold: float
    rubric_judge: dict[str, Any]
    path: Path  # for error reporting

    @classmethod
    def from_yaml(cls, path: Path) -> 'Scenario':
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
        if raw['id'] != path.stem:
            raise ValueError(
                f"scenario id {raw['id']!r} != filename stem {path.stem!r} ({path})"
            )
        return cls(
            id=raw['id'],
            description=str(raw.get('description', '')).strip(),
            persona=str(raw.get('persona', 'struggler')).lower(),
            subject=str(raw.get('subject', '')),
            lesson_id=int(raw['lesson_id']),
            tags=list(raw.get('tags') or []),
            mode=str(raw.get('mode', 'single_turn')),
            seed_history=list(raw.get('seed_history') or []),
            student_turn=str(raw.get('student_turn', '')),
            max_turns=int(raw.get('max_turns', 15)),
            assertions=dict(raw.get('assertions') or {}),
            rubric=[str(x) for x in (raw.get('rubric') or [])],
            pass_threshold=float(raw.get('pass_threshold', 0.7)),
            rubric_judge=dict(raw.get('rubric_judge') or {}),
            path=path,
        )


@dataclass
class ScenarioResult:
    scenario_id: str
    passed: bool
    persona: str
    subject: str
    lesson_id: int
    tags: list[str]
    mode: str = 'single_turn'
    tutor_response: str = ''            # single_turn: the one response. multi_turn: empty.
    suggested_labels: list[str] = field(default_factory=list)   # single_turn: last turn. multi_turn: empty.
    assertion_results: list[AssertionResult] = field(default_factory=list)
    rubric_result: dict | None = None   # serialised llm_rubric.RubricResult

    # Multi-turn only.
    transcript: list[dict] = field(default_factory=list)
    sim_reason: str = ''
    sim_turns: int = 0
    per_turn_labels: list[list[str]] = field(default_factory=list)

    session_id: int | None = None
    error: str = ''


@dataclass
class RunResult:
    started_at: str
    finished_at: str
    git_sha: str
    total_scenarios: int
    passed: int
    failed: int
    errored: int
    results: list[ScenarioResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scenario discovery & execution
# ---------------------------------------------------------------------------

def discover_scenarios(
    *, smoke: bool = False, scenario_id: str | None = None,
) -> list[Scenario]:
    """Return the scenario list under evals/dataset, honoring filters."""
    if scenario_id:
        # Find the matching file anywhere under dataset/.
        for path in DATASET_ROOT.rglob(f"{scenario_id}.yaml"):
            return [Scenario.from_yaml(path)]
        raise FileNotFoundError(f"No scenario file named {scenario_id}.yaml")

    if smoke:
        smoke_dir = DATASET_ROOT / 'smoke'
        return [Scenario.from_yaml(p) for p in sorted(smoke_dir.glob('*.yaml'))]

    # Full run: everything under dataset/ EXCEPT the smoke directory.
    return [
        Scenario.from_yaml(p)
        for p in sorted(DATASET_ROOT.rglob('*.yaml'))
        if 'smoke' not in p.parts
    ]


def _eval_institution_and_user() -> tuple[Institution, User]:
    try:
        inst = Institution.objects.get(pk=EVAL_INSTITUTION_PK)
    except Institution.DoesNotExist as exc:
        raise RuntimeError(
            f"Eval institution (pk={EVAL_INSTITUTION_PK}) not found. Run "
            "`python manage.py loaddata evals/fixtures/institution.json "
            "evals/fixtures/lessons.json` first."
        ) from exc
    try:
        user = User.objects.get(pk=EVAL_USER_PK)
    except User.DoesNotExist as exc:
        raise RuntimeError(
            f"Eval user (pk={EVAL_USER_PK}) not found. Reload fixtures."
        ) from exc
    return inst, user


def _inject_seed_history(session: TutorSession, history: list[dict]) -> None:
    """Persist seed_history as SessionTurns so the tutor sees prior context."""
    for entry in history:
        role = entry['role']
        if role not in ('tutor', 'student'):
            raise ValueError(f"seed_history role must be tutor|student, got {role!r}")
        SessionTurn.objects.create(
            session=session,
            role=role,
            content=str(entry.get('text', '')),
        )


def _run_single_turn(scenario: Scenario) -> ScenarioResult:
    """Drive one respond() call and score deterministically."""
    inst, user = _eval_institution_and_user()

    try:
        lesson = Lesson.objects.get(pk=scenario.lesson_id)
    except Lesson.DoesNotExist:
        return ScenarioResult(
            scenario_id=scenario.id,
            passed=False, persona=scenario.persona, subject=scenario.subject,
            lesson_id=scenario.lesson_id, tags=scenario.tags,
            tutor_response='',
            error=(
                f"lesson_id={scenario.lesson_id} not found in DB. "
                "Reload fixtures or pick a different lesson."
            ),
        )

    session = TutorSession.objects.create(
        student=user,
        lesson=lesson,
        institution=inst,
        status=TutorSession.Status.ACTIVE,
        is_synthetic=True,
        sim_persona=scenario.persona,
        engine_state={'eval_scenario_id': scenario.id},
    )

    try:
        _inject_seed_history(session, scenario.seed_history)
        # Engine dispatch: honor SIMPLE_TUTOR_ENGINE (same env var the
        # web views check) so the eval harness exercises whichever
        # engine is wired in for the staging/prod deploy. Without this
        # the harness always exercised the legacy CT, even when the
        # live app was running on simple-tutor — biasing eval results.
        from apps.tutoring import simple_tutor as _simple_tutor
        if _simple_tutor.is_enabled():
            from apps.tutoring.simple_tutor.engine import respond as _simple_respond
            out = _simple_respond(session, scenario.student_turn)
            tutor_text = (out.get('content') or '').strip()
        else:
            tutor = ConversationalTutor(session)
            msg = tutor.respond(scenario.student_turn)
            tutor_text = (msg.content or '').strip()

        # Pull the just-written tutor SessionTurn to read its judge signal.
        last_tutor_turn = (
            SessionTurn.objects.filter(session=session, role='tutor')
            .order_by('-id').first()
        )
        suggested_labels: list[str] = []
        if last_tutor_turn is not None:
            from apps.benchmark.autopopulate import derive_suggested_labels
            suggested_labels = derive_suggested_labels(
                last_tutor_turn.metadata or {},
                last_tutor_turn.judge_outputs or {},
            )

        assertion_results = deterministic_scorer.score(
            scenario.assertions, tutor_text, suggested_labels,
        )
        deterministic_passed = bool(assertion_results) and all(
            r.passed for r in assertion_results
        )

        # Layer 3 — LLM rubric, only if the scenario defines one.
        rubric_payload: dict | None = None
        rubric_passed = True  # absent rubric = no veto
        if scenario.rubric:
            rubric_result = llm_rubric.score(
                scenario.rubric,
                conversation=[
                    {'role': t.get('role'),
                     'content': t.get('text') or t.get('content', '')}
                    for t in scenario.seed_history
                ],
                student_turn=scenario.student_turn,
                tutor_text=tutor_text,
                pass_threshold=scenario.pass_threshold,
                judge_config=scenario.rubric_judge or None,
            )
            rubric_payload = asdict(rubric_result)
            # Errors from the rubric scorer are hard fails on the rubric
            # layer (treat error as "could not satisfy").
            rubric_passed = rubric_result.passed and not rubric_result.error

        passed = deterministic_passed and rubric_passed
        return ScenarioResult(
            scenario_id=scenario.id,
            passed=passed,
            persona=scenario.persona,
            subject=scenario.subject,
            lesson_id=scenario.lesson_id,
            tags=scenario.tags,
            tutor_response=tutor_text,
            suggested_labels=suggested_labels,
            assertion_results=assertion_results,
            rubric_result=rubric_payload,
            session_id=session.pk,
        )
    except Exception as exc:
        logger.exception("scenario %s blew up", scenario.id)
        return ScenarioResult(
            scenario_id=scenario.id,
            passed=False,
            persona=scenario.persona,
            subject=scenario.subject,
            lesson_id=scenario.lesson_id,
            tags=scenario.tags,
            tutor_response='',
            session_id=session.pk,
            error=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ['git', 'rev-parse', '--short=12', 'HEAD'],
            cwd=REPO_ROOT, stderr=subprocess.DEVNULL,
        )
        return out.decode('utf-8').strip()
    except Exception:
        return 'unknown'


def _run_multi_turn(scenario: Scenario) -> ScenarioResult:
    """Drive a full persona-vs-tutor session, then score the trajectory.

    Reuses ``apps.tutoring.student_sim.simulate_session`` — same code path
    the simulator uses for synthetic-traffic runs. Eval-specific behaviour:
    after the session completes (or errors), we read back the persisted
    SessionTurns, derive per-turn labels via
    ``apps.benchmark.autopopulate.derive_suggested_labels``, tag the
    session's engine_state with the scenario id for traceability, then
    score with the trajectory verbs.
    """
    from apps.benchmark.autopopulate import derive_suggested_labels
    from apps.tutoring.student_sim import simulate_session

    inst, _user = _eval_institution_and_user()

    try:
        Lesson.objects.get(pk=scenario.lesson_id)
    except Lesson.DoesNotExist:
        return ScenarioResult(
            scenario_id=scenario.id, passed=False, mode=scenario.mode,
            persona=scenario.persona, subject=scenario.subject,
            lesson_id=scenario.lesson_id, tags=scenario.tags,
            error=(
                f"lesson_id={scenario.lesson_id} not found in DB. "
                "Reload fixtures or pick a different lesson."
            ),
        )

    try:
        sim = simulate_session(
            lesson_id=scenario.lesson_id,
            persona=scenario.persona,
            max_turns=scenario.max_turns,
            institution_id=inst.id,
        )
    except Exception as exc:
        logger.exception("simulate_session failed for scenario %s", scenario.id)
        return ScenarioResult(
            scenario_id=scenario.id, passed=False, mode=scenario.mode,
            persona=scenario.persona, subject=scenario.subject,
            lesson_id=scenario.lesson_id, tags=scenario.tags,
            error=f"{type(exc).__name__}: {exc}",
        )

    # Tag the session with the scenario id post-hoc so DB readers can join.
    try:
        session = TutorSession.objects.get(pk=sim.session_id)
        state = dict(session.engine_state or {})
        state['eval_scenario_id'] = scenario.id
        session.engine_state = state
        session.save(update_fields=['engine_state'])
    except TutorSession.DoesNotExist:
        pass

    # Pull tutor SessionTurns in order; derive per-turn labels.
    tutor_turns = list(
        SessionTurn.objects.filter(session_id=sim.session_id, role='tutor')
        .order_by('id')
    )
    per_turn_labels: list[list[str]] = [
        derive_suggested_labels(t.metadata or {}, t.judge_outputs or {})
        for t in tutor_turns
    ]

    # Serialise transcript for the run blob and the rubric prompt.
    transcript_payload = [
        {'turn_number': t.turn_number, 'role': t.role, 'content': t.content,
         'phase': t.phase or '', 'is_complete': t.is_complete,
         'show_exit_ticket': t.show_exit_ticket}
        for t in sim.transcript
    ]

    # Trajectory verbs.
    assertion_results = trajectory_scorer.score(
        scenario.assertions, sim, per_turn_labels,
    )
    if not assertion_results:
        # Author forgot to include trajectory verbs in a multi_turn scenario.
        assertion_results.append(AssertionResult(
            'no_trajectory_assertions', passed=False,
            detail='multi_turn scenario has no trajectory verbs in assertions block',
        ))
    deterministic_passed = all(r.passed for r in assertion_results)

    # Layer 3 — rubric over the whole transcript, only if scenario defines one.
    rubric_payload: dict | None = None
    rubric_passed = True
    if scenario.rubric:
        rubric_result = llm_rubric.score_trajectory(
            scenario.rubric,
            transcript=[{'role': t['role'], 'content': t['content']}
                        for t in transcript_payload],
            pass_threshold=scenario.pass_threshold,
            judge_config=scenario.rubric_judge or None,
        )
        rubric_payload = asdict(rubric_result)
        rubric_passed = rubric_result.passed and not rubric_result.error

    sim_error = sim.error if sim.reason == 'error' else ''

    return ScenarioResult(
        scenario_id=scenario.id,
        passed=deterministic_passed and rubric_passed and not sim_error,
        mode=scenario.mode,
        persona=scenario.persona,
        subject=scenario.subject,
        lesson_id=scenario.lesson_id,
        tags=scenario.tags,
        suggested_labels=[],
        assertion_results=assertion_results,
        rubric_result=rubric_payload,
        transcript=transcript_payload,
        sim_reason=sim.reason,
        sim_turns=sim.turns,
        per_turn_labels=per_turn_labels,
        session_id=sim.session_id,
        error=sim_error,
    )


def run(scenarios: list[Scenario]) -> RunResult:
    started = dt.datetime.now(dt.timezone.utc)
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        if scenario.mode == 'single_turn':
            results.append(_run_single_turn(scenario))
        elif scenario.mode == 'multi_turn':
            results.append(_run_multi_turn(scenario))
        else:
            results.append(ScenarioResult(
                scenario_id=scenario.id,
                passed=False, mode=scenario.mode,
                persona=scenario.persona, subject=scenario.subject,
                lesson_id=scenario.lesson_id, tags=scenario.tags,
                error=f"unknown mode={scenario.mode!r}",
            ))
    finished = dt.datetime.now(dt.timezone.utc)
    return RunResult(
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        git_sha=_git_sha(),
        total_scenarios=len(results),
        passed=sum(1 for r in results if r.passed),
        failed=sum(1 for r in results if not r.passed and not r.error),
        errored=sum(1 for r in results if r.error),
        results=results,
    )


def write_run(result: RunResult) -> Path:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')
    path = RUNS_ROOT / f"{ts}_{result.git_sha}.json"
    path.write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False),
                    encoding='utf-8')
    return path
