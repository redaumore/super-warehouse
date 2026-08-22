# Tasks: mvp-ferreteria — Hardware Store Multi-Agent MVP

## Review Workload Forecast

Estimated changed lines: 5000–8000 (greenfield, 9 caps, 6 agents+orch, data model, 3 integrations, tests). 400-line budget risk: High. Chained PRs: Yes. Delivery: auto-chain. Chain: stacked-to-main.

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High

### Suggested Work Units

| Unit | Goal | PR | Test | Harness | Rollback |
|------|------|----|------|---------|----------|
| 1 | Repo+DB+channels | PR1 | `pytest test_db_models test_channels` | compose + curl /demo/telegram | drop `src/api`, `src/db`, `src/channels/{base,telegram}` |
| 2 | Pricing+clients+search | PR2 | `pytest test_pricing test_clients test_search` | cov-fail-under=100 | drop `src/pricing`, `src/agents/{customer,disambiguation,inventory}` |
| 3 | Orchestrator+orders | PR3 | `pytest test_order_lifecycle test_orchestrator` | APScheduler tick + integration | drop `src/agents`, `src/orchestrator`; ACKs |
| 4 | WA+Sheets+Gradio+OCR | PR4 | `pytest test_whatsapp test_sheets test_backoffice test_barcode test_ocr` | WA+Sheets mocks | drop WA, integrations, backoffice, OCR |

## Phase 1: Foundation

- [x] 1.1 `pyproject.toml`, `docker-compose.yml` (Postgres+pgvector), `.env.example`, `Makefile`.
- [x] 1.2 `src/db/models.py`: all design entities; `vector(1536)` on catalogo.
- [x] 1.3 `src/db/session.py` + Alembic init + first migration.
- [x] 1.4 `src/channels/base.py` ABC + `src/channels/telegram.py` adapter.
- [x] 1.5 `src/config.py` Settings (keys, TTL=30, thresholds).
- [x] 1.6 `src/api/webhook.py` skeleton+signature verify+ACK <5s.
- [x] 1.7 `tests/conftest.py` Postgres+pgvector fixture; RED: models+migration.

## Phase 2: Core

- [x] 2.1 `src/pricing/engine.py` pure `compute_base`/`compute_final`; RED: 1000×0.80×0.90=720.
- [x] 2.2 `src/agents/perception.py` Whisper STT + GPT-4o Vision.
- [x] 2.3 `src/agents/customer.py` phone normalize (`phonenumbers`); flag unknown.
- [x] 2.4 `src/agents/disambiguation.py` pgvector+`rapidfuzz`; auto-map or menu.
- [x] 2.5 `src/agents/inventory.py` soft-lock; `avail = stock − Σ active unexpired`.
- [x] 2.6 `src/agents/sales.py` quote + per-line adjustments.
- [x] 2.7 `src/agents/dispatch.py` owner notify + approve/reject.
- [x] 2.8 `src/orchestrator/{router,session}.py`; preserves context.
- [x] 2.9 `src/order_lifecycle/state.py` enum+`needs_requote`; release on reject.
- [x] 2.10 `src/scheduler/sweeper.py` APScheduler releases EXPIRED + sets `needs_requote`.
- [x] 2.11 RED: TTL release; reject release; expired cannot approve.

## Phase 3: Integration

- [x] 3.1 `src/channels/whatsapp.py` Cloud API adapter (verify + media).
- [x] 3.2 `src/integrations/openai.py` Whisper/Vision/embed; mockable.
- [x] 3.3 `src/integrations/sheets.py` gspread append; quarantine fail.
- [x] 3.4 `src/orchestrator/approval.py`: APPROVE→convert→Sheets→stock→confirm.
- [x] 3.5 `src/backoffice/app.py` Gradio Blocks (4 tabs).
- [x] 3.6 `src/backoffice/ingestion.py` upload→Vision→preview→confirm.
- [x] 3.7 `src/backoffice/{catalog,clients,monitor}.py` editors.
- [x] 3.8 `src/barcode/decoder.py` pyzbar; duplicate flagged. *(PR4 remediation: added `adjust_stock_by_barcode()` + `StockAdjustment` model + migration `b2f353dfc3d2` + `BarcodeAdjustmentError`, resolving verify W1 — 5 tests in `tests/test_barcode.py`)*
- [x] 3.9 `src/supplier/ocr.py` remito/invoice + price-list; reject illegible.

## Phase 4: Testing

- [x] 4.1 Unit `test_pricing`: base, 0-margin, both/only-list, multiplicative. *(verified — delivered in PR2, `tests/test_pricing.py` 100% coverage: base×1.35, 0-margin, only-list, only-particular, both compound to 720, multiplicative-vs-additive, HALF_UP)*
- [x] 4.2 Unit `test_phone`: format variants normalize. *(verified — delivered in PR2, `tests/test_phone.py`: 6 format variants → one canonical E.164, INVALID/UNKNOWN/KNOWN)*
- [x] 4.3 Unit `test_state_machine`: transitions + `needs_requote`. *(delivered in PR3 with task 2.9 — `tests/test_order_lifecycle.py`)*
- [x] 4.4 Integration `test_inventory`: RED—expiry release, reject release, blocked. *(delivered in PR3 with tasks 2.9–2.11 — `tests/test_order_lifecycle.py`, `tests/test_sweeper.py`)*
- [x] 4.5 Integration `test_search`: informal+misspelling resolve. *(delivered in PR2 with task 2.4 — `tests/test_search.py`: informal name, misspelling, synonym, unnormalized input, ranking, menu, not-found, vector auto-map + ambiguity)*
- [x] 4.6 Integration `test_intake`: ACK <5s; heavy work async. *(PR4 — `tests/test_intake.py` + BackgroundTasks wiring in `src/api/webhook.py`)*
- [x] 4.7 E2E `test_e2e_order`+`test_e2e_ingestion`: full flows. *(PR4 — WhatsApp mock, real Postgres)*
- [x] 4.8 Coverage `--cov=src --cov-fail-under=85`; pricing 100%. *(PR4 — 96% total, `src/pricing` 100%)*

## Phase 5: Cleanup

- [x] 5.1 `README.md` quickstart+env+mock flags.
- [x] 5.2 Per-Fase flags `FASE1..4_ENABLED`; stop at boundary. *(PR4 — `src/features.py` + gating + `tests/test_features.py`)*
- [x] 5.3 `docs/architecture.md` data-flow + agents.
- [x] 5.4 `docs/runbook.md`; ruff+mypy pass; pin deps. *(PR4 — mypy strict clean, ruff clean, deps pinned with `>=` bounds)*
