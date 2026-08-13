"""End-to-end test for the verify_math_regression management command.

Builds a tiny synthetic audit CSV that mimics the format produced by
audit_math_false_positives, runs the regression harness against it,
and asserts the command catches the false-positives correctly.

See memory/math_tutor_fix_plan.md Phase M6.
"""

import csv
import io
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase


_AUDIT_FIELDNAMES = [
    "tutor_turn_id",
    "session_id",
    "student_id",
    "course_title",
    "lesson_title",
    "step_index",
    "step_type",
    "created_at",
    "student_said",
    "student_parsed",
    "expected_answer",
    "expected_parsed",
    "verdict",
    "praise_hits",
    "tutor_said_first_120",
]


def _write_audit_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_AUDIT_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({**{k: "" for k in _AUDIT_FIELDNAMES}, **row})


class VerifyMathRegressionTest(TestCase):
    def test_the_production_bug_case_is_caught(self):
        """The exact screenshot case: student '3 3/4', expected '5 1/4',
        tutor praised. Harness should confirm new logic catches it."""
        with tempfile.TemporaryDirectory() as td:
            in_path = Path(td) / "audit.csv"
            out_path = Path(td) / "audit.verified.csv"
            _write_audit_csv(
                [
                    {
                        "tutor_turn_id": "1",
                        "student_said": "3 3/4",
                        "expected_answer": "5 1/4",
                        "tutor_said_first_120": (
                            "Brilliant, Vaani! You've got it — 21/4 = 5 1/4 kg."
                        ),
                    }
                ],
                in_path,
            )

            buf = io.StringIO()
            call_command(
                "verify_math_regression",
                input=str(in_path),
                output=str(out_path),
                stdout=buf,
            )

            output = buf.getvalue()
            self.assertIn("would have been caught:   1", output)
            self.assertIn("catch rate:               100.0%", output)

            with out_path.open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["would_have_caught"], "yes")
            self.assertEqual(rows[0]["new_verdict"], "incorrect")
            self.assertEqual(rows[0]["praise_filter_triggered"], "yes")

    def test_multiple_cases_mixed_outcomes(self):
        """Mix of clean catches, parser-misses, and already-correct rows.
        Asserts each bucket counted correctly."""
        with tempfile.TemporaryDirectory() as td:
            in_path = Path(td) / "audit.csv"
            _write_audit_csv(
                [
                    # Clean catch
                    {
                        "student_said": "3 3/4",
                        "expected_answer": "5 1/4",
                        "tutor_said_first_120": "Brilliant! 5 1/4.",
                    },
                    # Parser returns None (both sides unparseable)
                    {
                        "student_said": "I don't know",
                        "expected_answer": "5 1/4",
                        "tutor_said_first_120": "Great try!",
                    },
                    # New parser considers equivalent-form correct
                    {
                        "student_said": "21/4",  # == 5 1/4
                        "expected_answer": "5 1/4",
                        "tutor_said_first_120": "Exactly right!",
                    },
                ],
                in_path,
            )

            buf = io.StringIO()
            call_command(
                "verify_math_regression",
                input=str(in_path),
                stdout=buf,
            )
            output = buf.getvalue()
            self.assertIn("rows replayed:            3", output)
            self.assertIn("would have been caught:   1", output)
            self.assertIn("parser returned None:     1", output)
            self.assertIn("new parser says correct:  1", output)

    def test_expect_all_caught_raises_when_miss(self):
        """--expect-all-caught is a CI gate: non-zero exit when any row
        wouldn't be caught."""
        from django.core.management.base import CommandError

        with tempfile.TemporaryDirectory() as td:
            in_path = Path(td) / "audit.csv"
            _write_audit_csv(
                [
                    {
                        "student_said": "I don't know",  # parser miss
                        "expected_answer": "5 1/4",
                        "tutor_said_first_120": "Great try!",
                    },
                ],
                in_path,
            )

            with self.assertRaises(CommandError):
                call_command(
                    "verify_math_regression",
                    input=str(in_path),
                    expect_all_caught=True,
                )
