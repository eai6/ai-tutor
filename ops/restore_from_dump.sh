#!/usr/bin/env bash
#
# Restore a PostgreSQL dump into the AWS RDS instance, from INSIDE the VPC.
#
#   ./ops/restore_from_dump.sh <local-dump-file> [stack]
#
# RDS is publicly_accessible=False and lives in private subnets, which is the
# point — student records should not be reachable from the internet, not even
# briefly. So the restore does not run from a laptop. It uploads the dump to
# the ops bucket and runs a one-off Fargate task in the same private subnets,
# which is the only place with a route to the database.
#
# The task reuses the `migrate` task definition with a command override rather
# than adding a fourth definition: it already carries DATABASE_URL and the
# other secrets, already runs in the right subnets, and already uses the
# security group RDS accepts (compute.py:169-177).
#
# DESTRUCTIVE. It drops and recreates the target database. That is deliberate —
# ops/migrate_and_seed.sh has already run here, so the schema exists and
# ModelConfig/terms rows are seeded; restoring on top of that produces
# duplicate and diverging rows rather than a copy of the source.
#
# Prerequisites: the image must contain pg_restore (Dockerfile installs
# postgresql-client-16 via PGDG), and the ops bucket must exist.
#
# Plan: memory/aws_data_migration_plan.md
set -euo pipefail

DUMP_FILE="${1:?usage: restore_from_dump.sh <local-dump-file> [stack]}"
STACK="${2:-dev}"
PREFIX="aitutor-${STACK}"
REGION="${AWS_REGION:-us-east-1}"
CLUSTER="${PREFIX}-cluster"
SERVICE="${PREFIX}-service"
MIGRATE_TD="${PREFIX}-migrate"

[ -f "$DUMP_FILE" ] || { echo "no such dump: $DUMP_FILE" >&2; exit 1; }

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
OPS_BUCKET="${PREFIX}-ops-${ACCOUNT_ID}"
KEY="restore/$(basename "$DUMP_FILE")"

echo "==> restoring $(du -h "$DUMP_FILE" | cut -f1) into ${PREFIX} (${REGION})"

# ── 1. Stop the app ────────────────────────────────────────────────────────
# A database cannot be dropped while anything holds a connection, and the web
# containers hold a pool. Capture the current count so it can be restored even
# if this script is re-run.
ORIGINAL_COUNT=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].desiredCount' --output text)
echo "==> scaling ${SERVICE} ${ORIGINAL_COUNT} -> 0"
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
  --desired-count 0 >/dev/null
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"

# Whatever happens next, put the service back. Without this a failed restore
# leaves the site down.
restore_service() {
  echo "==> scaling ${SERVICE} back to ${ORIGINAL_COUNT}"
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
    --desired-count "$ORIGINAL_COUNT" >/dev/null || true
  aws s3 rm "s3://${OPS_BUCKET}/${KEY}" >/dev/null 2>&1 || true
}
trap restore_service EXIT

# ── 2. Stage the dump ──────────────────────────────────────────────────────
echo "==> uploading to s3://${OPS_BUCKET}/${KEY}"
aws s3 cp "$DUMP_FILE" "s3://${OPS_BUCKET}/${KEY}" --only-show-errors

# ── 3. Restore, inside the VPC ─────────────────────────────────────────────
# The work itself lives in ops/restore_inner.py, which is already in the image
# (the Dockerfile COPYs the repo). Passing a script through a container
# override needs it to survive two levels of quoting, which turns readable
# Python into character-code escapes — not something anyone should debug
# during a failed restore.

NETCFG=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].networkConfiguration' --output json)

OVERRIDES=$(python3 - "$OPS_BUCKET" "$KEY" <<'PY'
import json, sys
bucket, key = sys.argv[1], sys.argv[2]
print(json.dumps({"containerOverrides": [{
    "name": "migrate",
    "command": ["python", "ops/restore_inner.py"],
    "environment": [
        {"name": "OPS_BUCKET", "value": bucket},
        {"name": "DUMP_KEY", "value": key},
    ],
}]}))
PY
)

echo "==> starting the restore task"
TASK_ARN=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$MIGRATE_TD" \
  --launch-type FARGATE --network-configuration "$NETCFG" \
  --overrides "$OVERRIDES" --query 'tasks[0].taskArn' --output text)
echo "    $TASK_ARN"

# Poll rather than `ecs wait tasks-stopped` — that waiter gives up at ~10
# minutes and a restore of this size can exceed it.
DEADLINE=$((SECONDS + 1800))
while [ $SECONDS -lt $DEADLINE ]; do
  STATUS=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
    --query 'tasks[0].lastStatus' --output text)
  [ "$STATUS" = "STOPPED" ] && break
  echo "    status=$STATUS"
  sleep 15
done

TASK_ID="${TASK_ARN##*/}"
echo "==> task logs"
aws logs tail "/ecs/${PREFIX}" --log-stream-names "migrate/migrate/${TASK_ID}" \
  --since 40m || true

EXIT_CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)
REASON=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].stoppedReason' --output text)

if [ "$EXIT_CODE" != "0" ]; then
  echo "RESTORE FAILED: exit=${EXIT_CODE} reason=${REASON}" >&2
  exit 1
fi
echo "==> restore completed (exit 0)"
