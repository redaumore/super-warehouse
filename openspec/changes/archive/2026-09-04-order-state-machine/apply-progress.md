# Apply Progress: order-state-machine — Full Change (PR final)

**Mode**: Standard (`strict_tdd: false`)
**Delivery**: `exception-ok` — maintainer-approved single PR with `size:exception`
(~2,400–2,900 authored lines; budget 3,200). No chained PRs.
**Status**: Phases 1–8 implemented; full suite green
(**679 passed / 0 failed / 0 errors**); `ruff check` + `ruff format --check`
clean; migration smoke green (the conftest session fixture rebuilds the schema
with `alembic upgrade head` on every run).
**Branch**: `feat/order-state-machine` (base `main`)
**Issue**: https://github.com/redaumore/super-warehouse/issues/13
**PR**: opened per branch-pr skill (`Closes #13`, exactly one `type:*` label).

## Completed Tasks

### Phase 1 (slice 1, committed)

- [x] 1.1 Create `alembic/versions/f2b2570aed04_order_state_machine.py`
  (down_revision `7d2f4a1e8b90`): RENAME VALUE ×4, ADD DRAFT+PICKING
  (autocommit block — PG<12 cannot run ADD VALUE in a transaction; PG>=12
  keeps new values unusable until commit), partial unique index
  `uq_orders_one_draft_per_customer ON orders(customer_id) WHERE estado='DRAFT'`,
  guarded downgrade (reconcile DRAFT→CONFIRMED and PICKING→READY_FOR_DELIVERY,
  reverse renames, drop index; extra enum labels remain — documented).
- [x] 1.2 Update `src/db/models.py`: `OrderEstado` six values
  (DRAFT, CONFIRMED, PICKING, READY_FOR_DELIVERY, CANCELED, CLOSED), default
  `DRAFT`, partial index in `Order.__table_args__`, `delivery_date` stays the
  nullable `Date` column already modeled/migrated.
- [x] 1.3 Extend `tests/test_db_models.py`: six enum values via `db_inspector`,
  downgrade safety (one-step downgrade + guarded re-upgrade), partial-index
  lock, both enum-cardinality locks updated to six states.

### Phase 2: Forward transitions

- [x] 2.1 Rewrote `src/order_lifecycle/state.py`: `confirm_order` (TTL guard via
  `requires_requote`, Draft→Confirmed, sets `approved_at`), `start_picking`,
  `complete_picking`, `deliver_order` (sets `delivery_date` when absent),
  `cancel_order(actor)` (Draft/Confirmed release ACTIVE; Picking/Ready restore
  deducted stock + `StockAdjustment(reason='order_cancelled', actor)`),
  `modify_order` (Confirmed→Draft restores stock without double-count),
  `add_draft_item`/`remove_draft_item` (upsert/delete; empty Draft stays DRAFT).
  Deleted `approve_order`/`reject_order`/`mark_dispatched`; kept
  `expire_reservations`/`requires_requote`.
- [x] 2.2 Rewrote `tests/test_order_lifecycle.py`: 11 legal transitions as
  tables, illegal moves rejected, stale-quote `RequiresRequoteError`,
  happy path Draft→…→Closed with delivery date, late-cancel restore + audit,
  modify no-leak, draft line edits.

### Phase 3: Draft persistence

- [x] 3.1 `src/sourcing/draft_order.py`: `persist_draft_order` writes
  `estado=DRAFT`, no reservations at persist (AD10: soft-lock at the quote step).
- [x] 3.2 `src/agents/customer.py` `_persist_finalized_draft`: the quote step
  (`cerrá el pedido`) reserves LOCAL lines ACTIVE; Draft stays DRAFT.
- [x] 3.3 `add_draft_item`/`remove_draft_item` in `state.py` (upsert/delete
  OrderItem; empty draft stays DRAFT).
- [x] 3.4 First add that knows the customer persists Order(DRAFT) (parsed-order
  turn and finalize path); customer resolved/created (name+phone, Base list);
  single-draft app guard + `IntegrityError` catch (race backstop).
- [x] 3.5 Tests in `test_draft_order.py`/`test_finalize.py`: first-add persist,
  unknown-customer creation, remove is real, add-after-resume, second-draft
  rejected and preserved, two-session race → exactly one survives.

### Phase 4: Confirm ceremony

- [x] 4.1 `src/orchestrator/approval.py`: `approve_and_register` →
  `confirm_and_register` returning `ConfirmResult(order, converted,
  sheets_status, total, confirmation_text, cancelled_case, missing)`. Sheets
  quarantine TOLERATED (order stays CONFIRMED); `SheetsRegistrationError` and
  the approval rollback are gone. `SheetsWriteStatus.SKIPPED` added (Case
  C/B outcomes append no row).
- [x] 4.2 Ceremony per AD5: TTL guard → transition → classify A/B/C from the
  latest availability → reserve→convert→deduct → Sheets append. LOCAL lines
  price from `costo_proveedor × margin` at the quote step (never
  `precio_lista_base` — fixed in `persist_case_a_order` too); RAG lines use
  supplier margin or default (existing source-aware engine).
- [x] 4.3 `case_a.py` persists DRAFT; `case_b.py` sets IN_PREPARATION and
  enters CONFIRMED on the confirmed selection; `case_c.py` cancels via
  `cancel_order(actor)` + `SourcingState.CANCELLED`; owner-chat reply uses
  `pedido #N`.
- [x] 4.4 Tests: confirm idempotency (2nd confirm → `InvalidTransitionError`),
  quarantine keeps CONFIRMED, Case C/B discovered at confirm, pending-
  conversion blocked, stale quote refused.

### Phase 5: Cancel & modify

- [x] 5.1 `cancel_order(session, order, *, actor, now)` in `state.py`:
  Draft/Confirmed release ACTIVE; Picking/Ready restore deducted stock +
  `StockAdjustment(reason='order_cancelled', actor)`; all → CANCELED.
- [x] 5.2 `modify_order`: Confirmed→Draft restores deducted stock and releases
  CONVERTED reservations (no audit row — modify is not a cancel); Sheets
  append-only, re-confirm appends a fresh row.
- [x] 5.3 Case B cancel policy (AD9): order cancel never touches OPEN/SENT POs
  or `SourcingNeed` rows — proven by `test_cancel_case_b_order_never_touches_pos_or_needs`.
- [x] 5.4 Tests: cancel release/restore + audit trail (`test_order_lifecycle`,
  `test_dispatch`, `test_backoffice`), modify no-leak/no-double-count, Case C
  cancel path (`test_case_c`).

### Phase 6: Backoffice

- [x] 6.1 Action wrappers in `src/backoffice/customer_orders.py` (po.py
  pattern, commit inside): `start_picking_action`, `complete_picking_action`,
  `deliver_order_action`, `cancel_order_action(actor="backoffice")`, plus
  `legal_actions(estado)`.
- [x] 6.2 Gradio buttons + list refresh in `src/backoffice/app.py`; the four
  actions on the Customer Orders tab with a state-driven legal-actions label.
- [x] 6.3 Tests in `test_backoffice.py`: actions commit each transition,
  deliver stores the delivery date, cancel restores stock with the
  backoffice actor audited, monitor shows all six states, tab renders the
  four buttons, `legal_actions` per state.

### Phase 7: Chat handlers & rehydration

- [x] 7.1 `dispatch.py`: "aprobá" → adjustments + `confirm_and_register`;
  "rechazá" → `cancel_order(actor="owner")`; remove-product command (`sacá X`)
  in `customer.py` (parser in `product_search.py`, routed to CUSTOMER even
  with order context; removes from in-memory draft or persisted DRAFT).
- [x] 7.2 `session.py`: rehydrate the latest DRAFT order;
  `awaiting_decision` = awaiting confirm; CANCELED replaces REJECTED;
  supplier-selection flag derived from the needs themselves.
- [x] 7.3 Tests in `test_dispatch.py`, `test_dispatch_handler.py`,
  `test_orchestrator.py`, `test_customer.py`, `test_product_search.py`,
  `test_finalize.py`: confirm/cancel handlers, `pedido #N` override, unknown
  reply re-asks, quarantine kept CONFIRMED, remove command, routing.

### Phase 8: Sweep & docs

- [x] 8.1 Updated the remaining tests (e2e, pipeline, backoffice, sweeper,
  sourcing persistence, rehydration, purchasing accumulate, customers,
  finalize, case A/B/C) to the six-state values.
- [x] 8.2 Updated `docs/architecture.md`, `docs/sourcing.md`, `README.md` and
  the pipeline/sweeper docstrings; removed the dead four-state code.
- [x] 8.3 Full `pytest` green (679 passed / 0 failed / 0 errors) + `alembic
  upgrade head` smoke (conftest rebuilds the disposable test schema from the
  migrations every session); PO/pricing behavior unchanged (accumulate and
  pricing suites green).

## Work Unit Evidence (phases 2–8)

| Unit | Focused test command and exact result | Runtime harness command and exact result | Rollback boundary |
|------|----------------------------------------|------------------------------------------|-------------------|
| 2 transitions | `pytest tests/test_order_lifecycle.py` — 28 passed | N/A — pure lifecycle logic, unit + integration tested | revert the state.py commit |
| 3 draft persistence | `pytest tests/test_draft_order.py tests/test_finalize.py` — green | two-session race against the real DB → exactly one DRAFT survives, other IntegrityError | revert draft_order/customer finalize changes |
| 4 confirm ceremony | `pytest tests/test_approval.py tests/test_case_a.py` — green | fake `SheetsWriter` QUARANTINED → order stays CONFIRMED, status surfaced | revert approval/dispatch commits |
| 5 cancel/modify | `pytest tests/test_order_lifecycle.py tests/test_case_b.py tests/test_case_c.py` — green | inventory-delta + PO-retention integration (late cancel restore + audit; Case B cancel leaves POs/needs intact) | revert state.py cancel/modify + case policy |
| 6 backoffice | `pytest tests/test_backoffice.py` — 41 passed | Gradio-free wrappers: each action commits inside its `SessionLocal` | revert customer_orders.py + app.py |
| 7 chat + rehydration | `pytest tests/test_dispatch.py tests/test_dispatch_handler.py tests/test_orchestrator.py` — green | chat-turn replay fixtures: `aprobá`/`rechazá`/`sacá X` over real sessions | revert dispatch.py + session.py + router |
| 8 sweep + docs | full `pytest` — **679 passed, 0 failed, 0 errors** | `alembic upgrade head` on the disposable test DB (every conftest session) | revert test/docs edits only |

## Deviations from Design

- **Pricing runs at the quote step, not inside the ceremony.** The draft and
  Case A persist paths price every line by source (LOCAL `costo_proveedor ×
  margin`, never `precio_lista_base`; RAG supplier margin or default) when the
  quote is shown; the confirm ceremony consumes the frozen source-priced
  snapshots and applies the owner's adjustments on top. Re-pricing at confirm
  would clobber the applied adjustments (AD5 lists price before adjustments;
  the quote the owner saw must be the price they confirm). The spec's
  "Local line priced from cost … never precio_lista_base" is enforced at both
  persist paths and locked by `test_case_a`/`test_finalize`/`test_draft_order`.
- **Case B orders enter CONFIRMED at the supplier selection, not at the
  ceremony.** Per the sourcing spec ("on confirmed selection … the order
  itself enters CONFIRMED"), `confirm_selection` transitions the order;
  the ceremony's Case B branch (discovered at confirm for a draft whose
  availability dropped) keeps the order CONFIRMED and hands the selection
  prompt back. Case B conversion/deduction is decoupled: the POs are the
  sourcing truth and are fulfilled via receipts (unchanged PO flow).
- **`delivery_date` NOT re-added**: already exists as a nullable `Date` column
  from `a0bf3bd210f8`; the task's "ADD COLUMN TIMESTAMP" would duplicate it
  (documented in slice 1; deliver stores the date, not a new column).
- **`approved_at`/`rejected_at` reused as-is** (design open question,
  recommended option): `confirm_order` sets `approved_at`, `cancel_order` sets
  `rejected_at`. No column rename.
- **Tests committed before code** in the final batch: the pre-commit
  scenario-docs hook requires the generated `docs/escenarios-testeados.md` to
  match the full staged test set, which forced the test-migration commit to
  land first; the code commits follow and the final tree is green.
- **`docs/estado-pedido.md` left as the historical gap analysis** (planning
  artifact of this change); the living docs (architecture.md, sourcing.md,
  README) were updated to the six-state flow.

## Risks

- Case B orders that reach the ceremony via re-classification (draft-path)
  hold no reservation conversion/deduction for their available LOCAL portion;
  bounded and documented (the PO axis is the sourcing truth). A follow-up
  could convert the available portion at selection.
- The confirm-ceremony classification adds back the order's own ACTIVE
  reservations to availability; an expired-but-not-yet-swept reservation is
  still caught by the TTL guard before any conversion.
- The dev database (`ferreteria`) may still hold pre-migration rows; the
  migration renames values in place, so live rows land in diagram-equivalent
  states (slice 1).

## Slice 1 Boundary Evidence (historical)

Full run at the end of slice 1 (commit e504eb1): `45 failed, 541 passed,
46 errors` — the enum rename and removed transition functions were consumed
by Phases 2–8 modules and tests. That inventory is superseded: the same run
is now **679 passed / 0 failed / 0 errors**.

## Pending Tasks

None — all tasks 1.1–8.3 are complete. Next phase: `verify`.