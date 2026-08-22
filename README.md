# Super-Warehouse — Ferretería Multi-Agent MVP

WhatsApp-first order intake for a hardware store: customers text or send voice
notes, six domain agents turn them into quotes, the owner approves on WhatsApp,
and approved orders register to Google Sheets while stock deducts. A Gradio
backoffice handles catalog, clients, live orders and supplier-document
ingestion. Python 3.12+ · FastAPI · Postgres 16 + pgvector · OpenAI ·
Google Sheets · Gradio.

## Quick path

1. `make install` — creates `.venv` and installs the project (editable + dev deps).
2. `make db-up` — starts Postgres+pgvector in Docker.
3. Copy `.env.example` to `.env` and set at least `POSTGRES_PASSWORD`.
4. `make migrate` — applies the Alembic migration.
5. `make test` — runs the suite (223 tests: unit, integration, E2E).
6. `make run` — boots the intake API; `make backoffice` — boots the Gradio UI.

## Environment setup

| Variable | Purpose | Default |
|---|---|---|
| `POSTGRES_USER/PASSWORD/DB/HOST/PORT` | Local database | `ferreteria` / (set it) / `ferreteria` |
| `WEBHOOK_SECRET` | HMAC secret for webhook signatures | `change-me` |
| `RESERVATION_TTL_MINUTES` | Soft-lock expiry window | `30` |
| `WHATSAPP_TOKEN/PHONE_ID/VERIFY_TOKEN` | WhatsApp Cloud API | empty (no-op send) |
| `TELEGRAM_BOT_TOKEN` | Demo channel | empty (no-op send) |
| `OPENAI_API_KEY` | Whisper / GPT-4o Vision / embeddings | empty (client builds lazily) |
| `OPENAI_EMBEDDING_MODEL/DIMS` | Embedding model and dimension | `text-embedding-3-small` / `1536` |
| `GOOGLE_SHEETS_CREDENTIALS_FILE/SPREADSHEET_KEY` | gspread service account + sheet | empty (rows quarantine) |
| `FASE1..4_ENABLED` | Per-Fase feature flags (stop at boundary) | `true` |

External services (OpenAI, Google Sheets, WhatsApp) are **mocked in tests** —
no credentials or network needed. In dev, unset tokens make the adapters
no-ops (Telegram/WhatsApp) or quarantine (Sheets), and the OpenAI client
raises a clear error only when a real call is attempted.

## Commands

| Command | What it does |
|---|---|
| `make install` | Create venv + editable install |
| `make db-up` / `db-down` / `db-logs` | Docker Postgres+pgvector lifecycle |
| `make migrate` / `migrate-new m=...` | Alembic up / new revision |
| `make test` | Full pytest suite |
| `make run` | Uvicorn intake API (`:8000`) |
| `make backoffice` | Gradio backoffice (`:7860`) |
| `make lint` / `format` / `typecheck` | Ruff / Ruff format / mypy strict |
| `make test-docs` / `check-test-docs` | Regenerate / verify living test docs |

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — data flow, the six agents, state machine.
- [`docs/runbook.md`](docs/runbook.md) — day-to-day operations and failure modes.
- [`docs/escenarios-testeados.md`](docs/escenarios-testeados.md) — every tested behavior, generated from test docstrings (`make test-docs`).
- `openspec/changes/mvp-ferreteria/` — proposal, specs, design, tasks, apply progress.

## Checklist (first run)

- [ ] `docker compose ps` shows `super-warehouse-db` healthy
- [ ] `make migrate` completes
- [ ] `make test` → `223 passed`
- [ ] `make lint && make typecheck` → clean
- [ ] `curl localhost:8000/healthz` → `{"status":"ok"}`