# customer-order-persistence Specification

## Purpose

Persist the chat product-selection draft as a customer order: source-aware pricing, frozen RAG snapshots, ARS conversion, backoffice visibility.

## Requirements

### Requirement: Finalize draft into a persisted order

The system MUST persist the draft as an `Order` with `estado=DRAFT` at the FIRST product add, with one `OrderItem` per line and the customer resolved or created at that moment. The confirm intent MUST transition Draft → Confirmed. The customer MUST exist in `clientes`; when not found, the system MUST offer in-chat minimal creation (name and phone, Base price list).

(Previously: the draft was memory-only and persisted only at finalize as PENDING_APPROVAL, with the customer resolved at finalize.)

#### Scenario: Draft persisted at first add

- GIVEN a customer adding their first product
- WHEN the product is added
- THEN an Order with estado=DRAFT is persisted
- AND the customer is resolved or created and attached

#### Scenario: Unknown customer created minimally

- GIVEN a first add naming an unknown client
- WHEN the customer confirms minimal creation
- THEN the client is created on the Base list and attached to the draft

### Requirement: Source-aware base pricing at finalize

The system MUST price each line by source at confirm: LOCAL lines use `costo_proveedor` plus the order-time margin, never `precio_lista_base`; RAG lines use the agent-response price plus the supplier margin from `codigo_proveedor` → `suppliers.code`, or the default margin setting when unmapped.

(Previously: pricing ran at finalize; it now runs on the confirm transition.)

#### Scenario: Local line priced from cost

- GIVEN a LOCAL line in the draft
- WHEN the order is confirmed
- THEN the base is `costo_proveedor × (1 + margin)`, never `precio_lista_base`

#### Scenario: Unmapped supplier uses default margin

- GIVEN a RAG line whose `codigo_proveedor` matches no `suppliers.code`
- WHEN the order is confirmed
- THEN the default margin setting is applied

### Requirement: Frozen RAG line snapshots

The system MUST persist RAG lines as frozen snapshots (sku, name, price, currency, supplier, source), MUST NOT set a `catalogo` FK, and MUST normalize RAG SKUs before persisting.

#### Scenario: Snapshot without catalog link

- GIVEN a RAG line with no `catalogo` row
- WHEN the order is persisted
- THEN the line keeps its snapshot fields and no `catalogo` FK

#### Scenario: SKU normalized

- GIVEN a RAG SKU with a doubled prefix (e.g. `AMX-AMX-AT-5044`)
- WHEN the line is persisted
- THEN the normalized SKU is stored

### Requirement: Exchange-rate conversion

The system MUST convert line prices to ARS when the line currency is not ARS, via the `exchange_rates` table. When no rate exists, the system MUST save the order as pending-conversion and MUST recompute totals once a rate loads.

#### Scenario: Non-ARS line converted

- GIVEN a USD-priced RAG line and a rate row
- WHEN the order is priced
- THEN line prices and totals are stored in ARS

#### Scenario: Missing rate defers conversion

- GIVEN a USD line without a rate
- WHEN the order is persisted
- THEN the order is saved pending-conversion
- AND totals are recomputed when the rate loads

### Requirement: Save side effects (reserve and sync at confirm)

The system MUST reserve and deduct stock for LOCAL lines and sync Sheets during the confirm transition, not at draft save. RAG lines MUST NOT reserve stock or sync stock.

(Previously: stock was reserved for LOCAL lines at save and Sheets synced at approval.)

#### Scenario: Local lines reserve at confirm

- GIVEN an order with LOCAL lines being confirmed
- WHEN confirm runs
- THEN stock is reserved and then deducted for those lines
- AND Sheets is synced at confirm

#### Scenario: RAG lines skip stock

- GIVEN an order with RAG lines
- WHEN confirm runs
- THEN no stock is reserved or synced for RAG lines

### Requirement: Order retrieval with lines and totals

The system MUST retrieve persisted orders with lines and ARS totals for the Customer Orders tab.

#### Scenario: Order detail retrieved

- GIVEN a persisted order
- WHEN the backoffice requests its detail
- THEN its lines, prices, and totals are returned

### Requirement: Single draft per customer

The system MUST enforce at most one DRAFT Order per customer. A concurrent second draft-add for the same customer MUST fail safely without corrupting the existing draft.

#### Scenario: Second draft rejected

- GIVEN a customer with an existing DRAFT order
- WHEN a second draft is created for them
- THEN it is rejected and the existing draft is preserved

#### Scenario: Concurrent add races safely

- GIVEN two adds racing to create a first draft for the same customer
- WHEN both attempt to persist
- THEN exactly one draft survives and the other fails cleanly

### Requirement: Add and remove products on a persisted draft

The system MUST let products be added to and removed from a persisted Draft across time, updating `OrderItem` rows accordingly.

#### Scenario: Remove product is real

- GIVEN a persisted Draft with items
- WHEN a product is removed
- THEN its OrderItem is deleted and the draft persists

#### Scenario: Add product after resume

- GIVEN a persisted Draft resumed in a later session
- WHEN a product is added
- THEN the new OrderItem is appended to the same draft