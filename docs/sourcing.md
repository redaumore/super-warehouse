# Order Sourcing Workflow

The OWNER describes orders in free language ("…para Don Juan, 10 clavos… para
el viernes a la tarde"). The sourcing workflow parses the message, resolves the
CUSTOMER BY NAME, checks the local inventory, and routes the order to one of
three outcomes — full stock, supplier-sourced partial, or cancelled. Quotes,
cancellations and approvals all travel in the owner's chat; the legacy
Telegram push to `OWNER_PHONE` was removed.

## Flow

```
owner message ──► gate (owner sender) ──► parse (name + items + date)
       ──► resolve customer by name ──► resolve SKUs ──► availability (Inventory)
                                                              │
                                        ┌──────────────────────┴───────────────┐
                                  Case A (all ok)         Case B (partial)       Case C (no supplier)
                                  Order PENDING_ASSEMBLY  Order IN_PREPARATION   Order CANCELLED
                                  → reserve → quote       → list missing+suppliers → in-chat cancellation
                                  → approve (in chat)     → owner selects → accumulate PO
```

1. **Gate** — `handle_inbound` (`src/pipeline.py`) rejects any sender that is
   not the configured owner (`src/orchestrator/owner.py`) before routing.
2. **Parse** — the orchestrator's parse step (`src/agents/intake.py`) extracts
   the customer name (best effort), items with quantities and the delivery date
   from free text. Clearing both owner keys (`OWNER_TELEGRAM_CHAT_ID` /
   `OWNER_WHATSAPP_PHONE`) disables the parse step and keeps the legacy
   conversational intake (rollback path).
3. **Resolve** — `resolve_customer_name` (`src/agents/customers.py`) matches
   the parsed name against `Cliente.nombre_comercial`: exact first, then
   accent/case-folded containment. One match auto-selects; two or more show a
   numbered disambiguation menu (the pick arrives on a later turn); zero offers
   in-chat creation via `nuevo cliente <nombre> <teléfono>` (duplicate phones
   report the existing client). Each description is then resolved to a catalog
   SKU (`src/agents/disambiguation.py`); unresolvable items are treated as
   missing and reported, never silently dropped.
4. **Classify** — `classify_case` (`src/sourcing/classify.py`) compares each
   item's availability (`Inventory.quantity_on_hand` minus active
   reservations) with the requested quantity and asks the supplier searcher
   for candidates.

## Case matrix

| Case | Condition | OrderEstado | SourcingState | What happens |
|------|-----------|-------------|---------------|--------------|
| A | every item covered by stock | DRAFT → CONFIRMED (confirm ceremony) | PENDING_ASSEMBLY | reserve at quote → confirm in chat → classify at confirm → Sheets + Inventory deducted |
| B | some item missing AND every missing item has a supplier | DRAFT → CONFIRMED (on selection) | PENDING_ASSEMBLY → IN_PREPARATION (on selection) | list missing + suppliers → owner selects → accumulate OPEN PO per supplier |
| C | some missing item has NO supplier | DRAFT → CANCELED (cancel path) | CANCELLED | owner notified in chat the items are unavailable |

The six `OrderEstado` states are independent of the sourcing axis: sourcing
never drives the order state.

## Case A confirmation

The quote is the agent's in-chat reply ("…¿Lo aprobás? Respondé 'aprobá' o
'rechazá'"). The owner's reply routes to the wired DISPATCH agent
(`src/agents/dispatch.py::build_dispatch_handler`): `parse_decision` →
`apply_decision` → `confirm_and_register` (classify → convert → Sheets →
deduct). A `pedido #N` reference targets a specific order instead of the
latest DRAFT. If the Sheets write quarantines, the failure is tolerated — the
order stays CONFIRMED and the owner gets the error surfaced in chat (spec:
the order MUST remain Confirmed).

## Case B multi-turn selection

The reply lists each missing item with numbered supplier options. The owner
replies with the numbers ("1 y 3"). The selection is persisted on
`SourcingNeed` rows (DB source of truth) and the in-memory conversation state
is rehydrated from the database after the 30-minute TTL, so an abandoned
selection survives. Re-selecting a supplier before the PO is executed moves
the quantity between OPEN purchase orders; re-selecting after execution is
refused.

## Rehydration (owner-keyed)

`rehydrate_conversation` (`src/orchestrator/session.py`) rebuilds the OWNER's
conversation from the LATEST DRAFT ORDER ACROSS ALL CUSTOMERS (no owner entity —
the latest draft IS the owner's). An explicit `pedido #N` reference
overrides to a specific order.

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

The sourcing flow is enabled by configuring an owner sender key (either
channel). Leaving both empty disables the parse step and keeps the legacy
intake. Rollback: clear the two owner keys, or revert + redeploy; the
deprecated `OWNER_PHONE` stays parseable and ignored.