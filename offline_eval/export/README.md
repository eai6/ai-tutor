# Geography evaluation dataset

Five tutoring systems over the same 34 scenarios, same engine, same simulated
students. Two run on one local GPU, three through cloud APIs.

| arm | tier |
|---|---|
| `qwen3-4b-jetson` | on-device (RTX 3090, 24 GB) |
| `qwen3.8-27b-instruct` | on-device (same card) |
| `claude-opus-4-7` | cloud API |
| `gemini-3.5-flash` | cloud API |
| `gpt-5.4-mini` | cloud API |

## Files

**`ai_tutor_geography_dataset.xlsx` is self-contained** — everything below is a
sheet in it, transcripts included. The CSVs are the same tables for anyone who
would rather load them directly.

| sheet | rows | one row is |
|---|---:|---|
| `codebook` | 20 | field definitions — read first |
| **`transcripts`** | **2,882** | **one message, in order, with the text itself** |
| `sessions` | 170 | one tutoring session — with `persona` and `lesson_id` |
| `tutor_responses` | 1,526 | one tutor response: tokens, tools, grading verdict |
| `grades_long` | 1,353 | one graded session × dimension |

`transcripts_nested.jsonl` holds the same conversation text nested by session,
for anyone who wants records rather than rows. The workbook does not need it.

**There is no cost sheet, deliberately.** The measured quantity is tokens, and
those are in `tutor_responses` — fresh input, cached input, cache writes, and
the reply length in characters. Converting them to money needs list prices that
change, cache-rate multipliers, and an estimate of output tokens the tracer
never recorded. That is softer than the rest of this dataset, so the conversion
is left to whoever needs it, with prices they can defend.

## When it was run

All five arms ran on **2026-08-24**; grading was recorded on **2026-08-25**.

`run_date`, `run_started_at` and `run_finished_at` in `sessions` time the
**arm**, not the individual session — the boards carry no per-session clock, so
every session in one arm shares those values. `graded_at` in `grades_long` *is*
per session.

`git_sha` records the commit behind each run. The two on-device arms ran on
earlier commits than the three cloud arms; the engine was unchanged between
them, but the sha is there so that is checkable rather than taken on trust.

## Three things to know before using it

**`session_id` joins every sheet.** All four data sheets carry it, and it is
the key to use: filter `transcripts` to one `session_id` and sort by
`message_index` to read that conversation; the same id gives you its 8 grade
rows and its tutor responses.

**One scenario produced exactly one session per arm** — 170 sessions, 170
distinct arm × scenario pairs, none run twice. So `session_id` and
`scenario_id` identify the same thing in this dataset. `scenario_id` names the
situation being tested; `session_id` names the run of it.

**A "tutor response" is one message from the tutor, not an exchange.** A
session with 7 tutor responses also holds 6–7 student messages. The
transcript's own `exchange_number` counts the pair, so a tutor message and the
student reply share one value.

**`assertions_passed` is not teaching quality.** It records only that the
session ran, tools fired and grading resolved. The quality measure is
`session_passes` in `grades_long.csv`, from human grading.

**Filter grades before analysing them.** Use `grading_complete == TRUE`: an
incomplete grading has fewer than eight dimensions answered, and an unanswered
dimension is not a "no". `at_desideratum` is null where the verdict is `n/a`,
which the taxonomy treats as unscorable rather than failed.

## Reproducing the headline numbers

```python
import pandas as pd
g = pd.read_excel("ai_tutor_geography_dataset.xlsx", sheet_name="grades_long")
comp = g[g.grading_complete]
(comp.groupby(["arm", "scenario_id"]).session_passes.first()
     .groupby("arm").agg(graded="size", passed="sum"))
```

gives 23/34, 33/34, 24/34, 29/33, 34/34 for the five arms.

## Scope and limits

Geography only. The maths boards are excluded because two of their lessons hold
fewer bank questions than a session consumes, so sessions there end on the turn
cap rather than on the tutor's behaviour.

Grading is by one rater with no second pass, so there is no inter-rater
reliability figure. `peeked` marks sessions where the automated grade was
revealed before grading finished; none in this export.

Output token counts are estimated from reply length (~4 chars/token), not
recorded — they are 6–8% of each bill.

Regenerate with `python offline_eval/export_dataset.py`.
