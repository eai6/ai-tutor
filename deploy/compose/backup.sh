#!/usr/bin/env bash
# Back up an AI Tutor compose deployment: database + uploaded media.
#
#   ./backup.sh                 # writes ./backups/aitutor-YYYY-MM-DD-HHMM.tar.gz
#   ./backup.sh /mnt/usb        # or somewhere else
#
# Run it from this directory. Restore with ./restore.sh <file>.
#
# Both halves matter and they must come from the same moment. The database
# holds the reference to every uploaded figure; the media volume holds the
# files. A database restored against older media shows broken images in
# lessons, and nothing reports an error.
set -euo pipefail

cd "$(dirname "$0")"

DEST="${1:-./backups}"
STAMP="$(date +%Y-%m-%d-%H%M)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$DEST"

echo "==> Dumping the database"
# --clean --if-exists so the restore can run against an existing database
# without a manual drop first.
docker compose exec -T db \
  pg_dump -U aitutor -d aitutor --clean --if-exists \
  > "$WORK/database.sql"

echo "==> Archiving uploaded media"
# Read straight out of the running container rather than reaching into the
# Docker volume on the host: the volume path differs by platform, and on
# Docker Desktop it is inside a VM you cannot reach at all.
docker compose exec -T app tar -C /app -cf - media > "$WORK/media.tar"

echo "==> Recording the version this came from"
docker compose exec -T app sh -c 'git rev-parse --short HEAD 2>/dev/null || echo unknown' \
  > "$WORK/git-sha.txt" 2>/dev/null || echo unknown > "$WORK/git-sha.txt"

OUT="$DEST/aitutor-$STAMP.tar.gz"
tar -C "$WORK" -czf "$OUT" database.sql media.tar git-sha.txt

echo
echo "Backup written: $OUT ($(du -h "$OUT" | cut -f1))"
echo
echo "This file contains STUDENT DATA. Store it with the same care as the"
echo "server itself: encrypted at rest, restricted access, and off this"
echo "machine — a backup that only exists on the server it backs up is not a"
echo "backup. Test a restore on a spare machine at least once; an untested"
echo "backup is a guess."
