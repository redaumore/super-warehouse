# Delta for backoffice

## ADDED Requirements

### Requirement: Purchase order view and execution

The system MUST provide a backoffice module listing `SupplierPurchaseOrder`s with their state, and MUST let the owner execute transitions: send (OPEN → SENT), receive (partial/full), and cancel.

#### Scenario: Owner sends a purchase order

- GIVEN an OPEN purchase order in the backoffice
- WHEN the owner executes "send to supplier"
- THEN the purchase order moves to SENT

#### Scenario: Owner records partial then full receipt

- GIVEN a SENT purchase order
- WHEN the owner records a partial receipt
- THEN it moves to PARTIALLY_RECEIVED
- AND when the remaining quantity is received it moves to FULLY_RECEIVED

#### Scenario: Owner cancels a purchase order

- GIVEN an OPEN or SENT purchase order
- WHEN the owner cancels it
- THEN it moves to CANCELLED
