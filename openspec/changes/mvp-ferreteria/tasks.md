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

- [ ] 1.1 `pyproject.toml`, `docker-compose.yml` (Postgres+pgvector), `.env.example`, `Makefile`.
- [ ] 1.2 `src/db/models.py`: all design entities; `vector(1536)` on catalogo.
- [ ] 1.3 `src/db/session.py` + Alembic init + first migration.
- [ ] 1.4 `src/channels/base.py` ABC + `src/channels/telegram.py` adapter.
- [ ] 1.5 `src/config.py` Settings (keys, TTL=30, thresholds).
- [ ] 1.6 `src/api/webhook.py` skeleton+signature verify+ACK <5s.
- [ ] 1.7 `tests/conftest.py` Postgres+pgvector fixture; RED: models+migration.

## Phase 2: Core

- [ ] 2.1 `src/pricing/engine.py` pure `compute_base`/`compute_final`; RED: 1000×0.80×0.90=720.
- [ ] 2.2 `src/agents/perception.py` Whisper STT + GPT-4o Vision.
- [ ] 2.3 `src/agents/customer.py` phone normalize (`phonenumbers`); flag unknown.
- [ ] 2.4 `src/agents/disambiguation.py` pgvector+`rapidfuzz`; auto-map or menu.
- [ ] 2.5 `src/agents/inventory.py` soft-lock; `avail = stock − Σ active unexpired`.
- [ ] 2.6 `src/agents/sales.py` quote + per-line adjustments.
- [ ] 2.7 `src/agents/dispatch.py` owner notify + approve/reject.
- [ ] 2.8 `src/orchestrator/{router,session}.py`; preserves context.
- [ ] 2.9 `src/order_lifecycle/state.py` enum+`needs_requote`; release on reject.
- [ ] 2.10 `src/scheduler/sweeper.py` APScheduler releases EXPIRED + sets `needs_requote`.
- [ ] 2.11 RED: TTL release; reject release; expired cannot approve.

## Phase 3: Integration

- [ ] 3.1 `src/channels/whatsapp.py` Cloud API adapter (verify + media).
- [ ] 3.2 `src/integrations/openai.py` Whisper/Vision/embed; mockable.
- [ ] 3.3 `src/integrations/sheets.py` gspread append; quarantine fail.
- [ ] 3.4 `src/orchestrator/approval.py`: APPROVE→convert→Sheets→stock→confirm.
- [ ] 3.5 `src/backoffice/app.py` Gradio Blocks (4 tabs).
- [ ] 3.6 `src/backoffice/ingestion.py` upload→Vision→preview→confirm.
- [ ] 3.7 `src/backoffice/{catalog,clients,monitor}.py` editors.
- [ ] 3.8 `src/barcode/decoder.py` pyzbar; duplicate flagged.
- [ ] 3.9 `src/supplier/ocr.py` remito/invoice + price-list; reject illegible.

## Phase 4: Testing

- [ ] 4.1 Unit `test_pricing`: base, 0-margin, both/only-list, multiplicative.
- [ ] 4.2 Unit `test_phone`: format variants normalize.
- [ ] 4.3 Unit `test_state_machine`: transitions + `needs_requote`.
- [ ] 4.4 Integration `test_inventory`: RED—expiry release, reject release, blocked.
- [ ] 4.5 Integration `test_search`: informal+misspelling resolve.
- [ ] 4.6 Integration `test_intake`: ACK <5s; heavy work async.
- [ ] 4.7 E2E `test_e2e_order`+`test_e2e_ingestion`: full flows.
- [ ] 4.8 Coverage `--cov=src --cov-fail-under=85`; pricing 100%.

## Phase 5: Cleanup

- [ ] 5.1 `README.md` quickstart+env+mock flags.
- [ ] 5.2 Per-Fase flags `FASE1..4_ENABLED`; stop at boundary.
- [ ] 5.3 `docs/architecture.md` data-flow + agents.
- [ ] 5.4 `docs/runbook.md`; ruff+mypy pass; pin deps.
