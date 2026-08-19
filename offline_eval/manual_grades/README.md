# Manual grades — human verdicts on local transcripts

Exports from the **Grade** tab of `offline_eval/viewer_deploy/index.html` belong
here. Drop the downloaded file in this directory and commit it.

The viewer keeps grades in browser localStorage, which is per-profile and dies
with a cleared cache. A file in this directory is the only durable, shareable
copy — and the only form the grades can be analysed in outside the page.

```bash
mv ~/Downloads/manual_grades_37.json \
   offline_eval/manual_grades/mt100_2026-08-17_daniel.json
```

Rename on the way in. The exported filename carries only a count
(`manual_grades_37.json`), so a second export at the same count collides and
successive exports do not sort meaningfully. `<run>_<date>_<who>.json` keeps a
directory of them readable and makes two people's grades of the same run
obvious at a glance.

To load a file back into the page — your own, or someone else's — use
**Import**. It merges per session and keeps whichever copy has the newer `ts`,
so re-importing your own export is a no-op and importing a colleague's adds
only the sessions you have not graded yourself.

## File format

```json
{
  "version": 1,
  "exported": "2026-08-17T11:09:22.860Z",
  "dimensions": ["mistake_identification", "...", "human_likeness"],
  "graded": 5,
  "verdicts": {
    "00_prefix_colab|deepseek-v3.1|baseline_full_session_error_prone_1466_13": {
      "d": {
        "mistake_identification": "yes",
        "mistake_location": "yes",
        "revealing_answer": "no",
        "providing_guidance": "yes",
        "actionability": "yes",
        "coherence": "yes",
        "tutor_tone": "encouraging",
        "human_likeness": "yes"
      },
      "notes": "",
      "peeked": false,
      "ts": "2026-08-17T11:09:18.343Z"
    }
  }
}
```

| Field | Meaning |
|---|---|
| `dimensions` | the eight dimension keys, in the order the page asked them. Recorded so a file can be read without the page — they come from `ai_tutor/apps/benchmark/pedagogy.py`, the same module behind `/dashboard/benchmark/sessions/`. |
| `graded` | how many verdicts are **complete** (all eight answered). |
| `verdicts` | keyed `run\|model\|scenario`. Stable across rebuilds of the page, so grades reattach to the same sessions when new eval cycles are added. |
| `d` | one entry per dimension. Values come from `pedagogy.py`: `yes` / `to_some_extent` / `no`, plus `yes_correct` / `yes_incorrect` on `revealing_answer`, `encouraging` / `neutral` / `offensive` on `tutor_tone`, and `n/a` anywhere the taxonomy allows it. |
| `notes` | free text, may be empty. |
| `peeked` | the judge's grade was revealed before grading finished. Such a session is excluded from agreement figures and pass-rate percentages — it measures anchoring, not independent agreement — but still counts toward persona balance. |
| `ts` | last edit, ISO 8601. Import uses it to resolve conflicts. |

`verdicts` may contain **incomplete** records — a session you started and left
part-answered, or one where you only revealed the judge. Those have fewer than
eight entries in `d` and are not counted in `graded`. An unanswered dimension is
not a "No"; anything incomplete is excluded from scoring entirely. Filter on
all eight keys being present before computing anything from a file.

## Scoring rule

A session passes only if **every applicable dimension** sits at its desideratum
— all-or-nothing. `n/a` is excluded from scoring rather than counted as a
failure, and a session with nothing scorable fails rather than vacuously
passing. The authority is `pedagogy.session_passes`; the page ships a
JavaScript port of it, pinned against the Python by
`tests/test_viewer_grading.py` over ~2000 value combinations.
