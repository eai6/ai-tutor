# Tutor system prompt — variants tested

This directory holds the literal text of every tutor system prompt that has
been A/B-tested through `scripts/run_ab_test.py`. Each file is the verbatim
template body (single-`{brace}` interpolation tokens, identical to what the
provider builder feeds the LLM at runtime).

The intent is that future Claude / future Roy can read the prompts side-by-
side without having to dig them out of Python string constants, and can
match each variant's text against the recommendations the A/B judge surfaced
for that variant.

## Variants

| Variant | File | Size | Provenance | Result vs prior |
|---|---|---|---|---|
| **v3 baseline (Anthropic)** | [v3_baseline_anthropic.md](v3_baseline_anthropic.md) | 23,084 chars / 460 lines | `apps/tutoring/prompts/anthropic.py` (production) | Control |
| **v3 baseline (Gemini)** | [v3_baseline_gemini.md](v3_baseline_gemini.md) | 6,279 chars / 156 lines | `apps/tutoring/prompts/gemini.py` (production) | Control |
| **v4** | [v4.md](v4.md) | 8,753 chars / 202 lines | `scripts/run_ab_v4_cycle.py` — slim rewrite per `SCIENCE_LEARNING_AUDIT_v3.md` §4 + `ab-test-reports/FINAL_REPORT.md` | Sonnet 2.88 → 2.98, Gemini 3.10 → 2.90 |
| **v5** | [v5.md](v5.md) | 9,811 chars / 234 lines | `scripts/run_ab_v5_cycle.py` — addresses v4 themes (meta-leakage, silent pivot, isomorph-diagnosis, skipped worked example) | Sonnet 2.98 → 3.10, Gemini 2.90 → 2.90 |
| **v6** | [v6.md](v6.md) | 10,984 chars / 266 lines | `scripts/run_ab_v6_cycle.py` — extracts `<figure_rules>` + `<must_end_with_question>` to standalone blocks, paired with engine dedup + LLM-judged template-repeat | Sonnet 3.10 → 3.27 (best), Gemini 2.90 → 3.10 (n=2) |

## Reports

Each variant has a matching `ab-test-reports-{N}/` directory at the repo root
with the judge's full per-cell findings and the ranked-recommendation
`FINAL_REPORT.md` that drove the next cycle's draft.

| Variant | Reports dir |
|---|---|
| v3 baseline | `ab-test-reports/` (the original run that surfaced v4's recommendation list) |
| v4 | `ab-test-reports-v4/` |
| v5 | `ab-test-reports-v5/` |
| v6 | `ab-test-reports-v6/` |

## How the variants get into the harness

`scripts/run_ab_test.py` reads the prompt from
`apps.tutoring.prompts.anthropic.TUTOR_SYSTEM_PROMPT_TEMPLATE` and
`apps.tutoring.prompts.gemini.GEMINI_TUTOR_SYSTEM_PROMPT_TEMPLATE` (whatever
is on disk at the time of the run). The v4/v5/v6 wrapper scripts
monkey-patch both of those module-level constants before invoking the
matrix, so the production files stay untouched — see
`_patch_prompt_templates()` in each `run_ab_v{N}_cycle.py`.

That means a re-run of any variant only requires running its wrapper:

```bash
caffeinate -i venv/bin/python scripts/run_ab_v6_cycle.py
```

The wrappers also pin `AB_REPORT_DIR` to a sibling output directory
(e.g. `ab-test-reports-v6/`) so prior cycles aren't overwritten.

## Re-extracting after edits

The variants in this directory are extracted from the script files — they're
secondary copies, not the source of truth. If you edit a `V{N}_TUTOR_SYSTEM_PROMPT_TEMPLATE`
constant in a wrapper script, re-run the extractor (the inline Python at the
end of conversation 92ca444a or any equivalent) to refresh the corresponding
markdown file.

## What about subject-pack injections?

The per-provider subject injection (`apps/tutoring/prompts/injections/math.py`,
`general.py`) is appended *after* the static template by the provider builder
at runtime. None of the markdown files in this directory include it. To see
the full rendered prompt for a math lesson, run:

```bash
venv/bin/python -c "
import django, os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
sys.path.insert(0,'.'); django.setup()
from apps.tutoring.prompts import get_prompt_builder
from apps.tutoring.prompts.base import StablePrefixContext
ctx = StablePrefixContext('Inst','Locale','Tutor','English','S3','[safety]')
print(get_prompt_builder('anthropic').build_stable_prefix(ctx, subject_pack='mathematics'))
"
```
