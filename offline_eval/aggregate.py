"""Build an offline-model leaderboard from results/*.json (one per model).

Reads each model's RunResult JSON and prints a ranked table:
  pass-rate, rubric mean, error count, and the per-pedagogy-dimension
  bottleneck. Sorted by pass-rate desc.

    venv/bin/python offline_eval/aggregate.py
"""
import json
import glob
import os
from collections import defaultdict

ROOT = '/home/daniel/Documents/work/Nyansapo/web/ai-tutor'
# RESULTS_DIR env override lets a re-run aggregate from an alternate folder
# (e.g. single_turn_results/results2 for the per-family-tuned board, or
# multi_turn_results/ for Improved Eval 3). Defaults to the single-turn board.
RESULTS = os.environ.get('RESULTS_DIR') or os.path.join(ROOT, 'offline_eval', 'single_turn_results', 'results')


def summarize(path):
    d = json.load(open(path, encoding='utf-8'))
    results = d.get('results', []) or []
    total = d.get('total_scenarios', len(results))
    passed = d.get('passed', sum(1 for r in results if r.get('passed')))
    errored = d.get('errored', sum(1 for r in results if r.get('error')))
    # rubric mean across scenarios that ran a rubric. rubric_result has
    # mean_score; fall back to averaging items[].score if absent.
    rubric_scores = []
    for r in results:
        rr = r.get('rubric_result') or {}
        ms = rr.get('mean_score')
        if ms is None and rr.get('items'):
            its = [it.get('score') for it in rr['items']
                   if isinstance(it.get('score'), (int, float))]
            ms = sum(its) / len(its) if its else None
        if isinstance(ms, (int, float)):
            rubric_scores.append(ms)
    rubric_mean = sum(rubric_scores) / len(rubric_scores) if rubric_scores else None
    # per-dimension fail counts (Layer 3b advisory). dimensions is a LIST
    # of {name, passed, ...}; tolerate a dict shape too.
    dim_fail = defaultdict(int)
    for r in results:
        dims = (r.get('dimensions_result') or {}).get('dimensions') or []
        if isinstance(dims, dict):
            dims = [{'name': k, **(v if isinstance(v, dict) else {'passed': v})}
                    for k, v in dims.items()]
        for dim in dims:
            if isinstance(dim, dict) and dim.get('passed') is False:
                dim_fail[dim.get('name', '?')] += 1
    worst_dim = max(dim_fail.items(), key=lambda kv: kv[1]) if dim_fail else None
    # fail tags
    tag_fail = defaultdict(int)
    for r in results:
        if not r.get('passed'):
            for t in (r.get('tags') or []):
                tag_fail[t] += 1
    worst_tag = max(tag_fail.items(), key=lambda kv: kv[1]) if tag_fail else None
    return {
        'model': os.path.splitext(os.path.basename(path))[0],
        'total': total, 'passed': passed, 'errored': errored,
        'pass_rate': (passed / total) if total else 0.0,
        'rubric_mean': rubric_mean,
        'worst_dim': worst_dim, 'worst_tag': worst_tag,
    }


def main():
    files = sorted(glob.glob(os.path.join(RESULTS, '*.json')))
    if not files:
        print(f"No results in {RESULTS}. Run offline_eval/run_matrix.sh first.")
        return
    rows = [summarize(f) for f in files]
    rows.sort(key=lambda r: r['pass_rate'], reverse=True)

    print(f"\n{'MODEL':<22} {'PASS':>9} {'RATE':>7} {'RUBRIC':>7} {'ERR':>4}  BOTTLENECK")
    print('-' * 78)
    for r in rows:
        rate = f"{r['pass_rate']*100:.0f}%"
        rub = f"{r['rubric_mean']:.2f}" if r['rubric_mean'] is not None else '  -'
        passc = f"{r['passed']}/{r['total']}"
        bottleneck = ''
        if r['worst_tag']:
            bottleneck = f"{r['worst_tag'][0]} ({r['worst_tag'][1]})"
        elif r['worst_dim']:
            bottleneck = f"dim:{r['worst_dim'][0]} ({r['worst_dim'][1]})"
        print(f"{r['model']:<22} {passc:>9} {rate:>7} {rub:>7} {r['errored']:>4}  {bottleneck}")
    print('-' * 78)
    print("Sorted by pass-rate. RUBRIC = mean Anthropic-Haiku rubric score (0-1). "
          "Higher = better. ERR = scenarios that errored (e.g. model timeout).")


if __name__ == '__main__':
    main()
