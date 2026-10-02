#!/usr/bin/env bash
# Deploy one commit's image on a single-server (Path A) install.
#
#   sudo deploy/compose/deploy.sh <40-character commit sha>
#
# CI runs this through the <prefix>-deploy SSM document, which checks the commit
# out first (infra/aws-ec2/aitutor-ec2.yaml). It is also safe to run by hand.
# Either way the image must already exist: CI builds
# ghcr.io/<owner>/<repo>:aws-<first 12 of the sha> for every deployable commit.
# This script never builds, because a 30-minute build on the server is the
# step CI exists to remove.
#
# Order matters, and each step is placed where its failure costs least:
#
#   1. pull      a missing or unreadable image fails here, before anything changes
#   2. plan      does the new code carry migrations? That decides whether a
#                failed start can be rolled back automatically.
#   3. backup    database + media, off the box, before anything changes
#   4. switch    IMAGE_TAG in .env, then `up -d`; migrations run on start
#   5. verify    the container's healthcheck, then /health/ through Caddy
#   6. on failure, roll back to the previous image, but ONLY if no migration
#                ran. Django does not reverse migrations, so after one the old
#                image would run against a schema it does not know. That case
#                stops and alerts, and a person restores the step-3 archive.
set -euo pipefail
cd "$(dirname "$0")"

SHA="${1:-}"
if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: deploy.sh <40-character commit sha>" >&2
  exit 2
fi
TAG="aws-${SHA:0:12}"

LOG="${AITUTOR_DEPLOY_LOG:-/var/log/aitutor-deploy.log}"
exec > >(tee -a "$LOG") 2>&1

# Written by the CloudFormation stack at first boot: AWS_REGION, BACKUP_BUCKET,
# ALERT_TOPIC_ARN, SITE_HOSTNAME. A server set up without the stack can supply
# the same file by hand.
CONF="${AITUTOR_CONF:-/etc/aitutor/aitutor.env}"
# shellcheck source=/dev/null
[ -f "$CONF" ] && . "$CONF"

say() { echo "[deploy $(date -u +%FT%TZ)] $*"; }
alert() {
  say "ALERT: $1"
  if [ -n "${ALERT_TOPIC_ARN:-}" ]; then
    aws sns publish --region "$AWS_REGION" --topic-arn "$ALERT_TOPIC_ARN" \
      --subject "AI Tutor deploy on ${SITE_HOSTNAME:-$(hostname)}" --message "$1" >/dev/null \
      || say "could not publish the alert to SNS"
  fi
}

if [ ! -f .env ]; then
  say ".env is missing. Finish the runbook's phase 4 before the first deploy."
  exit 1
fi
env_value() { sed -n "s/^$1=//p" .env | tail -1; }
PREV="$(env_value IMAGE_TAG)"
REPO="$(env_value IMAGE_REPO)"
REPO="${REPO:-ghcr.io/eai6/ai-tutor}"

say "deploying $REPO:$TAG (commit $SHA); running now: ${PREV:-nothing}"

# --- 1. pull --------------------------------------------------------------
# The shell's IMAGE_TAG overrides .env for these two commands only.
if ! IMAGE_TAG="$TAG" docker compose pull --quiet app; then
  alert "could not pull $REPO:$TAG. Nothing changed. Has CI built it, and is the package public?"
  exit 1
fi

# --- 2. plan --------------------------------------------------------------
# The new image, against the live database, without starting it.
PLAN="$(IMAGE_TAG="$TAG" docker compose run --rm --no-deps -T app python manage.py migrate --plan 2>&1)" || {
  alert "migrate --plan failed for $TAG. Nothing changed. Output: $PLAN"
  exit 1
}
if echo "$PLAN" | grep -q "No planned migration operations"; then
  MIGRATES=0
  say "no pending migrations: a failed start rolls back automatically"
else
  MIGRATES=1
  say "pending migrations, so a failed start will NOT roll back automatically:"
  echo "$PLAN"
fi

# --- 3. backup ------------------------------------------------------------
BACKUP_NOTE="no off-box backup (BACKUP_BUCKET unset)"
if [ -n "${BACKUP_BUCKET:-}" ]; then
  ./backup-to-s3.sh "pre-deploy-$TAG"
  BACKUP_NOTE="pre-deploy backup in s3://$BACKUP_BUCKET/daily/ named pre-deploy-$TAG-*"
else
  say "BACKUP_BUCKET unset; writing the pre-deploy backup locally only"
  ./backup.sh /var/backups/aitutor
fi

# --- 4. switch ------------------------------------------------------------
set_tag() {
  if grep -q '^IMAGE_TAG=' .env; then
    sed -i "s|^IMAGE_TAG=.*|IMAGE_TAG=$1|" .env
  else
    echo "IMAGE_TAG=$1" >> .env
  fi
}
set_tag "$TAG"
docker compose up -d

# --- 5. verify ------------------------------------------------------------
# start_period is 300 s and the first start of new code migrates and rebuilds
# the help index, so allow 15 minutes before calling it.
wait_healthy() {
  local deadline=$((SECONDS + 900)) cid status
  while [ $SECONDS -lt $deadline ]; do
    cid="$(docker compose ps -q app)"
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true)"
    case "$status" in
      healthy)
        # Through Caddy as well: a healthy container behind a broken proxy is
        # still a site that is down.
        if [ -z "${SITE_HOSTNAME:-}" ] || curl -fsS --max-time 10 \
             --resolve "$SITE_HOSTNAME:443:127.0.0.1" "https://$SITE_HOSTNAME/health/" >/dev/null; then
          return 0
        fi ;;
      unhealthy) return 1 ;;
    esac
    sleep 10
  done
  return 1
}

if wait_healthy; then
  say "healthy on $TAG"
  # Keep this image and the one before it, so a rollback needs no pull.
  docker image ls --format '{{.Repository}}:{{.Tag}}' \
    | grep -F "$REPO:aws-" | grep -vxF -e "$REPO:$TAG" -e "$REPO:$PREV" \
    | xargs -r docker image rm >/dev/null || true
  docker image prune -f >/dev/null || true
  say "done; $BACKUP_NOTE"
  exit 0
fi

# --- 6. failure -------------------------------------------------------------
docker compose logs --tail 80 app || true
if [ "$MIGRATES" -eq 0 ] && [ -n "$PREV" ] && [ "$PREV" != "$TAG" ]; then
  say "rolling back to $PREV"
  set_tag "$PREV"
  docker compose up -d
  if wait_healthy; then
    alert "$TAG failed its health check and was rolled back to $PREV, which is serving. No migrations were involved."
  else
    alert "$TAG failed, and the rollback to $PREV is ALSO unhealthy. The site is down. $BACKUP_NOTE."
  fi
  exit 1
fi
alert "$TAG failed its health check after applying migrations, so it was NOT rolled back: the previous image would run against a schema it does not know. To go back, restore the $BACKUP_NOTE with restore.sh, then set IMAGE_TAG=$PREV in .env and run docker compose up -d."
exit 1
