.PHONY: bootstrap sync install lint format format-check test all

# Use uv if available (matches sibling repos); fall back to pip otherwise.
UV := $(shell command -v uv 2>/dev/null)

bootstrap: sync ## First-time setup: create venv + install dev deps

sync: ## Resolve + install deps from the lockfile (or refresh it)
ifeq ($(UV),)
	@echo "uv not found — install from https://docs.astral.sh/uv/getting-started/installation/"
	@exit 1
else
	uv sync --frozen || uv sync
endif

install: sync ## Alias for sync, kept for parity with agri-api/agri-db Makefiles

lint: ## ruff check
	uv run ruff check .

format: ## ruff format (writes)
	uv run ruff format .

format-check: ## ruff format --check (read-only)
	uv run ruff format --check .

test: ## Run the test suite
	uv run pytest -v

all: lint format-check test
