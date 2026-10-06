.PHONY: sync lint fmt test test-db secrets tws-password trace-headers dashboard-role build migrate up backup restore-check down logs deploy dev-up dev-backfill dev-logs dev-down

ZENBOOK ?= zenbook
# Checkout on the Zenbook, relative to trader's home
ZENBOOK_DIR ?= projects/trading-agent
TEST_DB := ta-test-db
# Risk engine and execution need 100 % branch coverage (IMPLEMENTATION.md 16.2).
RISK_COV := --cov=trading_agent.risk --cov=trading_agent.execution --cov-branch --cov-report=term-missing:skip-covered --cov-fail-under=100

sync:
	uv sync

lint:
	uv run ruff check
	uv run ruff format --check
	uv run pyright
	uv run lint-imports

fmt:
	uv run ruff check --fix
	uv run ruff format

test:
	uv run pytest -m "not ibkr and not llm" $(RISK_COV)

# Throwaway Postgres on 127.0.0.1:55432; never touches the real database.
test-db:
	docker run --rm -d --name $(TEST_DB) -p 127.0.0.1:55432:5432 \
	  -e POSTGRES_USER=agent -e POSTGRES_PASSWORD=test -e POSTGRES_DB=trading_test postgres:17 >/dev/null
	@until docker exec $(TEST_DB) pg_isready -h 127.0.0.1 -U agent -d trading_test >/dev/null 2>&1; do sleep 1; done
	DB_HOST=localhost DB_PORT=55432 DB_NAME=trading_test DB_PASSWORD=test uv run pytest -m db; \
	  rc=$$?; docker rm -f $(TEST_DB) >/dev/null; exit $$rc

# Creates missing secret files. Dir 0700 keeps other host users out; files 0644 so the
# container users (postgres uid 999, agent uid 10001) can read their bind mounts.
secrets: secrets/trace_headers
	@mkdir -p secrets && chmod 700 secrets
	@for s in postgres_password agent_db_password dashboard_db_password vnc_password; do \
	  if [ ! -f secrets/$$s ]; then openssl rand -hex 24 > secrets/$$s; echo "created secrets/$$s"; fi; \
	  chmod 644 secrets/$$s; \
	done

# Optional Langfuse auth for the agent; compose needs the file, so an empty one is created.
secrets/trace_headers:
	@mkdir -p secrets && chmod 700 secrets
	@: > $@ && chmod 644 $@ && echo "created $@ (empty: no tracing auth)"

# Langfuse API keys -> OTLP auth header in secrets/trace_headers (OBSERVABILITY.md); typed, not echoed.
trace-headers:
	@mkdir -p secrets && chmod 700 secrets
	@printf 'Langfuse public key (pk-lf-...): ' && IFS= read -r pk && \
	  printf 'Langfuse secret key (sk-lf-...): ' && { stty -echo 2>/dev/null; IFS= read -r sk; stty echo 2>/dev/null; echo; } && \
	  printf 'Authorization=Basic %s,x-langfuse-ingestion-version=4' "$$(printf '%s:%s' "$$pk" "$$sk" | base64 | tr -d '\n')" \
	    > secrets/trace_headers && chmod 644 secrets/trace_headers && \
	  echo "stored secrets/trace_headers"

# The IBKR paper password is typed, never generated or echoed (IMPLEMENTATION.md 15.5).
tws-password:
	@mkdir -p secrets && chmod 700 secrets
	@printf 'IBKR paper password: ' && stty -echo && IFS= read -r pw && stty echo && echo && \
	  printf '%s' "$$pw" > secrets/tws_password && chmod 644 secrets/tws_password && \
	  echo "stored secrets/tws_password"

# Creates or updates the read-only dashboard role on an existing database (a fresh volume
# gets it from docker/db/init). Recreates db first so it mounts the dashboard secret.
dashboard-role:
	docker compose up -d --wait db
	docker compose exec db sh /docker-entrypoint-initdb.d/20-dashboard-role.sh

build: secrets/trace_headers
	docker compose build

migrate: secrets/trace_headers
	docker compose run --rm agent alembic upgrade head

up: secrets/trace_headers
	docker compose up -d

backup:
	scripts/backup.sh

# FILE=backups/trading-....dump (or .dump.age): restore into a scratch database and drop it
restore-check:
	scripts/restore.sh check $(FILE)

down:
	docker compose down

logs:
	docker compose logs -f agent

deploy:
	ssh $(ZENBOOK) 'cd $(ZENBOOK_DIR) && git pull --ff-only && make secrets/trace_headers && docker compose build && docker compose run --rm agent alembic upgrade head && docker compose up -d'

# Local stack with the offline fake LLM and throwaway secrets (compose.dev.yaml).
DEV := docker compose -f compose.yaml -f compose.dev.yaml

dev-up:
	@mkdir -p .dev/secrets && chmod 700 .dev/secrets
	@for s in postgres_password agent_db_password dashboard_db_password tws_password vnc_password; do \
	  [ -f .dev/secrets/$$s ] || openssl rand -hex 24 > .dev/secrets/$$s; chmod 644 .dev/secrets/$$s; \
	done
	@[ -f .dev/secrets/trace_headers ] || : > .dev/secrets/trace_headers; chmod 644 .dev/secrets/trace_headers
	$(DEV) build
	$(DEV) up -d --wait db
	$(DEV) run --rm agent alembic upgrade head
	$(DEV) up -d agent dashboard
	@echo "dashboard http://127.0.0.1:$${DEV_DASHBOARD_PORT:-8502}, db 127.0.0.1:$${DEV_DB_PORT:-55433} (password in .dev/secrets/agent_db_password)"

# Yahoo, ECB and earnings history into the dev database (no FRED key: macro is skipped).
dev-backfill:
	$(DEV) run --rm agent trading-agent backfill

dev-logs:
	$(DEV) logs -f agent

# Removes the dev containers, their volume and the throwaway secrets.
dev-down:
	$(DEV) down -v
	rm -rf .dev
