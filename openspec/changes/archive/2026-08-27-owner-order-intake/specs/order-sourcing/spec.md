# Delta for order-sourcing

## MODIFIED Requirements

### Requirement: Case A creates order via quotation flow

The system MUST create the Order, route it through the quotation/approval flow, and reply in the owner's chat (with the `pedido #N` reference) confirming availability, delivery date, and order number. The separate Telegram push to `owner_phone` is removed.

(Previously: quoted to the customer and pushed to the owner's phone over a separate Telegram notification.)

#### Scenario: Full-stock order confirmed in owner chat

- GIVEN a Case A order
- WHEN it is persisted
- THEN the Order is created with sourcing PENDING_ASSEMBLY and a delivery date
- AND the owner's chat reply confirms availability, delivery date, and the `pedido #N` number

#### Scenario: Approval TTL and re-quote still apply

- GIVEN a Case A order in PENDING_ASSEMBLY
- WHEN it is quoted and sent for owner approval in chat
- THEN the existing reservation TTL and re-quote rules apply unchanged

### Requirement: Case B lists missing items and suppliers

The system MUST reply in the owner's chat listing the missing items and, per item, the candidate suppliers from the `SupplierCatalogSearcher`, and the owner answers the selection in chat.

(Previously: the supplier-selection question was answered by the customer sender.)

#### Scenario: Missing items with supplier options

- GIVEN a Case B order with missing items
- WHEN the reply is composed
- THEN the owner's chat lists each missing item and its supplier candidates

### Requirement: Case C notifies unavailability

The system MUST set sourcing CANCELLED and notify the owner in chat when missing items cannot be sourced.

(Previously: notified the customer over the customer-facing channel.)

#### Scenario: No-supplier order cancelled

- GIVEN a Case C order
- WHEN it is processed
- THEN sourcing is set to CANCELLED
- AND the owner's chat reply reports the missing items are unavailable
