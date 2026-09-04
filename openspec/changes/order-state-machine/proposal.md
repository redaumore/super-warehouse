# Proposal: Order State Machine Alignment

## Intent

Align lifecycle with six-state machine, making Draft durable and fulfillment executable.

## Scope

### In Scope
- Migrate `order_estado` to `{DRAFT, CONFIRMED, PICKING, READY_FOR_DELIVERY, CANCELED, CLOSED}`; enforce 11 transitions and one Draft per customer.
- Resolve/create the customer on first add; persist a Draft `Order` with add/remove/resume support.
- Move owner ceremony into confirm: TTL guard, Sheets quarantine, reservation conversion, deduction stay atomic.
- Cancel Draft/Confirmed/Picking/Ready, restoring stock and auditing `StockAdjustment`; add backoffice actions; update cases, rehydration, callers, and 14 tests (~70 refs).

### Out of Scope
- Chat fulfillment triggers.
- Purchase-order machine, pricing-rule, and RAG pricing changes.
- External research.

## Capabilities

### New Capabilities
- None.

### Modified Capabilities
- `order-lifecycle`: states, transitions, confirm side effects, TTL, cancellation.
- `customer-order-persistence`: Draft persistence, uniqueness, mutation.
- `backoffice`: fulfillment actions and state display.
- `order-sourcing`: Case A/B/C interaction; separate `SourcingState`.
- `barcode-stock-ops`: audited cancellation restoration.

## Approach

Use in-place PostgreSQL `ALTER TYPE ... RENAME VALUE` plus `ADD VALUE`, a partial unique index/app guard, and a guarded downgrade. Compose approval/Sheets/stock inside confirm, mirror PO actions, and keep sourcing/PO independent.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `src/db/models.py`, `alembic/versions/` | Modified | Enum, index, migration. |
| `src/order_lifecycle/`, `src/orchestrator/approval.py` | Modified | Transitions and side effects. |
| `src/sourcing/`, `src/agents/`, `src/orchestrator/session.py` | Modified | Drafts, cases, rehydration. |
| `src/backoffice/app.py`, `customer_orders.py` | Modified | Fulfillment UI/actions. |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Confirm/cancel side effects repeat | High | Transactions, idempotency, quarantine tests. |
| First-add resolution races uniqueness | High | App guard, index, `IntegrityError` handling. |
| Enum migration mishandles live rows | Medium | Verify PostgreSQL and reconcile data. |
| Case B cancellation leaves supplier work inconsistent | Medium | Resolve PO/SourcingNeed policy in design. |

## Open Questions

- What Sheets, stock, reservation, and pricing reconciliation does `CONFIRMED → DRAFT` perform?
- Does Case B cancellation cancel, retain, or detach OPEN/SENT POs and `SourcingNeed` rows?
- When do Case A/B/C classification and `SourcingState` updates occur?

## Rollback Plan

Disable new actions, restore the prior revision, and run a guarded downgrade after reconciling new-state rows and enum values. Never discard orders or audit records.

## Dependencies

- PostgreSQL enum support, Alembic head `7d2f4a1e8b90`, and existing Sheets/Inventory/audit infrastructure.

## Success Criteria

- [ ] Six enum values and all 11 transitions are enforced and tested.
- [ ] Draft add/remove/resume and uniqueness are race-safe.
- [ ] Confirm preserves TTL, quarantine, deduction, and auditability.
- [ ] Late cancel restores stock with an audit row; backoffice actions work.
- [ ] PO/pricing behavior is unchanged and tests pass.
