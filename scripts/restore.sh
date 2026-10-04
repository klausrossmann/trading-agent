#!/usr/bin/env bash
# Restore a backup (IMPLEMENTATION.md 15.4).
#   scripts/restore.sh check FILE   restore into a scratch database, print row counts, drop it
#   scripts/restore.sh into FILE    replace the `trading` database (agent and dashboard stopped)
# FILE may be a .dump or an age-encrypted .dump.age (key: AGE_KEY, default ~/.config/age/key.txt).
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=${COMPOSE:-docker compose}
mode=${1:-}
file=${2:-}
if [ -z "$mode" ] || [ ! -f "$file" ]; then
  echo "usage: scripts/restore.sh check|into FILE" >&2
  exit 2
fi

dump=$file
if [[ $file == *.age ]]; then
  dump=$(mktemp)
  trap 'rm -f "$dump"' EXIT
  age -d -i "${AGE_KEY:-$HOME/.config/age/key.txt}" -o "$dump" "$file"
fi

psql() { $COMPOSE exec -T db psql -U postgres -v ON_ERROR_STOP=1 "$@"; }

counts() {
  psql -d "$1" -At -F ' ' -c "
    select 'alembic', version_num from alembic_version
    union all select 'instruments', count(*)::text from instruments
    union all select 'bars_daily', count(*)::text from bars_daily
    union all select 'proposals', count(*)::text from proposals
    union all select 'brackets', count(*)::text from brackets
    union all select 'trades', count(*)::text from trades
    union all select 'audit_log', count(*)::text from audit_log"
}

case $mode in
  check)
    db=restore_check
    psql -d postgres -c "drop database if exists $db" -c "create database $db" > /dev/null
    $COMPOSE exec -T db pg_restore -U postgres -d "$db" --no-owner --no-acl --exit-on-error < "$dump"
    counts "$db"
    psql -d postgres -c "drop database $db" > /dev/null
    echo "restore check ok: $file"
    ;;
  into)
    if [ -n "$($COMPOSE ps --status running --services | grep -E '^(agent|dashboard)$' || true)" ]; then
      echo "stop the agent and the dashboard first: docker compose stop agent dashboard" >&2
      exit 1
    fi
    read -r -p "Replace the database 'trading' with $file? Type 'restore': " answer
    [ "$answer" = restore ] || { echo "aborted"; exit 1; }
    psql -d postgres -c "drop database trading with (force)" -c "create database trading" \
      -c "alter database trading owner to agent" \
      -c "grant connect on database trading to dashboard" > /dev/null
    # roles exist from docker/db/init; table owners, grants and default privileges come from the dump
    $COMPOSE exec -T db pg_restore -U postgres -d trading --exit-on-error < "$dump"
    counts trading
    echo "restored into 'trading'; start with: docker compose up -d"
    ;;
  *)
    echo "usage: scripts/restore.sh check|into FILE" >&2
    exit 2
    ;;
esac
