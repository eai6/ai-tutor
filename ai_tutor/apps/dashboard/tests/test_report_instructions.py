"""The teacher instruction on the session report.

The bands and the weak objectives are deterministic and stay that way — they
come from exit-ticket concept_tag matching. What the LLM writes is the sentence
a teacher reads, which used to be a constant f-string: the same words for every
AE group on every lesson on the platform, with three objective names spliced
in.

These tests hold the two properties that make that safe to do — it degrades to
the old text rather than breaking the page, and it cannot put an objective in
front of a teacher that the data did not contain.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.core.cache import cache

from ai_tutor.apps.dashboard import report_instructions as ri


LESSON = SimpleNamespace(id=7, title='Four-figure grid references',
                         objective='Read a four-figure grid reference')

GROUPS = [
    {'code': 'AE', 'students': [1, 2, 3],
     'common_weak': ['Locate a grid square using its two-figure reference on a map',
                     'Read the four-figure grid reference from a marked point']},
    {'code': 'ME', 'students': [4], 'common_weak': []},
]


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


class TestItFailsSoft:
    """The report is a page a teacher opens between lessons. Better the old
    constant sentence than a spinner, a traceback, or a 500."""

    def test_no_model_configured_returns_nothing(self, db, monkeypatch):
        from ai_tutor.apps.llm.models import ModelConfig
        monkeypatch.setattr(ModelConfig, 'get_for', staticmethod(lambda p: None))
        assert ri.generate(LESSON, GROUPS) == {}

    def test_a_raising_client_returns_nothing(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError('provider down')
        monkeypatch.setattr(ri, '_call_model', boom)
        assert ri.generate(LESSON, GROUPS) == {}

    def test_no_groups_makes_no_call(self, monkeypatch):
        called = []
        monkeypatch.setattr(ri, '_call_model',
                            lambda *a, **kw: called.append(1) or {})
        assert ri.generate(LESSON, []) == {}
        assert called == []


ALLOWED = ['Locate a grid square using its two-figure reference on a map',
           'Determine the horizontal easting value for a four-figure grid reference']


class TestItStaysGrounded:
    """A teacher acting on an invented sub-objective is the one genuinely
    harmful output this can produce.

    There is no proving a negative here. What is checkable is whether the
    advice is about an objective it was handed, which catches the real failure
    — the model writing about something else — while tolerating the grammar a
    good sentence needs.
    """

    def test_an_inflected_objective_passes(self):
        """The objective reads "Locate a grid square"; good advice reads
        "locating a grid square". An exact-substring check rejects the correct
        output and accepts nothing useful."""
        assert ri._grounded(
            'Re-teach locating a grid square using its two-figure reference. '
            'Work one through on the board.', ALLOWED) is True

    def test_a_shortened_objective_passes(self):
        """Dropping a tail the teacher does not need is still correct advice."""
        assert ri._grounded(
            'Re-teach determining the horizontal easting value. Model it with '
            'a ruler.', ALLOWED) is True

    def test_advice_about_something_else_is_rejected(self):
        assert ri._grounded(
            'Revise photosynthesis and the water cycle with a starter quiz.',
            ALLOWED) is False

    def test_filler_is_rejected(self):
        """"These students need more practice" is what the constant templates
        already said. It is not worth a model call."""
        assert ri._grounded(
            'These students need more practice and encouragement in class.',
            ALLOWED) is False

    def test_one_word_in_common_is_not_enough(self):
        assert ri._grounded('Spend the lesson on map skills generally.',
                            ALLOWED) is False

    def test_advice_naming_no_objective_is_allowed(self):
        """A band with no weak objectives is supposed to get general advice."""
        assert ri._grounded('Give them an extension task.', []) is True

    def test_an_empty_instruction_is_rejected(self):
        assert ri._grounded('', ALLOWED) is False

    def test_a_band_the_report_did_not_send_is_dropped(self, monkeypatch):
        """The model answering for a band that is not on this report would put
        advice under a heading the page never rendered."""
        fake = ri.ReportInstructions(bands=[
            ri.BandInstruction(code='AE', instruction='Re-teach locate a grid square.'),
            ri.BandInstruction(code='BE', instruction='Advice for a band not sent.'),
        ])
        monkeypatch.setattr(ri, '_call_model',
                            lambda lesson, groups, timeout: {
                                b.code: b.instruction for b in fake.bands
                                if b.code in {g['code'] for g in groups}})
        out = ri.generate(LESSON, GROUPS)
        assert 'BE' not in out


class TestItCallsOncePerReportState:
    """The report is opened repeatedly; the inputs change only when students
    submit. One call per distinct state, not per page view."""

    def test_a_second_identical_call_is_served_from_cache(self, monkeypatch):
        calls = []

        def once(lesson, groups, timeout):
            calls.append(1)
            return {'AE': 'Re-teach locate a grid square.'}

        monkeypatch.setattr(ri, '_call_model', once)
        assert ri.generate(LESSON, GROUPS)['AE']
        assert ri.generate(LESSON, GROUPS)['AE']
        assert len(calls) == 1

    def test_a_changed_weak_objective_is_a_different_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ri, '_call_model',
                            lambda lesson, groups, timeout: calls.append(1) or {'AE': 'x'})

        ri.generate(LESSON, GROUPS)
        moved = [dict(GROUPS[0], common_weak=['Something else entirely']), GROUPS[1]]
        ri.generate(LESSON, moved)
        assert len(calls) == 2

    def test_a_changed_group_size_is_a_different_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ri, '_call_model',
                            lambda lesson, groups, timeout: calls.append(1) or {'AE': 'x'})

        ri.generate(LESSON, GROUPS)
        bigger = [dict(GROUPS[0], students=[1, 2, 3, 4]), GROUPS[1]]
        ri.generate(LESSON, bigger)
        assert len(calls) == 2


class TestThePromptCarriesTheData:
    """Whatever the model does with it, the objectives have to reach it whole
    and attached to the right band."""

    def test_every_objective_appears_verbatim(self):
        prompt = ri._build_prompt(LESSON, GROUPS)
        for obj in GROUPS[0]['common_weak']:
            assert obj in prompt

    def test_a_band_with_no_weak_objectives_says_so(self):
        prompt = ri._build_prompt(LESSON, GROUPS)
        assert '<weak_objectives/>' in prompt

    def test_the_band_meaning_is_stated(self):
        prompt = ri._build_prompt(LESSON, GROUPS)
        assert 'close to the threshold' in prompt
        assert 'ready for the next lesson' in prompt

    def test_the_task_comes_after_the_data(self):
        """Instructions last — the ordering Anthropic measures as better and
        the one that keeps them closest to the tokens being generated."""
        prompt = ri._build_prompt(LESSON, GROUPS)
        assert prompt.index('<groups>') < prompt.index('<task>')


class TestUnassessedHasNothingToReTeach:
    """A student with no exit-ticket attempt fails every objective by default,
    so common_weak for UN is the whole lesson. Handing that to the model
    produced "Re-teach the foundational concepts: …" for students who have not
    been assessed at all — advice about a weakness nobody has measured."""

    UN = {'code': 'UN', 'students': [1, 2],
          'common_weak': ['Identify that angles around a point sum to 360',
                          'Identify a right angle as exactly 90 degrees']}

    def test_the_objectives_are_not_sent(self):
        assert ri._weak_for(self.UN) == []

    def test_the_prompt_shows_the_band_as_having_none(self):
        prompt = ri._build_prompt(LESSON, [self.UN])
        assert '<weak_objectives/>' in prompt
        assert 'Identify a right angle' not in prompt

    def test_the_prompt_says_why(self):
        prompt = ri._build_prompt(LESSON, [self.UN])
        assert 'has not taken the exit ticket' in prompt

    def test_other_bands_keep_theirs(self):
        assert ri._weak_for(GROUPS[0]) == GROUPS[0]['common_weak']

    def test_a_un_instruction_is_not_held_to_objectives_it_never_saw(self):
        """Grounding is checked against what the band was actually given."""
        assert ri._grounded('Chase them to finish and submit the exit ticket.',
                            ri._weak_for(self.UN)) is True
