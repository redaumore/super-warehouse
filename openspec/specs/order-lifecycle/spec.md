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

The system MUST process the owner's confirm response as the approval ceremony, applying any requested adjustments before the order leaves Confirmed. Confirm MUST refuse a stale quote with `RequiresRequoteError` when the reservation TTL has expired.

(Previously: approval was a separate post-finalize step.)

#### Scenario: Confirm with adjustment

- GIVEN the owner replies "aprobá pero hacé un 5% de descuento extra en clavos"
- WHEN confirm runs
- THEN the order is confirmed and the adjustment is applied before registration

#### Scenario: Plain confirm

- GIVEN the owner replies "sí, aprobá" with no adjustments
- WHEN confirm runs
- THEN the order is confirmed at the quoted prices

#### Scenario: Stale quote refused

- GIVEN a Draft whose reservation TTL has expired
- WHEN the owner confirms
- THEN the system refuses with RequiresRequoteError and re-quotes

### Requirement: Cancellation releases or restores stock

The system MUST cancel an order from Draft, Confirmed, Picking, or Ready for delivery. Cancel from Draft/Confirmed MUST release ACTIVE reservations; cancel from Picking/Ready for delivery MUST restore deducted stock and record a `StockAdjustment` row.

(Previously: only rejection from Pending Approval released reservations; no late-cancel restore existed.)

#### Scenario: Cancel before fulfillment releases reservations

- GIVEN a Draft or Confirmed order with active reservations
- WHEN it is canceled
- THEN the reservations are released immediately

#### Scenario: Late cancel restores deducted stock

- GIVEN a Picking or Ready for delivery order with deducted stock
- WHEN it is canceled
- THEN the deducted stock is restored and a StockAdjustment row is recorded

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

### Requirement: Register confirmed orders

The system MUST, on confirm, write the order to Google Sheets, convert the soft-lock to a definitive stock deduction, and confirm to the customer — atomically. If the Sheets write fails, the order MUST remain Confirmed, the row quarantined, and the failure surfaced to the owner.

(Previously: registration ran on approval and reverted the order to pending on Sheets failure.)

#### Scenario: Confirmed order registered end-to-end

- GIVEN a Draft being confirmed
- WHEN the confirm ceremony runs
- THEN the row is written to Sheets, the soft-lock becomes a deduction, and the customer is confirmed

#### Scenario: Sheets failure keeps order confirmed

- GIVEN the Sheets append fails during confirm
- WHEN confirm errors
- THEN the order stays Confirmed, the write is quarantined, and the failure is surfaced

### Requirement: Track order state machine

The system MUST track each order through exactly one of six states: Draft, Confirmed, Picking, Ready for delivery, Canceled, or Closed, and MUST enforce these transitions, rejecting all others:

| From | To | Trigger |
|---|---|---|
| Draft | Draft | add / remove product |
| Draft | Confirmed | confirm |
| Draft | Canceled | cancel order |
| Confirmed | Draft | modify |
| Confirmed | Picking | start picking |
| Confirmed | Canceled | cancel order |
| Picking | Ready for delivery | complete picking |
| Picking | Canceled | cancel order |
| Ready for delivery | Closed | deliver |
| Ready for delivery | Canceled | cancel order |

Sourcing remains a separate axis (`SourcingState`: PENDING_ASSEMBLY / IN_PREPARATION / CANCELLED) and MUST NOT change the order state. The system MUST store a `delivery_date`.

(Previously: four approval states — Pending Approval, Approved, In Dispatch, Rejected.)

#### Scenario: Happy path

- GIVEN a Draft order
- WHEN it is confirmed, started picking, completed picking, then delivered
- THEN it moves Draft → Confirmed → Picking → Ready for delivery → Closed

#### Scenario: Modify loops back to draft

- GIVEN a Confirmed order
- WHEN the owner modifies it
- THEN it returns to Draft

#### Scenario: Illegal transition rejected

- GIVEN an order in Ready for delivery
- WHEN a transition other than deliver or cancel order is attempted
- THEN the system rejects it and the state is unchanged

#### Scenario: Sourcing axis independent

- GIVEN an order whose sourcing is PENDING_ASSEMBLY, IN_PREPARATION, or CANCELLED
- WHEN order-state transitions run
- THEN the six states progress independently of the sourcing value

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

### Requirement: Modify confirmed order

The system MUST support Confirmed → Draft (modify) with defined, atomic, and tested reconciliation of Google Sheets, stock, and reservations. The mechanism is a design decision; the spec REQUIRES deterministic, tested behavior with no stock leak or double-count.

#### Scenario: Modify reconciles side effects

- GIVEN a Confirmed order with converted reservations and a Sheets row
- WHEN the owner modifies it
- THEN it returns to Draft and Sheets, stock, and reservations reconcile atomically
