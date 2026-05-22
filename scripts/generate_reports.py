"""Aggregate A/B test cell results + judge scores into final reports.

Reads:
  ab-test-reports/cell_results.jsonl        — programmatic metrics per cell
  ab-test-reports/judge_scores/_all_scores.jsonl — LLM-as-judge scores per cell

Writes:
  ab-test-reports/per_cell/<cell_key>.md    — full transcript + scores + metrics
  ab-test-reports/summary.md                — pivot tables + winner
  ab-test-reports/cost_latency.md           — token/wall-time breakdown
  ab-test-reports/FINAL_REPORT.md           — high-level overall narrative

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
        else:
            lines += ["## LLM-as-judge scores", "", "_(not yet scored)_", ""]

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
        lines.append('### Ranking by overall mean')
        lines.append('')
        for i, (m, v) in enumerate(ranking, 1):
            lines.append(f"{i}. **{m}** — overall {v:.2f}/5")
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


def write_final(cells: list[dict], scores_by_key: dict[str, dict]) -> None:
    per_model = mean_per_principle(scores_by_key)
    ranking = sorted(
        ((m, mean(d.values())) for m, d in per_model.items() if d),
        key=lambda r: -r[1],
    )
    winner = ranking[0][0] if ranking else 'no scores available'

    total_cells = len(cells)
    errored = sum(1 for c in cells if c.get('error'))
    completed = total_cells - errored
    total_wall = sum(c['wall_seconds'] for c in cells if not c.get('error'))
    total_student_tokens_in = sum(c['student_tokens_in'] for c in cells if not c.get('error'))
    total_student_tokens_out = sum(c['student_tokens_out'] for c in cells if not c.get('error'))

    lines = [
        '# A/B Test — Final Report',
        '',
        'Companion to `design/AB_TESTING_PLAN.md`. This report compares 3 tutoring models '
        'across 2 lessons × 2 personas (12 cells). Programmatic metrics + Claude Opus '
        'LLM-as-judge scoring against the 10 science-of-learning principles.',
        '',
        '## Setup',
        '',
        '- **Models tested**: Claude Sonnet 4 (Anthropic), Gemini 3 Flash Preview (Google), GPT-4o mini (OpenAI)',
        '- **Lessons**: L1137 (Math — Angles around a point) · L1425 (Geography — Map Scale and Map Types)',
        '- **Personas**: `struggler`, `capable` (synthetic LLM students, Sonnet 4 driving)',
        '- **Content source**: prod_content_dump.sql loaded into local Postgres (Docker)',
        '- **Matrix**: 3 × 2 × 2 = 12 cells',
        '- **Tutor model swap**: in-memory monkey-patch on `ModelConfig.get_for`, no DB writes',
        '- **Judge**: Claude Opus (temperature=0), 10-principle rubric',
        '',
        '## Headline result',
        '',
        f'**Winner by judge mean: `{winner}`**',
        '',
        '| Rank | Model | Overall mean (0-5) |',
        '|---:|---|---:|',
    ]
    for i, (m, v) in enumerate(ranking, 1):
        lines.append(f"| {i} | {m} | **{v:.2f}** |")
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
        '- `per_cell/<key>.md` — per-cell transcript + programmatic metrics + judge scores',
        '- `raw_transcripts/<key>.md` — raw transcript only',
        '- `judge_scores/<key>.json` — per-cell judge JSON output',
        '- `judge_rubric.md` — exact judge prompt + rubric for reproducibility',
        '- `cell_results.jsonl` — raw programmatic metrics (one cell per line)',
        '',
        '## Caveats',
        '',
        '- **Synthetic-student personas, not real students** — broad strokes (struggler/capable). '
        'Real student long-tail misconceptions not represented.',
        '- **Single run per cell** — no variance estimate. A 3-run-per-cell sweep would tighten signal.',
        '- **Cross-model only, not cross-prompt** — the AB_TESTING_PLAN R2 question (v3 vs current '
        'prompt on same model) is a separate follow-up.',
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
