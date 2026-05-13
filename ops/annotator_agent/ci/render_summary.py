"""Render headline run metrics as GitHub markdown.

Writes to ``$GITHUB_STEP_SUMMARY`` if set, otherwise stdout. The
markdown shows pass rate, failure category counts, slice breakdowns,
and a note about the screenshot.

Invoked by the workflow's "render run summary" step.
"""
import json
import os
import pathlib
import sys


def fmt(n) -> str:
    if n is None:
        return '—'
    try:
        return f"{float(n):.3f}"
    except (TypeError, ValueError):
        return str(n)


def main() -> None:
    src = pathlib.Path(
        os.environ.get(
            'METRICS_SRC',
            'ops/annotator_agent/transcripts/run_metrics.json',
        )
    )
    out_path = os.environ.get('GITHUB_STEP_SUMMARY', '')
    out = open(out_path, 'a') if out_path else sys.stdout

    def w(s: str = '') -> None:
        out.write(s + '\n')

    if not src.exists():
        w('_No run_metrics.json — agent never produced a run._')
        return

    data = json.loads(src.read_text())
    metrics = data.get('metrics') or {}
    overall = metrics.get('overall') or {}
    slices = metrics.get('slices') or {}
    fc = metrics.get('failure_categories') or {}

    w('# Annotator agent run')
    w('')
    w(
        f"**Run #{data['run_id']}** — variant `{data['system_variant']}`, "
        f"annotator `{data['annotator_role']}`"
    )
    w('')
    w(
        f"- **Pass rate**: `{fmt(overall.get('pass_rate'))}` "
        f"({data['passed']}/{data['total_items']})"
    )
    w(f"- **Failed**: {data['failed']}")
    w('')

    if fc:
        w('## Failure categories')
        w('')
        w('| Category | Count |')
        w('|---|---|')
        for cat, n in sorted(fc.items(), key=lambda kv: -kv[1]):
            w(f'| `{cat}` | {n} |')
        w('')

    for slice_name, buckets in slices.items():
        if not buckets:
            continue
        w(f'## Slice — {slice_name}')
        w('')
        w('| Bucket | Pass | Total | Rate |')
        w('|---|---|---|---|')
        for name, stats in sorted(buckets.items()):
            w(
                f"| `{name}` | {stats.get('passed', 0)} | "
                f"{stats.get('total', 0)} | "
                f"{fmt(stats.get('pass_rate'))} |"
            )
        w('')

    screenshot = pathlib.Path(
        'ops/annotator_agent/transcripts/run-detail.png'
    )
    if screenshot.exists():
        size_kb = screenshot.stat().st_size // 1024
        w(
            f'📸 Screenshot of run-detail page captured '
            f'({size_kb} KB) — see the **annotator-transcripts** '
            f'artifact below.'
        )
    else:
        w("_Screenshot not captured (agent didn't take one)._")

    if out_path:
        out.close()


if __name__ == '__main__':
    main()
