# Delta for order-lifecycle

## MODIFIED Requirements

### Requirement: Track order state machine

The system MUST track each order through exactly one of the four approval states: Pending Approval, Approved, In Dispatch, or Rejected. Sourcing/fulfillment status is a separate axis (`SourcingState`: PENDING_ASSEMBLY / IN_PREPARATION / CANCELLED) stored in its own column and MUST NOT change or replace the four approval states. The system MUST also store a `delivery_date` on the order.

(Previously: tracked only the four approval states, with no sourcing axis and no delivery date.)

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
