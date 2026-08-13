"""Eval-dataset alignment audit.

Catches the SHAPES of misalignment that have hit us at runtime:

  - Single-turn scenarios whose seed_history ends with the tutor posing
    a question but lacks a ``seed_inflight_question:`` block (engine
    refuses to grade, scenario fails spuriously).
  - References to unknown ``must_label`` / ``must_not_label`` labels
    (they silently never fire).
  - Lesson IDs not present in the loaded fixtures.
  - Deterministic verbs that don't exist in the scorer.
  - Scenarios where ``mode: multi_turn`` but the assertions block uses
    single-turn verbs only (and vice versa).
  - ``seed_inflight_question`` blocks that are missing required fields
    or fail the runner's validation.

Run from repo root:
    python scripts/audit_eval_dataset.py
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_ROOT = REPO_ROOT / 'evals' / 'dataset'

sys.path.insert(0, str(REPO_ROOT))


def _known_labels() -> set[str]:
    """Labels defined in ai_tutor.apps.benchmark.labels. The runner normalises
    `must_label: ADVANCE` against `L.ADVANCE = 'advance'`, so both the
    UPPER constant name and the lowercase value count as 'known'.
    """
    from ai_tutor.apps.benchmark import labels as L
    out: set[str] = set()
    for name in dir(L):
        if name.startswith('_'):
            continue
        val = getattr(L, name)
        if isinstance(val, str):
            out.add(name)
            out.add(val)
    return out


def _known_det_verbs() -> set[str]:
    from evals.scorers import deterministic as det
    return set(det._HANDLERS.keys())


def _known_trajectory_verbs() -> set[str]:
    from evals.scorers import trajectory as traj
    return set(getattr(traj, '_HANDLERS', {}).keys())


def _known_lessons() -> set[int]:
    from ai_tutor.apps.curriculum.models import Lesson
    return set(Lesson.objects.values_list('pk', flat=True))


def main() -> int:
    import django; django.setup()

    known_labels = _known_labels()
    known_det_verbs = _known_det_verbs()
    known_traj_verbs = _known_trajectory_verbs()
    known_lessons = _known_lessons()

    findings: list[tuple[str, Path, str]] = []
    by_finding: Counter[str] = Counter()

    for p in sorted(DATASET_ROOT.rglob('*.yaml')):
        if 'smoke' in p.parts:
            continue
        rel = p.relative_to(REPO_ROOT)
        raw = yaml.safe_load(p.read_text(encoding='utf-8'))

        scen_id = raw.get('id', p.stem)
        if scen_id != p.stem:
            findings.append(('id_mismatch', rel, f"id={scen_id!r} ≠ filename {p.stem!r}"))
            by_finding['id_mismatch'] += 1

        mode = raw.get('mode', 'single_turn')
        lesson_id = raw.get('lesson_id')
        if isinstance(lesson_id, int) and known_lessons and lesson_id not in known_lessons:
            findings.append(
                ('unknown_lesson_id', rel, f"lesson_id={lesson_id} not in loaded fixtures")
            )
            by_finding['unknown_lesson_id'] += 1

        # Assertion verb / label cross-check.
        assertions = raw.get('assertions') or {}
        for verb in assertions.keys():
            if mode == 'multi_turn':
                expected = known_traj_verbs
            else:
                expected = known_det_verbs
            if verb not in expected:
                findings.append(
                    ('unknown_assertion_verb', rel,
                     f"verb {verb!r} not in {sorted(expected)!r} ({mode} mode)")
                )
                by_finding['unknown_assertion_verb'] += 1

        for label_verb in ('must_label', 'must_not_label'):
            vals = assertions.get(label_verb)
            if vals is None:
                continue
            if isinstance(vals, str):
                vals = [vals]
            for label in vals:
                label = str(label).strip()
                if label and label not in known_labels:
                    findings.append(
                        ('unknown_label', rel,
                         f"{label_verb} references {label!r} — not in ai_tutor.apps.benchmark.labels")
                    )
                    by_finding['unknown_label'] += 1

        # Seed_inflight_question shape check.
        seed_block = raw.get('seed_inflight_question')
        if seed_block is not None:
            if not isinstance(seed_block, dict):
                findings.append(('inflight_not_a_dict', rel, type(seed_block).__name__))
                by_finding['inflight_not_a_dict'] += 1
                continue
            missing = [
                f for f in ('question_text', 'question_type', 'reference_answer')
                if not str(seed_block.get(f, '')).strip()
            ]
            if missing:
                findings.append(
                    ('inflight_missing_fields', rel, f"missing: {missing}")
                )
                by_finding['inflight_missing_fields'] += 1
            qtype = seed_block.get('question_type', '')
            if qtype not in ('mcq', 'short_numeric', 'short_answer'):
                findings.append(
                    ('inflight_bad_qtype', rel, f"qtype={qtype!r}")
                )
                by_finding['inflight_bad_qtype'] += 1
            if qtype == 'mcq':
                opts = seed_block.get('options') or []
                if len(opts) < 2:
                    findings.append(
                        ('inflight_mcq_missing_options', rel,
                         f"mcq with options={opts!r}")
                    )
                    by_finding['inflight_mcq_missing_options'] += 1

        # Missing seed_inflight on single-turn where seed_history ends in tutor.
        seed = raw.get('seed_history') or []
        if mode == 'single_turn' and not seed_block and seed and seed[-1].get('role') == 'tutor':
            last = str(seed[-1].get('text', '')).strip()[:80]
            # Skip if pure acknowledgement (engine in POSE mode is correct).
            ack = (
                last.lower().startswith(('right', 'correct', 'exactly', 'nice', 'great'))
                or 'ready for the next' in last.lower()
            )
            if not ack:
                findings.append(
                    ('missing_seed_inflight_question', rel,
                     f"last seed tutor turn looks like a posed question; "
                     f"no seed_inflight_question block")
                )
                by_finding['missing_seed_inflight_question'] += 1

        # Pass threshold sanity check.
        pt = raw.get('pass_threshold', 0.7)
        if not (0.0 <= float(pt) <= 1.0):
            findings.append(('bad_pass_threshold', rel, f"pass_threshold={pt}"))
            by_finding['bad_pass_threshold'] += 1

    # Render.
    print(f"Audited {sum(1 for _ in DATASET_ROOT.rglob('*.yaml'))} YAML files under {DATASET_ROOT.relative_to(REPO_ROOT)}.")
    print()
    if not findings:
        print("Clean — no misalignments detected.")
        return 0

    print(f"Findings: {sum(by_finding.values())} total")
    for kind, n in by_finding.most_common():
        print(f"  {kind}: {n}")
    print()
    by_kind: dict[str, list[tuple[Path, str]]] = {}
    for kind, p, detail in findings:
        by_kind.setdefault(kind, []).append((p, detail))
    for kind in sorted(by_kind):
        print(f"== {kind} ({len(by_kind[kind])}) ==")
        for p, detail in by_kind[kind][:20]:
            print(f"  {p}  -> {detail}")
        if len(by_kind[kind]) > 20:
            print(f"  ... and {len(by_kind[kind]) - 20} more")
        print()
    return 0 if not findings else 1


if __name__ == '__main__':
    sys.exit(main())
