# Apply Progress: mvp-ferreteria — PR4 (final slice: Integrations + tests + docs)

**Change**: mvp-ferreteria
**PR**: `feat/mvp-ferreteria-pr4` (final slice — single local merge, maintainer-approved `size:exception`; NOT pushed/PR'd)
**Mode**: Standard (strict_tdd: false, pytest runner present)
**Persistence**: hybrid (this file + Engram `sdd/mvp-ferreteria/apply-progress`)
**Batch**: PR4 — tasks 3.1–3.9, 4.1, 4.2, 4.5, 4.6, 4.7, 4.8, 5.1–5.4

## Completed Tasks (cumulative)

### PR1 (previous batch — persisted for continuity)
- [x] 1.1–1.7 Phase 1 foundation: pyproject/docker-compose/env/Makefile, ORM models (all entities, `vector(1536)`), session + Alembic, channel ABC + Telegram, config Settings, webhook skeleton + signature + ACK <5s, conftest + RED models tests.

### PR2 (previous batch — persisted for continuity)
- [x] 2.1 pricing engine (pure `compute_base`/`compute_final`, HALF_UP)
- [x] 2.3 customer agent (phone normalize + known/unknown/invalid)
- [x] 2.4 disambiguation agent (pgvector + rapidfuzz hybrid search)
- [x] 2.5 inventory agent (soft-lock, `avail = stock − Σ active unexpired`)

### PR3 (previous batch — persisted for continuity)
- [x] 2.2 perception protocols, 2.6 sales quote/adjustments, 2.7 dispatch notify/decide, 2.8 orchestrator router+session, 2.9 order state machine (+`Order.customer` relationship), 2.10 APScheduler sweeper, 2.11 RED tests (TTL release, reject release, stale approve refused).
- [x] 4.3 test_state_machine, 4.4 test_inventory RED (unit + Postgres integration).

### PR4 (this batch)
- [x] **3.1** `src/channels/whatsapp.py` — Cloud API adapter on the shared `Channel` ABC: payload normalization (text/voice/image/document with media_id), verify-token auth (hub.*) + endpoint-HMAC deferral, Graph API `send_text` + `fetch_media` (id→url→bytes). Registered in `webhook.CHANNELS`. Tests: `tests/test_whatsapp.py` (11, mocked httpx).
- [x] **3.2** `src/integrations/openai.py` — `OpenAITranscriber` (whisper verbose_json: segment logprobs → confidence [0,1], low-confidence fragments flagged), `OpenAIVisionAnalyzer` (gpt-4o; confidence from finish_reason), `OpenAIEmbedder` (fixed dims, input order preserved). Client lazy-built on first use (`OpenAINotConfiguredError` otherwise — openai 3.x raises at construction). Tests: `tests/test_openai.py` (9, mocked SDK).
- [x] **3.3** `src/integrations/sheets.py` — append-only `SheetsWriter`; any failure quarantines (quarantine sheet, else in-memory log) and NEVER raises into the order flow; `sheets_synced(order_id)` for the monitor. `GOOGLE_SHEETS_*` settings + `.env.example`. Tests: `tests/test_sheets.py` (5, mocked gspread; gspread 6.x typed `ValueInputOption` enum).
- [x] **3.4** `src/orchestrator/approval.py` — `approve_and_register` (lifecycle approve — refuses stale via `RequiresRequoteError` — then register) and `register_approved_order` (registration half for adjustment-approvals: ACTIVE→CONVERTED, Sheets row, stock deduction per reservation, owner confirmation mentioning quarantine when Sheets failed). Exported from orchestrator package. Tests: `tests/test_approval.py` (3 unit + 4 Postgres).
- [x] **3.5** `src/backoffice/app.py` — Gradio Blocks, 4 tabs (Catalog, Clients, Orders/Monitor, Ingestion); handlers over `SessionLocal`; `launch()` guarded; `make backoffice` target added. Tests: `tests/test_backoffice.py` structure tests (tab labels/components, no server).
- [x] **3.6** `src/backoffice/ingestion.py` — upload → Vision analyze (via supplier OCR) → editable preview grid (`to_grid_rows`) → `confirm_items` (existing SKU: +stock +cost +base recompute; unknown: create product with supplier margin via pricing engine). Tests: extraction/preview/confirm (unit + Postgres).
- [x] **3.7** `src/backoffice/{catalog,clients,monitor}.py` — catalog grid + stock/price/margin edits (margin recomputes base via pricing engine); clients list/create (phone normalization)/update; monitor (state, soft-lock counts, Sheets sync). Tests in `tests/test_backoffice.py` (Postgres).
- [x] **3.8** `src/barcode/decoder.py` — pyzbar `decode_image` (PIL), `lookup_barcode` → SINGLE / DUPLICATE (flagged for owner, never guessed) / UNKNOWN; `BarcodeDecodeError` on unreadable. Host needs `brew install zbar`. Tests: `tests/test_barcode.py` (3 unit + 3 Postgres).
- [x] **3.9** `src/supplier/ocr.py` — `extract_document` (VisionAnalyzer + deterministic line parser), `IllegibleDocumentError` on unusable extraction (never writes), partial lines flagged (`unparsed_lines`), price-list parse + `ingest_price_list_rows` (map existing SKU by code/name, suggest new; mappings upserted, never duplicated). Tests: `tests/test_ocr.py` (6 unit + 5 Postgres).
- [x] **4.1** `test_pricing` VERIFY — delivered in PR2; confirmed base (925.92×1.35), 0-margin, only-list, only-particular, both (720), multiplicative-vs-additive, HALF_UP cases; 100% coverage. Marked [x], no new case needed.
- [x] **4.2** `test_phone` VERIFY — delivered in PR2; 6 format variants → one canonical E.164 + INVALID/UNKNOWN/KNOWN. Marked [x].
- [x] **4.5** `test_search` VERIFY — delivered in PR2 with 2.4; informal names, misspellings, synonyms, vector channel all resolve. Marked [x].
- [x] **4.6** `tests/test_intake.py` — webhook now dispatches via FastAPI `BackgroundTasks`; a recording ASGI transport proves the ACK response is sent BEFORE the heavy handler starts; ACK <5 s even with a slow handler.
- [x] **4.7** `tests/test_e2e_order.py` + `tests/test_e2e_ingestion.py` — full WhatsApp intake→quote→approve→convert→Sheets→stock→confirm flow (outbound sends mocked at httpx; async/sync notifier bridge); reject releases; confirm-send failure doesn't abort registration; remito preview→confirm→inventory + owner grid corrections + barcode stock query. 7 tests, Postgres.
- [x] **4.8** Coverage gate — `--cov=src --cov-fail-under=85` → **96.47% total, 229 passed**; `src/pricing` stays **100%**. backoffice/app raised 70%→85% with real handler tests (no vacuous tests added).
- [x] **5.1** `README.md` — quickstart, env setup table, mock flags, commands, checklist.
- [x] **5.2** `src/features.py` — `FASE1..4_ENABLED` gating: `fase_enabled`/`require_fase`/`FeatureDisabledError`; webhook ACKs without dispatching when fase 2 off; backoffice refuses to build when fase 4 off. Tests: `tests/test_features.py` (8).
- [x] **5.3** `docs/architecture.md` — data flow, six agents table, state machine, pricing, backoffice, flags.
- [x] **5.4** `docs/runbook.md` + quality: **mypy strict clean (0 errors)** — fixed 16 pre-existing errors (channel ABC payload typing, sweeper/state expression annotations, `DecisionAction` → str enum); ruff clean; deps pinned (`openai>=1.30`, `gspread>=6.0`, `gradio>=4.0`, `pyzbar>=0.1.9`, `pillow>=10.0`); test-docs generator extended to 24 domains → 204 scenarios; `make check-test-docs` green.

## Files Changed (PR4)

| File | Action | What Was Done |
|------|--------|---------------|
| `src/channels/whatsapp.py` | Created | WhatsApp Cloud API adapter (verify + media download) |
| `src/channels/__init__.py` | Modified | Exports WhatsAppChannel |
| `src/integrations/openai.py` | Created | Whisper/Vision/embedding clients (lazy SDK client) |
| `src/integrations/sheets.py` | Created | Append-only Sheets writer with quarantine |
| `src/integrations/__init__.py` | Created | Package exports |
| `src/orchestrator/approval.py` | Created | Approve→convert→Sheets→stock→confirm |
| `src/orchestrator/__init__.py` | Modified | Approval exports |
| `src/backoffice/` | Created | app (4 tabs), catalog, clients, monitor, ingestion |
| `src/barcode/decoder.py` | Created | pyzbar decode + duplicate flagging |
| `src/supplier/ocr.py` | Created | Remito/invoice OCR + price-list ingestion |
| `src/features.py` | Created | Per-Fase feature flags |
| `src/api/webhook.py` | Modified | BackgroundTasks dispatch + WhatsApp channel + fase-2 gate |
| `src/config.py` | Modified | `GOOGLE_SHEETS_*` settings |
| `src/channels/base.py` | Modified | `dict[str, Any]` typing (mypy strict) |
| `src/channels/telegram.py` | Modified | `dict[str, Any]` typing (mypy strict) |
| `src/agents/dispatch.py` | Modified | `DecisionAction` → str-enum (mypy strict) |
| `src/orchestrator/session.py` | Modified | `with_updates(**changes: Any)` (mypy strict) |
| `src/order_lifecycle/state.py` | Modified | `_reservation_expired_expr() -> Any` (mypy strict) |
| `src/scheduler/sweeper.py` | Modified | apscheduler import ignore + `-> Any` (mypy strict) |
| `pyproject.toml` | Modified | +openai, gspread, gradio, pyzbar, pillow (`>=` bounds) |
| `.env.example` | Modified | GOOGLE_SHEETS_* + flag comments |
| `Makefile` | Modified | `make backoffice` target |
| `README.md` | Created | Quickstart + env + mock flags |
| `docs/architecture.md` | Created | Data flow + six agents |
| `docs/runbook.md` | Created | Ops + failure modes |
| `docs/escenarios-testeados.md` | Regenerated | 204 scenarios (24 domains) |
| `scripts/gen_test_scenarios.py` | Modified | New test modules registered in DOMAINS |
| `tests/test_whatsapp.py` | Created | 11 unit (mocked httpx) |
| `tests/test_openai.py` | Created | 9 unit (mocked SDK) |
| `tests/test_sheets.py` | Created | 5 unit (mocked gspread) |
| `tests/test_approval.py` | Created | 3 unit + 4 Postgres |
| `tests/test_backoffice.py` | Created | 21 (structure + module + app handlers) |
| `tests/test_barcode.py` | Created | 3 unit + 3 Postgres |
| `tests/test_ocr.py` | Created | 6 unit + 5 Postgres |
| `tests/test_intake.py` | Created | 3 (async ordering, mocked transport) |
| `tests/test_features.py` | Created | 8 unit + webhook gating |
| `tests/test_e2e_order.py` | Created | 4 E2E (WhatsApp mock, Postgres) |
| `tests/test_e2e_ingestion.py` | Created | 3 E2E (Postgres) |
| `openspec/changes/mvp-ferreteria/tasks.md` | Modified | 3.1–3.9, 4.1, 4.2, 4.5–4.8, 5.1–5.4 marked [x] |

## Work Unit Evidence (PR4)

| Work unit | Focused test command + result | Runtime harness | Rollback boundary |
|---|---|---|---|
| 3.1 whatsapp | `pytest tests/test_whatsapp.py -q` → 11 passed | E2E order flow posts owner quote via mocked Graph API; `fetch_media` id→url→bytes mocked | Delete `src/channels/whatsapp.py`, revert webhook CHANNELS + tests |
| 3.2 openai | `pytest tests/test_openai.py -q` → 9 passed | N/A — SDK mocked; lazy client verified: no key → clear error at call time, not import | Delete `src/integrations/openai.py` + tests |
| 3.3 sheets | `pytest tests/test_sheets.py -q` → 5 passed | Approval E2E: unconfigured Sheets → row quarantined, flow completes | Delete `src/integrations/sheets.py`, revert config fields + tests |
| 3.4 approval | `pytest tests/test_approval.py -q` → 7 passed | Postgres: approve converts reservation, stock 10→6, confirm sent; stale → `RequiresRequoteError` with zero side effects | Delete `src/orchestrator/approval.py` + tests |
| 3.5–3.7 backoffice | `pytest tests/test_backoffice.py -q` → 21 passed | `build_app()` constructs 4 tabs without server; margin edit recomputes base (100×0.50→150.00); client phone normalizes | Delete `src/backoffice/` + tests |
| 3.8 barcode | `pytest tests/test_barcode.py -q` → 6 passed | Postgres: one barcode→SINGLE, two SKUs→DUPLICATE (never guessed), none→UNKNOWN | Delete `src/barcode/` + tests |
| 3.9 ocr | `pytest tests/test_ocr.py -q` → 11 passed | Postgres: price list maps CLV-001→existing SKU, NEW-777 suggested; re-ingest upserts, no duplicates | Delete `src/supplier/` + tests |
| 4.6 intake | `pytest tests/test_intake.py -q` → 3 passed | Recording ASGI transport: response_sent_at ≤ handler start (async proof); slow handler ACK <5 s | Revert webhook BackgroundTasks change + tests |
| 4.7 E2E | `pytest tests/test_e2e_order.py tests/test_e2e_ingestion.py -q` → 7 passed | Postgres: full order→approve→convert→Sheets(quarantine)→stock 40→confirm; reject→RELEASED→50 | Delete both E2E test files |
| 4.8 coverage | `pytest --cov=src --cov-fail-under=85` → 229 passed, 96.47%, pricing 100% | N/A — harness gate | N/A (test-only) |
| 5.2 flags | `pytest tests/test_features.py -q` → 8 passed | webhook ACKs 200 with fase2 off and zero dispatch; backoffice refuses build with fase4 off | Revert `src/features.py`, webhook/backoffice gates + tests |
| 5.4 quality | `make lint` clean · `make typecheck` → Success (0 errors) · `make check-test-docs` clean | N/A | N/A |

Final full run: `pytest --cov=src --cov-fail-under=85` → **229 passed, coverage 96.47%** (TOTAL 1446 stmts, 62 missing). `src/pricing` 100%.

## Deviations from Design

1. **Webhook dispatch moved to FastAPI `BackgroundTasks`** — design said "BackgroundTasks + APScheduler"; PR1 skeleton dispatched synchronously. PR4 wires the actual background handoff so the ACK truly precedes heavy work (proven by recording transport).
2. **Dispatch notifier is sync; WhatsApp send is async** — the `Notifier` protocol in `dispatch.py` stays sync (its tests cover it); the async channel is bridged at the pipeline edge (E2E uses an async-notifier adapter). No change to the dispatch API.
3. **`register_approved_order` vs `approve_and_register`** — approvals that run per-line adjustments go through `apply_decision` first (which already calls `approve_order`), then the registration half; `approve_and_register` is the clean-approval convenience. Calling `approve_and_register` after `apply_decision` double-approves (`InvalidTransitionError`) — documented, not hidden.
4. **OpenAI clients build lazily** — openai 3.x raises at `OpenAI()` construction without a key; the client is built on first use so importing the backoffice never needs credentials.
5. **Price-list ingestion is Vision/text based** — no PDF/Excel parsing library was added (per dependency list); price lists are ingested from photos/text through the same Vision pipeline, with the mapping/upsert logic in `ingest_price_list_rows`. Native PDF/Excel parsing is an extension point.
6. **Feature flags: `require_fase` is raise-or-proceed, not a predicate** — the webhook calls it in a try/except so the ACK contract holds at the boundary (stop at boundary = ACK without dispatch).

## Issues Found (PR4)

- **openai 3.x API drift**: transcription response format/typing changed (`segments`/`avg_logprob` still available via `verbose_json`, but mypy overload resolution needs `cast(Any)` on the `file` argument); `OpenAI()` raises without a key → lazy client holder.
- **lru_cached settings leak across tests**: `get_settings()` is cached; monkeypatching env vars is order-dependent — new channels/analyzers accept injected `Settings` (deterministic tests).
- **TestClient blocks on background tasks**: `client.post` waits for BackgroundTasks to finish — ordering must be proven with a recording ASGI transport, not elapsed-time.
- **Fixture sequences**: explicit-id seeds don't advance Postgres sequences → auto-id inserts collide; fixtures bump via `pg_get_serial_sequence` (name-robust).
- **App `SessionLocal` sees only committed rows**: fixture seeds are uncommitted; app-handler tests commit the seed first (documented in the test file).
- **pyzbar needs the system zbar library** (`brew install zbar` on macOS) — import fails without it; documented in runbook.
- **Sheet quarantine message is Spanish** (`"cuarentena"`) in the owner confirmation — intentional (owner-facing copy, per artifact-language rules Spanish was kept for the owner UX strings already established; all code/identifiers/docstrings are English).
- Pre-existing note: `httpx/starlette TestClient` deprecation warning in test_webhook remains (harmless).
- None blocking.

## Remaining Tasks

None — PR4 is the final slice. All Phase 1–5 tasks are `[x]`. Next: verify (`sdd-verify`), then archive (`sdd-archive`).

## PR4 Remediation: W1 (barcode audited stock adjustments)

Verify flagged W1 (barcode-stock-ops "Record audited stock adjustments" had no implementation). Remediated:
- `StockAdjustment` model (`stock_adjustments`) + Alembic migration `b2f353dfc3d2` (creates only `stock_adjustments`).
- `adjust_stock_by_barcode()` + `BarcodeAdjustmentError`/`BarcodeAdjustmentErrorKind` in `src/barcode/decoder.py`.
- 5 tests in `tests/test_barcode.py` (increase, decrease, duplicate, unknown, negative). Full suite 234 passed; lint + mypy strict clean; test-docs 209 scenarios.

## Status

**15/15 assigned tasks complete (3.1–3.9, 4.1, 4.2, 4.5–4.8, 5.1–5.4). 229 tests passing at final full run (was 142 after PR3; +87 across PR4 work units). Coverage 96.47% ≥ 85; pricing 100%. ruff + mypy strict + test-docs all clean. Ready for verify.**