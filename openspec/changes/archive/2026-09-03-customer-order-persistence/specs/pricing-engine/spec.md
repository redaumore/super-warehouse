# Delta for pricing-engine

## MODIFIED Requirements

### Requirement: Compute base price from cost and margin

The system MUST compute the base price for LOCAL order lines as `Base = costo_proveedor × (1 + margin%)`, where margin is the order-time margin, and MUST NOT use `precio_lista_base` as the base for order lines.
(Previously: base price computed from catalog cost and margin for catalog items in general.)

#### Scenario: Base price computed

- GIVEN an item with cost 925.92 and margin 35%
- WHEN the base price is computed
- THEN the base price is `925.92 × (1 + 0.35) = 1249.99` (HALF_UP to cents)

#### Scenario: Zero margin yields cost

- GIVEN an item with margin 0%
- WHEN the base price is computed
- THEN the base price equals the cost

#### Scenario: List price never used as base

- GIVEN a local item whose `precio_lista_base` differs from `costo_proveedor × (1 + margin)`
- WHEN an order line is priced
- THEN the base is `costo_proveedor` plus margin, not `precio_lista_base`

## ADDED Requirements

### Requirement: Compute base price for RAG-sourced items

The system MUST compute the base price for RAG order lines as `Base = offer_price × (1 + default_margin_pct)`, where `offer_price` is the agent-response price (fallback: the RAG service price endpoint) and `default_margin_pct` comes from `codigo_proveedor` → `suppliers.code` or the configurable default margin when unmapped. Non-ARS offer prices MUST be converted to ARS before subtotal computation.

#### Scenario: Supplier margin applied to RAG line

- GIVEN a RAG line whose `codigo_proveedor` maps to a supplier with margin 25%
- WHEN the line is priced
- THEN the base equals `offer_price × 1.25`

#### Scenario: Fallback endpoint price used

- GIVEN a RAG line without an agent-response price
- WHEN the line is priced
- THEN the RAG service price endpoint supplies price and currency

#### Scenario: Unmapped supplier falls back to default

- GIVEN a RAG line whose `codigo_proveedor` matches no supplier
- WHEN the line is priced
- THEN the configurable default margin is applied

### Requirement: Persist order subtotal and total

The system MUST persist the order `subtotal` (sum of base × quantity per line, in ARS) and `total` (sum of final price × quantity after list and particular discounts, in ARS) on the Order when saved.

#### Scenario: Totals persisted on save

- GIVEN a finalized order with multiple lines
- WHEN the order is saved
- THEN subtotal and total are stored on the Order

#### Scenario: Pending-conversion totals deferred

- GIVEN an order saved pending-conversion
- WHEN the rate is loaded later
- THEN subtotal and total are recomputed and stored