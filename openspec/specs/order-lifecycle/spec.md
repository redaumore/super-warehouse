# order-lifecycle Specification

## Purpose

Manage an order from quotation through owner approval, inventory reservation (soft-lock), and final registration, including the full state machine and its time-based release rules.

## Requirements

### Requirement: Soft-lock inventory on quotation

The system MUST place a soft-lock reservation on ordered stock when a quotation is issued or sent for owner approval, with a 30-minute TTL.

#### Scenario: Reservation created on quotation

- GIVEN a quotation is generated for a customer order
- WHEN the quotation is issued
- THEN the ordered quantities are reserved for that customer
- AND the reservation carries a 30-minute TTL

#### Scenario: Reserved stock excluded from availability

- GIVEN an active reservation exists for a SKU
- WHEN another customer's availability is checked
- THEN available stock is computed as `stock_disponible − sum(stock_reservations)`
- AND the reserved quantity is not double-sold

### Requirement: Owner approval with adjustments

The system MUST process the owner's approval response, applying any requested adjustments (e.g. an extra discount on specific items).

#### Scenario: Approval with a custom adjustment

- GIVEN the owner replies "aprobá pero hacé un 5% de descuento extra en clavos"
- WHEN the response is processed
- THEN the order is approved
- AND the requested adjustment is applied to the affected items before registration

#### Scenario: Plain approval

- GIVEN the owner replies "sí, aprobá" with no adjustments
- WHEN the response is processed
- THEN the order is approved with the previously quoted prices unchanged

### Requirement: Rejection releases reservations

The system MUST immediately release all soft-lock reservations when the owner rejects an order.

#### Scenario: Order rejected

- GIVEN the owner rejects a quoted order
- WHEN the rejection is processed
- THEN all reservations for that order are released immediately
- AND the reserved stock becomes available to other customers

### Requirement: Auto-release after 30-minute TTL

The system MUST automatically release reservations when the owner does not approve within the 30-minute window.

#### Scenario: TTL expiry auto-releases

- GIVEN an order has been awaiting approval for 30 minutes with no decision
- WHEN the reservation TTL expires
- THEN the reservation is automatically released
- AND the stock becomes available again

#### Scenario: Expired order cannot be approved silently

- GIVEN an order's reservation expired
- WHEN the owner later attempts to approve it
- THEN the system does not proceed on stale reservations
- AND re-quotes or re-confirms availability before registration

### Requirement: Register approved orders

The system MUST, on approval, write the order to Google Sheets, deduct stock definitively, and confirm to the customer.

#### Scenario: Approved order registered end-to-end

- GIVEN an order is approved by the owner
- WHEN registration runs
- THEN the order row is written to Google Sheets
- AND the soft-lock is converted to a definitive stock deduction
- AND a confirmation is sent to the customer

#### Scenario: Registration failure is surfaced

- GIVEN the Sheets write or stock deduction fails
- WHEN registration errors
- THEN the failure is surfaced to the owner
- AND the order is not left in a half-registered state without notice

### Requirement: Track order state machine

The system MUST track each order through exactly one of the four approval states: Pending Approval, Approved, In Dispatch, or Rejected. Sourcing/fulfillment status is a separate axis (`SourcingState`: PENDING_ASSEMBLY / IN_PREPARATION / CANCELLED) stored in its own column and MUST NOT change or replace the four approval states. The system MUST also store a `delivery_date` on the order.

#### Scenario: State transitions on the happy path

- GIVEN a new order
- WHEN it is quoted and awaits the owner
- THEN it is in Pending Approval
- AND upon approval it moves to Approved
- AND upon dispatch it moves to In Dispatch

#### Scenario: Rejection path

- GIVEN an order in Pending Approval
- WHEN the owner rejects it
- THEN it moves to Rejected
- AND its reservations are released

#### Scenario: Sourcing axis is independent of approval

- GIVEN an order whose sourcing is PENDING_ASSEMBLY, IN_PREPARATION, or CANCELLED
- WHEN the approval flow runs
- THEN the four approval states progress independently
- AND the sourcing value does not alter the approval state

### Requirement: Quote response SLA

The system SHALL return a quotation to the customer within 3 minutes of the order being accepted.

#### Scenario: Quote delivered within SLA

- GIVEN an accepted order that resolves without extensive back-and-forth
- WHEN the quotation is generated
- THEN the customer receives it in under 3 minutes

#### Scenario: SLA tracked for pilot

- GIVEN real pilot orders
- WHEN quote times are measured
- THEN quote delivery time is recorded as a KPI against the 3-minute target

### Requirement: Voice approval adoption

The system SHALL support owner approval by voice, targeting at least 90% of approvals made by voice response.

#### Scenario: Voice approval accepted

- GIVEN the owner responds with a short voice note approving an order
- WHEN the response is processed
- THEN the system recognizes the approval intent and proceeds

#### Scenario: Adoption measured

- GIVEN approvals during the pilot
- WHEN the share of voice-based approvals is measured
- THEN the target is at least 90% of approvals by voice
