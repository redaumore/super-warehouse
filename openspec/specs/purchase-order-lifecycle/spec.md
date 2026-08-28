# purchase-order-lifecycle Specification

## Purpose

Model the owner's supplier purchase order (`SupplierPurchaseOrder`) that accumulates missing items across multiple customer orders and moves through its own state machine.

## Requirements

### Requirement: Track purchase-order state machine

The system MUST track each `SupplierPurchaseOrder` through exactly one of: OPEN, SENT, PARTIALLY_RECEIVED, FULLY_RECEIVED, or CANCELLED, with transitions owned by a dedicated module.

#### Scenario: Legal transitions

- GIVEN a purchase order in OPEN
- WHEN the owner sends it to the supplier
- THEN it moves to SENT
- AND upon partial receipt it moves to PARTIALLY_RECEIVED
- AND upon full receipt it moves to FULLY_RECEIVED (terminal)

#### Scenario: Cancellation

- GIVEN a purchase order in OPEN or SENT
- WHEN the owner cancels it
- THEN it moves to CANCELLED (terminal)

#### Scenario: Invalid transition rejected

- GIVEN a purchase order in FULLY_RECEIVED or CANCELLED
- WHEN a further transition is attempted
- THEN the system rejects it as invalid

### Requirement: Accumulate items across customer orders

The system MUST accumulate missing items into an existing OPEN purchase order for the same supplier, rather than creating duplicates.

#### Scenario: Second order merges into existing OPEN PO

- GIVEN an OPEN purchase order for supplier X holding an item
- WHEN a later customer order selects supplier X for the same or new missing items
- THEN the items are added to the existing purchase order

### Requirement: Group items by supplier

The system MUST create one purchase order per selected supplier, holding only that supplier's missing items.

#### Scenario: Multiple suppliers produce multiple POs

- GIVEN missing items sourced from suppliers X and Y
- WHEN the owner selects both
- THEN a purchase order is created for X and another for Y, each with its own items

### Requirement: Refuse inactive suppliers

The system MUST refuse INACTIVO suppliers in `open_or_create_po` and `accumulate_need`.

#### Scenario: PO creation refuses INACTIVO

- GIVEN a supplier set to INACTIVO
- WHEN `open_or_create_po` is called for it
- THEN the system rejects the operation

#### Scenario: Accumulation refuses INACTIVO

- GIVEN a supplier set to INACTIVO
- WHEN `accumulate_need` is called for it
- THEN the system rejects the operation
