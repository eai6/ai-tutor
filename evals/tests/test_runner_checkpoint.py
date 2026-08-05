"""Incremental checkpointing + streamed results (2026-08-05).

Born from the 2026-08-04 Colab OOM kill: ~22 completed scenarios lost
because the run JSON was written only at the end and per-scenario stdout
sat in a block buffer. run() now checkpoints partial_*.json after every
scenario and invokes an on_result callback the command uses to stream.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from evals import runner as R


def _fake_result(sid, passed=True):
    return R.ScenarioResult(
        scenario_id=sid, passed=passed, mode='multi_turn',
        persona='average', subject='math', lesson_id=1, tags=['v1'],
    )


def _fake_scenario(sid):
    import types
    return types.SimpleNamespace(
        id=sid, mode='multi_turn', persona='average', subject='math',
        lesson_id=1, tags=['v1'])


class CheckpointTest(TestCase):

    def setUp(self):
        # Never write into the real evals/runs — patch RUNS_ROOT to a
        # per-test temp dir.
        self._tmp = tempfile.TemporaryDirectory()
        patcher = patch.object(R, 'RUNS_ROOT', Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_checkpoint_written_per_scenario_and_removed_on_success(self):
        seen = []
        checkpoints = []

        def fake_multi(scenario):
            # Snapshot the checkpoint state BEFORE this scenario's write.
            return _fake_result(scenario.id)

        def on_result(sr, index, total):
            seen.append((sr.scenario_id, index, total))
            # After each scenario the checkpoint must exist and hold
            # exactly `index` results.
            partials = list(R.RUNS_ROOT.glob('partial_*.json'))
            self.assertEqual(len(partials), 1)
            data = json.loads(partials[0].read_text())
            checkpoints.append(data['completed_scenarios'])
            self.assertTrue(data['partial'])
            self.assertEqual(data['completed_scenarios'], index)
            self.assertEqual(data['total_scenarios'], total)
            self.assertEqual(len(data['results']), index)

        with patch.object(R, '_run_multi_turn', side_effect=fake_multi):
            result = R.run([_fake_scenario('a'), _fake_scenario('b')],
                           on_result=on_result)

        self.assertEqual(seen, [('a', 1, 2), ('b', 2, 2)])
        self.assertEqual(checkpoints, [1, 2])
        self.assertEqual(result.passed, 2)
        # Clean completion removes the checkpoint.
        self.assertEqual(list(R.RUNS_ROOT.glob('partial_*.json')), [])

    def test_crashing_scenario_still_checkpointed(self):
        def fake_multi(scenario):
            if scenario.id == 'boom':
                raise RuntimeError('scenario exploded')
            return _fake_result(scenario.id)

        with patch.object(R, '_run_multi_turn', side_effect=fake_multi):
            result = R.run([_fake_scenario('a'), _fake_scenario('boom')])

        self.assertEqual(result.passed, 1)
        self.assertEqual(result.errored, 1)
        self.assertEqual(list(R.RUNS_ROOT.glob('partial_*.json')), [])

    def test_callback_failure_never_kills_the_run(self):
        def bad_callback(sr, index, total):
            raise ValueError('ui blew up')

        with patch.object(R, '_run_multi_turn',
                          side_effect=lambda s: _fake_result(s.id)):
            result = R.run([_fake_scenario('a')], on_result=bad_callback)
        self.assertEqual(result.passed, 1)
