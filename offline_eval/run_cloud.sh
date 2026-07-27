#!/usr/bin/env bash
# Cloud benchmark sweep: run the eval harness once per cloud model in
# cloud_models.txt, swapping ONLY the tutor (TUTOR_MODEL_OVERRIDE) while judge +
# student-sim stay Anthropic. API-based (no Ollama). Saves one JSON per model
# under results/. Mirror of run_matrix.sh for the proprietary benchmark ceiling.
#
# Usage:
#   GEMINI_MAX_RETRIES=0 bash offline_eval/run_cloud.sh     # fail-fast when Gemini is down
#   FORCE=1 bash offline_eval/run_cloud.sh                  # redo even if a JSON exists
set -uo pipefail

ROOT="${AI_TUTOR_ROOT:-/home/daniel/Documents/work/Nyansapo/web/ai-tutor}"
cd "$ROOT"
PY="${PY:-$ROOT/venv/bin/python}"
RESULTS="${RESULTS_DIR:-$ROOT/offline_eval/single_turn_results/results}"
MODELS_FILE="${CLOUD_MODELS_FILE:-$ROOT/offline_eval/cloud_models.txt}"
MODE="${MODE:---single-turn}"
export SIMPLE_TUTOR_ENGINE="${SIMPLE_TUTOR_ENGINE:-1}"
mkdir -p "$RESULTS"

# One sweep per results dir. Two concurrent sweeps skip on the same JSONs, so they
# run the same model at the same time, interleave writes into one per-model .log,
# and the `grep Output:` below can then copy the wrong run's JSON. Cost doubles and
# the results are silently untrustworthy.
exec 9>"$RESULTS/.sweep.lock"
if ! flock -n 9; then
  echo "!! a sweep is already running against $RESULTS — refusing to start a second one." >&2
  echo "   (ps -eo pid,etime,cmd | grep run_cloud.sh)" >&2
  exit 1
fi

echo ">> Engine: SIMPLE_TUTOR_ENGINE=$SIMPLE_TUTOR_ENGINE   Mode: ${MODE:-(full suite)}"
echo ">> GEMINI_MAX_RETRIES=${GEMINI_MAX_RETRIES:-3} (0 = fail-fast to OpenAI judge fallback)"
echo

while read -r spec safe region _rest; do
  [[ -z "${spec:-}" || "$spec" == \#* ]] && continue
  if [[ -f "$RESULTS/${safe}.json" && "${FORCE:-0}" != "1" ]]; then
    echo "==================== $spec — already done, skipping ===================="
    echo; continue
  fi
  echo "==================== $spec ${region:+(region=$region)} ===================="
  start=$(date +%s)
  log="$RESULTS/${safe}.log"
  # Vertex MaaS models live in per-model regions; export GOOGLE_CLOUD_LOCATION
  # for those rows (non-override .env default applies to rows without a region).
  if [[ -n "${region:-}" ]]; then
    GOOGLE_CLOUD_LOCATION="$region" TUTOR_MODEL_OVERRIDE="$spec" "$PY" manage.py run_eval $MODE >"$log" 2>&1
  else
    TUTOR_MODEL_OVERRIDE="$spec" "$PY" manage.py run_eval $MODE >"$log" 2>&1
  fi
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
  echo
done < "$MODELS_FILE"

echo ">> Cloud sweep complete. Leaderboard:"
"$PY" offline_eval/aggregate.py
