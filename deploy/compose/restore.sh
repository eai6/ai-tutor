#!/usr/bin/env bash
# Restore an AI Tutor compose deployment from ./backup.sh output.
#
#   ./restore.sh backups/aitutor-2026-08-12-1830.tar.gz
#
# THIS REPLACES THE CURRENT DATABASE AND MEDIA. Everything recorded since the
# backup was taken is lost — sessions, exit-ticket attempts, uploads. It asks
# before doing anything.
set -euo pipefail

cd "$(dirname "$0")"

ARCHIVE="${1:-}"
if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
  echo "usage: ./restore.sh <backup.tar.gz>" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
tar -C "$WORK" -xzf "$ARCHIVE"

for f in database.sql media.tar; do
  [ -f "$WORK/$f" ] || { echo "archive is missing $f — is this a backup.sh file?" >&2; exit 1; }
done

echo "About to restore:  $ARCHIVE"
echo "  taken from git:  $(cat "$WORK/git-sha.txt" 2>/dev/null || echo unknown)"
echo "  database dump:   $(du -h "$WORK/database.sql" | cut -f1)"
echo "  media archive:   $(du -h "$WORK/media.tar" | cut -f1)"
echo
echo "This REPLACES the current database and uploaded media."
printf 'Type "restore" to continue: '
read -r reply
[ "$reply" = "restore" ] || { echo "Aborted — nothing changed."; exit 1; }

echo "==> Stopping the app so nothing writes mid-restore"
# The database is left running; it is the thing being restored into. Stopping
# the app first means no request can insert a row that the dump then clobbers,
# leaving a foreign key pointing at nothing.
docker compose stop app

echo "==> Restoring the database"
docker compose exec -T db psql -U aitutor -d aitutor --quiet < "$WORK/database.sql"

echo "==> Restoring media"
docker compose start app
docker compose exec -T app sh -c 'rm -rf /app/media && mkdir -p /app/media'
docker compose exec -T app tar -C /app -xf - < "$WORK/media.tar"

echo "==> Restarting"
docker compose restart app

echo
echo "Restored. Verify before trusting it:"
echo "  1. curl -sf https://<your-domain>/health/"
echo "  2. Sign in and open a lesson that contains a figure — a broken image"
echo "     means the database and media came from different moments."
echo "  3. Take one tutoring turn. That exercises the database, the knowledge"
echo "     base and the LLM credentials in a single action."
