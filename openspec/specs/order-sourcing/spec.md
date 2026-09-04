# order-sourcing Specification

## Purpose

Decide how a parsed customer order is fulfilled — full stock (PENDING_ASSEMBLY), supplier-sourced partial (IN_PREPARATION), or cancelled (CANCELLED) — and drive multi-turn supplier selection.

## Requirements

### Requirement: Classify sourcing case from availability

The system MUST compute each item's availability and classify the order: Case A (all items available), Case B (some missing with a supplier), or Case C (some missing with no supplier). Classification MUST run at confirm, where availability may have changed. `SourcingState` remains an informational axis and MUST NOT change or drive the order state.

(Previously: classification ran once at order creation.)

#### Scenario: Full stock is Case A

- GIVEN every ordered item's availability covers its quantity
- WHEN the order is classified
- THEN it is Case A with sourcing PENDING_ASSEMBLY

#### Scenario: Quantity exceeding stock is partial

- GIVEN an item's quantity exceeds its availability
- WHEN the order is classified
- THEN the missing portion routes to Case B or Case C

#### Scenario: Missing item with no supplier is Case C

- GIVEN a missing item for which no supplier is found
- WHEN the order is classified
- THEN it is Case C with sourcing CANCELLED

#### Scenario: Item unknown to catalog or inventory

- GIVEN an item that resolves to no catalog SKU or no inventory row
- WHEN the order is classified
- THEN the item is treated as missing and reported, never silently dropped

#### Scenario: Classification runs at confirm

- GIVEN an order whose availability changed while it was Draft
- WHEN the order is confirmed
- THEN the case is classified from the latest availability

### Requirement: Case A creates order via quotation flow

The system MUST create the Order as DRAFT at first add, then on confirm transition it to CONFIRMED with pricing and reservations, and reply in the owner's chat (with the `pedido #N` reference) confirming availability, delivery date, and order number.

(Previously: created the Order at finalize and routed it through a separate approval flow.)

#### Scenario: Full-stock order confirmed in owner chat

- GIVEN a Case A order
- WHEN it is confirmed
- THEN the Order is CONFIRMED with sourcing PENDING_ASSEMBLY and a delivery date
- AND the owner's chat reply confirms availability, delivery date, and the `pedido #N` number

#### Scenario: Confirm TTL and re-quote still apply

- GIVEN a Case A order in PENDING_ASSEMBLY
- WHEN it is confirmed
- THEN the existing reservation TTL and re-quote rules apply unchanged

### Requirement: Case B lists missing items and suppliers

The system MUST reply in the owner's chat listing the missing items and, per item, the candidate suppliers from the `SupplierCatalogSearcher`, and the owner answers the selection in chat.

(Previously: the supplier-selection question was answered by the customer sender.)

#### Scenario: Missing items with supplier options

- GIVEN a Case B order with missing items
- WHEN the reply is composed
- THEN the owner's chat lists each missing item and its supplier candidates

### Requirement: Multi-turn supplier selection persisted on the order

The system MUST persist the owner's supplier selections and the missing items on the Order row (database source of truth) so selection survives the in-memory 30-minute TTL.

#### Scenario: Selection survives TTL

- GIVEN the owner has begun selecting suppliers for a Case B order
- WHEN the in-memory conversation state expires
- THEN the pending selections and missing items are recoverable from the database

#### Scenario: Re-selection before execution

- GIVEN the owner selected supplier X but has not yet executed the purchase order
- WHEN the owner changes the selection to supplier Y
- THEN the selection is updated on the order before any purchase order is sent

### Requirement: Case B creates or accumulates purchase orders

The system MUST, on confirmed selection, create (or accumulate into) an OPEN `SupplierPurchaseOrder` per selected supplier with the missing items, and set sourcing IN_PREPARATION. The order itself enters CONFIRMED (and later PICKING) independent of PO progress.

(Previously: sourcing IN_PREPARATION was set at order creation, not as part of the new state machine.)

#### Scenario: Purchase order created on selection

- GIVEN the owner selects a supplier for the missing items
- WHEN the selection is confirmed
- THEN an OPEN purchase order is created (or accumulated) for that supplier
- AND sourcing is set to IN_PREPARATION

### Requirement: Case C notifies unavailability

The system MUST cancel the order via the cancel path and notify the owner in chat when missing items cannot be sourced.

(Previously: notified the customer over the customer-facing channel.)

#### Scenario: No-supplier order cancelled

- GIVEN a Case C order
- WHEN it is processed
- THEN the order is Canceled via the cancel path
- AND the owner's chat reply reports the missing items are unavailable

### Requirement: Capture delivery date

The system MUST extract the customer's delivery date (fuzzy phrases resolved to a concrete date) and store it on the order; the date is informational only.

#### Scenario: Fuzzy date resolved

- GIVEN a message like "para el viernes a la tarde"
- WHEN the order is parsed
- THEN a concrete delivery date is resolved and stored
- AND the date does not drive scheduling

#### Scenario: Missing delivery date tolerated

- GIVEN a message with no delivery date
- WHEN the order is parsed
- THEN the order proceeds with a null delivery date

### Requirement: Case B cancellation policy

The system MUST apply a defined, tested policy when a Case B order is canceled with OPEN/SENT purchase orders and `SourcingNeed` rows. Whether to cancel, retain, or detach is a design decision; the spec REQUIRES consistent behavior with no orphaned supplier work.

#### Scenario: Cancel a Picking Case B order

- GIVEN a Case B order in Picking with OPEN/SENT POs and SourcingNeed rows
- WHEN it is canceled
- THEN the POs and SourcingNeed rows are handled per the defined policy
- AND no supplier work is left inconsistent