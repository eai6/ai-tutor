"""Tests for the terminal tutor client (apps/tutoring/cli/).

The render helpers are pure functions by design, so they are tested without a
TTY, a database, or an LLM. Plan: memory/terminal_tutor_client_plan.md
"""
from django.test import SimpleTestCase

from apps.tutoring.cli import render
from apps.tutoring.cli.session import BootstrapError, subject_filter


class SubjectFilterTest(SimpleTestCase):
    """The filter must match subject_code OR subject_type.

    Courses in this database are classified through different fields —
    Mathematics S3 has subject_type='math' with subject_code empty, Mount Fleuri
    Geography S3 has subject_code='geography' with subject_type empty. A filter
    checking only one field returns nothing for half the catalogue, and the
    failure looks like "no lessons found" rather than a bug.
    """

    def test_math_matches_both_classification_fields(self):
        children = str(subject_filter('math'))
        self.assertIn('subject_code', children)
        self.assertIn('subject_type', children)

    def test_geography_matches_subject_code(self):
        self.assertIn('geography', str(subject_filter('geography')))

    def test_unknown_subject_lists_the_known_ones(self):
        with self.assertRaises(BootstrapError) as ctx:
            subject_filter('astrology')
        message = str(ctx.exception)
        self.assertIn('astrology', message)
        self.assertIn('math', message)
        self.assertIn('geography', message)


class RenderReplyTest(SimpleTestCase):
    def test_reads_message_not_content(self):
        """respond_for_view returns 'message'; the raw engine returns 'content'.

        Asserted explicitly because the two entry points differ on exactly this
        key (engine.py:2286 vs engine.py:324), and reading the wrong one yields
        a silently blank tutor reply rather than an error.
        """
        self.assertEqual(
            render.format_reply({'message': 'hello', 'content': 'WRONG'}, colour=False),
            'hello',
        )

    def test_missing_message_is_empty_not_crash(self):
        self.assertEqual(render.format_reply({}, colour=False), '')

    def test_colour_false_emits_no_ansi(self):
        out = render.format_reply({'message': 'hi'}, colour=True)
        self.assertIn('\033[', out)
        self.assertNotIn('\033[', render.format_reply({'message': 'hi'}, colour=False))


class RenderStateTest(SimpleTestCase):
    def test_ungraded_turn_distinguished_from_incorrect(self):
        """is_correct=None means 'the tutor taught', not 'the student was wrong'.

        Conflating them would make every teaching turn look like a failure while
        debugging.
        """
        ungraded = render.format_state({'is_correct': None}, colour=False)
        wrong = render.format_state({'is_correct': False}, colour=False)
        self.assertIn('not graded', ungraded)
        self.assertIn('incorrect', wrong)
        self.assertNotIn('incorrect', ungraded)

    def test_step_position_and_flags(self):
        out = render.format_state(
            {'step_number': 2, 'total_steps': 10, 'phase': 'explore',
             'is_correct': True, 'show_exit_ticket': True},
            colour=False,
        )
        self.assertIn('step 2/10', out)
        self.assertIn('explore', out)
        self.assertIn('correct', out)
        self.assertIn('EXIT TICKET', out)

    def test_media_urls_surface(self):
        out = render.format_state(
            {'media': [{'url': 'http://x/fig.png'}]}, colour=False)
        self.assertIn('http://x/fig.png', out)


class RenderToolsTest(SimpleTestCase):
    def test_no_tool_calls_is_stated_not_blank(self):
        self.assertIn('no tool calls', render.format_tools({}, colour=False))
        self.assertIn('no tool calls', render.format_tools(None, colour=False))

    def test_verdict_preferred_over_raw_result(self):
        out = render.format_tools(
            {'tool_calls': [{'tool': 'record_answer',
                             'result': {'verdict': 'correct', 'noise': 'x' * 500}}]},
            colour=False,
        )
        self.assertIn('record_answer', out)
        self.assertIn('verdict=correct', out)
        self.assertNotIn('xxxxx', out)

    def test_long_result_is_truncated(self):
        out = render.format_tools(
            {'tool_calls': [{'tool': 'pose_question', 'result': {'blob': 'y' * 900}}]},
            colour=False,
        )
        self.assertLess(len(out), 200)
        self.assertIn('…', out)


class RenderJudgeTest(SimpleTestCase):
    def test_empty_judge_explains_itself(self):
        """simple_tutor has no per-turn combined judge — that is expected.

        An unexplained blank would read as a bug in the client.
        """
        out = render.format_judge({}, colour=False)
        self.assertIn('grader.py', out)

    def test_populated_judge_renders_keys(self):
        out = render.format_judge({'grading': {'verdict': 'correct'}}, colour=False)
        self.assertIn('grading', out)
        self.assertIn('correct', out)


class RenderTimingTest(SimpleTestCase):
    def test_seconds_only_without_turn(self):
        self.assertIn('1.5s', render.format_timing(1.5, None, colour=False))

    def test_tokens_and_rate_when_turn_has_them(self):
        class _Turn:
            tokens_in, tokens_out = 100, 40
        out = render.format_timing(4.0, _Turn(), colour=False)
        self.assertIn('in=100 out=40', out)
        self.assertIn('10.0 tok/s', out)

    def test_zero_elapsed_does_not_divide_by_zero(self):
        class _Turn:
            tokens_in, tokens_out = 10, 5
        render.format_timing(0.0, _Turn(), colour=False)  # must not raise


class RenderLessonTableTest(SimpleTestCase):
    def test_empty_suggests_loading_fixtures(self):
        self.assertIn('loaddata', render.format_lesson_table([]))

    def test_rows_include_ids(self):
        out = render.format_lesson_table([(42, 'Maths S3', 'Angles')])
        self.assertIn('42', out)
        self.assertIn('Angles', out)
