"""Dump the latest BenchmarkRun row from the ephemeral DB as JSON.

Invoked by .github/workflows/annotator_agent.yml from inside the
django container via `python manage.py shell -c "exec(...)"`.

We use a tiny Django entrypoint instead of a heredoc'd `manage.py
shell -c` because GitHub Actions has historically choked on the
indentation interaction between YAML literal blocks, bash heredocs,
and Python's significant whitespace.
"""
import json
import os
import sys


def main() -> None:
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
    django.setup()
    from ai_tutor.apps.benchmark.models import BenchmarkRun

    run = BenchmarkRun.objects.order_by('-id').first()
    if run is None:
        sys.stderr.write(
            "No BenchmarkRun found — agent never clicked Score now\n"
        )
        sys.exit(1)
    payload = {
        'run_id': run.id,
        'system_variant': run.system_variant,
        'annotator_role': run.annotator_role,
        'total_items': run.total_items,
        'passed': run.passed,
        'failed': run.failed,
        'metrics': run.metrics,
        'notes': run.notes,
        'created_at': run.created_at.isoformat(),
    }
    print(json.dumps(payload, default=str))


if __name__ == '__main__':
    main()
