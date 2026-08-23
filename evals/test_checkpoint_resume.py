"""Checkpoint + resume: a killed sweep must cost one scenario, not all of them.

Written after 2026-08-23, when a 27b arm was SIGKILLed at scenario 10 of 34 and
lost all ten — `write_run` was the only writer and it ran once, at the end. The
same shape cost 22 of 30 scenarios (9+ hours of GPU) on the 2026-08-04 Colab
sweep.

These tests drive the real `run()` with fake scenarios, so they exercise the
loop that actually writes checkpoints rather than asserting on the helpers in
isolation.
"""
import json
import os
import types

import pytest

from evals import runner


class _Boom(RuntimeError):
    pass


def _scn(sid):
    return types.SimpleNamespace(
        id=sid, mode='single_turn', persona='average', subject='geography',
        lesson_id=1, tags=['t'], assertions={}, rubric=None,
        pass_threshold=0.6, rubric_judge={},
    )


@pytest.fixture()
def ckpt_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('EVAL_CHECKPOINT_DIR', str(tmp_path))
    monkeypatch.setattr(runner, 'RUNS_ROOT', tmp_path)
    return tmp_path


def _stub_single_turn(monkeypatch, fail_on=None):
    """Make _run_single_turn cheap and optionally explode on one scenario."""
    def fake(scenario):
        if fail_on is not None and scenario.id == fail_on:
            raise _Boom('kaboom')
        return runner.ScenarioResult(
            scenario_id=scenario.id, passed=True, mode='single_turn',
            persona=scenario.persona, subject=scenario.subject,
            lesson_id=scenario.lesson_id, tags=scenario.tags,
        )
    monkeypatch.setattr(runner, '_run_single_turn', fake)
    monkeypatch.setattr(runner, '_tutor_model_spec', lambda: 'local_ollama/testmodel')
    monkeypatch.setattr(runner, '_git_sha', lambda: 'deadbeef')


def test_checkpoint_written_after_every_scenario(ckpt_dir, monkeypatch):
    seen = []
    _stub_single_turn(monkeypatch)

    real = runner._write_partial

    def spy(path, **kw):
        seen.append(len(kw['results']))
        return real(path, **kw)

    monkeypatch.setattr(runner, '_write_partial', spy)
    runner.run([_scn(f's{i}') for i in range(5)])
    assert seen == [1, 2, 3, 4, 5], (
        f'expected a checkpoint after each scenario, got {seen}')


def test_checkpoint_is_removed_on_clean_completion(ckpt_dir, monkeypatch):
    """A finished run's checkpoint must not linger — the next run of the same
    model would auto-resume it and skip everything."""
    _stub_single_turn(monkeypatch)
    runner.run([_scn('a'), _scn('b')])
    assert list(ckpt_dir.glob('partial_*.json')) == []


def test_a_crash_leaves_a_resumable_checkpoint(ckpt_dir, monkeypatch):
    """The whole point. Scenario 3 of 5 explodes the process; the first two
    must survive on disk."""
    _stub_single_turn(monkeypatch)
    real = runner._write_partial
    calls = {'n': 0}

    def die_after_two(path, **kw):
        real(path, **kw)
        calls['n'] += 1
        if calls['n'] == 2:
            raise KeyboardInterrupt('simulated SIGKILL')

    monkeypatch.setattr(runner, '_write_partial', die_after_two)
    with pytest.raises(KeyboardInterrupt):
        runner.run([_scn(f's{i}') for i in range(5)])

    partials = list(ckpt_dir.glob('partial_*.json'))
    assert len(partials) == 1, 'crash left no checkpoint'
    data = json.loads(partials[0].read_text())
    assert data['partial'] is True
    assert data['completed_scenarios'] == 2
    assert [r['scenario_id'] for r in data['results']] == ['s0', 's1']


def test_resume_skips_completed_and_reports_the_union(ckpt_dir, monkeypatch):
    _stub_single_turn(monkeypatch)
    prior = [runner.ScenarioResult(
        scenario_id='s0', passed=True, mode='single_turn', persona='average',
        subject='geography', lesson_id=1, tags=['t'])]
    ran = []
    real_fake = runner._run_single_turn

    def track(scenario):
        ran.append(scenario.id)
        return real_fake(scenario)

    monkeypatch.setattr(runner, '_run_single_turn', track)
    result = runner.run([_scn('s0'), _scn('s1'), _scn('s2')], prior_results=prior)

    assert ran == ['s1', 's2'], f'resumed run re-ran completed work: {ran}'
    assert result.total_scenarios == 3, 'final JSON must report the union'
    assert {r.scenario_id for r in result.results} == {'s0', 's1', 's2'}


def test_a_scenario_crash_is_recorded_and_the_sweep_continues(ckpt_dir, monkeypatch):
    """One bad scenario must not cost the other 33."""
    _stub_single_turn(monkeypatch, fail_on='s1')
    result = runner.run([_scn('s0'), _scn('s1'), _scn('s2')])
    assert result.total_scenarios == 3
    errored = [r for r in result.results if r.error]
    assert len(errored) == 1 and errored[0].scenario_id == 's1'
    assert result.errored == 1


def test_resumable_partials_ranks_by_fullness_not_recency(ckpt_dir, monkeypatch):
    """A restart-from-scratch writes a NEWER but EMPTIER checkpoint. Picking
    the newest would throw away the fuller one's work."""
    monkeypatch.setattr(runner, '_tutor_model_spec', lambda: 'local_ollama/m')
    for name, n in (('partial_2026-01-01T00-00-00_aaa.json', 30),
                    ('partial_2026-06-01T00-00-00_bbb.json', 3)):
        (ckpt_dir / name).write_text(json.dumps({
            'partial': True, 'tutor_model': 'local_ollama/m',
            'completed_scenarios': n, 'results': [],
        }))
    ranked = runner.resumable_partials('local_ollama/m')
    assert [n for _, n in ranked] == [30, 3]
    assert runner.auto_resume_partial().name.endswith('aaa.json')


def test_checkpoints_are_keyed_on_the_tutor_model(ckpt_dir, monkeypatch):
    """A sweep points every arm at ONE checkpoint dir. Folding arm A's
    scenarios into arm B's board would publish A's scores under B's name."""
    (ckpt_dir / 'partial_2026-01-01T00-00-00_x.json').write_text(json.dumps({
        'partial': True, 'tutor_model': 'local_ollama/other-arm',
        'completed_scenarios': 12, 'results': [],
    }))
    assert runner.resumable_partials('local_ollama/mine') == []


def test_load_partial_refuses_a_final_run_json(ckpt_dir):
    """Resuming from a FINAL run JSON would re-report old results as new."""
    final = ckpt_dir / 'not_a_partial.json'
    final.write_text(json.dumps({'results': [], 'git_sha': 'abc'}))
    with pytest.raises(ValueError):
        runner.load_partial(final)


def test_checkpoint_dir_is_overridable(tmp_path, monkeypatch):
    """EVAL_CHECKPOINT_DIR exists so checkpoints can live on durable storage
    rather than a VM disk that disappears with the runtime."""
    monkeypatch.setenv('EVAL_CHECKPOINT_DIR', str(tmp_path / 'elsewhere'))
    assert runner.checkpoint_root() == tmp_path / 'elsewhere'
    monkeypatch.delenv('EVAL_CHECKPOINT_DIR')
    assert runner.checkpoint_root() == runner.RUNS_ROOT
