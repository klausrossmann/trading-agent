#!/usr/bin/env bash
# Nightly database backup (IMPLEMENTATION.md 15.4). Run by cron as the deploy user, e.g.
#   15 3 * * * cd ~/trading-agent && scripts/backup.sh >> backups/backup.log 2>&1
# Keeps 14 days of dumps in backups/. On Sundays (or with OFFSITE=1) it also writes an
# age-encrypted copy to backups/offsite/ for your sync tool, keeping the last 8.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=${COMPOSE:-docker compose}
DIR=backups
OFFSITE_DIR=$DIR/offsite
KEEP_DAYS=14
KEEP_OFFSITE=8

env_value() {  # reads KEY=value from .env without sourcing it
  [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -e 's/[[:space:]]*#.*$//' -e 's/^"//' -e 's/"$//' || true
}

mkdir -p "$OFFSITE_DIR"
chmod 700 "$DIR"
umask 077
stamp=$(date +%Y%m%d-%H%M)
file="$DIR/trading-$stamp.dump"
trap 'rm -f "$file.part"' EXIT

$COMPOSE exec -T db pg_dump -U postgres -d trading -Fc > "$file.part"
$COMPOSE exec -T db pg_restore --list < "$file.part" > /dev/null  # readable, not truncated
mv "$file.part" "$file"
find "$DIR" -maxdepth 1 -name 'trading-*.dump' -mtime +"$KEEP_DAYS" -delete

if [ "$(date +%u)" = 7 ] || [ "${OFFSITE:-0}" = 1 ]; then
  recipient=$(env_value BACKUP_AGE_RECIPIENT)
  if [ -z "$recipient" ]; then
    echo "BACKUP_AGE_RECIPIENT not set in .env: no offsite copy" >&2
  else
    age -r "$recipient" -o "$OFFSITE_DIR/trading-$stamp.dump.age" "$file"
    ls -1t "$OFFSITE_DIR"/trading-*.dump.age | tail -n +$((KEEP_OFFSITE + 1)) | xargs -r rm --
  fi
fi

heartbeat=$(env_value BACKUP_HEARTBEAT_URL)
if [ -n "$heartbeat" ]; then
  curl -fsS -m 10 --retry 3 "$heartbeat" > /dev/null || echo "heartbeat ping failed" >&2
fi
echo "$(date '+%Y-%m-%dT%H:%M:%S%z') backup ok: $file ($(du -h "$file" | cut -f1))"
