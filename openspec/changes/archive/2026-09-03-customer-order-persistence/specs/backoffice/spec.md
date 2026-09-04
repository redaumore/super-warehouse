# Delta for backoffice

## ADDED Requirements

### Requirement: Customer Orders tab

The system MUST provide a Customer Orders tab listing persisted orders with state, customer, and ARS totals, and MUST show line-item detail (SKU, name, quantity, prices, source) per order.

#### Scenario: Orders listed with totals

- GIVEN persisted orders
- WHEN the owner opens the Customer Orders tab
- THEN orders show state, customer, and total

#### Scenario: Line detail per order

- GIVEN an order in the list
- WHEN the owner opens its detail
- THEN lines show SKU, name, quantity, prices, and source

### Requirement: Exchange rate maintenance

The system MUST provide a rate maintenance view editing `rate_to_ars` per currency, MUST NOT allow editing ARS, and MUST trigger recomputation of pending-conversion orders when a rate is saved.

#### Scenario: Rate edited

- GIVEN a USD rate row
- WHEN the owner saves a new rate
- THEN the rate and timestamp are stored
- AND pending-conversion orders recompute their totals

#### Scenario: ARS rate not editable

- GIVEN the ARS currency row
- WHEN the owner tries to edit it
- THEN the edit is rejected

### Requirement: Default margin maintenance

The system MUST expose the default supplier margin (used when `codigo_proveedor` does not map to `suppliers.code`) as an editable setting, seeded at 20%.

#### Scenario: Default margin edited

- GIVEN the default margin setting
- WHEN the owner saves a new value
- THEN subsequent unmapped RAG lines use the new value

#### Scenario: Seed value present

- GIVEN a fresh database
- WHEN the setting is first read
- THEN it equals 20%