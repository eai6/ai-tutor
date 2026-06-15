#!/usr/bin/env bash
# Pull + probe a broad set of OSS models for Ollama tool-calling support.
# Prioritizes tool-use fine-tuned models. Writes a running results table.
cd /home/daniel/Documents/work/Nyansapo/web/ai-tutor
PY=venv/bin/python
OUT=offline_eval/tool_probe_results.txt
: > "$OUT"

# Ordered by likelihood of working / relevance. Pull failures (nonexistent
# tags) are skipped. Tool-use fine-tunes first, then general families.
MODELS="
llama3-groq-tool-use:8b
hermes3:3b
nemotron-mini
granite3.1-moe:3b
granite3.1-dense:2b
llama3.2:3b
llama3.2:1b
llama3.1:8b
mistral:7b
qwen3:1.7b
qwen3:4b
command-r7b
gemma2:2b
"

for m in $MODELS; do
  [ -z "$m" ] && continue
  echo "=== $m ===" | tee -a "$OUT"
  if ! ollama pull "$m" >/dev/null 2>&1; then
    echo "  PULL FAILED (tag may not exist in this Ollama lib) — skip" | tee -a "$OUT"
    echo | tee -a "$OUT"; continue
  fi
  $PY offline_eval/_probe_ollama_tools.py "$m" 2>&1 | grep -E "tool_calls returned|RESULT|->" | sed 's/^/  /' | tee -a "$OUT"
  ollama stop "$m" >/dev/null 2>&1 || true
  echo | tee -a "$OUT"
done
echo "DONE" | tee -a "$OUT"
