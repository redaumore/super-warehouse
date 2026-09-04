# Tasks: Order State Machine Alignment

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | 2,400–2,900 |
| 400-line budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | PR 1 → PR 8 (8 work units) |
| Delivery strategy | auto-chain |
| Chain strategy | pending |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: pending
400-line budget risk: High

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | Enum migration + model | PR 1 | `pytest tests/test_db_models.py` | `alembic upgrade head` + `downgrade -1` on scratch DB | revert migration + `src/db/models.py` |
| 2 | Forward transitions | PR 2 | `pytest tests/test_order_lifecycle.py` | N/A — pure logic, unit-tested | revert `src/order_lifecycle/state.py` |
| 3 | Draft persistence | PR 3 | `pytest tests/test_draft_order.py tests/test_customer.py` | two-session race, real DB | revert `src/sourcing/draft_order.py` + customer first-add |
| 4 | Confirm ceremony | PR 4 | `pytest tests/test_approval.py tests/test_order_pricing.py tests/test_classify.py` | fake-SheetsWriter quarantine integration | revert `src/orchestrator/approval.py` + case files |
| 5 | Cancel/modify | PR 5 | `pytest tests/test_barcode.py tests/test_case_b.py tests/test_case_c.py` | inventory-delta + PO-retention integration | revert cancel/modify + case policy |
| 6 | Backoffice actions | PR 6 | `pytest tests/test_backoffice.py` | Gradio-free wrappers (po.py pattern) | revert `src/backoffice/customer_orders.py` + `app.py` |
| 7 | Chat + rehydration | PR 7 | `pytest tests/test_dispatch.py tests/test_orchestrator.py` | chat-turn replay fixtures | revert `src/agents/dispatch.py` + `src/orchestrator/session.py` |
| 8 | Sweep + docs | PR 8 | `pytest` | `alembic upgrade head` on staging | revert test/docs edits only |

## Phase 1: Migration & Model (PR 1)

- [x] 1.1 Create `alembic/versions/{rev}_order_state_machine.py`: RENAME VALUE ×4 (PENDING_APPROVAL→CONFIRMED, APPROVED→READY_FOR_DELIVERY, IN_DISPATCH→CLOSED, REJECTED→CANCELED), ADD DRAFT+PICKING (autocommit pre-flight if PG<12), ADD COLUMN `delivery_date TIMESTAMP NULL`, unique index `uq_orders_one_draft_per_customer ON orders(customer_id) WHERE estado='DRAFT'`, guarded downgrade (reconcile DRAFT/PICKING rows, reverse renames, drop index+column, extra labels remain — documented). Head `7d2f4a1e8b90` (dep: none)
- [x] 1.2 Update `src/db/models.py`: `OrderEstado` six values, default `DRAFT`, partial index in `Order.__table_args__`, nullable `delivery_date` (dep: 1.1)
- [x] 1.3 Extend `tests/test_db_models.py` round-trip: six enum values via `db_inspector`; downgrade safety; update enum-cardinality locks (:86-91, :178-184)

## Phase 2: Forward Transitions (PR 2)

- [ ] 2.1 Rewrite `src/order_lifecycle/state.py`: `confirm_order` (TTL guard via `requires_requote`, Draft→Confirmed), `start_picking`, `complete_picking`, `deliver_order` (sets `delivery_date`); delete `approve_order`/`reject_order`/`mark_dispatched`; keep `expire_reservations` (dep: 1.2)
- [ ] 2.2 Update `tests/test_order_lifecycle.py`: transition tables (11 legal, illegal rejected), stale-quote `RequiresRequoteError`, happy path Draft→…→Closed; delete `mark_dispatched` tests (:170-181)

## Phase 3: Draft Persistence (PR 3)

- [ ] 3.1 Update `src/sourcing/draft_order.py`: `persist_draft_order` writes `estado=DRAFT`, no reservation at persist (dep: 2.1)
- [ ] 3.2 Move LOCAL reservation creation to quote step (`cerrá el pedido`) in `src/agents/customer.py`; Draft stays DRAFT (AD10, dep: 3.1)
- [ ] 3.3 Add `add_draft_item`/`remove_draft_item` to `src/order_lifecycle/state.py`: upsert/delete `OrderItem`; empty draft stays DRAFT
- [ ] 3.4 First add persists Order(DRAFT) + resolves/creates customer (name+phone, Base list) in `src/agents/customer.py`; single-draft app guard + `IntegrityError` catch (dep: 3.1)
- [ ] 3.5 Tests in `tests/test_draft_order.py`, `tests/test_customer.py`: first-add persist, unknown-customer creation, remove is real, add-after-resume; two-session race → exactly one survives, other `IntegrityError`

## Phase 4: Confirm Ceremony (PR 4)

- [ ] 4.1 Rewrite `src/orchestrator/approval.py`: `approve_and_register` → `confirm_and_register` returning `ConfirmResult(order, converted, sheets_status, total, confirmation_text, cancelled_case)`; Sheets quarantine tolerated (order stays CONFIRMED), no rollback on `SheetsRegistrationError` (dep: 2.1)
- [ ] 4.2 Confirm applies adjustments, classifies A/B/C from latest availability, prices by source (LOCAL `costo_proveedor × margin`, never `precio_lista_base`; RAG supplier margin or default), then reserve→convert→deduct atomically (dep: 3.2)
- [ ] 4.3 Update `src/sourcing/case_a.py`, `case_b.py`, `case_c.py`: classify at confirm, Case B OPEN PO accumulate + sourcing IN_PREPARATION on selection, Case C cancel via `cancel_order` + `SourcingState.CANCELLED`; owner-chat reply `pedido #N` (dep: 5.1)
- [ ] 4.4 Integration tests in `tests/test_approval.py`, `test_order_pricing.py`, `test_classify.py`: confirm idempotency (2nd confirm → `InvalidTransitionError`), fake `SheetsWriter` QUARANTINED keeps CONFIRMED, per-spec pricing/classification scenarios

## Phase 5: Cancel & Modify (PR 5)

- [ ] 5.1 Add `cancel_order(session, order, *, actor, now)` in `src/order_lifecycle/state.py`: Draft/Confirmed release ACTIVE reservations; Picking/Ready restore deducted stock + `StockAdjustment(reason='order_cancelled', actor)`; all → CANCELED (dep: 2.1)
- [ ] 5.2 Add `modify_order` in `src/order_lifecycle/state.py`: Confirmed→Draft restores deducted stock, releases CONVERTED; Sheets append-only, re-confirm appends fresh row (dep: 5.1)
- [ ] 5.3 Case B cancel policy: order cancel never touches OPEN/SENT POs or `SourcingNeed` rows (shared-PO accumulation, AD9); no orphaned supplier work (dep: 4.3)
- [ ] 5.4 Tests: cancel release/restore + audit trail (`tests/test_barcode.py`, `test_inventory.py`); modify no-leak/no-double-count; Case C cancel path (`tests/test_case_c.py`)

## Phase 6: Backoffice (PR 6)

- [ ] 6.1 Add action wrappers in `src/backoffice/customer_orders.py` (po.py pattern, commit inside): start_picking, complete_picking, deliver_order, cancel_order(actor=backoffice) (dep: 2.1, 5.1)
- [ ] 6.2 Add Gradio buttons + list refresh in `src/backoffice/app.py`; only legal actions per state rendered (dep: 6.1)
- [ ] 6.3 Tests in `tests/test_backoffice.py`: actions commit + refresh; monitor shows six states + soft-lock + Sheets status (dep: 6.1)

## Phase 7: Chat Handlers & Rehydration (PR 7)

- [ ] 7.1 Update `src/agents/customer.py` + `src/agents/dispatch.py`: "aprobá"→`confirm_order`, "rechazá"→`cancel_order(actor=owner)`, remove-product command (dep: 3.3, 4.1, 5.1)
- [ ] 7.2 Update `src/orchestrator/session.py`: rehydrate latest DRAFT order; `awaiting_decision` = awaiting confirm; CANCELED replaces REJECTED
- [ ] 7.3 Tests in `tests/test_dispatch.py`, `tests/test_dispatch_handler.py`, `tests/test_orchestrator.py`: new commands/states (dep: 7.1-7.2)

## Phase 8: Sweep & Docs (PR 8)

- [ ] 8.1 Update remaining tests (`tests/test_e2e_order.py`, `test_finalize.py`, `test_intake.py`, `test_features.py`, `test_channels.py`, etc.): rename four-state refs to six-state values (dep: all)
- [ ] 8.2 Update docs/comments referencing old states (`src/orchestrator/owner.py`, README/docs); remove dead code surfaced by sweep
- [ ] 8.3 Full `pytest` green + `alembic upgrade head` smoke on staging; verify PO/pricing behavior unchanged (dep: 8.1)