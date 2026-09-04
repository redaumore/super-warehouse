# customer-order-persistence Specification

## Purpose

Persist the chat product-selection draft as a customer order: source-aware pricing, frozen RAG snapshots, ARS conversion, backoffice visibility.

## Requirements

### Requirement: Finalize draft into a persisted order

The system MUST, on the finalize intent, persist the draft as an `Order` with one `OrderItem` per line and MUST clear the draft afterwards. The customer MUST be the session customer when set, else the system MUST ask. The customer MUST exist in `clientes`; when not found, the system MUST offer in-chat minimal creation (name and phone, Base price list).

#### Scenario: Session customer attached

- GIVEN a conversation with `customer_id` set
- WHEN the customer finalizes
- THEN an Order with OrderItems is persisted
- AND the draft is cleared

#### Scenario: Customer asked at finalization

- GIVEN a draft with no session customer
- WHEN the customer finalizes
- THEN the system asks for the customer before persisting

#### Scenario: Unknown customer created minimally

- GIVEN a finalize naming an unknown client
- WHEN the customer confirms minimal creation
- THEN the client is created on the Base list and attached

### Requirement: Source-aware base pricing at finalize

The system MUST price each line by source at finalize: LOCAL lines use `costo_proveedor` plus the order-time margin, never `precio_lista_base`; RAG lines use the agent-response price (fallback: the RAG price endpoint) plus the supplier margin from `codigo_proveedor` → `suppliers.code`, or the default margin setting when unmapped.

#### Scenario: Local line priced from cost

- GIVEN a LOCAL line in the draft
- WHEN the order is priced
- THEN the base is `costo_proveedor × (1 + margin)`, never `precio_lista_base`

#### Scenario: Unmapped supplier uses default margin

- GIVEN a RAG line whose `codigo_proveedor` matches no `suppliers.code`
- WHEN the order is priced
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

### Requirement: Save side effects (stock now, Sheets at approval)

The system MUST reserve stock for LOCAL lines at save time; RAG lines MUST NOT reserve stock or sync stock. Sheets sync MUST remain on the existing Case A approval path (register_approved_order), not at save.

#### Scenario: Local lines reserve stock at save

- GIVEN an order with LOCAL lines
- WHEN the order is saved
- THEN stock is reserved for those lines
- AND Sheets is not synced at save

#### Scenario: RAG lines skip stock

- GIVEN an order with RAG lines
- WHEN the order is saved
- THEN no stock is reserved or synced

### Requirement: Order retrieval with lines and totals

The system MUST retrieve persisted orders with lines and ARS totals for the Customer Orders tab.

#### Scenario: Order detail retrieved

- GIVEN a persisted order
- WHEN the backoffice requests its detail
- THEN its lines, prices, and totals are returned