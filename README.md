# Super-Warehouse — Ferretería Multi-Agent MVP

WhatsApp/Telegram order intake for a hardware store, owner-first: the OWNER is
the only chat actor. The owner texts or sends voice notes naming the customer,
agents turn them into quotes, the owner confirms in chat, and confirmed orders
register to Google Sheets while stock deducts. Every inbound message is gated
against the owner sender allowlist before routing; non-owner senders get a
polite rejection. A Gradio backoffice handles catalog, clients, live orders and
supplier-document ingestion. Python 3.12+ · FastAPI · Postgres 16 + pgvector ·
OpenAI · Google Sheets · Gradio.

## Quick path

1. `make install` — creates `.venv` and installs the project (editable + dev deps).
2. `make db-up` — starts Postgres+pgvector in Docker.
3. Copy `.env.example` to `.env`, set at least `POSTGRES_PASSWORD` and the owner
   sender keys (`OWNER_TELEGRAM_CHAT_ID` and/or `OWNER_WHATSAPP_PHONE`).
4. `make migrate` — applies the Alembic migration.
5. `make test` — runs the suite (unit, integration, E2E) against a disposable
   `ferreteria_test` database rebuilt from the Alembic migrations on every run;
   the dev database is never touched by the suite.
6. `make run` — boots the intake API; `make backoffice` — boots the Gradio UI.

## Environment setup

| Variable | Purpose | Default |
|---|---|---|
| `POSTGRES_USER/PASSWORD/DB/HOST/PORT` | Local database (tests derive a `_test` database from this URL) | `ferreteria` / (set it) / `ferreteria` |
| `WEBHOOK_SECRET` | HMAC secret for webhook signatures | `change-me` |
| `RESERVATION_TTL_MINUTES` | Soft-lock expiry window | `30` |
| `OWNER_TELEGRAM_CHAT_ID` | Owner Telegram chat id (gate + sender) | empty (gate open) |
| `OWNER_WHATSAPP_PHONE` | Owner WhatsApp phone (gate + sender) | empty (gate open) |
| `OWNER_PHONE` | **DEPRECATED** legacy owner push target — kept parseable, ignored | empty |
| `WHATSAPP_TOKEN/PHONE_ID/VERIFY_TOKEN` | WhatsApp Cloud API | empty (no-op send) |
| `TELEGRAM_BOT_TOKEN` | Demo channel | empty (no-op send) |
| `TELEGRAM_SECRET_TOKEN` | Telegram webhook auth (set at `setWebhook`) | empty (accept any webhook) |
| `OPENAI_API_KEY` | Whisper / GPT-4o Vision / embeddings | empty (client builds lazily) |
| `OPENAI_EMBEDDING_MODEL/DIMS` | Embedding model and dimension | `text-embedding-3-small` / `1536` |
| `GOOGLE_SHEETS_CREDENTIALS_FILE/SPREADSHEET_KEY` | gspread service account + sheet | empty (approvals roll back) |
| `FASE1..4_ENABLED` | Per-Fase feature flags (stop at boundary) | `true` |

External services (OpenAI, Google Sheets, WhatsApp) are **mocked in tests** —
no credentials or network needed. In dev, unset tokens make the adapters
no-ops (Telegram/WhatsApp); a missing Sheets configuration makes the approval
flow roll the order back (it stays pending) instead of half-registering it.

## Commands

| Command | What it does |
|---|---|
| `make install` | Create venv + editable install |
| `make db-up` / `db-down` / `db-logs` | Docker Postgres+pgvector lifecycle |
| `make migrate` / `migrate-new m=...` | Alembic up / new revision |
| `make test` | Full pytest suite on a disposable `ferreteria_test` database rebuilt from the Alembic migrations (the dev database is never touched) |
| `make run` | Uvicorn intake API (`:8000`) |
| `make backoffice` | Gradio backoffice (`:7860`) |
| `make lint` / `format` / `typecheck` | Ruff / Ruff format / mypy strict |
| `make test-docs` / `check-test-docs` | Regenerate / verify living test docs |

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — data flow, owner gate, the six agents, state machine.
- [`docs/sourcing.md`](docs/sourcing.md) — order sourcing workflow (owner sender → parse → resolve by name → Case A/B/C), PO lifecycle, searcher seam.
- [`docs/runbook.md`](docs/runbook.md) — day-to-day operations and failure modes.
- [`docs/escenarios-testeados.md`](docs/escenarios-testeados.md) — every tested behavior, generated from test docstrings (`make test-docs`).
- `openspec/changes/owner-order-intake/` — proposal, specs, design, tasks, apply progress.

## Module map

| Path | Responsibility |
|---|---|
| `src/agents/` | Customer (owner-assistant LLM chat + sourcing turn with name resolution), Customers (name matcher + create command), Disambiguation (SKU resolution), Inventory (availability + reservations), Dispatch (wired confirm/cancel), Sales (quotes), Perception (STT/vision), Intake (NL order parsing) |
| `src/orchestrator/` | Owner gate, router + session store (parse step, owner-keyed DB rehydration), approval orchestration |
| `src/sourcing/` | Case A/B/C flows: classification, persistence of `SourcingNeed`, multi-turn supplier selection |
| `src/purchasing/` | `SupplierPurchaseOrder` state machine + accumulation (one OPEN PO per supplier) |
| `src/supplier/` | OCR, barcode, and the `SupplierCatalogSearcher` seam (fake until the RAG exists) |
| `src/backoffice/` | Gradio app: catalog, clients, order monitor, purchase orders, supplier ingestion |

## Checklist (first run)

- [ ] `docker compose ps` shows `super-warehouse-db` healthy
- [ ] `make migrate` completes
- [ ] `make test` → full suite green (runs on `ferreteria_test`, dev data untouched)
- [ ] `make lint && make typecheck` → clean
- [ ] `curl localhost:8000/healthz` → `{"status":"ok"}`