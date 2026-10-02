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

.PHONY: db-up db-down migrate dagster-dev seed-rehearsal check-google sheet-init sheet-init-dry meta-init meta-check meta-page-token meta-refresh connect-youtube check-youtube telegram-init telegram-chat telegram-check telegram-test alerts-digest init check-setup deploy deploy-secrets
db-up:  ## start the local dev Postgres (Docker) on :5433
	docker compose -f compose.dev.yaml up -d --wait
db-down:  ## stop it (data kept)
	docker compose -f compose.dev.yaml down
migrate:  ## apply migrations to $$DATABASE_URL
	uv run --env-file .env dk migrate
dagster-dev:  ## Dagster UI + daemon on :3000 (dry-run); needs DATABASE_URL
	mkdir -p "$(DAGSTER_HOME)" && cp dagster/dagster.dev.yaml "$(DAGSTER_HOME)/dagster.yaml"
	DAGSTER_HOME="$(DAGSTER_HOME)" uv run dagster dev -w dagster/workspace.yaml
seed-rehearsal:  ## create approved dry-run posts a few minutes out
	uv run --env-file .env dk seed-rehearsal
check-google:  ## test the Google service account, Sheet and Drive folder
	uv run --env-file .env dk check-google
sheet-init-dry:  ## show what `sheet-init` would change; writes nothing
	uv run --env-file .env dk sheet init --dry-run
sheet-init:  ## create/repair the Google Sheet layout (never overwrites data)
	uv run --env-file .env dk sheet init
meta-init:  ## create the one-file Meta credentials template (no secrets in it)
	uv run --env-file .env dk meta init
meta-check:  ## test the Meta tokens without posting anything
	uv run --env-file .env dk meta check
meta-page-token:  ## swap the user token in meta.json for the Page's own token
	uv run --env-file .env dk meta page-token
meta-refresh:  ## renew the tokens that expire
	uv run --env-file .env dk meta refresh
connect-youtube:  ## one-time YouTube login (opens your browser)
	uv run --env-file .env dk connect youtube
check-youtube:  ## test the saved YouTube login
	uv run --env-file .env dk connect youtube --check
telegram-init:  ## create the Telegram credentials file and show the steps
	uv run --env-file .env dk telegram init
telegram-chat:  ## find your chat id after messaging the bot
	uv run --env-file .env dk telegram chat
telegram-check:  ## test the bot token
	uv run --env-file .env dk telegram check
telegram-test:  ## send yourself a test message
	uv run --env-file .env dk telegram test
alerts-digest:  ## send today's digest now
	uv run --env-file .env dk alerts digest
init:  ## create dk.json, the one file for every id, key and token
	uv run --env-file .env dk init
check-setup:  ## test everything in dk.json (posts nothing)
	uv run --env-file .env dk check-setup
deploy:  ## ship the code to the server (details from deploy/server.conf)
	deploy/deploy.sh
deploy-secrets:  ## ship the code AND your dk.json + Google key (after changing a token or key)
	deploy/deploy.sh --secrets
