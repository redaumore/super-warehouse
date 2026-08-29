# Runbook — day-to-day operations

What to do when the system is up, what to check, and what each failure mode
means. Start with the **Daily flow**, then consult the tables.

## Daily flow

1. `make db-up` — Postgres+pgvector must be healthy (`docker compose ps`).
2. `make migrate` — apply any pending Alembic revisions.
3. `make run` — intake API on `:8000`; `make backoffice` — UI on `:7860`.
4. `curl localhost:8000/healthz` → `{"status":"ok"}`.
5. `make test` — sanity-check the suite before a deploy.

## Verification

| Check | Command | Expected |
|---|---|---|
| API alive | `curl localhost:8000/healthz` | `{"status":"ok"}` |
| Webhook auth | signed POST to `/webhook/telegram` | `200 ACK` |
| Webhook auth (bad) | unsigned POST | `401 invalid signature` |
| Docs in sync | `make check-test-docs` | "está al día" |
| Quality gates | `make lint && make typecheck` | clean |
| Coverage gate | `pytest --cov=src --cov-fail-under=85` | ≥ 85% |

## Failure modes

| Symptom | Cause | Action |
|---|---|---|
| `whatsapp send failed: ...` in logs | Graph API down / token invalid | Check `WHATSAPP_TOKEN`; retry; the owner still sees the quote via Telegram |
| Order confirmed but "cuarentena" in the message | Google Sheets not configured or failing | Check `GOOGLE_SHEETS_CREDENTIALS_FILE` + key; rows are in the quarantine sheet or in the writer's memory log; re-append manually |
| `openai api key not configured` | `OPENAI_API_KEY` unset | Set it; the client builds lazily so the app boots anyway |
| `could not decode barcode image` | blurry photo, or `zbar` library missing | Retake the photo; on macOS `brew install zbar` |
| `illegible` on ingestion | document unreadable by Vision | Route to manual entry (spec: out of MVP scope) |
| `fase N is disabled` | `FASE_N_ENABLED=false` | Re-enable the flag to run that phase's features |
| `RequiresRequoteError` on approval | order reservations expired | Re-quote the order before approving (by design) |
| Tests skip (`Postgres not running`) | DB down | `make db-up` |
| Order shows `CANCELLED` / sourcing cancelado | missing items had no supplier candidate | Expected Case C until the supplier RAG is wired; check the searcher seam (`src/supplier/searcher.py`) |

## Order sourcing

The sourcing workflow (see `docs/sourcing.md`) is enabled by setting
`OWNER_PHONE` in `.env`:

| Action | Command / setting |
|---|---|
| Enable sourcing flow | `OWNER_PHONE=+54911...` in `.env`; notifications go over Telegram |
| Disable (legacy intake) | remove `OWNER_PHONE` (parse step turns off) |
| Backfill inventory | `python3 scripts/seed_inventory.py` (idempotent; the migration already backfills from `catalogo.stock_disponible`) |
| Verify availability source | `SELECT sku_id, quantity_on_hand FROM inventory;` — this is the canonical on-hand |
| Execute a PO | backoffice → **Purchase Orders** tab: send / receive (partial or full) / cancel |

Rollback of the sourcing axis is a plain downgrade of the two additive
migrations:

```bash
make migrate   # current head = 5f304e18a765 (supplier purchase orders)
# to revert: .venv/bin/alembic downgrade -1  (drops PO tables + sourcing_needs)
#            .venv/bin/alembic downgrade -1  (drops inventory + order sourcing columns)
```

Both downgrades keep `OrderEstado` and every legacy table untouched.

## Backup

```bash
docker compose exec db pg_dump -U ferreteria ferreteria > backup_$(date +%F).sql
```

Restore: `docker compose exec -T db psql -U ferreteria ferreteria < backup.sql`.

## Deploying the branch

Local merges to `main` with periodic backup push (solo-dev policy):

```bash
git checkout main && git merge feat/mvp-ferreteria-pr4 --no-ff
git push origin main   # periodic backup, not per-PR
```

Never open a remote PR; the branch is verified before merge (`make test`,
`make lint`, `make typecheck`, `make check-test-docs`).

## Adding a test

1. Write the test with a **Spanish docstring as its first line** (this feeds the living docs).
2. Mock external boundaries (OpenAI, Sheets, WhatsApp) — never hit the network.
3. DB tests: reuse the `db_session` fixture and guard with the `skipif` Postgres check.
4. `make test-docs` to regenerate `docs/escenarios-testeados.md`.
5. `make check-test-docs` must pass before committing (pre-commit hook enforces it).