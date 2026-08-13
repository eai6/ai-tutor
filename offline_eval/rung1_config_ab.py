"""Rung 1 — the config A/B that decides whether qwen3.5:4b's 42% was real.

Background. Two committed multi-turn draws put qwen3:4b far above qwen3.5:4b
(mt50 44/50 vs 21/50; oss13_mt 20/20 vs 5/20) while the single-turn board
INVERTS them (178/200 vs 115/200). Per-turn skill with no protocol endurance is
the signature of a model being run wrong.

The suspect: apps/llm/model_profiles.py had no exact key for the qwen3.5 tags,
so get_model_profile() fell through to the generic r"qwen3" FAMILY_PATTERNS
entry — a CLOUD profile with max_tokens=16000 and no num_ctx. client.py then
derives num_ctx = max(8192, 16000+8192) = 24192, the window the Jetson profile
comment says OOMs an 8 GB Orin, and sends no `think` flag at all.

This script varies exactly that, on two axes, and nothing else:

    config: fallthrough (max_tokens=16000, no num_ctx, no think)
            vs profiled  (the exact-key ModelProfile: 3072 / 16384 / think)
    schema: the 2-property toy pose_question
            vs the production TOOL_SCHEMAS (6 properties, ~4 KB of prose)

Both go through OllamaClient.generate_with_tools, so the derived num_ctx and
the think flag are computed by the real code path rather than simulated.

Usage:
    AI_TUTOR_ROOT=$PWD DJANGO_SETTINGS_MODULE=ai_tutor.config.settings \
      .venv/bin/python offline_eval/rung1_config_ab.py --trials 30

    # narrow while iterating
    .venv/bin/python offline_eval/rung1_config_ab.py --trials 3 \
      --tags qwen3.5:4b --configs profiled --schemas real

Writes one JSON per (tag, config, schema) cell to offline_eval/jetson_rung1/
plus a summary table on stdout. Resume-safe: an existing cell file is skipped.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import asdict

ROOT = os.environ.get('AI_TUTOR_ROOT') or os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..'))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
import django  # noqa: E402
django.setup()

from ai_tutor.apps.llm.client import get_llm_client  # noqa: E402
from ai_tutor.apps.llm.model_profiles import get_model_profile  # noqa: E402
from ai_tutor.apps.llm.models import ModelConfig  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from bench_tutor_quality import (  # noqa: E402
    _run_a_ollama, scenario_a_tools,
)

OUT_DIR = os.path.join(ROOT, 'offline_eval', 'jetson_rung1')

# Tags that cleared rung 0 (tool-emission probe). qwen3.5:9b is deliberately
# absent: 6.6 GB of Q4 weights against ~5.6 GB free is the crash, not a config.
DEFAULT_TAGS = ('qwen3.5:4b', 'qwen3-4b-jetson', 'qwen3:4b-instruct', 'qwen3:4b')

# The pre-H1 fallthrough, reproduced exactly: what the generic r"qwen3" family
# pattern yielded. Kept as a literal rather than read from FAMILY_PATTERNS so
# the control stays fixed even if that table is later edited.
FALLTHROUGH_SAMPLING = {'temperature': 0.7, 'top_p': 0.8, 'top_k': 20}
FALLTHROUGH_MAX_TOKENS = 16000


def cell_config(tag: str, config: str) -> tuple[dict, int, str]:
    """Return (sampling, max_tokens, description) for a config arm."""
    if config == 'fallthrough':
        return (dict(FALLTHROUGH_SAMPLING), FALLTHROUGH_MAX_TOKENS,
                'generic qwen3 family pattern (num_ctx derives to 24192, no think)')
    profile = get_model_profile(f'local_ollama/{tag}')
    if profile is None:
        raise SystemExit(
            f"no ModelProfile for local_ollama/{tag} — add an exact key to "
            f"MODEL_PROFILES before running the profiled arm")
    return (profile.sampling_dict(), profile.max_tokens,
            f'exact profile (num_ctx={profile.num_ctx}, '
            f'think={profile.ollama_think})')


def run_cell(tag: str, config: str, schema: str, trials: int,
             timeout_note: list) -> dict:
    sampling, max_tokens, desc = cell_config(tag, config)
    tools = scenario_a_tools(real_schemas=(schema == 'real'))
    cfg = ModelConfig.resolve_runtime('local_ollama', tag)
    if cfg is None:
        raise SystemExit(f"could not resolve a ModelConfig for local_ollama/{tag}")
    client = get_llm_client(cfg)

    print(f"\n  [{tag}] config={config} schema={schema}  ({desc})")
    print(f"     tools={len(tools)} max_tokens={max_tokens} sampling={sampling}")
    sys.stdout.flush()

    rows, errors = [], []
    for i in range(trials):
        try:
            r = _run_a_ollama(client, tools=tools, sampling=sampling,
                              max_tokens=max_tokens)
            rows.append(asdict(r))
            mark = ('.' if r.tool_called and r.args_compliant
                    else ('c' if r.tool_called else 'x'))
        except Exception as exc:                      # noqa: BLE001
            msg = f'{type(exc).__name__}: {exc}'
            errors.append(msg)
            timeout_note.append(f'{tag}/{config}/{schema}: {msg}')
            mark = 'E'
        sys.stdout.write(mark)
        sys.stdout.flush()
    print()

    n = len(rows)
    called = [r for r in rows if r['tool_called']]
    compliant = [r for r in called if r['args_compliant']]
    shapes: dict[str, int] = {}
    for r in called:
        shapes[r['arg_shape']] = shapes.get(r['arg_shape'], 0) + 1
    tools_picked: dict[str, int] = {}
    for r in called:
        key = r['tool_name'] or '?'
        tools_picked[key] = tools_picked.get(key, 0) + 1

    return {
        'tag': tag, 'config': config, 'schema': schema,
        'config_description': desc,
        'sampling': sampling, 'max_tokens': max_tokens, 'n_tools': len(tools),
        'trials_requested': trials, 'trials_completed': n,
        'errors': errors,
        'tool_call_rate': (len(called) / n) if n else 0.0,
        'args_compliant_rate': (len(compliant) / n) if n else 0.0,
        'arg_shapes': shapes,
        'tools_picked': tools_picked,
        'noncompliance_reasons': sorted({
            r['args_reason'] for r in called if not r['args_compliant']}),
        'latency_ms_median': (
            statistics.median([r['latency_ms'] for r in rows]) if rows else None),
        'tokens_out_median': (
            statistics.median([r['tokens_out'] for r in rows]) if rows else None),
        'trials': rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--trials', type=int, default=30)
    ap.add_argument('--tags', nargs='+', default=list(DEFAULT_TAGS))
    ap.add_argument('--configs', nargs='+', default=['fallthrough', 'profiled'],
                    choices=['fallthrough', 'profiled'])
    ap.add_argument('--schemas', nargs='+', default=['toy', 'real'],
                    choices=['toy', 'real'])
    ap.add_argument('--force', action='store_true',
                    help='re-run cells whose JSON already exists')
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    started = time.strftime('%Y-%m-%dT%H:%M:%S')
    print(f"rung1 config A/B — {started}")
    print(f"tags={args.tags} configs={args.configs} schemas={args.schemas} "
          f"trials={args.trials}")

    summary, notes = [], []
    for tag in args.tags:
        for config in args.configs:
            for schema in args.schemas:
                safe = tag.replace(':', '_').replace('/', '_')
                path = os.path.join(OUT_DIR, f'{safe}__{config}__{schema}.json')
                if os.path.exists(path) and not args.force:
                    print(f"  skip (exists) {os.path.basename(path)}")
                    summary.append(json.load(open(path)))
                    continue
                cell = run_cell(tag, config, schema, args.trials, notes)
                with open(path, 'w') as fh:
                    json.dump(cell, fh, indent=2)
                summary.append(cell)

    print(f"\n{'='*96}")
    print(f"{'tag':20s} {'config':12s} {'schema':7s} {'tool%':>7s} "
          f"{'args_ok%':>9s} {'err':>4s} {'med_ms':>9s}  shapes")
    print('-' * 96)
    for c in summary:
        print(f"{c['tag']:20s} {c['config']:12s} {c['schema']:7s} "
              f"{c['tool_call_rate']*100:6.1f}% {c['args_compliant_rate']*100:8.1f}% "
              f"{len(c['errors']):4d} "
              f"{(c['latency_ms_median'] or 0):8.0f}ms  "
              f"{c['arg_shapes'] or '-'}")
    print('=' * 96)
    if notes:
        print('\nerrors observed:')
        for n in notes[:20]:
            print(f'  - {n}')
    print(f"\nwrote {len(summary)} cells to {OUT_DIR}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
