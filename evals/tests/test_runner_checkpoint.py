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


class ResumeTest(TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patcher = patch.object(R, 'RUNS_ROOT', Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_prior_results_skip_completed_scenarios(self):
        ran = []

        def fake_multi(scenario):
            ran.append(scenario.id)
            return _fake_result(scenario.id)

        prior = [_fake_result('a'), _fake_result('b', passed=False)]
        with patch.object(R, '_run_multi_turn', side_effect=fake_multi):
            result = R.run(
                [_fake_scenario('a'), _fake_scenario('b'), _fake_scenario('c')],
                prior_results=prior)

        self.assertEqual(ran, ['c'])                 # only the missing one
        self.assertEqual(result.total_scenarios, 3)  # union reported
        self.assertEqual(result.passed, 2)
        self.assertEqual(result.failed, 1)

    def test_indices_continue_from_prior(self):
        seen = []
        with patch.object(R, '_run_multi_turn',
                          side_effect=lambda s: _fake_result(s.id)):
            R.run([_fake_scenario('a'), _fake_scenario('b')],
                  prior_results=[_fake_result('a')],
                  on_result=lambda sr, i, t: seen.append((sr.scenario_id, i, t)))
        self.assertEqual(seen, [('b', 2, 2)])

    def test_load_partial_roundtrip_and_final_json_rejected(self):
        # Write a checkpoint via the real writer, read it back.
        path = Path(self._tmp.name) / 'partial_x.json'
        R._write_partial(path, started='2026-08-05T00:00:00',
                         git_sha='abc123', total=3,
                         results=[_fake_result('a')])
        results, sha = R.load_partial(path)
        self.assertEqual([r.scenario_id for r in results], ['a'])
        self.assertEqual(sha, 'abc123')
        # A final run JSON (no partial marker) must be refused.
        final = Path(self._tmp.name) / 'final.json'
        final.write_text(json.dumps({'results': []}))
        with self.assertRaises(ValueError):
            R.load_partial(final)


class CheckpointDirOverrideTest(TestCase):
    """2026-08-05 laptop-shutdown loss: checkpoints on the VM disk died with
    the runtime. EVAL_CHECKPOINT_DIR points them at a durable (Drive) dir."""

    def test_checkpoint_written_to_override_dir(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {'EVAL_CHECKPOINT_DIR': tmp}):
                seen_partials = []

                def on_result(sr, index, total):
                    seen_partials.extend(Path(tmp).glob('partial_*.json'))

                with patch.object(R, '_run_multi_turn',
                                  side_effect=lambda s: _fake_result(s.id)):
                    R.run([_fake_scenario('a')], on_result=on_result)
                self.assertTrue(seen_partials, 'no checkpoint in override dir')
                # Clean completion removes it from the override dir too.
                self.assertEqual(list(Path(tmp).glob('partial_*.json')), [])

    def test_latest_partial_reads_override_dir(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {'EVAL_CHECKPOINT_DIR': tmp}):
                p = Path(tmp) / 'partial_2026-08-05T00-00-00_abc.json'
                R._write_partial(p, started='x', git_sha='abc', total=1,
                                 results=[_fake_result('a')])
                self.assertEqual(R.latest_partial(), p)
