"""Report + diff for eval runs.

Loads one or two run JSON blobs from ``evals/runs/`` and prints a human-
readable summary: overall pass rate, per-persona / per-tag breakdowns,
plus newly-passing / newly-failing scenarios when ``--diff`` is set.

Usage:
    python -m evals.report                       # most recent run
    python -m evals.report <path>                # specific run
    python -m evals.report --diff                # latest vs second-latest
    python -m evals.report <path> --diff <prev>  # explicit pair

Phase 5 from memory/eval_harness_plan.md. The dataset is the same across
runs; only ``production.tutor_response``, ``actual_labels``, and
``verdict.passes`` change per system variant — so the diff math is just
"set membership of (scenario_id) crossed with passed-bool flip."

No LLM calls. No DB access. Pure JSON-in, text-out.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = REPO_ROOT / 'evals' / 'runs'


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

@dataclass
class Bucket:
    """Pass/fail counts for one cohort (a persona, a tag, etc.)."""
    passed: int = 0
    failed: int = 0
    errored: int = 0

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errored

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


@dataclass
class Summary:
    started_at: str
    finished_at: str
    git_sha: str
    total: int
    passed: int
    failed: int
    errored: int
    overall: Bucket
    by_persona: dict[str, Bucket] = field(default_factory=dict)
    by_tag: dict[str, Bucket] = field(default_factory=dict)
    by_mode: dict[str, Bucket] = field(default_factory=dict)
    scenarios: dict[str, dict] = field(default_factory=dict)
    rubric_tokens_in: int = 0
    rubric_tokens_out: int = 0
    rubric_errors: int = 0
    # Per-dimension stats: dim_name -> Bucket of pass/fail across scenarios.
    by_dimension: dict[str, Bucket] = field(default_factory=dict)
    dimensions_tokens_in: int = 0
    dimensions_tokens_out: int = 0
    dimensions_errors: int = 0


def _scenario_status(r: dict) -> str:
    if r.get('error'):
        return 'error'
    return 'pass' if r.get('passed') else 'fail'


def _bump(bucket: Bucket, status: str) -> None:
    if status == 'pass':
        bucket.passed += 1
    elif status == 'fail':
        bucket.failed += 1
    else:
        bucket.errored += 1


def summarize(run: dict) -> Summary:
    overall = Bucket()
    by_persona: dict[str, Bucket] = defaultdict(Bucket)
    by_tag: dict[str, Bucket] = defaultdict(Bucket)
    by_mode: dict[str, Bucket] = defaultdict(Bucket)
    scenarios: dict[str, dict] = {}
    rubric_in = rubric_out = rubric_err = 0
    by_dim: dict[str, Bucket] = defaultdict(Bucket)
    dim_in = dim_out = dim_err = 0

    for r in run.get('results') or []:
        scenario_id = r.get('scenario_id') or '?'
        status = _scenario_status(r)
        persona = r.get('persona') or 'unknown'
        mode = r.get('mode') or 'single_turn'
        tags = r.get('tags') or []

        _bump(overall, status)
        _bump(by_persona[persona], status)
        _bump(by_mode[mode], status)
        for tag in tags:
            _bump(by_tag[tag], status)

        rubric = r.get('rubric_result') or {}
        rubric_in += int(rubric.get('tokens_in') or 0)
        rubric_out += int(rubric.get('tokens_out') or 0)
        if rubric.get('error'):
            rubric_err += 1

        # Pedagogical dimensions — per-scenario per-dimension stats.
        dimensions = r.get('dimensions_result') or {}
        dim_in += int(dimensions.get('tokens_in') or 0)
        dim_out += int(dimensions.get('tokens_out') or 0)
        if dimensions.get('error'):
            dim_err += 1
        for d in (dimensions.get('dimensions') or []):
            name = d.get('name', '?')
            _bump(by_dim[name], 'pass' if d.get('passed') else 'fail')

        scenarios[scenario_id] = {
            'status': status,
            'persona': persona,
            'tags': tags,
            'mode': mode,
            'sim_reason': r.get('sim_reason') or '',
            'fail_reasons': _short_fail_reasons(r),
            'error_msg': (r.get('error') or '')[:120],
        }

    return Summary(
        started_at=run.get('started_at', '?'),
        finished_at=run.get('finished_at', '?'),
        git_sha=run.get('git_sha', '?'),
        total=run.get('total_scenarios', 0),
        passed=run.get('passed', 0),
        failed=run.get('failed', 0),
        errored=run.get('errored', 0),
        overall=overall,
        by_persona=dict(by_persona),
        by_tag=dict(by_tag),
        by_mode=dict(by_mode),
        scenarios=scenarios,
        rubric_tokens_in=rubric_in,
        rubric_tokens_out=rubric_out,
        rubric_errors=rubric_err,
        by_dimension=dict(by_dim),
        dimensions_tokens_in=dim_in,
        dimensions_tokens_out=dim_out,
        dimensions_errors=dim_err,
    )


def _short_fail_reasons(r: dict) -> list[str]:
    fails = [a['name'] for a in (r.get('assertion_results') or []) if not a.get('passed')]
    rubric = r.get('rubric_result') or {}
    if rubric and not rubric.get('passed'):
        if rubric.get('error'):
            fails.append('rubric(err)')
        else:
            fails.append(f"rubric({rubric.get('mean_score', 0):.2f}<{rubric.get('pass_threshold', 0):.2f})")
    dimensions = r.get('dimensions_result') or {}
    if dimensions and not dimensions.get('passed'):
        if dimensions.get('error'):
            fails.append('dimensions(err)')
        else:
            bad = [d['name'] for d in (dimensions.get('dimensions') or [])
                   if not d.get('passed')]
            fails.append(f"dim({','.join(bad)})")
    return fails


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_bucket(b: Bucket) -> str:
    return f"{b.passed:>3}/{b.total:<3} ({b.pass_rate * 100:5.1f}%)"


def _delta(curr: int, prior: int) -> str:
    if curr == prior:
        return '──'
    sign = '↑' if curr > prior else '↓'
    return f"{sign} {curr - prior:+d}"


def format_summary(s: Summary, prior: Summary | None = None) -> str:
    out: list[str] = []
    out.append('=' * 72)
    out.append(f"Eval run: {s.git_sha} ({s.started_at[:16]})")
    if prior:
        out.append(f"  vs prior: {prior.git_sha} ({prior.started_at[:16]})")
    out.append('=' * 72)
    out.append('')

    # Overall.
    out.append("OVERALL")
    out.append(f"  this run:  {_fmt_bucket(s.overall)}")
    if prior:
        out.append(f"  prior:     {_fmt_bucket(prior.overall)}")
        out.append(
            f"  Δ:         passed {_delta(s.passed, prior.passed)}  "
            f"failed {_delta(s.failed, prior.failed)}  "
            f"errored {_delta(s.errored, prior.errored)}"
        )
    out.append('')

    # By persona.
    out.append("BY PERSONA")
    personas = sorted(set(s.by_persona) | set(prior.by_persona if prior else {}))
    width = max((len(p) for p in personas), default=8)
    for p in personas:
        b = s.by_persona.get(p, Bucket())
        line = f"  {p:<{width}}  {_fmt_bucket(b)}"
        if prior:
            pb = prior.by_persona.get(p, Bucket())
            line += f"   prior {_fmt_bucket(pb)}  Δ {_delta(b.passed, pb.passed)}"
        out.append(line)
    out.append('')

    # By mode.
    out.append("BY MODE")
    modes = sorted(set(s.by_mode) | set(prior.by_mode if prior else {}))
    for m in modes:
        b = s.by_mode.get(m, Bucket())
        line = f"  {m:<12}  {_fmt_bucket(b)}"
        if prior:
            pb = prior.by_mode.get(m, Bucket())
            line += f"   prior {_fmt_bucket(pb)}  Δ {_delta(b.passed, pb.passed)}"
        out.append(line)
    out.append('')

    # By tag — top failure categories.
    out.append("BY TAG (top failure clusters this run)")
    tags_sorted = sorted(
        s.by_tag.items(),
        key=lambda kv: (-kv[1].failed - kv[1].errored, -kv[1].total),
    )
    tag_width = max((len(t) for t, _ in tags_sorted[:15]), default=8)
    for tag, b in tags_sorted[:15]:
        line = f"  {tag:<{tag_width}}  {_fmt_bucket(b)}"
        if prior and tag in prior.by_tag:
            pb = prior.by_tag[tag]
            line += f"   prior {_fmt_bucket(pb)}  Δ {_delta(b.passed, pb.passed)}"
        out.append(line)
    out.append('')

    # Diff: newly passing / failing.
    if prior:
        new_pass: list[str] = []
        new_fail: list[str] = []
        shared = set(s.scenarios) & set(prior.scenarios)
        only_current = set(s.scenarios) - set(prior.scenarios)
        only_prior = set(prior.scenarios) - set(s.scenarios)

        for sid in sorted(shared):
            curr_status = s.scenarios[sid]['status']
            prior_status = prior.scenarios[sid]['status']
            if curr_status == 'pass' and prior_status != 'pass':
                new_pass.append(sid)
            elif curr_status != 'pass' and prior_status == 'pass':
                new_fail.append(sid)

        out.append(f"NEWLY PASSING ({len(new_pass)})")
        for sid in new_pass:
            data = s.scenarios[sid]
            out.append(f"  {sid}  ({data['persona']})")
        if not new_pass:
            out.append("  (none)")
        out.append('')

        out.append(f"NEWLY FAILING ({len(new_fail)})  {'⚠' if new_fail else ''}")
        for sid in new_fail:
            data = s.scenarios[sid]
            reasons = ', '.join(data['fail_reasons']) or data.get('error_msg', '')
            out.append(f"  {sid}  ({data['persona']})  -> {reasons[:100]}")
        if not new_fail:
            out.append("  (none)")
        out.append('')

        if only_current:
            out.append(f"NEW SCENARIOS THIS RUN ({len(only_current)})")
            for sid in sorted(only_current):
                d = s.scenarios[sid]
                out.append(f"  {sid}  [{d['status']}]")
            out.append('')
        if only_prior:
            out.append(f"DROPPED SINCE PRIOR ({len(only_prior)})")
            for sid in sorted(only_prior):
                out.append(f"  {sid}")
            out.append('')

    # Failing scenarios (only when no diff — diff already covers regressions).
    if not prior:
        failing = [
            (sid, d) for sid, d in s.scenarios.items()
            if d['status'] != 'pass'
        ]
        if failing:
            out.append(f"FAILING THIS RUN ({len(failing)})")
            for sid, d in sorted(failing):
                reasons = ', '.join(d['fail_reasons']) or d.get('error_msg', '')
                marker = 'ERR ' if d['status'] == 'error' else 'FAIL'
                out.append(f"  [{marker}] {sid}  ({d['persona']})  -> {reasons[:100]}")
            out.append('')

    # Prompt-rule coverage — process check, NOT a scorer. Surfaces
    # rules that lack any eval check + typos in the registry. Wired
    # 2026-05-27 per memory/simple_tutor_systematic_eval_plan.md
    # Phase 4. A clean coverage report has 0 unknown verbs and 0
    # unknown dimensions; uncovered rules are listed for transparency
    # but acknowledged via their notes field.
    try:
        from evals.rule_registry import (
            build_coverage_report, format_coverage_report,
        )
        out.append(format_coverage_report(build_coverage_report()))
        out.append('')
    except Exception as exc:  # pragma: no cover - defensive
        out.append(f"  (rule-coverage section unavailable: {exc})")
        out.append('')

    # Pedagogical dimensions.
    if s.by_dimension:
        out.append("PEDAGOGICAL DIMENSIONS (per-dimension pass rate)")
        dim_width = max(len(n) for n in s.by_dimension)
        for name in sorted(s.by_dimension):
            b = s.by_dimension[name]
            line = f"  {name:<{dim_width}}  {_fmt_bucket(b)}"
            if prior and name in prior.by_dimension:
                pb = prior.by_dimension[name]
                line += f"   prior {_fmt_bucket(pb)}  Δ {_delta(b.passed, pb.passed)}"
            out.append(line)
        out.append(
            f"  judge tokens: in={s.dimensions_tokens_in:,} "
            f"out={s.dimensions_tokens_out:,}"
            f"  (errors: {s.dimensions_errors}/{s.total})"
        )
        out.append('')

    # Rubric usage.
    out.append("RUBRIC LAYER")
    out.append(
        f"  judge tokens: in={s.rubric_tokens_in:,} out={s.rubric_tokens_out:,}"
        f"  (errors: {s.rubric_errors}/{s.total})"
    )
    if prior:
        out.append(
            f"  prior:        in={prior.rubric_tokens_in:,} "
            f"out={prior.rubric_tokens_out:,}"
            f"  (errors: {prior.rubric_errors}/{prior.total})"
        )
    out.append('')
    out.append('=' * 72)
    return '\n'.join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _latest_runs(n: int = 2) -> list[Path]:
    """Return the n most recent run JSONs (most recent first)."""
    paths = sorted(RUNS_ROOT.glob('*.json'), reverse=True)
    return paths[:n]


def _resolve_run_path(arg: str | None, fallback_index: int = 0) -> Path:
    if arg:
        p = Path(arg)
        if not p.exists():
            raise SystemExit(f"ERROR: run not found: {p}")
        return p
    runs = _latest_runs(fallback_index + 1)
    if len(runs) <= fallback_index:
        raise SystemExit(
            f"ERROR: not enough runs in {RUNS_ROOT} for fallback index {fallback_index}"
        )
    return runs[fallback_index]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        'run_path', nargs='?', default=None,
        help='Path to the run JSON. Defaults to the most recent under evals/runs/.',
    )
    parser.add_argument(
        '--diff', nargs='?', const='__latest_prior__', default=None,
        help=(
            'Compare against a prior run. With no value, auto-resolves to the '
            'second-most-recent run.'
        ),
    )
    args = parser.parse_args(argv)

    run_path = _resolve_run_path(args.run_path, fallback_index=0)
    with run_path.open() as f:
        run = json.load(f)
    summary = summarize(run)

    prior_summary: Summary | None = None
    if args.diff is not None:
        if args.diff == '__latest_prior__':
            # If user gave no run_path, prior is index 1; otherwise also 1.
            prior_path = _resolve_run_path(None, fallback_index=1)
            if prior_path == run_path:
                # Edge case: user passed the latest as run_path; still want index 1.
                prior_path = _resolve_run_path(None, fallback_index=2)
        else:
            prior_path = _resolve_run_path(args.diff)
        with prior_path.open() as f:
            prior = json.load(f)
        prior_summary = summarize(prior)

    print(format_summary(summary, prior_summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
