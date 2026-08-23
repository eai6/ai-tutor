"""Per-turn latency, tutor vs student, from a multi-turn results directory.

    RESULTS_DIR=$PWD/offline_eval/multi_turn_results/hg1_prod \
      ./venv/bin/python offline_eval/latency_report.py

Reads `latency_ms` off each transcript turn (recorded by
apps/tutoring/student_sim/driver.py) and reports the distribution per role per
arm. Pure standard library — no venv or Django needed.

WHAT THE TUTOR NUMBER INCLUDES, and why it is the honest one to quote: it is
wall-clock around the engine call, so it carries the whole turn — both LLM
calls under TUTOR_CALL_MODE=two, the tool loop, the in-session grader, and the
network hop to wherever Ollama is. That is what a student actually waits
through. A raw model-decode figure would be smaller and would not describe the
product.

The student number is the simulator, NOT a human. It is useful for costing a
sweep and for spotting a sim that has started rambling; it says nothing about
how long a real student takes to answer.
"""
import glob
import json
import os
import statistics
import sys


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def main() -> int:
    results = os.environ.get('RESULTS_DIR')
    if not results:
        print('set RESULTS_DIR, e.g. '
              'RESULTS_DIR=$PWD/offline_eval/multi_turn_results/hg1_prod')
        return 2

    files = sorted(f for f in glob.glob(os.path.join(results, '*.json')))
    if not files:
        print(f'no result JSONs in {results}')
        return 1

    print(f'{"arm":26}{"role":9}{"n":>6}{"median":>10}{"p95":>10}{"max":>10}'
          f'{"total":>10}')
    print('-' * 81)
    grand = {}
    for path in files:
        try:
            data = json.load(open(path))
        except (json.JSONDecodeError, OSError) as exc:
            print(f'{os.path.basename(path)}: unreadable ({exc})')
            continue
        arm = data.get('tutor_model') or os.path.basename(path)[:-5]
        arm = arm.split('/')[-1]

        by_role: dict[str, list[float]] = {'tutor': [], 'student': []}
        for r in data.get('results', []):
            for t in r.get('transcript', []):
                ms = t.get('latency_ms')
                if ms is None:                    # pre-instrumentation run
                    continue
                by_role.setdefault(t.get('role', '?'), []).append(float(ms))

        if not any(by_role.values()):
            print(f'{arm:26}{"—":9}{"":>6}  no latency_ms — run predates the '
                  f'instrumentation')
            continue

        for role in ('tutor', 'student'):
            xs = by_role.get(role) or []
            if not xs:
                continue
            print(f'{arm:26}{role:9}{len(xs):>6}'
                  f'{statistics.median(xs)/1000:>9.2f}s'
                  f'{pct(xs, 0.95)/1000:>9.2f}s'
                  f'{max(xs)/1000:>9.2f}s'
                  f'{sum(xs)/1000/60:>9.1f}m')
        grand[arm] = by_role

    # Session-level view: what one full lesson costs in wall-clock.
    print()
    print(f'{"arm":26}{"sessions":>10}{"median session":>17}{"tutor share":>14}')
    print('-' * 67)
    for path in files:
        try:
            data = json.load(open(path))
        except (json.JSONDecodeError, OSError):
            continue
        arm = (data.get('tutor_model') or os.path.basename(path)[:-5]).split('/')[-1]
        per_session, tutor_ms, all_ms = [], 0.0, 0.0
        for r in data.get('results', []):
            xs = [t.get('latency_ms') for t in r.get('transcript', [])
                  if t.get('latency_ms') is not None]
            if not xs:
                continue
            per_session.append(sum(xs))
            all_ms += sum(xs)
            tutor_ms += sum(t.get('latency_ms') or 0
                            for t in r.get('transcript', [])
                            if t.get('role') == 'tutor')
        if not per_session:
            continue
        share = (100 * tutor_ms / all_ms) if all_ms else 0
        print(f'{arm:26}{len(per_session):>10}'
              f'{statistics.median(per_session)/1000:>16.1f}s{share:>13.0f}%')
    return 0


if __name__ == '__main__':
    sys.exit(main())
