# Makefile — local dev environment for llms-text. See CLAUDE.md "Commands" and
# README.md "Run locally" for the human-readable walkthrough.
#
# Targeting GNU Make 3.81: no `.ONESHELL`, no `!=` shell assignment. Every recipe line
# runs in its own shell, so multi-step recipes use `\` line continuations + `&&`, or call
# out to scripts/.

SHELL := /bin/bash

PYTHON := python3.12
VENV_DIR := backend/.venv
VENV_BIN := $(CURDIR)/$(VENV_DIR)/bin
WORKER_MODULE ?= app.worker.settings.WorkerSettings

export VENV_DIR
export WORKER_MODULE

.PHONY: help setup dev migrate migrate-apply test lint down reset \
        check-python check-docker check-supabase check-venv check-db-deps

help: ## Show this help and exit
	@echo "Usage: make <target>"
	@echo ""
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*## "}; {printf "  %-16s %s\n", $$1, $$2}'

# --- prerequisite checks (internal; not listed in `make help`) ------------------------

check-python:
	@command -v $(PYTHON) >/dev/null 2>&1 || { \
		echo "make: '$(PYTHON)' not found on PATH." >&2; \
		echo "make: install Python 3.12+ (README.md > Run locally > Prerequisites) and re-run." >&2; \
		exit 1; \
	}

check-docker:
	@command -v docker >/dev/null 2>&1 || { \
		echo "make: 'docker' not found on PATH. Install Docker and re-run." >&2; \
		exit 1; \
	}
	@docker info >/dev/null 2>&1 || { \
		echo "make: the Docker daemon is not running. Start Docker Desktop and re-run." >&2; \
		exit 1; \
	}

check-supabase:
	@command -v supabase >/dev/null 2>&1 || { \
		echo "make: 'supabase' CLI not found on PATH." >&2; \
		echo "make: install it (pinned version: 2.111.0) — see README.md > Run locally > Prerequisites." >&2; \
		exit 1; \
	}

check-venv:
	@[ -x "$(VENV_BIN)/python" ] || { \
		echo "make: $(VENV_DIR) not found. Run 'make setup' first." >&2; \
		exit 1; \
	}

check-db-deps:
	@[ -d db/node_modules ] || { \
		echo "make: db/node_modules not found. Run 'make setup' first." >&2; \
		exit 1; \
	}

# --- user-facing targets ---------------------------------------------------------------

setup: check-python ## Create backend/.venv and install backend + db deps (frontend if present)
	$(PYTHON) -m venv $(VENV_DIR)
	$(VENV_BIN)/pip install --upgrade pip
	$(VENV_BIN)/pip install -r backend/requirements-dev.txt
	cd db && npm ci
	@if [ -f frontend/package.json ]; then \
		cd frontend && npm ci; \
	else \
		echo "frontend: skipped — frontend/ does not exist yet (lands with PER-147)"; \
	fi

dev: check-python check-docker check-supabase check-venv ## Run Supabase, Redis, the API, worker, and frontend
	bash scripts/dev.sh

migrate: check-supabase check-db-deps ## Create a migration from schema.prisma (review the SQL, then commit it)
	bash scripts/local-env.sh write-db-env
	cd db && npm run migrate:dev

migrate-apply: check-supabase check-db-deps ## Apply pending migrations to the local database
	bash scripts/local-env.sh write-db-env
	cd db && npm run migrate:apply

test: check-python check-venv ## Run the backend test suite (frontend tests land with PER-147)
	cd backend && $(VENV_BIN)/pytest
	@if [ -f frontend/package.json ]; then \
		cd frontend && npm run --if-present test; \
	else \
		echo "frontend: skipped — frontend/ does not exist yet (lands with PER-147)"; \
	fi

lint: check-python check-venv ## Run ruff + mypy on backend/ (frontend lint lands with PER-147)
	$(VENV_BIN)/ruff check backend
	$(VENV_BIN)/ruff format --check backend
	cd backend && $(VENV_BIN)/mypy app
	@if [ -f frontend/package.json ]; then \
		cd frontend && npm run --if-present lint && npm run --if-present typecheck; \
	else \
		echo "frontend: skipped — frontend/ does not exist yet (lands with PER-147)"; \
	fi

down: check-docker ## Stop Supabase and Redis containers
	supabase stop
	docker compose down

reset: check-supabase check-db-deps ## Recreate the local DB, reseed it, and replay Prisma migrations
	supabase db reset --local
	docker compose up -d --force-recreate --wait redis
	bash scripts/local-env.sh write-db-env
	cd db && npm run migrate:deploy
