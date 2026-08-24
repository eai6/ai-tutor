"""The tracer must be invisible in production and honest in the eval.

Two properties matter more than the fields themselves:
  * OFF unless TUTOR_TRACE_DIR is set — a student's lesson pays nothing.
  * Never raises — a broken tracer must not break a turn. That is why every
    assertion below feeds it something hostile.
"""
import json
import pathlib

import pytest

from ai_tutor.apps.tutoring.simple_tutor import trace


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv('TUTOR_TRACE_DIR', raising=False)
    assert trace.enabled() is False


def test_writes_nothing_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv('TUTOR_TRACE_DIR', raising=False)
    trace.emit(session_id=1, note='should not appear')
    assert list(tmp_path.glob('*.jsonl')) == []


def test_one_json_object_per_turn(monkeypatch, tmp_path):
    monkeypatch.setenv('TUTOR_TRACE_DIR', str(tmp_path))
    trace.emit(session_id=1, verdict='correct')
    trace.emit(session_id=1, verdict='incorrect')
    lines = (tmp_path / 'turns.jsonl').read_text().strip().split('\n')
    assert len(lines) == 2
    assert [json.loads(x)['verdict'] for x in lines] == ['correct', 'incorrect']


def test_long_fields_are_clipped_not_dropped(monkeypatch, tmp_path):
    """A full question_pool would bloat the file; losing it entirely would
    defeat the point. Clip, and say how much was cut."""
    monkeypatch.setenv('TUTOR_TRACE_DIR', str(tmp_path))
    trace.emit(session_id=1, blob='y' * 5000)
    rec = json.loads((tmp_path / 'turns.jsonl').read_text().strip())
    assert len(rec['blob']) < 5000
    assert rec['blob'].startswith('yyy')
    assert 'chars]' in rec['blob']


@pytest.mark.parametrize('bad', [
    {'obj': object()},                  # not JSON-serialisable
    {'nested': {'set': {1, 2, 3}}},     # sets are not JSON
])
def test_unserialisable_values_do_not_raise(monkeypatch, tmp_path, bad):
    monkeypatch.setenv('TUTOR_TRACE_DIR', str(tmp_path))
    trace.emit(session_id=1, **bad)     # must not raise


def test_an_unwritable_dir_does_not_raise(monkeypatch, tmp_path):
    """A tracer that can take down a lesson is worse than no tracer."""
    target = tmp_path / 'afile'
    target.write_text('not a directory')
    monkeypatch.setenv('TUTOR_TRACE_DIR', str(target))
    trace.emit(session_id=1, verdict='correct')


def test_trace_name_is_overridable(monkeypatch, tmp_path):
    """One file per run; a sweep sets this per arm so arms do not interleave."""
    monkeypatch.setenv('TUTOR_TRACE_DIR', str(tmp_path))
    monkeypatch.setenv('TUTOR_TRACE_NAME', 'arm-a')
    trace.emit(session_id=1)
    assert (tmp_path / 'arm-a.jsonl').exists()


class TestInstrumentationCannotBreakATurn:
    """The tracer sits in the LLM call path. On 2026-08-24 a NameError there
    errored every scenario in a run inside 11 seconds — the instrumentation
    broke the thing it was measuring. These pin the guards."""

    def test_note_model_survives_a_junk_config(self):
        from ai_tutor.apps.tutoring.simple_tutor import engine
        engine._reset_turn_diagnostics()
        engine._note_model(object())          # no provider/model_name
        engine._note_model(None)
        assert engine.turn_diagnostics()['model'] == ''

    def test_note_call2_survives_junk(self):
        from ai_tutor.apps.tutoring.simple_tutor import engine
        engine._reset_turn_diagnostics()
        engine._note_call2_content('tool', None)
        assert isinstance(engine.turn_diagnostics()['call2'], list)

    def test_diagnostics_work_without_a_reset(self):
        """A turn path that never called _reset_turn_diagnostics must still
        return a usable dict rather than raising AttributeError."""
        from ai_tutor.apps.tutoring.simple_tutor import engine
        import threading
        out = {}

        def worker():
            out['d'] = engine.turn_diagnostics()   # fresh thread, no reset

        t = threading.Thread(target=worker)
        t.start(); t.join()
        assert set(out['d']) >= {'retries', 'last_error', 'call2', 'model'}


class TestFailedTurnsAreTraced:
    """The turns worth investigating were the only ones never recorded.

    When _call_llm returns None the turn serves _FALLBACK_REPLY and returns
    early — past the emit at the end of the turn. So on 2026-08-24 a math-27b
    run's trace reported "0 placeholders" while 15 of its 34 sessions
    deadlocked on that fallback repeating, and I read the trace as evidence the
    run was healthy. A monitor that goes quiet during a failure is worse than
    no monitor, because it is believed.
    """

    def test_the_early_return_emits_before_returning(self):
        """Structural: the emit must sit above the fallback return, not after."""
        import pathlib
        src = pathlib.Path(
            'ai_tutor/apps/tutoring/simple_tutor/engine.py').read_text()
        i = src.index("if response is None:")
        block = src[i:i + 1600]
        assert '_trace.emit(' in block, 'failed turn returns without tracing'
        assert block.index('_trace.emit(') < block.index("'fallback': True"), \
            'the emit must run BEFORE the early return'

    def test_the_failed_turn_is_marked_as_a_placeholder(self):
        """placeholder=True is what the report counts; a failed turn that
        traced with placeholder=False would be just as invisible."""
        import pathlib
        src = pathlib.Path(
            'ai_tutor/apps/tutoring/simple_tutor/engine.py').read_text()
        i = src.index("if response is None:")
        block = src[i:i + 1600]
        assert 'placeholder=True' in block
        assert "failed_call='call1'" in block, 'record WHICH call gave up'
