"""Compose the per-run JSONL line that gets appended to the
metrics-history branch.

Reads run_metrics.json (produced by extract_run_metrics.py from inside
the django container), pulls workflow context out of env vars, and
prints one JSONL line to stdout.

The workflow redirects stdout into a file (or appends directly) and
then commits.
"""
import datetime
import json
import os
import pathlib
import sys


def main() -> None:
    src = pathlib.Path(
        os.environ.get(
            'METRICS_SRC',
            'ops/annotator_agent/transcripts/run_metrics.json',
        )
    )
    if not src.exists():
        sys.stderr.write(f"run_metrics.json not found at {src}\n")
        sys.exit(1)
    data = json.loads(src.read_text())
    metrics = data.get('metrics') or {}

    try:
        max_items = int(os.environ.get('MAX_ITEMS', '0') or 0)
    except ValueError:
        max_items = 0

    line = {
        'ts': datetime.datetime.utcnow().isoformat() + 'Z',
        'sha': (os.environ.get('GITHUB_SHA', '') or '')[:12],
        'workflow_run_id': os.environ.get('GITHUB_RUN_ID', ''),
        'trigger_actor': os.environ.get('GITHUB_TRIGGERING_ACTOR', ''),
        'persona': os.environ.get('PERSONA', ''),
        'max_items': max_items,
        'annotator_role': data.get('annotator_role'),
        'total': data.get('total_items'),
        'passed': data.get('passed'),
        'failed': data.get('failed'),
        'pass_rate': (metrics.get('overall') or {}).get('pass_rate'),
        'by_subject': (metrics.get('slices') or {}).get('by_subject'),
        'by_stratum': (metrics.get('slices') or {}).get('by_stratum'),
        'by_eval_layer': (metrics.get('slices') or {}).get('by_eval_layer'),
        'failure_categories': metrics.get('failure_categories'),
    }
    print(json.dumps(line, default=str))


if __name__ == '__main__':
    main()
