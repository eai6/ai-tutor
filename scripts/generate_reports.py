"""Aggregate A/B test cell results + judge output into the final report.

Reads:
  ab-test-reports/cell_results.jsonl              — programmatic metrics per cell
  ab-test-reports/judge_scores/_all_scores.jsonl  — judge scores + recommendations per cell

Writes:
  ab-test-reports/per_cell/<cell_key>.md    — full transcript + scores + recommendations + metrics
  ab-test-reports/summary.md                — pivot tables (rubric scores, programmatic counts)
  ab-test-reports/cost_latency.md           — token/wall-time breakdown
  ab-test-reports/FINAL_REPORT.md           — consolidated, ranked recommendations for
                                              improving the tutoring system prompt

The report headline is the ranked recommendation list, NOT a model ranking. See
`design/AB_TESTING_PLAN.md` — models are a robustness axis, not the unit of evaluation.

Run with:  venv/bin/python scripts/generate_reports.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

OUT = Path('ab-test-reports')
CELL_RESULTS = OUT / 'cell_results.jsonl'
JUDGE_ALL = OUT / 'judge_scores' / '_all_scores.jsonl'
PER_CELL_DIR = OUT / 'per_cell'
TRANSCRIPTS = OUT / 'raw_transcripts'

PRINCIPLES = [
    'active_learning', 'direct_instruction_active_practice', 'deliberate_practice',
    'mastery_learning', 'cognitive_load', 'layering', 'non_interference',
    'interleaving', 'testing_effect', 'targeted_remediation',
]

REC_BUCKETS = [
    ('prompt_recommendations', 'System-prompt edits'),
    ('flow_recommendations', 'Engine / flow changes'),
    ('experience_recommendations', 'Student-experience changes'),
]

SEVERITY_WEIGHT = {'high': 3, 'medium': 2, 'low': 1}


def _norm_title(s: str) -> str:
    """Normalise a recommendation title for clustering — lowercase, alnum + spaces, collapsed."""
    out = []
    for ch in (s or '').lower():
        out.append(ch if ch.isalnum() or ch.isspace() else ' ')
    return ' '.join(''.join(out).split())


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def _cell_key(model_key: str, lesson_id: int | str, persona: str) -> str:
    return f"{model_key}_L{lesson_id}_{persona}"


def _cell_key_from_transcript_name(name: str) -> str:
    # e.g. 'sonnet-4_L1137_struggler' → same; strip extension if present
    return Path(name).stem


def write_per_cell(cells: list[dict], scores_by_key: dict[str, dict]) -> None:
    PER_CELL_DIR.mkdir(parents=True, exist_ok=True)
    for c in cells:
        if c.get('error'):
            key = _cell_key(c['model_key'], c['lesson_id'], c['persona'])
            (PER_CELL_DIR / f"{key}.md").write_text(
                f"# {key}\n\n**ERROR**: {c['error']}\n\nWall: {c['wall_seconds']:.1f}s\n"
            )
            continue
        key = _cell_key(c['model_key'], c['lesson_id'], c['persona'])
        score = scores_by_key.get(key, {})
        transcript_text = ''
        tp = Path(c.get('transcript_path') or '')
        if tp.exists():
            transcript_text = tp.read_text()

        lines = [
            f"# Cell: {key}",
            "",
            f"- Model: **{c['model_label']}** ({c['provider']}/{c['model_name']})",
            f"- Lesson: L{c['lesson_id']} — {c['lesson_label']}",
            f"- Persona: **{c['persona']}**",
            f"- Session ID (Postgres): {c['session_id']}",
            f"- Reason: `{c['reason']}` — {c['turns']} turn(s)",
            "",
            "## Programmatic metrics",
            "",
            f"| Metric | Value |",
            f"|---|---|",
            f"| tutor turns | {c['tutor_turns']} |",
            f"| tool-use rate | {c['tool_use_rate']:.0%} |",
            f"| regen triggered | {c['regen_triggered_turns']} |",
            f"| regen clean cycle-1 | {c['regen_clean_first_cycle']} |",
            f"| regen shipped dirty | {c['regen_cycles_exhausted']} |",
            f"| **answer-leak incidents** | **{c['answer_leak_incidents']}** |",
            f"| repeated-question incidents | {c['repeated_question_incidents']} |",
            f"| no-question incidents | {c['no_question_incidents']} |",
            f"| wall seconds | {c['wall_seconds']:.1f} |",
            f"| student tokens (in/out) | {c['student_tokens_in']} / {c['student_tokens_out']} |",
            "",
        ]
        if c.get('validator_issue_counts'):
            lines.append("Validator issue breakdown:")
            lines.append("")
            for k, v in c['validator_issue_counts'].items():
                lines.append(f"- `{k}`: {v}")
            lines.append("")

        if score:
            lines += ["## LLM-as-judge scores (Claude Opus, 0-5)", ""]
            if 'error' in score:
                lines.append(f"**Judge ERROR**: {score['error']}")
            else:
                lines.append("| Principle | Score | Evidence |")
                lines.append("|---|---:|---|")
                for p in PRINCIPLES:
                    item = score.get('scores', {}).get(p, {})
                    sc = item.get('score', '?')
                    ev = (item.get('evidence', '') or '').replace('\n', ' ')[:200]
                    lines.append(f"| {p} | {sc} | {ev} |")
                lines.append('')
                if score.get('overall_summary'):
                    lines.append('**Judge overall summary**')
                    lines.append('')
                    lines.append(score['overall_summary'])
                    lines.append('')
                if score.get('strongest_behaviors'):
                    lines.append('**Strongest behaviors**')
                    lines.append('')
                    for b in score['strongest_behaviors']:
                        lines.append(f'- {b}')
                    lines.append('')
                if score.get('weakest_behaviors'):
                    lines.append('**Weakest behaviors**')
                    lines.append('')
                    for b in score['weakest_behaviors']:
                        lines.append(f'- {b}')
                    lines.append('')

                for bucket_key, bucket_label in REC_BUCKETS:
                    recs = score.get(bucket_key) or []
                    if not recs:
                        continue
                    lines.append(f'### {bucket_label} ({bucket_key})')
                    lines.append('')
                    for r in recs:
                        title = r.get('title', '(untitled)')
                        sev = r.get('severity', '?')
                        lines.append(f"- **[{sev}] {title}**")
                        if r.get('rationale'):
                            lines.append(f"  - Rationale: {r['rationale']}")
                        if r.get('evidence_quote'):
                            q = r['evidence_quote'].replace('\n', ' ')
                            lines.append(f"  - Evidence ({r.get('evidence_turn', '?')}): \"{q}\"")
                        if r.get('suggested_prompt_edit'):
                            lines.append(f"  - Suggested edit: {r['suggested_prompt_edit']}")
                        if r.get('expected_effect'):
                            lines.append(f"  - Expected effect: {r['expected_effect']}")
                    lines.append('')
        else:
            lines += ["## LLM-as-judge scores + recommendations", "", "_(not yet scored)_", ""]

        if transcript_text:
            lines += ["## Transcript", "", "```", transcript_text, "```", ""]

        (PER_CELL_DIR / f"{key}.md").write_text('\n'.join(lines))


def mean_per_principle(scores_by_key: dict[str, dict]) -> dict[str, dict[str, float]]:
    """Return {model_key: {principle: mean_score}}"""
    by_model: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for key, sc in scores_by_key.items():
        if 'error' in sc or not sc.get('scores'):
            continue
        model = key.split('_L')[0]
        for p in PRINCIPLES:
            v = sc['scores'].get(p, {}).get('score')
            if isinstance(v, (int, float)):
                by_model[model][p].append(v)
    return {
        m: {p: (mean(vs) if vs else float('nan')) for p, vs in d.items()}
        for m, d in by_model.items()
    }


def write_summary(cells: list[dict], scores_by_key: dict[str, dict]) -> None:
    # Per-cell row
    lines = [
        '# A/B Test Summary',
        '',
        '_Generated by `scripts/generate_reports.py`. Source data: '
        '`cell_results.jsonl` + `judge_scores/_all_scores.jsonl`._',
        '',
        '## Per-cell programmatic + judge mean',
        '',
        '| Model | Lesson | Persona | Turns | Reason | Tool-use | Leak | RepeatQ | NoQ | Wall (s) | Judge mean |',
        '|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|',
    ]
    for c in cells:
        if c.get('error'):
            lines.append(
                f"| {c['model_label']} | L{c['lesson_id']} | {c['persona']} | "
                f"{c['turns']} | **ERROR** | — | — | — | — | {c['wall_seconds']:.1f} | — |"
            )
            continue
        key = _cell_key(c['model_key'], c['lesson_id'], c['persona'])
        sc = scores_by_key.get(key, {})
        if sc and 'error' not in sc and sc.get('scores'):
            jm = mean(v['score'] for v in sc['scores'].values() if isinstance(v.get('score'), (int, float)))
            jm_s = f"{jm:.2f}"
        else:
            jm_s = '—'
        lines.append(
            f"| {c['model_label']} | L{c['lesson_id']} | {c['persona']} | "
            f"{c['turns']} | `{c['reason']}` | {c['tool_use_rate']:.0%} | "
            f"{c['answer_leak_incidents']} | {c['repeated_question_incidents']} | "
            f"{c['no_question_incidents']} | {c['wall_seconds']:.1f} | {jm_s} |"
        )
    lines.append('')

    # Pivot: model × principle
    per_model = mean_per_principle(scores_by_key)
    if per_model:
        lines += [
            '## Model × Principle mean score (0-5)',
            '',
            '| Model | ' + ' | '.join(PRINCIPLES) + ' | **Overall** |',
            '|---|' + '|'.join(['---:'] * (len(PRINCIPLES) + 1)) + '|',
        ]
        ranking: list[tuple[str, float]] = []
        for model in sorted(per_model):
            row = per_model[model]
            cells_str = []
            valid = []
            for p in PRINCIPLES:
                v = row.get(p, float('nan'))
                cells_str.append('—' if v != v else f'{v:.2f}')
                if v == v:
                    valid.append(v)
            overall = mean(valid) if valid else float('nan')
            ranking.append((model, overall))
            lines.append(f"| {model} | " + ' | '.join(cells_str) + f" | **{overall:.2f}** |")
        lines.append('')
        ranking.sort(key=lambda r: -r[1])
        lines.append('### Per-model overall mean (robustness check, not a ranking)')
        lines.append('')
        lines.append('_Use these numbers to detect whether a prompt change is model-robust. '
                     'They are **not** a model evaluation — see `design/AB_TESTING_PLAN.md`._')
        lines.append('')
        for m, v in ranking:
            lines.append(f"- **{m}** — overall {v:.2f}/5")
        lines.append('')

    # Pivot: model × persona programmatic
    lines += [
        '## Model × Persona programmatic',
        '',
        '| Model | Persona | mean turns | mean tool-use | leak total | wall mean (s) |',
        '|---|---|---:|---:|---:|---:|',
    ]
    by_mp: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in cells:
        if c.get('error'):
            continue
        by_mp[(c['model_label'], c['persona'])].append(c)
    for (model, persona), rows in sorted(by_mp.items()):
        lines.append(
            f"| {model} | {persona} | "
            f"{mean(r['turns'] for r in rows):.1f} | "
            f"{mean(r['tool_use_rate'] for r in rows):.0%} | "
            f"{sum(r['answer_leak_incidents'] for r in rows)} | "
            f"{mean(r['wall_seconds'] for r in rows):.1f} |"
        )
    lines.append('')
    (OUT / 'summary.md').write_text('\n'.join(lines))


def write_cost_latency(cells: list[dict], scores_by_key: dict[str, dict]) -> None:
    lines = [
        '# Cost & latency breakdown',
        '',
        '## Per-cell',
        '',
        '| Model | Lesson | Persona | Turns | Wall (s) | Sec/turn | Student tokens (in/out) |',
        '|---|---|---|---:|---:|---:|---:|',
    ]
    by_model: dict[str, list[dict]] = defaultdict(list)
    for c in cells:
        if c.get('error'):
            continue
        by_model[c['model_label']].append(c)
        per_turn = c['wall_seconds'] / max(c['turns'], 1)
        lines.append(
            f"| {c['model_label']} | L{c['lesson_id']} | {c['persona']} | "
            f"{c['turns']} | {c['wall_seconds']:.1f} | {per_turn:.1f} | "
            f"{c['student_tokens_in']}/{c['student_tokens_out']} |"
        )
    lines.append('')

    lines += [
        '## Aggregate per model',
        '',
        '| Model | Cells | Mean turns | Mean wall (s) | Mean s/turn |',
        '|---|---:|---:|---:|---:|',
    ]
    for m, rows in sorted(by_model.items()):
        lines.append(
            f"| {m} | {len(rows)} | "
            f"{mean(r['turns'] for r in rows):.1f} | "
            f"{mean(r['wall_seconds'] for r in rows):.1f} | "
            f"{mean(r['wall_seconds'] / max(r['turns'], 1) for r in rows):.2f} |"
        )
    lines.append('')

    # Judge cost
    judge_in = sum(s.get('tokens_in', 0) for s in scores_by_key.values())
    judge_out = sum(s.get('tokens_out', 0) for s in scores_by_key.values())
    lines += [
        '## Judge cost',
        '',
        f'- Judge model: Claude Opus',
        f'- Total cells scored: {sum(1 for s in scores_by_key.values() if "error" not in s)}',
        f'- Total input tokens: {judge_in:,}',
        f'- Total output tokens: {judge_out:,}',
        '',
    ]
    (OUT / 'cost_latency.md').write_text('\n'.join(lines))


def aggregate_recommendations(scores_by_key: dict[str, dict]) -> dict[str, list[dict]]:
    """Cluster recommendations across cells by normalised title.

    Returns {bucket_key: [cluster, ...]} where each cluster is:
      {
        'title': str (representative — first seen),
        'count': int (how many cells surfaced it),
        'cells': [cell_key, ...],
        'severity_score': int (sum of severity weights across cells),
        'top_severity': 'high'|'medium'|'low',
        'examples': [recommendation dict, ...] (first 3 raw items),
      }
    Sorted by (severity_score desc, count desc) within each bucket.
    """
    out: dict[str, list[dict]] = {}
    for bucket_key, _label in REC_BUCKETS:
        clusters: dict[str, dict] = {}
        for cell_key, sc in scores_by_key.items():
            if 'error' in sc:
                continue
            recs = sc.get(bucket_key) or []
            for r in recs:
                norm = _norm_title(r.get('title', ''))
                if not norm:
                    continue
                sev = (r.get('severity') or 'medium').lower()
                weight = SEVERITY_WEIGHT.get(sev, 2)
                c = clusters.setdefault(norm, {
                    'title': r.get('title', '(untitled)'),
                    'count': 0,
                    'cells': [],
                    'severity_score': 0,
                    'severities': [],
                    'examples': [],
                })
                c['count'] += 1
                c['cells'].append(cell_key)
                c['severity_score'] += weight
                c['severities'].append(sev)
                if len(c['examples']) < 3:
                    c['examples'].append(r)
        # Resolve top_severity
        for c in clusters.values():
            if 'high' in c['severities']:
                c['top_severity'] = 'high'
            elif 'medium' in c['severities']:
                c['top_severity'] = 'medium'
            else:
                c['top_severity'] = 'low'
            del c['severities']
        out[bucket_key] = sorted(
            clusters.values(),
            key=lambda x: (-x['severity_score'], -x['count']),
        )
    return out


def write_final(cells: list[dict], scores_by_key: dict[str, dict]) -> None:
    total_cells = len(cells)
    errored = sum(1 for c in cells if c.get('error'))
    completed = total_cells - errored
    total_wall = sum(c['wall_seconds'] for c in cells if not c.get('error'))
    total_student_tokens_in = sum(c['student_tokens_in'] for c in cells if not c.get('error'))
    total_student_tokens_out = sum(c['student_tokens_out'] for c in cells if not c.get('error'))

    models_seen = sorted({c.get('model_label', '?') for c in cells})
    personas_seen = sorted({c.get('persona', '?') for c in cells})
    lessons_seen = sorted({f"L{c.get('lesson_id')}" for c in cells})

    rec_clusters = aggregate_recommendations(scores_by_key)

    lines = [
        '# A/B Run — Recommendations to Improve the Tutoring System Prompt',
        '',
        'Companion to `design/AB_TESTING_PLAN.md`. **This is not a model bake-off.** '
        'The purpose of this run is to surface evidence-anchored recommendations for '
        'improving the tutoring system prompt (primary), engine flow (secondary), and '
        'student experience (secondary). Models are a robustness axis, not the unit '
        'of evaluation.',
        '',
        '## Setup',
        '',
        f'- **Models (robustness axis)**: {", ".join(models_seen)}',
        f'- **Lessons**: {", ".join(lessons_seen)}',
        f'- **Personas**: {", ".join(personas_seen)} (synthetic LLM students)',
        '- **Content source**: prod_content_dump.sql loaded into local Postgres',
        '- **Tutor model swap**: in-memory monkey-patch on `ModelConfig.get_for`, no DB writes',
        '- **Judge**: Claude Opus (temperature=0), 10-principle rubric + structured recommendations',
        '- **Scope**: OpenAI/GPT explicitly excluded — see `design/AB_TESTING_PLAN.md`',
        '',
        '## Headline — Top recommendations (ranked across all cells)',
        '',
        'Ranked by aggregated severity (high=3, medium=2, low=1) summed across the cells '
        'where the recommendation appeared, then by frequency. Use this list to drive the '
        'next revision of the tutoring system prompt.',
        '',
    ]
    for bucket_key, bucket_label in REC_BUCKETS:
        clusters = rec_clusters.get(bucket_key) or []
        lines.append(f'### {bucket_label}')
        lines.append('')
        if not clusters:
            lines.append('_No recommendations in this bucket._')
            lines.append('')
            continue
        for i, c in enumerate(clusters[:10], 1):
            lines.append(
                f"**{i}. [{c['top_severity']}] {c['title']}** "
                f"— surfaced in {c['count']} cell(s), severity score {c['severity_score']}"
            )
            if c['examples']:
                ex = c['examples'][0]
                if ex.get('rationale'):
                    lines.append(f"   - Rationale: {ex['rationale']}")
                if ex.get('suggested_prompt_edit'):
                    lines.append(f"   - Suggested edit: {ex['suggested_prompt_edit']}")
                if ex.get('expected_effect'):
                    lines.append(f"   - Expected effect: {ex['expected_effect']}")
                if ex.get('evidence_quote'):
                    q = ex['evidence_quote'].replace('\n', ' ')[:240]
                    lines.append(f"   - Example evidence ({ex.get('evidence_turn', '?')}): \"{q}\"")
            lines.append(f"   - Cells: {', '.join(c['cells'])}")
            lines.append('')
        if len(clusters) > 10:
            lines.append(f'_…{len(clusters) - 10} additional recommendation(s) in `summary.md` and per-cell files._')
            lines.append('')

    # Cross-model robustness check
    per_model = mean_per_principle(scores_by_key)
    if per_model:
        lines += [
            '## Cross-model robustness check (not a ranking)',
            '',
            'Mean rubric scores per model — used only to decide whether a prompt change '
            'should be considered model-robust or model-specific. **Do not read this as a '
            'model evaluation.** A large gap here means the prompt change holds differently '
            'across providers; a small gap means it generalises.',
            '',
            '| Model | Overall mean (0-5) | Cells scored |',
            '|---|---:|---:|',
        ]
        for model in sorted(per_model):
            vals = list(per_model[model].values())
            valid = [v for v in vals if v == v]
            if not valid:
                continue
            ncells = sum(
                1 for k, sc in scores_by_key.items()
                if k.split('_L')[0] == model and 'error' not in sc
            )
            lines.append(f"| {model} | {mean(valid):.2f} | {ncells} |")
        lines.append('')

    lines += [
        '## Run statistics',
        '',
        f'- Cells completed: {completed}/{total_cells}',
        f'- Cells errored: {errored}',
        f'- Total wall time: {total_wall:.0f}s ({total_wall/60:.1f} min)',
        f'- Synthetic-student tokens (in/out): {total_student_tokens_in:,} / {total_student_tokens_out:,}',
        '',
        '## Programmatic failure-mode counts (aggregated)',
        '',
        'Supplementary signal; the judge\'s recommendations remain the headline.',
        '',
        '| Model | Answer leaks | Repeated Q | No question | Regen shipped dirty |',
        '|---|---:|---:|---:|---:|',
    ]
    agg: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for c in cells:
        if c.get('error'):
            continue
        m = c['model_label']
        agg[m]['leak'] += c['answer_leak_incidents']
        agg[m]['repeat'] += c['repeated_question_incidents']
        agg[m]['noq'] += c['no_question_incidents']
        agg[m]['dirty'] += c['regen_cycles_exhausted']
    for m, d in sorted(agg.items()):
        lines.append(f"| {m} | {d['leak']} | {d['repeat']} | {d['noq']} | {d['dirty']} |")
    lines.append('')

    lines += [
        '## Files',
        '',
        '- `summary.md` — full pivot tables (model × principle, model × persona)',
        '- `cost_latency.md` — wall-time + token spend breakdown',
        '- `per_cell/<key>.md` — per-cell transcript + programmatic metrics + judge scores + recommendations',
        '- `raw_transcripts/<key>.md` — raw transcript only',
        '- `judge_scores/<key>.json` — per-cell judge JSON output (scores + recommendations)',
        '- `judge_rubric.md` — exact judge prompt + rubric for reproducibility',
        '- `cell_results.jsonl` — raw programmatic metrics (one cell per line)',
        '',
        '## Caveats',
        '',
        '- **Synthetic-student personas, not real students** — broad strokes (struggler/capable). '
        'Real student long-tail misconceptions not represented.',
        '- **Single run per cell** — no variance estimate. A 3-run-per-cell sweep would tighten signal.',
        '- **Cross-prompt is the canonical comparison** — cross-model variation here is a robustness '
        'check on prompt changes, not a model evaluation. See `design/AB_TESTING_PLAN.md`.',
        '- **20-turn cap per cell** — sessions that would naturally run longer are truncated.',
        '',
    ]
    (OUT / 'FINAL_REPORT.md').write_text('\n'.join(lines))


def main():
    cells = _load_jsonl(CELL_RESULTS)
    score_rows = _load_jsonl(JUDGE_ALL)
    scores_by_key = {}
    for row in score_rows:
        key = _cell_key_from_transcript_name(row.get('transcript', ''))
        if key:
            scores_by_key[key] = row

    write_per_cell(cells, scores_by_key)
    write_summary(cells, scores_by_key)
    write_cost_latency(cells, scores_by_key)
    write_final(cells, scores_by_key)
    print(f'Reports written to {OUT}/')
    print(f'  - FINAL_REPORT.md  · summary.md  · cost_latency.md')
    print(f'  - per_cell/  ({len(cells)} files)')


if __name__ == '__main__':
    main()
