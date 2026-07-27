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
from apps.tutoring.models import SessionTurn, TutorSession

from evals.scorers import AssertionResult
from evals.scorers import deterministic as deterministic_scorer
from evals.scorers import llm_rubric
from evals.scorers import trajectory as trajectory_scorer

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_ROOT = REPO_ROOT / 'evals' / 'dataset'
RUNS_ROOT = REPO_ROOT / 'evals' / 'runs'

# Multi-turn scenarios default to Sonnet 4.6, not the Haiku DEFAULT_RUBRIC_JUDGE.
# Judging a whole 20-40 turn session is a harder, longer-context task than the
# single-turn eval, and the Haiku-vs-Sonnet A/B (2026-07-06, offline_eval/
# judge_ab.py) found Haiku too lenient: pass 7/8 vs Sonnet 2/8, Cohen's kappa
# 0.09 (~chance), and it missed a real tutor self-contradiction that Sonnet
# caught. A scenario can still override via its own `rubric_judge` block.
# Single-turn keeps Haiku (llm_rubric.DEFAULT_RUBRIC_JUDGE) for continuity with
# the frozen Eval 2 board.
MULTI_TURN_RUBRIC_JUDGE = {
    'provider': 'anthropic',
    'model': 'claude-sonnet-4-6',
    'temperature': 0.0,
    'max_tokens': 4096,
}

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
    # Optional in-flight slot for single_turn scenarios. When the
    # seed_history ends with the tutor posing a question, the simple_tutor
    # engine expects an InFlightQuestion row already persisted so the
    # student's next turn is graded against the right reference. Without
    # this, the engine re-poses instead of grading and the scenario fails
    # spuriously. Multi-turn scenarios don't need it — they drive
    # pose_question themselves through the student_sim driver. Shape:
    #   {'question_text': str,
    #    'question_type': 'mcq' | 'short_numeric' | 'short_answer',
    #    'reference_answer': str,
    #    'options': [str, ...]   # required for mcq
    #    'source': 'catalog' | 'inline_authored'}
    seed_inflight_question: dict[str, Any] | None
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
            seed_inflight_question=(
                dict(raw['seed_inflight_question'])
                if isinstance(raw.get('seed_inflight_question'), dict)
                else None
            ),
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
    dimensions_result: dict | None = None   # serialised llm_rubric.DimensionsResult

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
    # Which tutor engine actually ran the sessions — 'simple_tutor' (the
    # production engine, the target of this eval) or 'conversational_tutor'
    # (legacy). Recorded so a run can never silently score the wrong engine.
    engine: str = ''
    # Which model powered the tutor — TUTOR_MODEL_OVERRIDE when a sweep set
    # it, else the active tutoring ModelConfig. Without this, identifying
    # which model produced a run means matching file sizes against
    # offline_eval result files (as the 2026-07-18 bottleneck analysis had
    # to). Empty string when neither source resolves.
    tutor_model: str = ''


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


def _inject_inflight_question(
    session: TutorSession, payload: dict[str, Any] | None,
) -> None:
    """When a scenario seeds a posed question into seed_history, mirror the
    state that ``handle_pose_question`` would have created at runtime.

    The simple_tutor engine looks up ``InFlightQuestion`` to decide
    GRADE vs POSE mode. If seed_history says the tutor asked an MCQ and
    student_turn is "A", but no slot exists, the engine sees no
    in-flight question, falls into POSE mode, refuses to grade, and
    re-poses — the student's answer is never evaluated. That spuriously
    fails the scenario.

    Multi-turn scenarios should NOT call this — they drive
    pose_question themselves through the student_sim driver.

    Returns silently if payload is None or empty.
    """
    if not payload:
        return
    from apps.tutoring.models import InFlightQuestion

    qtext = str(payload.get('question_text', '') or '').strip()
    ref = str(payload.get('reference_answer', '') or '').strip()
    qtype = str(payload.get('question_type', '') or '').strip().lower()
    src = str(payload.get('source', 'inline_authored') or 'inline_authored').strip().lower()

    if not qtext or not ref:
        raise ValueError(
            f"seed_inflight_question requires question_text and reference_answer "
            f"(got text={qtext[:40]!r}, ref={ref!r}) on session={session.pk}"
        )
    if qtype not in ('mcq', 'short_numeric', 'short_answer'):
        raise ValueError(
            f"seed_inflight_question question_type must be mcq / short_numeric / "
            f"short_answer, got {qtype!r}"
        )
    if src not in ('catalog', 'inline_authored'):
        src = 'inline_authored'

    options_raw = payload.get('options') or []
    if not isinstance(options_raw, list):
        options_raw = []
    options = [str(o)[:300] for o in options_raw]
    if qtype == 'mcq' and len(options) < 2:
        raise ValueError(
            f"seed_inflight_question.options must have at least 2 entries "
            f"for mcq, got {options!r}"
        )

    catalog_id = payload.get('catalog_question_id')
    if not isinstance(catalog_id, int):
        catalog_id = None

    InFlightQuestion.objects.create(
        session=session,
        question_text=qtext,
        question_type=qtype,
        options=options,
        reference_answer=ref,
        source=src,
        catalog_question_id=catalog_id,
        posed_at_turn_id=None,
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
        _inject_inflight_question(session, scenario.seed_inflight_question)
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
            # Imported here rather than at module scope: this is the only use,
            # and it is reached only when simple_tutor is disabled. A top-level
            # import coupled the whole eval harness to the legacy engine, so
            # anything that stopped conversational_tutor.py importing took out
            # `manage.py run_eval` and `pytest evals/` outright.
            from apps.tutoring.conversational_tutor import ConversationalTutor
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

        # Universal voice checks — every single-turn scenario gets
        # meta_reasoning_leak + passive_ending policed by default.
        # Both default to ``False`` (the desired state). A scenario
        # that explicitly sets either to True is opting out of the
        # check (or asserting the opposite for a negative-control
        # test). These replaced the dropped `max_paragraphs` cap on
        # 2026-05-27 — see evals/reports/prompt_audit_2026-05-27.md.
        merged_assertions = {
            'meta_reasoning_leak': False,
            'passive_ending': False,
            **scenario.assertions,
        }
        assertion_results = deterministic_scorer.score(
            merged_assertions, tutor_text, suggested_labels,
        )
        deterministic_passed = bool(assertion_results) and all(
            r.passed for r in assertion_results
        )

        conversation_for_judge = [
            {'role': t.get('role'),
             'content': t.get('text') or t.get('content', '')}
            for t in scenario.seed_history
        ]

        # Layer 3a — LLM rubric (scenario-specific items), only if the
        # scenario defines one.
        rubric_payload: dict | None = None
        rubric_passed = True  # absent rubric = no veto
        if scenario.rubric:
            rubric_result = llm_rubric.score(
                scenario.rubric,
                conversation=conversation_for_judge,
                student_turn=scenario.student_turn,
                tutor_text=tutor_text,
                pass_threshold=scenario.pass_threshold,
                judge_config=scenario.rubric_judge or None,
            )
            rubric_payload = asdict(rubric_result)
            # Errors from the rubric scorer are hard fails on the rubric
            # layer (treat error as "could not satisfy").
            rubric_passed = rubric_result.passed and not rubric_result.error

        # Layer 3b — Universal pedagogical dimensions (8 categorical
        # dimensions evaluated on every single-turn scenario). Added
        # 2026-05-27 per memory/simple_tutor_systematic_eval_plan.md.
        #
        # ADVISORY only: dimensions surface in the report for visibility
        # but do NOT gate scenario pass. Reason: a single LLM judge call
        # produces ~5% calibration noise per dimension; with 8
        # dimensions × strict "all-must-pass" gating, ~30% of clean
        # scenarios would fail spuriously on one borderline judgement
        # (the phase-5 run hit exactly this on 12 scenarios). The
        # scenario rubric — which already includes the BEA-aligned
        # 8-item block + scenario-specific items + threshold — remains
        # the gatekeeper. Dimensions add per-dimension tracking in the
        # report (which dimension is the bottleneck across the suite)
        # without false-negatives at the gate.
        dimensions_payload: dict | None = None
        try:
            dim_result = llm_rubric.score_pedagogical_dimensions(
                conversation=conversation_for_judge,
                student_turn=scenario.student_turn,
                tutor_text=tutor_text,
                judge_config=scenario.rubric_judge or None,
            )
            dimensions_payload = asdict(dim_result)
        except Exception as exc:
            logger.warning(
                "dimensions judge failed for %s: %s — non-fatal",
                scenario.id, exc,
            )

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
            dimensions_result=dimensions_payload,
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

def _tutor_model_spec() -> str:
    """Resolve which model powers the tutor for this run, for the run
    header. Sweep runs set TUTOR_MODEL_OVERRIDE; otherwise fall back to
    the active tutoring ModelConfig. Fail-soft — never blocks a run."""
    import os
    spec = (os.environ.get('TUTOR_MODEL_OVERRIDE') or '').strip()
    if spec:
        return spec
    try:
        from apps.llm.models import ModelConfig
        cfg = ModelConfig.objects.filter(
            purpose=ModelConfig.Purpose.TUTORING, is_active=True,
        ).first()
        if cfg is not None:
            return f"{cfg.provider}/{cfg.model_name}"
    except Exception:
        logger.warning("could not resolve tutoring ModelConfig for run header",
                       exc_info=True)
    return ''


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
            judge_config=scenario.rubric_judge or MULTI_TURN_RUBRIC_JUDGE,
            end_reason=sim.reason,
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
    from django.db import connections
    started = dt.datetime.now(dt.timezone.utc)
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        # Close idle DB connections between scenarios. Azure Postgres
        # aggressively closes long-idle SSL connections; without this
        # a multi-turn scenario can succeed but the NEXT scenario's
        # ORM call dies with "InterfaceError: connection already
        # closed", killing the whole suite mid-run.
        connections.close_all()
        try:
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
        except Exception as exc:
            # Per-scenario crash should not kill the suite. Record the
            # error and keep going so the user gets a complete report.
            logger.exception("scenario %s crashed", scenario.id)
            results.append(ScenarioResult(
                scenario_id=scenario.id, passed=False,
                mode=scenario.mode, persona=scenario.persona,
                subject=scenario.subject, lesson_id=scenario.lesson_id,
                tags=scenario.tags,
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            ))
    finished = dt.datetime.now(dt.timezone.utc)
    from apps.tutoring import simple_tutor as _st
    engine_name = 'simple_tutor' if _st.is_enabled() else 'conversational_tutor'
    return RunResult(
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        git_sha=_git_sha(),
        total_scenarios=len(results),
        passed=sum(1 for r in results if r.passed),
        failed=sum(1 for r in results if not r.passed and not r.error),
        errored=sum(1 for r in results if r.error),
        results=results,
        engine=engine_name,
        tutor_model=_tutor_model_spec(),
    )


def write_run(result: RunResult) -> Path:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')
    path = RUNS_ROOT / f"{ts}_{result.git_sha}.json"
    path.write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False),
                    encoding='utf-8')
    return path
