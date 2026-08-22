# Verification Report: mvp-ferreteria

**Change**: mvp-ferreteria
**Branch**: `feat/mvp-ferreteria-pr4` (HEAD `601b24c`)
**Verifier**: sdd-verify executor
**Date**: 2026-08-22
**Mode**: Standard (strict_tdd: false)

---

## Verdict: PASS

All 234 tests pass (229 + 5 new W1 remediation tests). Coverage ≥ 85% gate. `src/pricing` at 100%. Ruff and mypy strict clean. The single WARNING (W1: barcode audited stock adjustments) is now implemented and tested. No CRITICAL blockers; SUGGESTIONs remain non-blocking.

---

## 1. Test Results

```
234 passed in 6.44s
```

**Full suite**: `.venv/bin/python -m pytest -q` — 234 passed, 0 failed, 0 errors.

## 2. Coverage

```
Required test coverage of 85% reached. Total coverage: 95.71%
```

| Module | Coverage |
|--------|----------|
| `src/pricing/engine.py` | **100%** |
| `src/db/models.py` | 100% |
| `src/agents/customer.py` | 100% |
| `src/agents/dispatch.py` | 100% |
| `src/agents/inventory.py` | 100% |
| `src/agents/perception.py` | 100% |
| `src/agents/sales.py` | 100% |
| `src/orchestrator/router.py` | 100% |
| `src/orchestrator/session.py` | 100% |
| `src/order_lifecycle/state.py` | 100% |
| `src/features.py` | 100% |
| `src/barcode/decoder.py` | 100% |
| `src/backoffice/monitor.py` | 100% |
| TOTAL (1446 stmts, 62 missing) | **95.71%** |

## 3. Lint & Typecheck

```
ruff:   All checks passed!
mypy:   Success: no issues found in 39 source files
```

## 4. Task Conformance (39/39)

All 39 tasks are `[x]` in `tasks.md`. Every task has real implementation files and covering tests:

| Phase | Tasks | Status | Evidence |
|-------|-------|--------|----------|
| Phase 1 (Foundation) | 1.1–1.7 (7) | ✅ | pyproject, docker-compose, models, session, alembic, channels, config, webhook, conftest |
| Phase 2 (Core) | 2.1–2.11 (11) | ✅ | pricing, perception, customer, disambiguation, inventory, sales, dispatch, orchestrator, state, sweeper, RED tests |
| Phase 3 (Integration) | 3.1–3.9 (9) | ✅ | whatsapp, openai, sheets, approval, backoffice app/ingestion/catalog/clients/monitor, barcode, ocr |
| Phase 4 (Testing) | 4.1–4.8 (8) | ✅ | test_pricing, test_phone, test_state_machine, test_inventory, test_search, test_intake, test_e2e_*, coverage gate |
| Phase 5 (Cleanup) | 5.1–5.4 (4) | ✅ | README, features flags, architecture.md, runbook.md + quality |

## 5. Spec Conformance Matrix

### 5.1 agent-orchestration (4 requirements, 8 scenarios)

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Six specialized agents | ✅ COMPLIANT | `AgentName` enum in `router.py` (6 agents); each agent in own module; no overlap |
| Route inbound messages | ✅ COMPLIANT | `route_message()` routes voice→perception, image→perception, text→customer/dispatch/sales/disambiguation |
| Run heavy processing async | ✅ COMPLIANT | `webhook.py` uses `BackgroundTasks.add_task()`; `test_intake.py` proves ACK sent before handler starts |
| Orchestrator coordinates flow | ✅ COMPLIANT | `Orchestrator.handle_inbound()` loads context→route→agent→persist; session preserved across steps |

### 5.2 backoffice (4 requirements, 8 scenarios)

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Supplier document ingestion | ✅ COMPLIANT | `ingestion.py`: `extract_document_items()` + `to_grid_rows()` + `confirm_items()` |
| Catalog and stock editor | ✅ COMPLIANT | `catalog.py`: `list_products()`, `update_stock()`, `update_price()`, `update_margin()` (recomputes base) |
| Clients and price lists | ✅ COMPLIANT | `clients.py`: `create_client()` (phone normalize), `update_client()` (list, discount) |
| Live order monitor | ✅ COMPLIANT | `monitor.py`: `list_orders()` shows state, soft-lock counts, Sheets sync status |

### 5.3 barcode-stock-ops (5 requirements, 10 scenarios)

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Decode barcode photos | ✅ COMPLIANT | `decoder.py`: `decode_image()` via pyzbar; `BarcodeDecodeError` on unreadable |
| Return product and stock on query | ✅ COMPLIANT | `lookup_barcode()` returns SINGLE/UNKNOWN with product data; stock via `Catalogo.stock_disponible` |
| Record audited stock adjustments | ✅ RESOLVED (was W1) | `adjust_stock_by_barcode()` in `decoder.py`: SINGLE→`stock_disponible += delta` + `StockAdjustment` row (reason, actor); DUPLICATE/UNKNOWN/NEGATIVE raise `BarcodeAdjustmentError` (kind). `StockAdjustment` model + Alembic migration `b2f353dfc3d2`. 5 tests in `test_barcode.py`. |
| Handle duplicate barcode mappings | ✅ COMPLIANT | `BarcodeLookupKind.DUPLICATE` flagged; never silently picks one |
| Notify on unknown barcodes | ✅ COMPLIANT | `BarcodeLookupKind.UNKNOWN` returned; surfaced to owner |

### 5.4 catalog-search (5 requirements, 10 scenarios)

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Hybrid fuzzy + vector search | ✅ COMPLIANT | `disambiguation.py`: pgvector + rapidfuzz hybrid; `test_search.py` covers informal, misspelling, synonym |
| Auto-map high-confidence | ✅ COMPLIANT | `resolve_item()` returns `AUTO_MAPPED` when confidence ≥ threshold |
| Disambiguation menu on ambiguity | ✅ COMPLIANT | `AMBIGUOUS` kind with candidates; numbered menu sent |
| Report no-match results | ✅ COMPLIANT | `NOT_FOUND` kind; customer informed; doesn't block rest of order |
| Precision target 85% | ℹ️ KPI | Operational metric measured during pilot; not code-testable |

### 5.5 clients-and-price-lists (5 requirements, 10 scenarios)

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Identify client by phone | ✅ COMPLIANT | `customer.py`: `lookup_phone()` normalizes + matches; `test_phone.py` covers 6 formats |
| Handle unknown phones | ✅ COMPLIANT | Falls back to base list; flagged as UNKNOWN for backoffice registration |
| Resolve assigned price list | ✅ COMPLIANT | `Cliente.lista_precios_id` → `ListaPrecios.descuento_lista_pct`; Base=0%, Gremio A=10%, Gremio B=20% |
| Apply particular discount | ✅ COMPLIANT | `Cliente.descuento_particular_pct` applied in `compute_final()` |
| Exclude credit/payment | ✅ COMPLIANT | `Cliente` model has no credit/payment fields; pricing ignores them |

### 5.6 order-lifecycle (7 requirements, 14 scenarios)

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Soft-lock inventory on quotation | ✅ COMPLIANT | `inventory.py`: `reserve_stock()` with 30-min TTL; `available_stock() = stock − Σ active` |
| Owner approval with adjustments | ✅ COMPLIANT | `dispatch.py`: `apply_decision()` with per-line adjustments; `approval.py`: `approve_and_register()` |
| Rejection releases reservations | ✅ COMPLIANT | `state.py`: `reject_order()` sets all ACTIVE→RELEASED immediately |
| Auto-release after 30-min TTL | ✅ COMPLIANT | `state.py`: `expire_reservations()` + `requires_requote()`; sweeper runs periodically |
| Register approved orders | ✅ COMPLIANT | `approval.py`: Sheets row + stock deduction + owner confirmation; failures quarantined |
| Track order state machine | ✅ COMPLIANT | `OrderEstado` enum: PENDING_APPROVAL→APPROVED→IN_DISPATCH / REJECTED; `test_order_lifecycle.py` |
| Quote SLA 3 min / Voice adoption 90% | ℹ️ KPI | Operational metrics for pilot; not code-testable |

### 5.7 pricing-engine (4 requirements, 8 scenarios)

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Compute base from cost + margin | ✅ COMPLIANT | `compute_base()`: `cost × (1 + margin)` HALF_UP; 925.92×1.35=1249.99 |
| Compute final with discounts | ✅ COMPLIANT | `compute_final()`: `base × (1−list) × (1−particular)` |
| Discounts multiplicative, not additive | ✅ COMPLIANT | 1000×0.80×0.90=720 (NOT 700); tested in `test_pricing.py` |
| Absent discounts = zero | ✅ COMPLIANT | `None` → `Decimal(0)`; tested: no-discount, only-list, only-particular |

### 5.8 supplier-document-ingestion (7 requirements, 13 scenarios)

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Extract items, quantities, costs | ✅ COMPLIANT | `ocr.py`: `extract_document_items()` via VisionAnalyzer; line parser |
| Confirm before writing inventory | ✅ COMPLIANT | `confirm_items()` only called after owner preview; no auto-write |
| Map to existing SKUs or suggest new | ✅ COMPLIANT | `ingest_price_list_rows()` maps by code/name; suggests new SKUs |
| Parse supplier price lists | ✅ COMPLIANT | Price-list parse + `ingest_price_list_rows()` with mapping upsert |
| Handle OCR failure | ✅ COMPLIANT | `IllegibleDocumentError` raised; no inventory write; `unparsed_lines` flagged |
| Reject illegible handwriting | ✅ COMPLIANT | Low-confidence extraction → `IllegibleDocumentError`; deferred to later version |
| Reduce manual entry time 80% | ℹ️ KPI | Operational metric for pilot; not code-testable |

### 5.9 whatsapp-order-intake (4 requirements, 8 scenarios)

| Requirement | Status | Evidence |
|-------------|--------|----------|
| Ingest text and voice orders | ✅ COMPLIANT | `whatsapp.py`: `parse_inbound()` handles text, voice, image, document |
| Ephemeral ACK <5 seconds | ✅ COMPLIANT | `webhook.py`: `BackgroundTasks.add_task()` then `Response("ACK")`; `test_intake.py` proves ACK before handler |
| Transcribe voice notes | ✅ COMPLIANT | `perception.py`: `transcribe_voice()` via Transcriber protocol; low-confidence fragments flagged |
| Handle transcription failure | ✅ COMPLIANT | `TranscriptionError` raised on failure/empty; caller notifies customer to resend |

## 6. Design Coherence

| Design Decision | Implementation | Aligned? |
|-----------------|----------------|----------|
| BackgroundTasks + APScheduler | `webhook.py` uses FastAPI `BackgroundTasks`; `sweeper.py` uses APScheduler | ✅ |
| State machine on `orders.estado` | `OrderEstado` enum (4 states) + `needs_requote` flag | ✅ |
| Pricing as pure function | `pricing/engine.py`: no I/O, `compute_base`/`compute_final` | ✅ |
| pgvector in Postgres | `Catalogo.embedding` is `Vector(1536)` | ✅ |
| Channel abstraction | `Channel` ABC in `base.py`; Telegram + WhatsApp adapters | ✅ |
| Per-Fase feature flags | `features.py`: `FASE1..4_ENABLED` + `require_fase()` | ✅ |

## 7. Deviation Verification

All 6 documented deviations were cross-checked against specs:

| Deviation | Violates MUST? | Assessment |
|-----------|---------------|------------|
| BackgroundTasks for webhook dispatch | No | Design already specified "BackgroundTasks + APScheduler" |
| `Order.customer` ORM relationship | No | Schema unchanged; relationship-only addition |
| `parse_decision` percent→fraction | No | Correct normalization (5%→0.05); `re\b` fix is implementation detail |
| OpenAI clients lazy-built | No | Import-time behavior; runtime contract unchanged |
| Price-list ingestion via Vision/text | No | Spec says "PDF or Excel"; Vision-based parsing fulfills extraction requirement |
| `require_fase` raise-or-proceed | No | Webhook catches exception to ACK without dispatch; boundary behavior correct |

**No deviation violates a MUST requirement.**

## 8. Findings

### CRITICAL

None.

### WARNING

| # | Spec | Requirement | Evidence |
|---|------|-------------|----------|
| W1 | barcode-stock-ops | "Record audited stock adjustments" — spec requires stock increase/decrease by barcode with reason and audit trail | **RESOLVED (PR4 remediation).** `adjust_stock_by_barcode()` implemented in `src/barcode/decoder.py` (SINGLE→adjust `stock_disponible` + `StockAdjustment` row; DUPLICATE/UNKNOWN/NEGATIVE raise `BarcodeAdjustmentError`). `StockAdjustment` model + migration `b2f353dfc3d2`. 5 tests in `tests/test_barcode.py`. |

### SUGGESTION

| # | Spec | Area | Detail |
|---|------|------|--------|
| S1 | barcode-stock-ops | Location in query response | Spec scenario "Location included in query response" says response includes product location "where available." The `Catalogo` model has no `location` field. Non-blocking for MVP but worth noting for future enhancement. |
| S2 | order-lifecycle | E2E transcription failure path | `TranscriptionError` is raised correctly but no E2E test covers the full path from WhatsApp voice → transcription failure → customer notification. Unit tests cover the error; integration coverage would strengthen confidence. |
| S3 | whatsapp-order-intake | Partial transcription confirmation | Spec says "system does not proceed to quotation on guessed items; prompts customer to confirm." The `low_confidence_fragments` field exists but the orchestrator-level confirmation flow for partial transcriptions is not E2E tested. |

## 9. Summary

| Gate | Result |
|------|--------|
| Tests | ✅ 234 passed, 0 failed |
| Coverage | ✅ ≥ 85%; `src/pricing` 100% |
| Lint (ruff) | ✅ Clean |
| Typecheck (mypy strict) | ✅ 0 errors in 39 files |
| Tasks | ✅ 39/39 complete with evidence |
| Spec conformance | ✅ W1 resolved (barcode audited stock adjustments implemented + tested) |
| Design coherence | ✅ All decisions aligned |
| Deviations | ✅ None violate MUST requirements |
