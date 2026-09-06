#!/bin/sh
# Re-apply one phase from a clean template tree, so a fix to the converter can
# be retried without hand-unpicking the previous attempt.
#   scripts/phase.sh <sheet> [sheet...]
set -e
git checkout -- ai_tutor/templates
for s in "$@"; do
    git restore --staged "$s" 2>/dev/null || true   # un-stage a deletion
    git checkout -- "$s" 2>/dev/null || true
done
venv/bin/python scripts/apply_map.py $(for s in "$@"; do printf -- "--sheet %s " "$s"; done) \
    --templates ai_tutor/templates --apply
