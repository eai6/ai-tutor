# Offline-model evaluation experiment

Goal: find the best **offline** open-source model (run locally via Ollama) for
the AI Tutor, across **phone/tablet** (≤4B) and **laptop** (7–9B) size tiers.
Everything here is local and **gitignored** — nothing is committed.

## Design

- **Model under test = the tutor.** We swap only the tutor model per run via the
  built-in `TUTOR_MODEL_OVERRIDE="local_ollama/<tag>"` env var, which routes
  `ModelConfig.get_for('tutoring')` to the Ollama model. Both tutor engines
  (legacy `ConversationalTutor` and the current `simple_tutor`) read that.
- **Scoring stays on Anthropic (trusted + identical across all models):**
  - Layer 1 — deterministic assertions (model-free).
  - Layer 3 — rubric judge = Anthropic `claude-haiku-4-5` (hardcoded in the harness).
  - Student simulator (multi-turn) = Anthropic `claude-haiku-4-5`.
  - The runtime grader is cross-family by design — it never grades with the
    tutor's own provider — so swapping the tutor to OSS keeps grading off the OSS model.
- **Eval set:** `evals/dataset/**` — 81 scenarios (61 single-turn + 20 multi-turn).
  The sweep runs single-turn by default (fast signal); run multi-turn for finalists.

## Prerequisites

1. Install Ollama (you're doing this): https://ollama.com/download — then start it:
   ```
   ollama serve &
   ```
2. `ANTHROPIC_API_KEY` in `.env` (already present — used by the judge + sim).
3. DB migrated + eval institution loaded (already done this session).

Model weights download into `offline_eval/ollama_models/` (gitignored) via the
`OLLAMA_MODELS` env var the runner sets.

## Run it

```bash
# Full single-turn sweep over every model in models.txt:
bash offline_eval/run_matrix.sh

# Then the leaderboard:
venv/bin/python offline_eval/aggregate.py
```

Knobs (env vars):
- `MODE="--single-turn"` (default) | `""` (full suite) | `"--multi-turn"`
- `SIMPLE_TUTOR_ENGINE=1` (default, current engine) | `0` (legacy engine)
- `MODELS_FILE=offline_eval/finalists.txt` to run a curated subset

## Files

- `models.txt` — the model matrix (edit to add/remove tags or tiers).
- `seed_ollama_configs.py` — creates inactive `local_ollama` ModelConfig rows so
  the override resolves. Idempotent; run automatically by `run_matrix.sh`.
- `run_matrix.sh` — pull → eval → save `results/<model>.json` per model.
- `aggregate.py` — ranks `results/*.json` into a leaderboard.
- `results/` — per-model run JSON + logs (gitignored).

## Caveats on this 8GB / CPU-only box

- CPU inference is slow — minutes per model for the single-turn set; the laptop
  tier (7–9B) is the slowest and `gemma2:9b` is borderline on 8GB RAM.
- 14B+ models won't fit here. Add them to `models.txt` (`big` tier section) and
  run this same harness on a ≥16GB / GPU host — it's hardware-agnostic.
- Models run one at a time and are unloaded (`ollama stop`) between runs to free RAM.
- Each scenario makes a few Anthropic API calls (judge/sim) — small but non-zero cost.
