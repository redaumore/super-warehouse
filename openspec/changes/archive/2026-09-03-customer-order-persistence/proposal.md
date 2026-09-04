# Proposal: Customer Order Persistence (chat draft → DB → backoffice)

## Intent

Persist the Telegram product-selection draft as a customer order: source-aware per-line pricing (denomination base → ARS → supplier margin → list discount), frozen RAG line snapshots, and a backoffice "Customer Orders" section. The draft never reaches the DB today.

## Scope

### In Scope
- Finalize intent: price `draft_items`, attach an existing `clientes` customer, persist Order + OrderItems.
- Base price: local = `costo_proveedor` + order-time margin (never `precio_lista_base`); RAG = agent-response `precio` (else RAG table) + `default_margin_pct` via `codigo_proveedor` → `suppliers.code`; no match → configurable default.
- Manual exchange-rate table + backoffice maintenance; convert only when currency ≠ ARS.
- RAG lines = frozen snapshots (sku, name, price, currency, supplier, source), no `catalogo` FK.
- On save: reserve stock for local items + Sheets sync via Case A; RAG items skip stock.
- Missing rate: save pending-conversion; recompute on rate load.
- Customer: reuse session one, else ask at finalization; in-chat minimal creation fallback.
- Backoffice: "Customer Orders" tab (orders, lines, totals), rate/margin maintenance.

### Out of Scope
- `descuento_particular_pct` (follow-up); RAG stock; Case B; list-price CRUD.

## Capabilities

### New
- `customer-order-persistence`: finalize, source-aware pricing, snapshots, rates, Customer Orders tab.

### Modified
- `pricing-engine`: per-source base-price rule; persist subtotal/total on Order.
- `backoffice`: new tab + rate/margin UI.
- `supplier-management`: `default_margin_pct` also consumed at order time (RAG lines).
- `clients-and-price-lists`: finalize-time attachment via name resolution (not the stale phone rule).

## Approach

Exploration approach 3: pure `order_pricing.py` module (injectable rate source) feeding `compute_base`/`compute_final`; persist mirrors `persist_case_a_order`; loosen routing so drafts reach CUSTOMER; retain `codigo_proveedor` on `RagProduct`.

## Affected Areas

| Area | Impact |
|------|--------|
| `src/pricing/order_pricing.py` | New |
| `src/db/models.py`, `alembic/versions/` | Order/OrderItem columns, `exchange_rates` |
| `src/agents/customer.py`, `src/orchestrator/router.py` | Finalize intent, routing |
| `src/integrations/rag.py` | Retain `codigo_proveedor` |
| `src/backoffice/` | Customer Orders tab |
| `tests/`, `tests/conftest.py` | TRUNCATE_TABLES, new cases |

## Open Questions

1. RAG price fallback: new RAG endpoint vs direct DB read; leaning endpoint, owner pending.
2. Default-margin storage: DB-backed backoffice setting proposed; value owner pending.

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Double margin (local) | High | Pin rule in spec |
| Routing dead path | Med | Draft-state flag |
| RAG SKU/currency hygiene | Med | `normalize_rag_sku`; whitelist codes |
| No ListaPrecios rows in prod | Med | Seed or fail-visible |
| Working-tree entanglement | Med | Branch off `feat/rag-product-query` |

## Rollback Plan

Reversible migration (`down()` drops new columns/table); revert routing + tab wiring. Engine and Case A untouched.

## Dependencies

- Base branch `feat/rag-product-query` (PR #10); RAG reachable.

## Success Criteria

- [ ] RAG + local draft persists with correct ARS totals.
- [ ] Local = `costo_proveedor` + margin; RAG = snapshot + supplier margin.
- [ ] Missing rate → pending-conversion order; rate load recomputes.
- [ ] Tab lists orders, lines, totals; rates/margin editable.
- [ ] Stock + Sheets local lines only.
