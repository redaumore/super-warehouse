.PHONY: install db-up db-down db-logs migrate migrate-new test run lint format typecheck

PY := .venv/bin/python

# Python / deps (idempotent: creates .venv + editable install only when missing)
.venv/bin/python:
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

install: .venv/bin/python

# Docker Postgres+pgvector
db-up:
	docker compose up -d db
db-down:
	docker compose down
db-logs:
	docker compose logs -f db

# Alembic migrations
migrate:
	$(PY) -m alembic upgrade head
migrate-new:
	$(PY) -m alembic revision --autogenerate -m "$(m)"

# Tests (unit + integration). DB tests require `make db-up`.
test:
	$(PY) -m pytest

# Runtime harness: boot the API for a manual ACK check.
run:
	$(PY) -m uvicorn src.api.webhook:app --reload

# Quality
lint:
	$(PY) -m ruff check src tests
format:
	$(PY) -m ruff format src tests
typecheck:
	$(PY) -m mypy src
