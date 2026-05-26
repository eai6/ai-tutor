"""M5.5 acceptance tests — matching grader (Tier 1, deterministic).

Real production shape (queried from staging 2026-05-26, 1,461 matching
questions across math + geography):

    answer_data = {
        'pairs': [
            {'left': 'Sugarcane farming', 'right': 'Primary'},
            {'left': 'Sugar refinery',    'right': 'Secondary'},
            {'left': 'Tourism hotel',     'right': 'Tertiary'},
        ],
        'distractor_rights': ['Road construction', 'Fishing vessel operation'],
    }

Grader: counts correctly-matched pairs, returns CORRECT (all),
PARTIAL with per-pair scores (some), INCORRECT (none).
"""
from types import SimpleNamespace
from unittest import TestCase

from apps.tutoring.simple_tutor.grader import (
    Verdict,
    grade_answer,
    _grade_matching,
    _parse_matching_answer,
)


# Real production example from staging
_PAIRS_INDUSTRY = [
    {'left': 'Sugarcane farming', 'right': 'Primary'},
    {'left': 'Sugar refinery',    'right': 'Secondary'},
    {'left': 'Tourism hotel',     'right': 'Tertiary'},
]


def _matching(pairs=None, distractors=None):
    """Matching question stand-in."""
    ad = {'pairs': pairs or _PAIRS_INDUSTRY}
    if distractors is not None:
        ad['distractor_rights'] = distractors
    return SimpleNamespace(
        pk=303,
        question_type='matching',
        question_text='Match each example to its category:',
        correct_answer='',
        answer_data=ad,
    )


# ============================================================================
# All correct — list-form student input (pair order)
# ============================================================================


class AllCorrectListInputTest(TestCase):
    def test_list_in_order(self):
        q = _matching()
        r = _grade_matching(q, ['Primary', 'Secondary', 'Tertiary'])
        self.assertEqual(r.verdict, Verdict.CORRECT)
        self.assertEqual(r.tier, 'matching')

    def test_list_case_insensitive(self):
        q = _matching()
        r = _grade_matching(q, ['primary', 'SECONDARY', 'Tertiary'])
        self.assertEqual(r.verdict, Verdict.CORRECT)


# ============================================================================
# All correct — dict-form student input (order-free)
# ============================================================================


class AllCorrectDictInputTest(TestCase):
    def test_dict_in_order(self):
        q = _matching()
        r = _grade_matching(q, {
            'Sugarcane farming': 'Primary',
            'Sugar refinery':    'Secondary',
            'Tourism hotel':     'Tertiary',
        })
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_dict_out_of_order(self):
        q = _matching()
        r = _grade_matching(q, {
            'Tourism hotel':     'Tertiary',
            'Sugarcane farming': 'Primary',
            'Sugar refinery':    'Secondary',
        })
        self.assertEqual(r.verdict, Verdict.CORRECT)


# ============================================================================
# Partial credit
# ============================================================================


class PartialCreditTest(TestCase):
    def test_one_wrong(self):
        # 2/3 correct → PARTIAL
        q = _matching()
        r = _grade_matching(q, ['Primary', 'Secondary', 'Secondary'])  # last wrong
        self.assertEqual(r.verdict, Verdict.PARTIAL)
        self.assertTrue(r.needs_followup)
        # per_criterion_scores reflects which pair landed
        score_values = list(r.per_criterion_scores.values())
        self.assertEqual(sorted(score_values), [0.0, 1.0, 1.0])

    def test_one_correct_two_wrong(self):
        # 1/3 → PARTIAL
        q = _matching()
        r = _grade_matching(q, ['Primary', 'Tertiary', 'Primary'])
        self.assertEqual(r.verdict, Verdict.PARTIAL)
        self.assertIn('1/3', r.justification)


# ============================================================================
# None correct
# ============================================================================


class NoneCorrectTest(TestCase):
    def test_all_wrong(self):
        q = _matching()
        r = _grade_matching(q, ['Tertiary', 'Primary', 'Secondary'])
        self.assertEqual(r.verdict, Verdict.INCORRECT)
        self.assertIn('0/3', r.justification)

    def test_all_distractors(self):
        q = _matching(distractors=['Road construction', 'Fishing vessel operation'])
        r = _grade_matching(q, ['Road construction', 'Fishing vessel operation',
                                 'Road construction'])
        self.assertEqual(r.verdict, Verdict.INCORRECT)


# ============================================================================
# Wrong shape / count
# ============================================================================


class WrongShapeTest(TestCase):
    def test_too_few(self):
        q = _matching()
        r = _grade_matching(q, ['Primary'])   # only 1 of 3
        self.assertEqual(r.verdict, Verdict.INCORRECT)

    def test_too_many(self):
        q = _matching()
        # 4 answers when only 3 pairs — falls through to "could not parse"
        # since list-length != pairs-length and isn't a dict
        r = _grade_matching(q, ['Primary', 'Secondary', 'Tertiary', 'extra'])
        self.assertEqual(r.verdict, Verdict.INCORRECT)

    def test_empty(self):
        q = _matching()
        r = _grade_matching(q, '')
        self.assertEqual(r.verdict, Verdict.INCORRECT)


# ============================================================================
# String inputs (LLM extraction variations)
# ============================================================================


class StringInputTest(TestCase):
    """The tutor LLM may pass back a JSON string or arrow-form string."""

    def test_json_dict_string(self):
        q = _matching()
        r = _grade_matching(
            q,
            '{"Sugarcane farming": "Primary", "Sugar refinery": "Secondary", "Tourism hotel": "Tertiary"}',
        )
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_json_list_string(self):
        q = _matching()
        r = _grade_matching(q, '["Primary", "Secondary", "Tertiary"]')
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_arrow_form_string(self):
        q = _matching()
        r = _grade_matching(
            q,
            'Sugarcane farming -> Primary, Sugar refinery -> Secondary, Tourism hotel -> Tertiary',
        )
        self.assertEqual(r.verdict, Verdict.CORRECT)

    def test_unicode_arrow(self):
        q = _matching()
        r = _grade_matching(
            q,
            'Sugarcane farming → Primary, Sugar refinery → Secondary, Tourism hotel → Tertiary',
        )
        self.assertEqual(r.verdict, Verdict.CORRECT)


# ============================================================================
# Defensive errors
# ============================================================================


class DefensiveTest(TestCase):
    def test_no_pairs_raises(self):
        q = SimpleNamespace(
            pk=1, question_type='matching', question_text='x',
            correct_answer='', answer_data={},
        )
        with self.assertRaisesRegex(ValueError, 'pairs'):
            _grade_matching(q, ['something'])


# ============================================================================
# Dispatcher routes to matching grader
# ============================================================================


class DispatcherTest(TestCase):
    def test_matching_routes_to_matching_grader(self):
        q = _matching()
        r = grade_answer(question=q, student_answer=['Primary', 'Secondary', 'Tertiary'])
        self.assertEqual(r.tier, 'matching')
        self.assertEqual(r.verdict, Verdict.CORRECT)


# ============================================================================
# _parse_matching_answer helper
# ============================================================================


class ParseMatchingAnswerTest(TestCase):
    def test_dict_passthrough(self):
        r = _parse_matching_answer({'a': 'x', 'b': 'y'}, _PAIRS_INDUSTRY)
        self.assertEqual(r, {'a': 'x', 'b': 'y'})

    def test_list_of_dicts(self):
        # When LLM returns pair-object form
        r = _parse_matching_answer(
            [{'left': 'a', 'right': '1'}, {'left': 'b', 'right': '2'}],
            _PAIRS_INDUSTRY,
        )
        self.assertEqual(r, {'a': '1', 'b': '2'})

    def test_list_of_strings_correct_length(self):
        r = _parse_matching_answer(['Primary', 'Secondary', 'Tertiary'],
                                    _PAIRS_INDUSTRY)
        self.assertEqual(r, {
            'Sugarcane farming': 'Primary',
            'Sugar refinery':    'Secondary',
            'Tourism hotel':     'Tertiary',
        })

    def test_list_of_strings_wrong_length_returns_none(self):
        # Wrong count → None (caller treats as INCORRECT)
        r = _parse_matching_answer(['Primary'], _PAIRS_INDUSTRY)
        self.assertIsNone(r)

    def test_arrow_string(self):
        r = _parse_matching_answer('a -> 1, b -> 2', _PAIRS_INDUSTRY)
        self.assertEqual(r, {'a': '1', 'b': '2'})

    def test_empty_returns_none(self):
        self.assertIsNone(_parse_matching_answer('', _PAIRS_INDUSTRY))
        self.assertIsNone(_parse_matching_answer(None, _PAIRS_INDUSTRY))
