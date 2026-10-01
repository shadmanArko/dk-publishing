DAGSTER_HOME := $(CURDIR)/.dagster_home

.PHONY: help install lint format typecheck arch test check
help:  ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'
install:  ## create venv and install locked deps
	uv sync
lint:  ## ruff
	uv run ruff check . && uv run ruff format --check .
format:  ## auto-format
	uv run ruff check --fix . && uv run ruff format .
typecheck:  ## mypy --strict
	uv run mypy
arch:  ## import-linter layer contract
	uv run lint-imports
test:  ## pytest with coverage gate
	uv run pytest --cov --cov-report=term-missing
check: lint typecheck arch test  ## everything CI runs

.PHONY: db-up db-down migrate dagster-dev seed-rehearsal check-google
db-up:  ## start the local dev Postgres (Docker) on :5433
	docker compose -f compose.dev.yaml up -d --wait
db-down:  ## stop it (data kept)
	docker compose -f compose.dev.yaml down
migrate:  ## apply migrations to $$DATABASE_URL
	uv run --env-file .env dk migrate
dagster-dev:  ## Dagster UI + daemon on :3000 (dry-run); needs DATABASE_URL
	mkdir -p $(DAGSTER_HOME) && cp dagster/dagster.dev.yaml $(DAGSTER_HOME)/dagster.yaml
	DAGSTER_HOME=$(DAGSTER_HOME) uv run dagster dev -w dagster/workspace.yaml
seed-rehearsal:  ## create approved dry-run posts a few minutes out
	uv run --env-file .env dk seed-rehearsal
check-google:  ## test the Google service account, Sheet and Drive folder
	uv run --env-file .env dk check-google
