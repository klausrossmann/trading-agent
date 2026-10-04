#!/bin/sh
# Read-only role for the dashboard (IMPLEMENTATION.md 4.2). Runs on the first start with an
# empty data volume; idempotent, so `make dashboard-role` applies it to an existing database.
set -eu

db="${POSTGRES_DB:-trading}"
psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-postgres}" --dbname "$db" \
  -v pw="$(cat /run/secrets/dashboard_db_password)" -v db="$db" <<'EOSQL'
SELECT 'CREATE ROLE dashboard' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dashboard')
\gexec
ALTER ROLE dashboard WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD :'pw';
ALTER ROLE dashboard SET default_transaction_read_only = on;
ALTER ROLE dashboard SET statement_timeout = '30s';
GRANT CONNECT ON DATABASE :"db" TO dashboard;
GRANT USAGE ON SCHEMA public TO dashboard;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dashboard;
-- Tables that later migrations create (as agent) become readable too.
ALTER DEFAULT PRIVILEGES FOR ROLE agent IN SCHEMA public GRANT SELECT ON TABLES TO dashboard;
EOSQL
