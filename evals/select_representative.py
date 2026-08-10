"""Draw the 100-scenario `v2` subset from the 200 multi-turn scenarios.

Forces every `v1` scenario in — that keeps the existing mt30 boards readable as
a sub-board of mt100 rather than orphaning them — then fills to 100 with a
greedy pass that repeatedly takes whichever candidate most reduces total
deviation from the population's proportions across persona, subject, kind and
lesson.

A plain proportional per-cell halving was tried first and rejected: six
persona x subject x kind cells hold more `v1` scenarios than their target, so it
overshoots to 106 and lands `baseline` at 14 against an ideal of 9.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import random

import yaml

DATASET = pathlib.Path(__file__).resolve().parent / 'dataset' / 'multi_turn'
AXES = ('persona', 'subject', 'kind', 'lesson')
SEED = 20260810
SUBSET_TAG = 'v2'


def load_scenarios() -> list[dict]:
    rows = []
    for path in sorted(DATASET.glob('*.yaml')):
        doc = yaml.safe_load(path.read_text())
        tags = set(doc.get('tags') or [])
        kind = ('edge_case' if 'edge_case' in tags
                else 'baseline' if 'baseline' in tags else 'other')
        rows.append({
            'id': doc['id'], 'path': path, 'persona': doc.get('persona'),
            'subject': doc.get('subject'), 'lesson': doc.get('lesson_id'),
            'kind': kind, 'v1': 'v1' in tags,
        })
    return rows


def _deviation(selected: list[dict], target: dict) -> float:
    counts = {ax: collections.Counter(r[ax] for r in selected) for ax in AXES}
    return sum(abs(counts[ax][k] - target[ax][k])
               for ax in AXES for k in target[ax])


def select(rows: list[dict], *, size: int = 100, seed: int = SEED) -> list[dict]:
    target = {
        ax: {k: n * size / len(rows)
             for k, n in collections.Counter(r[ax] for r in rows).items()}
        for ax in AXES
    }
    rng = random.Random(seed)
    chosen = [r for r in rows if r['v1']]
    pool = [r for r in rows if not r['v1']]
    while len(chosen) < size:
        # Sampling the pool bounds the inner loop; 60 is wide enough that the
        # greedy choice is stable across seeds but keeps this a second, not a
        # minute, on 200 scenarios.
        candidates = pool if len(pool) <= 60 else rng.sample(pool, 60)
        best = min(candidates, key=lambda r: _deviation(chosen + [r], target))
        chosen.append(best)
        pool.remove(best)
    return chosen


def _add_tag(path: pathlib.Path) -> bool:
    """Append SUBSET_TAG to the scenario's inline `tags:` list. Idempotent."""
    text = path.read_text()
    for line in text.splitlines():
        if line.startswith('tags:'):
            if SUBSET_TAG in [t.strip() for t in
                              line.split('[', 1)[1].rstrip(']').split(',')]:
                return False
            new = line.rstrip().rstrip(']') + f', {SUBSET_TAG}]'
            path.write_text(text.replace(line, new, 1))
            return True
    raise ValueError(f'{path.name}: no inline `tags:` line to extend')


def main(write: bool) -> None:
    rows = load_scenarios()
    chosen = select(rows)
    for axis in ('persona', 'subject', 'kind'):
        pop = collections.Counter(r[axis] for r in rows)
        got = collections.Counter(r[axis] for r in chosen)
        print(f'\n{axis}:   selected / ideal')
        for level in sorted(pop, key=str):
            print(f'   {str(level):16s} {got[level]:3d} / '
                  f'{pop[level] * len(chosen) / len(rows):5.1f}')
    print(f'\nlessons {len({r["lesson"] for r in chosen})}'
          f' of {len({r["lesson"] for r in rows})}'
          f' | v1 retained {sum(1 for r in chosen if r["v1"])}/30')
    if write:
        changed = sum(_add_tag(r['path']) for r in chosen)
        print(f'\ntagged {changed} file(s) with `{SUBSET_TAG}` '
              f'({len(chosen) - changed} already tagged)')
    else:
        print('\ndry run — pass --write to tag the YAML files')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--write', action='store_true',
                    help=f'add the `{SUBSET_TAG}` tag to the selected files')
    main(ap.parse_args().write)
