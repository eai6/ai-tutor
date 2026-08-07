#!/usr/bin/env sh
# Migration + seed chain, copied verbatim from the Dockerfile CMD.
#
# Azure Container Apps still runs this via CMD, where single-revision mode
# serialises it across a rollout. On ECS it must run as a ONE-SHOT task before
# the web service is updated -- ECS starts every task at once, so running it in
# CMD would race the migrations across replicas.
#
# The Dockerfile is deliberately NOT changed: Azure depends on its CMD, and the
# AWS web task definition overrides `command` to gunicorn alone instead. Keep
# this list in step with the Dockerfile CMD if either changes.
set -eu

python manage.py migrate
python manage.py seed_gamification
python manage.py backfill_progress
python manage.py classify_unit_grades
python manage.py seed_help_assistant_model
python manage.py generate_recent_updates
python manage.py build_help_index --with-source
