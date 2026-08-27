# Order Sourcing Workflow

Customers describe orders in free language ("…clavos… para el viernes a la
tarde"). The sourcing workflow parses the message, checks the local
inventory, and routes the order to one of three outcomes — full stock,
supplier-sourced partial, or cancelled.

## Flow

```
inbound ──► parse (extract items/date) ──► resolve SKUs ──► availability (Inventory)
                                                                      │
                                          ┌───────────────────────────┴───────────────┐
                                    Case A (all ok)            Case B (partial)        Case C (no supplier)
                                    Order PENDING_ASSEMBLY    Order IN_PREPARATION    Order CANCELLED
                                    → reserve → quote         → list missing+suppliers → notify
                                    → approve (unchanged)     → owner selects → accumulate PO
```

1. **Parse** — the orchestrator's parse step (`src/agents/intake.py`) extracts
   customer name (best effort), items with quantities and the delivery date
   from free text. Turning it off (no `OWNER_PHONE`) keeps the legacy
   conversational intake.
2. **Resolve** — each description is resolved to a catalog SKU
   (`src/agents/disambiguation.py`); unresolvable items are treated as
   missing and reported, never silently dropped.
3. **Classify** — `classify_case` (`src/sourcing/classify.py`) compares each
   item's availability (`Inventory.quantity_on_hand` minus active
   reservations) with the requested quantity and asks the supplier searcher
   for candidates.

## Case matrix

| Case | Condition | OrderEstado | SourcingState | What happens |
|------|-----------|-------------|---------------|--------------|
| A | every item covered by stock | PENDING_APPROVAL → APPROVED (unchanged flow) | PENDING_ASSEMBLY | reserve → quote → owner approves → Inventory deducted |
| B | some item missing AND every missing item has a supplier | PENDING_APPROVAL (approval bypassed) | IN_PREPARATION | list missing + suppliers → owner selects → accumulate OPEN PO per supplier |
| C | some missing item has NO supplier | REJECTED (existing rejection flow releases reservations) | CANCELLED | customer notified the items are unavailable; owner alerted |

The four `OrderEstado` states are untouched: sourcing is a separate axis on
the order.

## Case B multi-turn selection

The reply lists each missing item with numbered supplier options. The owner
replies with the numbers ("1 y 3"). The selection is persisted on
`SourcingNeed` rows (DB source of truth) and the in-memory conversation state
is rehydrated from the database after the 30-minute TTL, so an abandoned
selection survives. Re-selecting a supplier before the PO is executed moves
the quantity between OPEN purchase orders; re-selecting after execution is
refused.

## Purchase order lifecycle

`SupplierPurchaseOrder` accumulates missing items across customer orders —
one OPEN PO per supplier, items aggregated by SKU — and moves through its own
state machine (`src/purchasing/state.py`):

```
OPEN ──send──► SENT ──partial receipt──► PARTIALLY_RECEIVED ──full receipt──► FULLY_RECEIVED
  │             │
  └──cancel─────┴──────────────────────────► CANCELLED
```

Receiving bumps the canonical `Inventory.quantity_on_hand`. The owner executes
transitions (send / receive / cancel) in the backoffice **Purchase Orders**
tab.

## Supplier catalog searcher seam

`src/supplier/searcher.py` exposes the `SupplierCatalogSearcher` protocol
(code + semantic search) that the sourcing workflow consumes to learn which
suppliers can offer a missing item. The external supplier-catalog RAG is not
built yet: production wires `FakeSupplierCatalogSearcher` (empty candidates),
so every missing item classifies as Case C — the safe degraded behavior —
until a real searcher replaces it.

## Feature flag

The sourcing flow is enabled by setting `OWNER_PHONE` (owner notifications
are delivered over Telegram). Leaving it empty disables the parse step and
keeps the legacy intake. Rollback: unset `OWNER_PHONE`, or downgrade the
additive migrations (`alembic downgrade` ×2 — both revisions are safe to
reverse and keep `OrderEstado` untouched).