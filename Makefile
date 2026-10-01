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
