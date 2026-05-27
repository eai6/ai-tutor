"""One-shot: strip ``must_contain_phrase`` / ``must_not_contain_phrase`` from
every scenario YAML, per the 2026-05-27 user direction:

    "remove the item specific assertions and focus on the 8 dimension of
    the paper"

The 8 BEA-aligned standard rubric items (now present in every scenario)
plus the universal `meta_reasoning_leak` + `passive_ending` deterministic
checks injected by the runner cover the same ground semantically and
won't false-positive on coarse keyword matches (e.g. "exactly", "the
diagram", "360") the way the dropped verbs did.

What stays
----------
- response_nonempty (sanity)
- must_label / must_not_label (production-judge labels, different signal layer)
- must_end_with_question (structural)
- meta_reasoning_leak / passive_ending (runner-injected; not in YAML)

What is stripped
----------------
- must_contain_phrase (free-form keyword)
- must_not_contain_phrase (free-form keyword)

Edits files in place. Idempotent.

Run:
    python scripts/strip_phrase_assertions.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_ROOT = REPO_ROOT / 'evals' / 'dataset'


# Match a `must_(not_)contain_phrase:` key + its value block (scalar or list),
# stopping at the next top-level (4-space-indented) key under `assertions:`.
# We use a textual edit rather than yaml round-tripping to preserve all
# comments and exact formatting of the scenario files.

_KEY = re.compile(
    r"^(  )(must_contain_phrase|must_not_contain_phrase)\s*:"
    r"(.*?)"
    r"(?=^  \w|^\w|\Z)",
    re.DOTALL | re.MULTILINE,
)


def strip_one(text: str) -> tuple[str, int]:
    """Return (new_text, num_stripped)."""
    n = 0

    def _sub(m):
        nonlocal n
        n += 1
        return ''

    new_text = _KEY.sub(_sub, text)
    return new_text, n


def main() -> int:
    total_files = 0
    total_stripped = 0
    edited = []
    for p in sorted(DATASET_ROOT.rglob('*.yaml')):
        total_files += 1
        original = p.read_text(encoding='utf-8')
        new_text, n = strip_one(original)
        if n:
            total_stripped += n
            edited.append((p.relative_to(REPO_ROOT), n))
            p.write_text(new_text, encoding='utf-8')

    print(f"Scanned {total_files} files; stripped {total_stripped} keys across {len(edited)} files.")
    for path, n in edited[:60]:
        print(f"  {path}  (-{n})")
    if len(edited) > 60:
        print(f"  ... and {len(edited) - 60} more")
    return 0


if __name__ == '__main__':
    sys.exit(main())
