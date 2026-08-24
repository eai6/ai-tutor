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
