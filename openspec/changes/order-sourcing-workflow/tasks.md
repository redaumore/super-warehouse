# Tasks: Order Sourcing Workflow

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~3300 (range 3000-3500) |
| 400-line budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | S1 → S2 → S3 → S4 → S5 → S6 → S7 → S8 |
| Delivery strategy | single-pr |
| Chain strategy | pending |

Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: pending
400-line budget risk: High

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| S1 | Inventory + sourcing columns + migrations | PR 1 | `pytest tests/test_inventory.py tests/test_db_models.py -q` | `make db-up && alembic upgrade head` | `alembic downgrade -1` × 2; revert `src/db/models.py` |
| S2 | PO state machine + searcher seam + accumulation | PR 2 | `pytest tests/test_purchasing_state.py tests/test_purchasing_accumulate.py tests/test_searcher.py -q` | `make db-up && pytest -q` | Remove `src/purchasing/`; drop PO tables via downgrade |
| S3 | NL parser + classify + router step + DB rehydration | PR 3 | `pytest tests/test_intake.py tests/test_classify.py tests/test_router_sourcing.py tests/test_session_rehydrate.py -q` | N/A (pure functions + in-process session) | Revert `src/sourcing/`, `src/agents/intake.py`, router + session edits |
| S4 | Case A through unchanged quotation/approval | PR 4 | `pytest tests/test_case_a.py tests/test_e2e_order.py -q` | `make db-up && pytest -q` | Remove `src/sourcing/case_a.py`; revert `_deduct_stock` Inventory write |
| S5 | Case B multi-turn selection + PO accumulation | PR 5 | `pytest tests/test_case_b.py tests/test_sourcing_persistence.py -q` | `make db-up && pytest -q` | Remove `src/sourcing/case_b.py`, `src/sourcing/persistence.py` |
| S6 | Case C REJECTED + sourcing CANCELLED + notify | PR 6 | `pytest tests/test_case_c.py -q` | `make db-up && pytest -q` | Remove `src/sourcing/case_c.py`; sourcing stays nullable |
| S7 | Backoffice PO list + send/receive/cancel tab | PR 7 | `pytest tests/test_backoffice_po.py -q` | `python -m src.backoffice.app launch` (manual) | Remove PO tab from `app.py`; drop `src/backoffice/po.py` |
| S8 | Docs + runbook + seed script + module map | PR 8 | `pytest -q` (full regression: existing 229 + new tests) | `mkdocs build` (if docs tool present) | Revert docs/; feature flag stays OFF |

User budget override: 2500 lines. Estimated ~3300 → over custom budget AND over default 400-line guard. With `single-pr` delivery strategy, apply MUST NOT start unless owner grants `size:exception` or scope is reduced.

## Phase 1: S1 — Models + Migrations + Inventory Seed

- [x] 1.1 Add `SourcingState` (PENDING_ASSEMBLY/IN_PREPARATION/CANCELLED) + `SupplierPurchaseOrderState` enums in `src/db/models.py` via `sa.Enum(name=...)`.
- [x] 1.2 Add `Order.sourcing_state` (default PENDING_ASSEMBLY) and `Order.delivery_date` (nullable, informational); `OrderEstado` four-state machine UNTOUCHED.
- [x] 1.3 Add `Inventory` (sku_id PK, quantity_on_hand, updated_at), `SupplierPurchaseOrder`, `SupplierPurchaseOrderItem` (sku, quantity aggregated, received_quantity), `SourcingNeed` (order_id FK, sku, missing_quantity, nullable supplier_id, nullable po_item_id).
- [x] 1.4 Create `alembic/versions/*_sourcing_axis_inventory.py`: enums, additive nullable columns, `inventory` table, backfill `INSERT INTO inventory SELECT codigo_interno, stock_disponible, now() FROM catalogo`.
- [x] 1.5 Create `alembic/versions/*_supplier_purchase_orders.py`: PO header + item + `sourcing_needs` tables; indexes on `(order_id)` and `(supplier_id)`.
- [x] 1.6 Repoint `available_stock` in `src/agents/inventory.py` to `Inventory.quantity_on_hand − Σ(ACTIVE unexpired)`; unknown SKU returns `0` (KeyError → 0).
- [x] 1.7 Update `tests/test_inventory.py` for KeyError→0; add migration RED test asserting new tables/enums exist after upgrade.

## Phase 2: S2 — Purchasing PO Lifecycle + Searcher Seam

- [x] 2.1 Create `src/purchasing/state.py` mirroring `src/order_lifecycle/state.py`: `send_po`, `receive_po`, `cancel_po` with `InvalidTransitionError`; OPEN→SENT→PARTIALLY_RECEIVED→FULLY_RECEIVED, CANCELLED.
- [x] 2.2 Create `src/purchasing/accumulate.py`: `open_or_create_po`, `accumulate_need` — one OPEN PO per supplier; sum quantity on existing SKU row, else insert new item.
- [x] 2.3 Create `src/supplier/searcher.py`: `SupplierCandidate` dataclass + `SupplierCatalogSearcher` Protocol + `FakeSupplierCatalogSearcher` (in-memory candidates).
- [x] 2.4 Extend `tests/conftest.py` TRUNCATE list with `supplier_purchase_orders`, `supplier_purchase_order_items`, `sourcing_needs`, `inventory`.
- [x] 2.5 Add `tests/test_purchasing_state.py` (unit, `_FakeSession`): every legal transition + terminal rejection.
- [x] 2.6 Add `tests/test_purchasing_accumulate.py` (integration): same-supplier merge + multi-supplier split.

## Phase 3: S3 — NL Parsing + Classification + Router + Rehydration

- [x] 3.1 Create `src/agents/intake.py`: `OrderParser` Protocol, `ParsedOrder` dataclass (customer_name, items, delivery_date), fuzzy date resolver for Spanish phrases.
- [x] 3.2 Create `src/sourcing/classify.py`: `classify_case(items, availability, searcher)` → Case A/B/C; unknown SKU treated as missing.
- [x] 3.3 Modify `src/orchestrator/router.py`: parse step before Customer agent; route owner selection replies on Case B orders to confirm flow.
- [x] 3.4 Modify `src/orchestrator/session.py`: when `store.get()` returns `None`, rehydrate `ConversationState` from the sender's latest open `Order` + `SourcingNeed` rows.
- [x] 3.5 Add `tests/test_intake_parser.py`, `tests/test_classify.py`, `tests/test_router_sourcing.py`, `tests/test_session_rehydrate.py` (pure + integration).

## Phase 4: S4 — Case A Integration (Quotation/Approval Unchanged)

- [ ] 4.1 Create `src/sourcing/case_a.py`: `persist_case_a_order` — full-stock items through existing reservation + quotation flow; sourcing=PENDING_ASSEMBLY; delivery_date stored.
- [ ] 4.2 Modify `src/agents/customer.py` Case A reply: confirm availability, delivery date, order number.
- [ ] 4.3 Modify `src/orchestrator/approval.py` `_deduct_stock`: write `Inventory.quantity_on_hand` (touch `updated_at`) on approval; `Catalogo.stock_disponible` untouched.
- [ ] 4.4 Add `tests/test_case_a.py` (integration + orchestrator e2e): PENDING_ASSEMBLY + TTL/re-quote rules apply unchanged.

## Phase 5: S5 — Case B Multi-Turn Selection + Accumulation

- [ ] 5.1 Create `src/sourcing/persistence.py`: `upsert_sourcing_need`, `record_supplier_selection` (writes nullable `SourcingNeed.supplier_id`; re-selection updates before PO execution).
- [ ] 5.2 Create `src/sourcing/case_b.py`: `list_missing_with_suppliers` (calls searcher) + `confirm_selection` (calls `accumulate_need`, sets sourcing=IN_PREPARATION).
- [ ] 5.3 Modify `src/agents/customer.py` Case B reply: list each missing item + candidate suppliers.
- [ ] 5.4 Add `tests/test_case_b.py` (orchestrator e2e) and `tests/test_sourcing_persistence.py` (selection survives 30-min TTL via DB rehydration).

## Phase 6: S6 — Case C Cancellation + Notify

- [ ] 6.1 Create `src/sourcing/case_c.py`: `cancel_for_no_supplier` — `OrderEstado=REJECTED` via existing reject flow + `sourcing=CANCELLED`; notify via injected `Notifier`.
- [ ] 6.2 Modify `src/agents/customer.py` Case C reply: notify customer missing items are unavailable.
- [ ] 6.3 Add `tests/test_case_c.py` (integration): REJECTED + sourcing CANCELLED + reservations released + notifier called.

## Phase 7: S7 — Backoffice PO View + Execution Tab

- [ ] 7.1 Add `src/backoffice/po.py`: `list_purchase_orders`, `send_po_action`, `receive_po_action` (partial/full), `cancel_po_action` wrapping `src/purchasing/state.py`.
- [ ] 7.2 Modify `src/backoffice/app.py`: new "Purchase Orders" tab wired to PO functions with refresh button (no server auto-launch).
- [ ] 7.3 Add `tests/test_backoffice_po.py` (integration): OPEN→SENT→PARTIALLY_RECEIVED→FULLY_RECEIVED + CANCELLED from OPEN and SENT.

## Phase 8: S8 — Docs / Migration / Rollback / Cleanup

- [ ] 8.1 Add `docs/sourcing.md`: workflow, Case A/B/C matrix, PO lifecycle, searcher seam.
- [ ] 8.2 Update `ops/runbook.md` (or create) with backfill, downgrade, feature-flag disable (parse-step off → legacy intake).
- [ ] 8.3 Add `scripts/seed_inventory.py` (idempotent `INSERT … ON CONFLICT (sku_id) DO NOTHING`).
- [ ] 8.4 Update `README.md` module map with `src/sourcing/`, `src/purchasing/`, `src/supplier/searcher.py`.
- [ ] 8.5 Update `tests/test_db_models.py` asserting new enums + tables; full regression (existing 229 + new) meets ≥85% coverage gate.
