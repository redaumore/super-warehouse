# Proposal: Order Sourcing Workflow

## Intent

Customers describe orders in free language ("...clavos... para el viernes a la tarde"). Today no conversation can create a persisted Order, stock only flows through Sheets at approval/dispatch, and no supplier-order entity exists. This change builds customer-order creation and sourcing: parse the request, check local inventory, then route to full-stock fulfillment, supplier-sourced fulfillment, or cancellation.

## Scope

### In Scope
- Structured NL parsing: customer, items/quantities, delivery date (informational).
- Real local-inventory table with availability queries.
- Sourcing decision: Case A full stock → order with sourcing "Pendiente de Armado" (PENDING_ASSEMBLY); Case B partial → supplier lookup, owner picks supplier(s), accumulating Purchase Order → "En Preparación" (IN_PREPARATION); Case C no supplier → "Cancelado" (CANCELLED).
- `SupplierPurchaseOrder` entity with own state machine; backoffice execution view.

### Out of Scope
- End-customer self-service (single actor = owner this version).
- Building the supplier-catalog RAG (consumed via searcher seam only).
- Delivery-date-driven planning; credit/payment terms.

## Capabilities

### New Capabilities
- `local-inventory`: `Inventory` table — `sku_id` FK (unique), `quantity_on_hand` int, `updated_at`. Availability = `quantity_on_hand − sum(active reservations)`. Seed source decided in spec.
- `supplier-catalog-search`: `SupplierCatalogSearcher` Protocol (code + semantic search) consuming the external RAG; seam only.
- `purchase-order-lifecycle`: `SupplierPurchaseOrder` + items, accumulating across customer orders; states OPEN → SENT → PARTIALLY_RECEIVED → FULLY_RECEIVED, CANCELLED; transitions module mirroring `src/order_lifecycle/state.py`.
- `order-sourcing`: parsing, Case A/B/C decision, multi-turn supplier selection, delivery-date confirmation.

### Modified Capabilities
- `order-lifecycle`: carve-out that `OrderEstado` remains fixed at four states; sourcing axis (`SourcingState` PENDING_ASSEMBLY/IN_PREPARATION/CANCELLED) is a separate column; add `delivery_date`; Case A passes unchanged through quotation/approval.
- `whatsapp-order-intake`: add structured-extraction requirement after transcription.

## Approach

Additive (exploration approach 1). New enum columns + new entities + Alembic migrations. NL parsing as dedicated router step before the Customer agent. Case B persists missing items + supplier options on the Order row — DB is source of truth, so multi-turn selection survives the 30-min in-memory TTL (ConversationState becomes a rehydratable cache). PO execution uses channel adapters.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `src/db/models.py` | Modified | SourcingState, delivery_date, Inventory, SupplierPurchaseOrder/Item |
| `alembic/versions/` | New | Additive migrations |
| `src/purchasing/` | New | PO state machine + accumulation |
| `src/orchestrator/session.py` | Modified | DB-row rehydration |
| `src/orchestrator/router.py` | Modified | Parse step + supplier-selection routing |
| `src/agents/customer.py` | Modified | Per-case confirmation replies |
| `src/backoffice/monitor.py` | Modified | PO view + execution |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Supplier RAG not ready | Med | Protocol seam + fake searcher in tests |
| Greenfield conversation→Order path | High | Smallest slice first: parse → persist → Case A |
| Postgres enum migration | Med | New enum types, additive columns, downgrade scripts |
| Fuzzy date parsing | Med | Date-only storage; explicit spec scenarios |
| Multi-turn loss (30-min TTL) | Med | Sourcing state persisted to DB |

## Rollback Plan

Migrations are additive; revert = downgrade + disable parse-step feature flag (intake keeps legacy routing). No existing four-state transitions modified.

## Dependencies

- External supplier-catalog RAG (in progress) — behind `SupplierCatalogSearcher`.
- Inventory seed strategy (spec phase).

## Success Criteria

- [ ] Full-stock message creates Order with delivery date, sourcing PENDING_ASSEMBLY; reply confirms order number.
- [ ] Partial case lists missing items + suppliers; owner selection creates/accumulates PO (OPEN).
- [ ] No-supplier case sets CANCELLED and notifies.
- [ ] PO progresses to FULLY_RECEIVED via own state machine; owner executes in backoffice.
- [ ] Existing 229 tests green; new code ≥85% coverage.
