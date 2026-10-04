.PHONY: sync lint fmt test test-db secrets dashboard-role build migrate up down logs deploy

ZENBOOK ?= zenbook
TEST_DB := ta-test-db

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
	uv run pytest -m "not ibkr and not llm"

# Throwaway Postgres on 127.0.0.1:55432; never touches the real database.
test-db:
	docker run --rm -d --name $(TEST_DB) -p 127.0.0.1:55432:5432 \
	  -e POSTGRES_USER=agent -e POSTGRES_PASSWORD=test -e POSTGRES_DB=trading_test postgres:17 >/dev/null
	@until docker exec $(TEST_DB) pg_isready -h 127.0.0.1 -U agent -d trading_test >/dev/null 2>&1; do sleep 1; done
	DB_HOST=localhost DB_PORT=55432 DB_NAME=trading_test DB_PASSWORD=test uv run pytest -m db; \
	  rc=$$?; docker rm -f $(TEST_DB) >/dev/null; exit $$rc

# Creates missing secret files. Dir 0700 keeps other host users out; files 0644 so the
# container users (postgres uid 999, agent uid 10001) can read their bind mounts.
secrets:
	@mkdir -p secrets && chmod 700 secrets
	@for s in postgres_password agent_db_password dashboard_db_password; do \
	  if [ ! -f secrets/$$s ]; then openssl rand -hex 24 > secrets/$$s; echo "created secrets/$$s"; fi; \
	  chmod 644 secrets/$$s; \
	done

# Creates or updates the read-only dashboard role on an existing database (a fresh volume
# gets it from docker/db/init). Recreates db first so it mounts the dashboard secret.
dashboard-role:
	docker compose up -d --wait db
	docker compose exec db sh /docker-entrypoint-initdb.d/20-dashboard-role.sh

build:
	docker compose build

migrate:
	docker compose run --rm agent alembic upgrade head

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f agent

deploy:
	ssh $(ZENBOOK) 'cd ~/trading-agent && git pull --ff-only && docker compose build && docker compose run --rm agent alembic upgrade head && docker compose up -d'
