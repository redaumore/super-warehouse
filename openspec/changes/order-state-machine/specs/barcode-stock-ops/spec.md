# Delta for barcode-stock-ops

## ADDED Requirements

### Requirement: Audited cancellation stock restoration

The system MUST restore deducted stock to inventory when an order is canceled from Picking or Ready for delivery, and MUST record a `StockAdjustment` row (reason `order_cancelled`, with actor) for each restoration, preserving the audit trail.

#### Scenario: Late cancel restores stock with audit

- GIVEN a Picking or Ready for delivery order whose stock was already deducted
- WHEN the order is canceled
- THEN the deducted quantity is restored to `Inventory.quantity_on_hand`
- AND a StockAdjustment row records the reason and actor

#### Scenario: Restoration is auditable

- GIVEN a late-cancel stock restoration
- WHEN the adjustment is queried
- THEN the adjustment shows the order, the restored quantity, the reason `order_cancelled`, and the actor
