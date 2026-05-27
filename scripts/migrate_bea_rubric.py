"""Append the 8 BEA-aligned standard rubric items to every scenario YAML.

Strategy B from the rubric-migration discussion: every scenario keeps its
scenario-specific rubric items AND gets the 8 standard BEA-aligned items
appended just before ``pass_threshold:``. This preserves diagnostic detail
(scenario-specific items continue to surface specific failure modes) while
adding uniform cross-scenario coverage on the 8 dimensions defined in the
BEA-2025 evaluation rubric.

Idempotent: a scenario already migrated (carries the BEA header sentinel)
is left alone on subsequent runs. The previously-injected single-line
"action-presence" item from the prior migration is removed if present —
its content is covered by BEA #5 (Actionability).

Run from the repo root:
    python scripts/migrate_bea_rubric.py
"""
from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = REPO_ROOT / 'evals' / 'dataset'

BEA_HEADER = (
    "  # --- BEA-aligned standard rubric (universal across scenarios) ---"
)
BEA_ITEMS: list[str] = [
    # 1. Mistake identification
    "  - \"If the student made a mistake, the response identifies or "
    "recognises it; if the student was correct, the response affirms that "
    "clearly without false hedging or unnecessary second-guessing.\"",
    # 2. Mistake location
    "  - \"If a mistake exists, the response points at its specific "
    "location or nature — a particular step, a named misconception, a "
    "specific arithmetic slip — not just a generic 'try again' or "
    "'not quite'.\"",
    # 3. No answer reveal (desideratum: No)
    "  - \"The response does NOT reveal the final answer outright. Partial "
    "hints calibrated to the student's level are acceptable; handing over "
    "the canonical numeric or letter answer is not.\"",
    # 4. Providing guidance
    "  - \"The response offers correct and relevant guidance — an "
    "explanation, elaboration, hint, worked example, or scaffolding — "
    "calibrated to the student's apparent level and the current concept.\"",
    # 5. Actionability
    "  - \"It is clear from the response what the student should do next — "
    "a specific question to answer, an MCQ option to pick, a calculation "
    "to perform, or another structured prompt that hands the "
    "conversational floor back.\"",
    # 6. Coherence
    "  - \"The response is logically consistent with the conversation so "
    "far — does not contradict its own prior turns, does not assume facts "
    "not yet established, and does not ignore what the student just "
    "said.\"",
    # 7. Tutor tone (encouraging)
    "  - \"The tutor's tone is warm and encouraging — supportive without "
    "being condescending, honest without being harsh. Never offensive, "
    "dismissive, or impatient.\"",
    # 8. Human-likeness
    "  - \"The response sounds natural and conversational, not robotic, "
    "templated, or padded with filler openers like 'Great question!', "
    "'Let me think about this carefully', or 'I'm going to explain...'.\"",
]

# Match a single-line rubric item whose text begins with the action-presence
# wording from the prior migration. Multi-line block scalars are not used in
# the dataset, so a line-based match is sufficient.
PRIOR_ACTION_ITEM_RE = re.compile(
    r'^[ \t]*-[ \t]+"The response leaves the student with a clear, concrete '
    r'action[^"]*"[ \t]*\n',
    re.MULTILINE,
)

# Match the start of the pass_threshold line (anchored at column 0 — top-
# level key).
PASS_THRESH_RE = re.compile(r'^pass_threshold:\s', re.MULTILINE)


def migrate(path: Path) -> str:
    """Apply the migration to a single file. Returns one of:
      'migrated' — file modified
      'already'  — file already carries the BEA header
      'skip'     — file has no rubric block / no pass_threshold (unusual)
    """
    text = path.read_text(encoding='utf-8')

    if BEA_HEADER in text:
        return 'already'

    if not PASS_THRESH_RE.search(text):
        return 'skip'

    # 1. Strip the prior single-line action-presence item (if present).
    text = PRIOR_ACTION_ITEM_RE.sub('', text)

    # 2. Build the insertion block: header + 8 BEA items, each followed by \n.
    insertion = BEA_HEADER + '\n' + '\n'.join(BEA_ITEMS) + '\n'

    # 3. Insert immediately before pass_threshold:.
    new_text = PASS_THRESH_RE.sub(
        lambda m: insertion + m.group(0),
        text,
        count=1,
    )

    path.write_text(new_text, encoding='utf-8')
    return 'migrated'


def main() -> None:
    counts = {'migrated': 0, 'already': 0, 'skip': 0}
    skipped: list[Path] = []
    for path in sorted(DATASET.rglob('*.yaml')):
        status = migrate(path)
        counts[status] += 1
        if status == 'skip':
            skipped.append(path)

    print(f"Migrated: {counts['migrated']}")
    print(f"Already migrated (idempotent skip): {counts['already']}")
    print(f"Skipped (no pass_threshold): {counts['skip']}")
    if skipped:
        print('Skipped files:')
        for p in skipped:
            print(f'  {p.relative_to(REPO_ROOT)}')


if __name__ == '__main__':
    main()
