# pricing-engine Specification

## Purpose

Compute base and final prices for catalog items by applying cost margin and the customer's list and particular discounts, in a defined order.

## Requirements

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

### Requirement: Compute final price with list and particular discounts

The system MUST compute the final price as `Final = Base × (1 − list_discount) × (1 − particular_discount)`.

#### Scenario: Both discounts applied

- GIVEN a base price, a list discount, and a particular discount
- WHEN the final price is computed
- THEN the final price equals `Base × (1 − list_discount) × (1 − particular_discount)`

#### Scenario: Only list discount present

- GIVEN a customer with a list discount and no particular discount
- WHEN the final price is computed
- THEN the final price equals `Base × (1 − list_discount)`

### Requirement: Apply discounts multiplicatively in order

The system MUST apply discounts multiplicatively, list discount first then particular discount, and MUST NOT add the percentages.

#### Scenario: Discounts compound, not sum

- GIVEN a base price of 1000, list discount 20%, particular discount 10%
- WHEN the final price is computed
- THEN the result is `1000 × 0.80 × 0.90 = 720`
- AND is NOT `1000 × (1 − 0.30) = 700`

#### Scenario: Order of application is list then particular

- GIVEN both discounts are present
- WHEN the final price is computed
- THEN the list discount is applied to the base price first
- AND the particular discount is applied to the list-discounted price second

### Requirement: Treat absent discounts as zero

The system SHALL treat any absent discount as zero percent.

#### Scenario: No discounts configured

- GIVEN an item with neither list nor particular discount
- WHEN the final price is computed
- THEN the final price equals the base price

#### Scenario: Missing list discount only

- GIVEN a customer with a particular discount but no list discount
- WHEN the final price is computed
- THEN the list discount is treated as 0%
- AND the particular discount is applied to the base price

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
