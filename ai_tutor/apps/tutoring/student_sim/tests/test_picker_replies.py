"""The student sim answers with a letter when the buttons are its only input.

A sim that types prose into a picker session measures an interface the
deployment does not have, and hands the grader free-text signal a
button-tapping student could never produce.
"""
import pytest

from ai_tutor.apps.tutoring.student_sim.client import (
    _coerce_to_letter, _picker_instruction, _picker_letters,
)

PAYLOAD = {'letters': [{'letter': 'A', 'text': 'north'},
                       {'letter': 'B', 'text': 'south'},
                       {'letter': 'C', 'text': 'east'},
                       {'letter': 'D', 'text': 'west'}]}


class TestPickerLetters:
    def test_reads_the_engine_payload_shape(self):
        assert _picker_letters(PAYLOAD) == ['A', 'B', 'C', 'D']

    def test_two_option_question(self):
        assert _picker_letters({'letters': [{'letter': 'A', 'text': 'yes'},
                                            {'letter': 'B', 'text': 'no'}]}) == ['A', 'B']

    @pytest.mark.parametrize('bad', [None, {}, {'letters': []}, {'letters': None},
                                     'ABCD', 42])
    def test_no_picker_means_empty_not_a_crash(self, bad):
        """Empty => the caller falls back to free text. That is the safe
        direction: constraining a student to no letters at all would hang the
        session, while an unconstrained reply is merely the old behaviour."""
        assert _picker_letters(bad) == []

    def test_junk_letters_are_dropped(self):
        assert _picker_letters({'letters': [{'letter': 'A', 'text': 'x'},
                                            {'letter': 'Z', 'text': 'y'},
                                            {'letter': '', 'text': 'z'}]}) == ['A']


class TestCoerceToLetter:
    @pytest.mark.parametrize('raw,want', [
        ('B', 'B'), ('b', 'B'), (' C ', 'C'), ('D.', 'D'),
        ('A)', 'A'), ('I think B', 'B'), ('The answer is C', 'C'),
    ])
    def test_pulls_the_letter_out(self, raw, want):
        assert _coerce_to_letter(raw, ['A', 'B', 'C', 'D']) == want

    def test_a_letter_not_offered_is_not_accepted(self):
        """D is a valid letter but not on this two-button question."""
        assert _coerce_to_letter('D', ['A', 'B']) == 'A'

    @pytest.mark.parametrize('raw', ['', '   ', 'I have no idea', 'north'])
    def test_prose_falls_back_to_an_offered_letter(self, raw):
        """Never free text. A real picker UI cannot send a sentence, so a wrong
        letter is truthful where a sentence is not."""
        assert _coerce_to_letter(raw, ['A', 'B', 'C', 'D']) in {'A', 'B', 'C', 'D'}

    def test_the_word_north_does_not_become_a_letter_by_accident(self):
        assert _coerce_to_letter('north', ['A', 'B']) in {'A', 'B'}


class TestPickerInstruction:
    def test_names_only_the_offered_letters(self):
        assert 'A, B' in _picker_instruction(['A', 'B'])

    def test_tells_the_persona_to_keep_being_wrong(self):
        """Appended to the persona prompt, not substituting it: a struggler
        must still pick badly, or the picker arm would look better than the
        model is purely because guessing is easier than composing."""
        assert 'wrong letter' in _picker_instruction(['A', 'B', 'C', 'D'])
