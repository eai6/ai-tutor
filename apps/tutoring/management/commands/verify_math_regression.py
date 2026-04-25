"""Regression harness for the math-tutor false-positive fix.

Reads the CSV produced by `audit_math_false_positives`, replays each
flagged row through the deterministic numeric check + praise filter, and
asserts that the new logic would have caught the historical bug.

Outputs a second CSV alongside the input with per-row pass/fail and an
aggregate summary at the end. Meant to be run BEFORE deploy so you have
positive evidence the fix catches real production cases.

See memory/math_tutor_fix_plan.md Phase M6.

Usage:
    # First, run the audit against a DB snapshot:
    python manage.py audit_math_false_positives --output /tmp/audit.csv

    # Then verify the fix would catch each false-positive:
    python manage.py verify_math_regression --input /tmp/audit.csv
"""

from __future__ import annotations

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.tutoring.grader import check_math_answer, parse_math_answer
from apps.tutoring.praise_filter import strip_praise_if_wrong, _PRAISE_RE


class Command(BaseCommand):
    help = (
        "Replay audit CSV rows through the new math-eval pipeline and "
        "report how many historical false-positives would now be caught."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--input",
            required=True,
            help="Path to CSV produced by `audit_math_false_positives`.",
        )
        parser.add_argument(
            "--output",
            default=None,
            help=(
                "Path to write detailed per-row verification CSV. "
                "Defaults to <input>.verified.csv."
            ),
        )
        parser.add_argument(
            "--expect-all-caught",
            action="store_true",
            help=(
                "Exit non-zero if ANY row from the audit would not be caught "
                "by the new logic. Useful as a CI gate."
            ),
        )

    def handle(self, *args, **options):
        input_path = Path(options["input"])
        if not input_path.exists():
            raise CommandError(f"Input CSV not found: {input_path}")

        output_path = Path(
            options["output"] or str(input_path.with_suffix(".verified.csv"))
        )
        expect_all_caught = options["expect_all_caught"]

        total = 0
        caught = 0
        parser_missed = 0
        praise_missed = 0
        already_correct = 0

        with input_path.open("r", encoding="utf-8") as f_in, \
             output_path.open("w", newline="", encoding="utf-8") as f_out:
            reader = csv.DictReader(f_in)
            fieldnames = (reader.fieldnames or []) + [
                "would_have_caught",
                "new_verdict",
                "praise_filter_triggered",
                "stripped_preview",
                "failure_reason",
            ]
            writer = csv.DictWriter(f_out, fieldnames=fieldnames)
            writer.writeheader()

            for row in reader:
                total += 1
                student_said = row.get("student_said", "")
                expected = row.get("expected_answer", "")
                tutor_said = row.get("tutor_said_first_120", "") or row.get("tutor_said", "")

                new_verdict = "unknown"
                praise_triggered = False
                stripped_preview = ""
                failure_reason = ""

                # Layer 1 replay
                check = check_math_answer(student_said, expected)
                if check is None:
                    parser_missed += 1
                    failure_reason = "parser_returned_none"
                elif check.is_correct:
                    # Audit said this was a false positive but new parser
                    # now considers it correct. Could be a parser
                    # false-negative in the audit, or stricter tolerance.
                    already_correct += 1
                    new_verdict = "correct"
                    failure_reason = "new_parser_says_correct"
                else:
                    new_verdict = "incorrect"

                # Layer 3 replay (only meaningful if layer 1 said wrong)
                if check is not None and not check.is_correct and tutor_said:
                    stripped, modified = strip_praise_if_wrong(
                        tutor_said, is_correct=False,
                    )
                    stripped_preview = stripped[:200]
                    if modified and not _PRAISE_RE.search(stripped):
                        praise_triggered = True
                    else:
                        # The filter didn't catch the praise — unusual but
                        # possible if the audit regex matched praise that
                        # the filter regex doesn't (or left some through).
                        praise_missed += 1
                        failure_reason = failure_reason or "praise_not_stripped"

                would_catch = (
                    check is not None
                    and not check.is_correct
                    and praise_triggered
                )
                if would_catch:
                    caught += 1

                out_row = {**row}
                out_row["would_have_caught"] = "yes" if would_catch else "no"
                out_row["new_verdict"] = new_verdict
                out_row["praise_filter_triggered"] = "yes" if praise_triggered else "no"
                out_row["stripped_preview"] = stripped_preview
                out_row["failure_reason"] = failure_reason
                writer.writerow(out_row)

        # Summary
        self.stdout.write(self.style.SUCCESS("Regression verification complete."))
        self.stdout.write(f"  rows replayed:            {total}")
        self.stdout.write(f"  would have been caught:   {caught}")
        self.stdout.write(f"  parser returned None:     {parser_missed}")
        self.stdout.write(f"  new parser says correct:  {already_correct}")
        self.stdout.write(f"  praise filter missed:     {praise_missed}")
        if total > 0:
            rate = 100.0 * caught / total
            self.stdout.write(f"  catch rate:               {rate:.1f}%")
        self.stdout.write(f"\n  Detailed CSV:             {output_path.resolve()}")

        if expect_all_caught and caught < total:
            raise CommandError(
                f"{total - caught} audit rows NOT caught by the new logic. "
                "Review the detailed CSV before deploying."
            )
