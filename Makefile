.PHONY: install db-up db-down db-logs migrate migrate-new test test-integration run lint format

# Python / deps
install:
	poetry install --with dev

# Docker Postgres+pgvector
db-up:
	docker compose up -d db
db-down:
	docker compose down
db-logs:
	docker compose logs -f db

# Alembic migrations
migrate:
	poetry run alembic upgrade head
migrate-new:
	poetry run alembic revision --autogenerate -m "$(m)"

# Tests (unit + integration). Integration tests require `make db-up`.
test:
	poetry run pytest -x

# Runtime harness: boot the API for a manual ACK check.
run:
	poetry run uvicorn src.api.webhook:app --reload

# Quality
lint:
	poetry run ruff check src tests
format:
	poetry run ruff format src tests
