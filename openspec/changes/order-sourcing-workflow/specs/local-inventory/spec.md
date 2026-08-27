# local-inventory Specification

## Purpose

Track real on-hand stock per SKU and compute availability (on-hand minus active reservations) as the input to the sourcing decision.

## Requirements

### Requirement: Track on-hand stock per SKU

The system MUST persist on-hand stock in an `Inventory` table keyed by a unique `sku_id`, storing `quantity_on_hand` and `updated_at`.

#### Scenario: Stock recorded per SKU

- GIVEN inventory rows exist for catalog SKUs
- WHEN availability is queried
- THEN each SKU returns its on-hand quantity and last-update timestamp

#### Scenario: SKU absent from inventory

- GIVEN an item whose SKU has no inventory row
- WHEN availability is queried
- THEN the item is treated as unavailable (zero on hand)

### Requirement: Compute availability excluding reservations

The system MUST compute a SKU's availability as `quantity_on_hand − sum(active reservations)`.

#### Scenario: Active reservation reduces availability

- GIVEN a SKU with quantity_on_hand = 10 and an ACTIVE reservation of 4
- WHEN availability is queried
- THEN availability equals 6

#### Scenario: Released or expired reservations do not reduce availability

- GIVEN a SKU with quantity_on_hand = 10 and reservations of 4 that are RELEASED or EXPIRED
- WHEN availability is queried
- THEN availability equals 10

### Requirement: Seed inventory from catalog stock

The system MUST seed `Inventory` from each catalog product's `stock_disponible` as the initial backfill; later stock changes MUST update `Inventory` and its `updated_at`.

#### Scenario: Initial backfill

- GIVEN catalog products carrying a `stock_disponible` value
- WHEN inventory is seeded
- THEN each SKU's `quantity_on_hand` mirrors that value

#### Scenario: Stock adjustments update inventory

- GIVEN a confirmed stock adjustment or supplier-ingestion confirmation changes a SKU's stock
- WHEN the change is applied
- THEN `quantity_on_hand` and `updated_at` are updated in `Inventory`
