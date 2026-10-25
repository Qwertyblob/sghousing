# sghousing task runner. Requires GNU Make (3.81+), uv and Docker.
# Run `make` or `make help` to list targets.

SHELL := /bin/bash
COMPOSE ?= docker compose
PSQL := $(COMPOSE) exec -T -e PGOPTIONS='-c client_min_messages=warning' db \
	psql -U sg -d sg_housing -v ON_ERROR_STOP=1 -q

# Pipeline stages must never run concurrently (e.g. `make -j all`).
.NOTPARALLEL:
.DEFAULT_GOAL := help

.PHONY: help setup db-up db-down db-shell db-reset db-check test test-db lint \
	ingest transform geocode features check train evaluate export \
	site-dev site-build publish snapshot restore-snapshot all clean

help: ## List all targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-17s\033[0m %s\n", $$1, $$2}'

# --- Setup -------------------------------------------------------------------

setup: ## Install Python 3.12 + locked deps, create .env, install git hooks
	@command -v uv >/dev/null || { echo "uv not found; install it: https://docs.astral.sh/uv/"; exit 1; }
	@command -v docker >/dev/null || { echo "docker not found; install Docker Desktop"; exit 1; }
	uv python install 3.12
	uv sync --locked
	@test -f .env || { cp .env.example .env && echo "Created .env from .env.example"; }
	uv run pre-commit install
	@if [ -f site/package.json ]; then cd site && npm ci; fi

db-up: ## Start Postgres/PostGIS, wait until healthy, create schemas
	$(COMPOSE) up -d --wait
	$(PSQL) -f - < pipeline/sql/000_schemas.sql
	@echo "Database ready."

db-down: ## Stop the database (keeps data volume)
	$(COMPOSE) down

db-shell: ## Open psql inside the database container
	$(COMPOSE) exec db psql -U sg -d sg_housing

db-reset: ## DESTROY the database volume and recreate it empty
	$(COMPOSE) down -v
	$(MAKE) db-up

db-check: ## Connect from Python via DATABASE_URL; verify PostGIS and schemas
	uv run python -m pipeline.common.db

test: ## Run unit tests (no database needed)
	uv run pytest

test-db: ## Run database integration tests (needs `make db-up`)
	uv run pytest -m db

lint: ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

# --- Pipeline (stubs fail until their phase is implemented) -------------------

define not_implemented
	@echo "make $@: not implemented yet (Phase $(1))" >&2; exit 1
endef

ingest: ## Download data.gov.sg datasets into data/raw and load raw tables
	uv run python -m pipeline.ingest

transform: ## Run SQL staging transforms (raw -> staging)
	uv run python -m pipeline.common.sql_runner '0*.sql' '1*.sql'

geocode: ## Geocode addresses via OneMap (needs ONEMAP_EMAIL/ONEMAP_PASSWORD)
	uv run python -m pipeline.geocode

features: ## Build marts.block_features and marts.resale_features (PostGIS SQL)
	uv run python -m pipeline.common.sql_runner '2*.sql'

check: ## Data quality checks + report (Phase 5)
	$(call not_implemented,5)

train: ## Train baselines and LightGBM models (Phase 7)
	$(call not_implemented,7)

evaluate: ## Evaluate models, write metrics and figures (Phase 7)
	$(call not_implemented,7)

export: ## Export site data and ONNX models (Phase 8)
	$(call not_implemented,8)

site-dev: ## Serve the site locally (Phase 9)
	$(call not_implemented,9)

site-build: ## Build the static site into site/dist (Phase 9)
	$(call not_implemented,9)

publish: ## Export, build and push site/dist to gh-pages (Phase 10)
	$(call not_implemented,10)

snapshot: ## Export marts tables to Parquet (Phase 10)
	$(call not_implemented,10)

restore-snapshot: ## Load a Parquet snapshot into Postgres (Phase 10)
	$(call not_implemented,10)

# --- Shortcuts ---------------------------------------------------------------

all: ingest transform geocode features check train evaluate export ## Full pipeline, ingest through export

clean: ## Remove caches and build output (keeps data/ and the DB volume)
	rm -rf .pytest_cache .ruff_cache site/dist
	find . -path ./.venv -prune -o -path ./site/node_modules -prune -o \
		-type d -name __pycache__ -exec rm -rf {} +
