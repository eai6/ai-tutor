"""The overview's triage rail.

The rail decides what a teacher sees first, so its ordering and its silence
are both behaviour worth pinning. Everything here is pure — build_attention_items
takes numbers and returns dicts — so these run without a database.
"""
from django.test import SimpleTestCase

from ai_tutor.apps.dashboard.attention import (
    DECLINED_FLOOR,
    ENGAGEMENT_FLOOR_PCT,
    REACH_FLOOR_PCT,
    SCORE_FLOOR_PCT,
    build_attention_items,
)


def healthy(**overrides):
    """Arguments describing a window where nothing needs the teacher."""
    kwargs = {
        'flag_count': 0,
        'et': {
            'reach_pct': 92, 'sessions_reached': 120,
            'attempts': 140, 'avg_pct': 81,
        },
        'prog': {'gain': {'declined': 0}},
        'total_students': 40,
        'active_students': 38,
    }
    kwargs.update(overrides)
    return kwargs


def keys(items):
    return [item['key'] for item in items]


class SilenceTests(SimpleTestCase):
    """An empty rail is a result, not a missing feature."""

    def test_a_healthy_window_surfaces_nothing(self):
        self.assertEqual(build_attention_items(**healthy()), [])

    def test_an_empty_window_does_not_invent_problems(self):
        # No students, no sessions, no data at all: every threshold has a
        # zero denominator and none of them may fire.
        self.assertEqual(
            build_attention_items(flag_count=0, et={}, prog={},
                                  total_students=0, active_students=0),
            [],
        )

    def test_missing_keys_are_tolerated(self):
        """A window mid-migration must still render a dashboard."""
        items = build_attention_items(
            flag_count=0, et={'reach_pct': None}, prog={'gain': {}},
            total_students=10, active_students=9,
        )
        self.assertEqual(items, [])


class OrderingTests(SimpleTestCase):
    """Urgency, not category. The teacher reads top-left first."""

    def test_safety_outranks_everything(self):
        items = build_attention_items(**healthy(
            flag_count=2,
            et={'reach_pct': 30, 'sessions_reached': 20, 'attempts': 40, 'avg_pct': 41},
            prog={'gain': {'declined': 9}},
            active_students=2,
        ))
        self.assertEqual(keys(items)[0], 'safety_flags')
        self.assertEqual(
            keys(items),
            ['safety_flags', 'declined', 'drop_off', 'low_scores', 'low_engagement'],
        )

    def test_declining_students_outrank_a_cohort_average(self):
        items = build_attention_items(**healthy(
            prog={'gain': {'declined': 3}},
            et={'reach_pct': 95, 'sessions_reached': 90, 'attempts': 90, 'avg_pct': 44},
        ))
        self.assertEqual(keys(items), ['declined', 'low_scores'])


class ThresholdTests(SimpleTestCase):
    """Each threshold fires on the wrong side of its floor and not on the right."""

    def test_reach_floor(self):
        below = healthy(et={'reach_pct': REACH_FLOOR_PCT - 1, 'sessions_reached': 10,
                            'attempts': 10, 'avg_pct': 90})
        at = healthy(et={'reach_pct': REACH_FLOOR_PCT, 'sessions_reached': 10,
                         'attempts': 10, 'avg_pct': 90})
        self.assertIn('drop_off', keys(build_attention_items(**below)))
        self.assertNotIn('drop_off', keys(build_attention_items(**at)))

    def test_drop_off_reports_the_share_that_stopped_not_the_share_that_finished(self):
        items = build_attention_items(**healthy(
            et={'reach_pct': 59, 'sessions_reached': 287, 'attempts': 341, 'avg_pct': 74},
        ))
        item = next(i for i in items if i['key'] == 'drop_off')
        self.assertEqual(item['figure'], '41%')

    def test_score_floor_needs_attempts_behind_it(self):
        """A 0% mean over zero attempts is an empty window, not a crisis."""
        no_attempts = healthy(et={'reach_pct': 95, 'sessions_reached': 5,
                                  'attempts': 0, 'avg_pct': 0})
        self.assertNotIn('low_scores', keys(build_attention_items(**no_attempts)))

        some = healthy(et={'reach_pct': 95, 'sessions_reached': 5,
                           'attempts': 5, 'avg_pct': SCORE_FLOOR_PCT - 1})
        self.assertIn('low_scores', keys(build_attention_items(**some)))

    def test_declined_floor(self):
        self.assertIn('declined', keys(build_attention_items(
            **healthy(prog={'gain': {'declined': DECLINED_FLOOR}}))))
        self.assertNotIn('declined', keys(build_attention_items(
            **healthy(prog={'gain': {'declined': DECLINED_FLOOR - 1}}))))

    def test_engagement_floor_counts_the_absent_not_the_present(self):
        total, active = 100, ENGAGEMENT_FLOOR_PCT - 10
        items = build_attention_items(**healthy(
            total_students=total, active_students=active))
        item = next(i for i in items if i['key'] == 'low_engagement')
        self.assertEqual(item['figure'], str(total - active))


class ShapeTests(SimpleTestCase):
    """The template tag reads these keys directly, so they are the contract."""

    REQUIRED = {'key', 'tone', 'icon', 'figure', 'label', 'detail', 'url'}
    TONES = {'danger', 'warning', 'info', 'success'}

    def test_every_item_is_complete_and_linked(self):
        items = build_attention_items(**healthy(
            flag_count=1,
            et={'reach_pct': 20, 'sessions_reached': 5, 'attempts': 5, 'avg_pct': 30},
            prog={'gain': {'declined': 4}},
            active_students=1,
        ))
        self.assertEqual(len(items), 5)
        for item in items:
            self.assertTrue(self.REQUIRED <= set(item), item)
            self.assertIn(item['tone'], self.TONES)
            # A triage row that goes nowhere is a notification, not triage.
            self.assertTrue(item['url'].startswith('/'), item['url'])
            # The icon name must not carry the sprite's own prefix — icon.html
            # adds it, and "i-i-flag" resolves to nothing at all.
            self.assertFalse(item['icon'].startswith('i-'), item['icon'])
