# Delta for backoffice

## MODIFIED Requirements

### Requirement: Live order monitor

The system MUST provide a live monitor showing incoming orders, their state, and synchronization with Google Sheets.

(Previously: showed the four approval states.)

#### Scenario: Monitor order states

- GIVEN the owner opens the order monitor
- WHEN orders are active
- THEN the monitor shows each order's state (Draft, Confirmed, Picking, Ready for delivery, Canceled, Closed)
- AND reflects its soft-lock status

#### Scenario: Sheets synchronization visible

- GIVEN an order is registered to Google Sheets
- WHEN the monitor refreshes
- THEN the registration status is reflected in the monitor view

### Requirement: Customer Orders tab

The system MUST provide a Customer Orders tab listing persisted orders with state, customer, and ARS totals, MUST show line-item detail (SKU, name, quantity, prices, source), and MUST expose fulfillment actions on eligible orders.

(Previously: the tab was read-only with no actions.)

#### Scenario: Orders listed with totals

- GIVEN persisted orders
- WHEN the owner opens the Customer Orders tab
- THEN orders show state, customer, and total

#### Scenario: Line detail per order

- GIVEN an order in the list
- WHEN the owner opens its detail
- THEN lines show SKU, name, quantity, prices, and source

#### Scenario: Actions shown only when legal

- GIVEN an order in the list
- WHEN its state is inspected
- THEN only the legal next-state actions are shown (e.g. start picking on Confirmed)

## ADDED Requirements

### Requirement: Fulfillment actions

The system MUST provide backoffice actions that execute the order transitions: start picking (Confirmed → Picking), complete picking (Picking → Ready for delivery), deliver (Ready for delivery → Closed), and cancel order (Draft/Confirmed/Picking/Ready for delivery → Canceled). Each action MUST commit atomically and refresh the list.

#### Scenario: Start picking

- GIVEN a Confirmed order in the Customer Orders tab
- WHEN the owner executes "start picking"
- THEN the order moves to Picking

#### Scenario: Complete picking

- GIVEN a Picking order
- WHEN the owner executes "complete picking"
- THEN the order moves to Ready for delivery

#### Scenario: Deliver

- GIVEN a Ready for delivery order
- WHEN the owner executes "deliver"
- THEN the order moves to Closed

#### Scenario: Cancel from any eligible state

- GIVEN an order in Draft, Confirmed, Picking, or Ready for delivery
- WHEN the owner executes "cancel order"
- THEN the order moves to Canceled with the state-specific side effects
