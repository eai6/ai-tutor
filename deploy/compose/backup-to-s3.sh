#!/usr/bin/env bash
# backup.sh, then the archive off the box to S3. Nightly from
# systemd/aitutor-backup.timer, and before every deploy from deploy.sh.
#
#   sudo deploy/compose/backup-to-s3.sh [label]     # label defaults to "nightly"
#
# Reads AWS_REGION, BACKUP_BUCKET and ALERT_TOPIC_ARN from
# /etc/aitutor/aitutor.env, which the CloudFormation stack writes at first boot.
# Uploads with the instance role, which may write daily/ and nothing else: it
# can neither read old backups nor delete them, so a compromised server cannot
# wipe its own history. The bucket's lifecycle rule expires them instead.
set -euo pipefail
cd "$(dirname "$0")"

LABEL="${1:-nightly}"
. /etc/aitutor/aitutor.env
: "${BACKUP_BUCKET:?BACKUP_BUCKET is not set in /etc/aitutor/aitutor.env}"

DEST=/var/backups/aitutor
umask 077          # the archive holds student data
mkdir -p "$DEST"

fail() {
  echo "backup FAILED: $1" >&2
  if [ -n "${ALERT_TOPIC_ARN:-}" ]; then
    aws sns publish --region "$AWS_REGION" --topic-arn "$ALERT_TOPIC_ARN" \
      --subject "AI Tutor backup failed on ${SITE_HOSTNAME:-$(hostname)}" \
      --message "$LABEL backup failed: $1" >/dev/null || true
  fi
  exit 1
}

MARK="$(mktemp)"
trap 'rm -f "$MARK"' EXIT
./backup.sh "$DEST" || fail "backup.sh exited non-zero"
OUT="$(find "$DEST" -maxdepth 1 -name 'aitutor-*.tar.gz' -newer "$MARK" | head -1)"
[ -n "$OUT" ] || fail "backup.sh reported success but wrote no archive in $DEST"

KEY="daily/$LABEL-$(basename "$OUT")"
aws s3 cp --only-show-errors --region "$AWS_REGION" "$OUT" "s3://$BACKUP_BUCKET/$KEY" \
  || fail "upload to s3://$BACKUP_BUCKET/$KEY failed"

# Local copies are a convenience for a quick restore, not the backup.
find "$DEST" -maxdepth 1 -name 'aitutor-*.tar.gz' -mtime +7 -delete
echo "uploaded s3://$BACKUP_BUCKET/$KEY"
