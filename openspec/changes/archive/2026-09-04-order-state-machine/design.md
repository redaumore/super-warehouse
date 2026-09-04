# Design: Order State Machine Alignment

## Technical Approach

In-place `ALTER TYPE order_estado` migration (rename ×4, add ×2); `Draft` becomes a
persisted `Order` row; the owner-approval ceremony folds into `confirm`
(`Draft → Confirmed`); fulfillment/cancel become backoffice actions on the PO-tab
pattern; cancel generalizes with audited stock restore. Maps 1:1 to the proposal
approach and the five delta specs.

## Architecture Decisions

| # | Decision | Choice | Rejected | Why |
|---|---|---|---|---|
| 1 | Enum migration | `RENAME VALUE` ×4 (`PENDING_APPROVAL→CONFIRMED`, `APPROVED→READY_FOR_DELIVERY`, `IN_DISPATCH→CLOSED`, `REJECTED→CANCELED`) then `ADD VALUE DRAFT` + `PICKING`. Never drop a value. | recreate type / varchar | native-enum convention (26a4a1b103fe:99); reversible; live rows land in diagram-equivalent states (estado-pedido.md:50-54) |
| 2 | Draft shape | `Order` row `estado=DRAFT` + `OrderItem` children; customer resolved at the first add that knows the customer (parsed-order turn, or `cerrá el pedido para X`) | separate drafts tables | diagram models Draft as an order state; `subtotal/total` already nullable (models.py:291-292) |
| 3 | Source of truth | DB `OrderItem` rows once Draft persisted; `draft_items` stays only as the pre-customer in-memory buffer | keep memory as truth | add-after-resume and rehydration need durable items |
| 4 | Single draft | partial unique index `WHERE estado='DRAFT'` + app guard + `IntegrityError` catch | app-only guard | DB backstop beats the resolution race (precedent `uq_suppliers_cuit`, models.py:150-155) |
| 5 | Confirm ceremony | `Draft→Confirmed` in one transaction: TTL guard → apply adjustments → classify A/B/C → price → reserve→convert→deduct → Sheets append. Sheets quarantine is tolerated: **order stays CONFIRMED** (spec order-lifecycle). | Sheets-fail rolls back | spec "order MUST remain Confirmed"; `SheetsWriter` already quarantines internally (sheets.py:90-94) |
| 6 | Modify | `Confirmed→Draft`: restore deducted stock, release CONVERTED reservations, `estado=DRAFT`. Sheets is append-only: prior row stays; re-confirm appends a fresh row (chronological log). | delete Sheets row / un-convert reservations | no delete API; fresh re-reserve avoids stale TTL |
| 7 | Cancel | Draft/Confirmed → release ACTIVE reservations; Picking/Ready → restore stock + `StockAdjustment(reason='order_cancelled', actor)`. All four mark `estado=CANCELED` (never delete). | delete Draft row | spec is a state transition; audit trail |
| 8 | Backoffice | four action functions wrapping lifecycle transitions, commit inside (po.py:51-73 pattern); Gradio buttons + list refresh | single "advance" button | spec requires four explicit transitions, legal-only rendering |
| 9 | Sourcing | classify at confirm; `SourcingState` informational; order cancel never touches POs | cancel/detach shared POs | POs accumulate across orders (`accumulate_need`); canceling a shared PO orphans other orders |
| 10 | Reservation timing | created ACTIVE at the quote step (`cerrá el pedido`, Draft stays Draft); converted+deducted at confirm | create at confirm | "stale quote refused" needs a pre-existing TTL reservation |

## Data Flow

```
first add ──▶ Order(DRAFT) ──quote──▶ reserve(ACTIVE)+price ──confirm──▶
   (resolve customer)      (Draft stays Draft)   ┌ TTL guard ┐
                                                 │ classify  │
CONFIRMED ──start picking──▶ PICKING ──complete──▶ READY_FOR_DELIVERY ──deliver──▶ CLOSED
    │   │                       │                        │
    └───┴─────cancel────────────┴────cancel──────────────┘
        Draft/Confirmed: release ACTIVE
        Picking/Ready: restore stock + StockAdjustment
```

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `alembic/versions/{rev}_order_state_machine.py` | Create | enum rename/add, partial unique index, nullable `delivery_date` column, guarded downgrade (head `7d2f4a1e8b90`) |
| `src/db/models.py` | Modify | `OrderEstado` 6 values; default `DRAFT`; partial unique index in `Order.__table_args__`; nullable `delivery_date` set by `deliver_order` |
| `src/order_lifecycle/state.py` | Modify | replace `approve_order`/`reject_order`/`mark_dispatched` with `confirm_order`, `start_picking`, `complete_picking`, `deliver_order`, `cancel_order(actor)`, `modify_order`, `add_draft_item`, `remove_draft_item` |
| `src/orchestrator/approval.py` | Modify | `approve_and_register` → `confirm_and_register`; quarantine tolerated, no `SheetsRegistrationError` rollback |
| `src/sourcing/draft_order.py` | Modify | `persist_draft_order` writes `estado=DRAFT`; drop reservation from persist |
| `src/sourcing/case_a.py`, `case_b.py`, `case_c.py` | Modify | persist DRAFT; case C cancels via `cancel_order` + `SourcingState.CANCELLED` |
| `src/agents/customer.py`, `dispatch.py` | Modify | first-add persists Draft; "aprobá"→confirm, "rechazá"→cancel; remove-product command |
| `src/orchestrator/session.py` | Modify | rehydrate latest DRAFT order; `awaiting_decision` = awaiting confirm; CANCELED replaces REJECTED |
| `src/backoffice/customer_orders.py`, `app.py` | Modify | fulfillment actions + buttons on Customer Orders tab |
| `src/backoffice/monitor.py` | Modify | `.value` follows automatically (no change expected) |
| `src/purchasing/state.py` | None | unchanged; `cancel_po` stays in the PO tab only |
| `tests/*` (14 files) | Modify | rename state references; enum-cardinality lock (test_db_models.py:86-91, 178-184) |

## Interfaces / Contracts

```python
# src/order_lifecycle/state.py
def confirm_order(session, order, *, now=None) -> Order          # Draft→Confirmed (TTL guard)
def start_picking(session, order) -> Order                        # Confirmed→Picking
def complete_picking(session, order) -> Order                     # Picking→Ready for delivery
def deliver_order(session, order) -> Order                        # Ready for delivery→Closed (sets delivery_date)
def cancel_order(session, order, *, actor, now=None) -> Order     # any pre-Closed state → Canceled
def modify_order(session, order) -> Order                         # Confirmed→Draft (restore+release)
def add_draft_item(session, order, sku, cantidad) -> OrderItem
def remove_draft_item(session, order, sku) -> None                # last item ⇒ empty Draft stays DRAFT

# confirm_result dataclass in approval.py
@dataclass
class ConfirmResult:
    order: Order; converted: int; sheets_status: SheetsWriteStatus
    total: Decimal; confirmation_text: str; cancelled_case: bool = False
```

## Testing Strategy

| Layer | What | Approach |
|-------|------|----------|
| Unit | transitions legal/illegal; TTL guard; adjustment re-price | per-transition tables (existing test_order_lifecycle pattern) |
| Unit | add/remove item; empty-draft-stays-DRAFT; single-draft guard | customer-order-persistence tests |
| Unit | modify restores stock + releases CONVERTED; late-cancel restores + audit row | assert Inventory delta + StockAdjustment(reason, actor) |
| Integration | migration round-trip: enum has 6 values via `db_inspector`; downgrade safety | extend test_db_models round-trip (46bdbdc4a575) |
| Integration | confirm idempotency (2nd confirm → `InvalidTransitionError`); Sheets quarantine keeps CONFIRMED | fake `SheetsWriter` returning `QUARANTINED` |
| Integration | single-draft race: two sessions insert DRAFT, one raises `IntegrityError` | two `SessionLocal` scopes |
| E2E | backoffice actions commit + refresh; Case A/B/C under new states | Gradio-free wrappers (po.py pattern) |

`mark_dispatched` tests (test_order_lifecycle.py:170-181) die with the dead code.

## Threat Matrix

N/A — no routing, shell, subprocess, VCS/PR automation, executable-file
classification, or process-integration boundary changes. Google Sheets is an
external HTTP append already quarantined by `SheetsWriter` (sheets.py:96-111);
its behavior is unchanged and covered by the confirm-quarantine test.

## Migration / Rollout

One migration. Upgrade: `RENAME VALUE` ×4, then `ADD VALUE DRAFT`/`PICKING`
(`ADD VALUE` must run outside a transaction on PG <12 — verify server version
pre-flight), plus `ADD COLUMN delivery_date TIMESTAMP NULL`. Then
`CREATE UNIQUE INDEX uq_orders_one_draft_per_customer ON orders
(customer_id) WHERE estado='DRAFT'`. Downgrade (guarded): `UPDATE` any
`DRAFT→CONFIRMED` and `PICKING→READY_FOR_DELIVERY` rows, reverse the four
renames, drop the index and the `delivery_date` column; the two extra enum
labels remain (PG cannot drop enum values without recreating the type —
documented, data-safe). No feature flag: ship with the migration, actions are
additive.

## Open Questions

- [ ] PostgreSQL server version (determines whether `ADD VALUE` needs an
      autocommit block outside the migration transaction).
- [ ] Rename `approved_at`/`rejected_at` columns to `confirmed_at`/`cancelled_at`,
      or reuse as-is (recommended: reuse — minimal diff).
