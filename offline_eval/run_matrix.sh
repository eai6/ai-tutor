#!/usr/bin/env bash
# Offline-model eval sweep: run the eval harness once per model in models.txt,
# swapping ONLY the tutor model (TUTOR_MODEL_OVERRIDE) while judge + student-sim
# stay on Anthropic. Saves one run JSON per model under results/.
#
# Usage:
#   bash offline_eval/run_matrix.sh                 # single-turn sweep (default, fastest)
#   MODE="" bash offline_eval/run_matrix.sh         # full suite (single + multi turn)
#   MODE="--multi-turn" bash offline_eval/run_matrix.sh
#   SIMPLE_TUTOR_ENGINE=0 bash offline_eval/run_matrix.sh   # use legacy ConversationalTutor
#   MODELS_FILE=offline_eval/finalists.txt bash offline_eval/run_matrix.sh
#
# Prereqs: Ollama installed + `ollama serve` running. Judge/sim call the
# Anthropic API (needs ANTHROPIC_API_KEY in .env), so each run incurs a small
# Anthropic cost. CPU-only inference is slow — expect minutes per model.
set -uo pipefail

ROOT="/home/daniel/Documents/work/Nyansapo/web/ai-tutor"
cd "$ROOT"
PY="$ROOT/venv/bin/python"
RESULTS="$ROOT/offline_eval/results"
MODELS_FILE="${MODELS_FILE:-$ROOT/offline_eval/models.txt}"
MODE="${MODE:---single-turn}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$ROOT/offline_eval/ollama_models}"
# simple_tutor (the production engine) is now provider-agnostic: its _call_llm
# routes non-Anthropic providers through get_llm_client().generate_with_tools(),
# so it drives OSS Ollama models that support tool-calling (e.g. llama3.2:3b).
# Default ON to evaluate the real production engine. Set =0 for the legacy
# ConversationalTutor (no tool-use; works with non-tool models too).
# NOTE: models that can't tool-call will hit the engine's fallback on simple_tutor.
export SIMPLE_TUTOR_ENGINE="${SIMPLE_TUTOR_ENGINE:-1}"
mkdir -p "$RESULTS" "$OLLAMA_MODELS"

command -v ollama >/dev/null 2>&1 || { echo "ERROR: ollama not installed."; exit 1; }
ollama list >/dev/null 2>&1 || { echo "ERROR: Ollama server not reachable. Start it: 'ollama serve &'"; exit 1; }

echo ">> Seeding local_ollama ModelConfig rows..."
"$PY" offline_eval/seed_ollama_configs.py || { echo "seed failed"; exit 1; }

echo ">> Engine: SIMPLE_TUTOR_ENGINE=$SIMPLE_TUTOR_ENGINE   Mode: ${MODE:-(full suite)}"
echo ">> Model weights: $OLLAMA_MODELS"
echo

while read -r tag tier _rest; do
  [[ -z "${tag:-}" || "$tag" == \#* ]] && continue
  safe="${tag//[:\/]/_}"
  # Resume-safety: skip models already scored (set FORCE=1 to redo).
  if [[ -f "$RESULTS/${safe}.json" && "${FORCE:-0}" != "1" ]]; then
    echo "==================== $tag ($tier) — already done, skipping ===================="
    echo; continue
  fi
  echo "==================== $tag ($tier) ===================="
  echo ">> pulling $tag ..."
  if ! ollama pull "$tag"; then
    echo "!! pull failed for $tag — skipping"; echo; continue
  fi
  start=$(date +%s)
  log="$RESULTS/${safe}.log"
  TUTOR_MODEL_OVERRIDE="local_ollama/$tag" "$PY" manage.py run_eval $MODE >"$log" 2>&1
  rc=$?
  elapsed=$(( $(date +%s) - start ))
  out=$(grep -oE "Output: .*\.json" "$log" | tail -1 | sed 's/^Output: //')
  if [[ -n "$out" && -f "$out" ]]; then
    cp "$out" "$RESULTS/${safe}.json"
    summary=$(grep -E "^Result:" "$log" | tail -1)
    echo ">> saved results/${safe}.json   ${elapsed}s   $summary"
  else
    echo "!! no run JSON (rc=$rc, ${elapsed}s) — inspect $log"
  fi
  ollama stop "$tag" >/dev/null 2>&1 || true   # unload to free RAM
  # CLEANUP_MODELS=1 also deletes the weights from disk after scoring — used on
  # Colab (small disk; results already saved so a re-run skips this model
  # without re-pulling). Off by default so the laptop keeps its cached models.
  if [[ "${CLEANUP_MODELS:-0}" == "1" && -f "$RESULTS/${safe}.json" ]]; then
    ollama rm "$tag" >/dev/null 2>&1 || true
    echo ">> removed $tag weights from disk (CLEANUP_MODELS=1)"
  fi
  echo
done < "$MODELS_FILE"

echo ">> Sweep complete. Leaderboard:"
"$PY" offline_eval/aggregate.py
