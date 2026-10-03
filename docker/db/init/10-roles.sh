#!/bin/sh
# Runs once, on the first start with an empty data volume.
# The agent role owns the database; the superuser password is only used for maintenance.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v agent_pw="$(cat /run/secrets/agent_db_password)" -v db="$POSTGRES_DB" <<'EOSQL'
CREATE ROLE agent LOGIN PASSWORD :'agent_pw';
ALTER DATABASE :"db" OWNER TO agent;
EOSQL
