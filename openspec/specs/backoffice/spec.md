# backoffice Specification

## Purpose

Provide a lightweight web interface for the owner to manage supplier master data, supplier document ingestion, catalog and stock, clients and price lists, and live order monitoring.

## Requirements

### Requirement: Supplier document ingestion module

The system MUST provide a backoffice module for uploading supplier remito/invoice images or PDFs and confirming extracted data before entry.

#### Scenario: Upload and preview

- GIVEN the owner opens the ingestion module
- WHEN they drop or select a supplier document
- THEN the extracted item grid (code, description, quantity, supplier cost) is displayed for review

#### Scenario: Confirm entry to inventory

- GIVEN a previewed grid of extracted items
- WHEN the owner clicks "Confirmar e Ingresar a Inventario"
- THEN the confirmed items are written to inventory and catalog

### Requirement: Catalog and stock editor

The system MUST provide a module to view and edit catalog products, margins, prices, and current stock.

#### Scenario: View catalog fields

- GIVEN the owner opens the catalog module
- WHEN they browse products
- THEN each product shows SKU, barcode, description, supplier cost, margin, list price, and current stock

#### Scenario: Quick stock/price edit

- GIVEN the owner edits a product's stock or price
- WHEN the edit is saved
- THEN the change is reflected in subsequent pricing and availability

### Requirement: Clients and price lists module

The system MUST provide a module to manage clients, their assigned price list (Gremio A / Gremio B / Base), and their particular discount.

#### Scenario: Manage a client's commercial condition

- GIVEN the owner opens the clients module
- WHEN they edit a client
- THEN they can set the WhatsApp phone, commercial name, assigned price list, and particular discount

#### Scenario: New client registered

- GIVEN a flagged unknown phone from an order
- WHEN the owner registers it in the clients module
- THEN the client record is created with its list and discount

### Requirement: Live order monitor

The system MUST provide a live monitor showing incoming orders, their state, and synchronization with Google Sheets.

#### Scenario: Monitor order states

- GIVEN the owner opens the order monitor
- WHEN orders are active
- THEN the monitor shows each order's state (Pending Approval, Approved, In Dispatch, Rejected)
- AND reflects its soft-lock status

#### Scenario: Sheets synchronization visible

- GIVEN an order is registered to Google Sheets
- WHEN the monitor refreshes
- THEN the registration status is reflected in the monitor view

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

### Requirement: Supplier management module

The system MUST provide a sixth "Suppliers" tab listing suppliers with quick search by CUIT/business_name/code and a status filter, edit and toggle-status row actions, and a create/edit form with reactive front-and-back validation, a code assistant field, and default margin and IVA condition inputs.

#### Scenario: List with quick search and filter

- GIVEN the owner opens the Suppliers tab
- WHEN they type a CUIT, business name, or code, or select a status
- THEN the list filters to matching suppliers

#### Scenario: Toggle status

- GIVEN a supplier row
- WHEN the owner toggles its status
- THEN the supplier becomes ACTIVO or INACTIVO

#### Scenario: Create with reactive validation

- GIVEN the create/edit form
- WHEN the owner enters code, margin, or IVA condition
- THEN validation reacts on the front and backend before save

### Requirement: Customer Orders tab

The system MUST provide a Customer Orders tab listing persisted orders with state, customer, and ARS totals, and MUST show line-item detail (SKU, name, quantity, prices, source) per order.

#### Scenario: Orders listed with totals

- GIVEN persisted orders
- WHEN the owner opens the Customer Orders tab
- THEN orders show state, customer, and total

#### Scenario: Line detail per order

- GIVEN an order in the list
- WHEN the owner opens its detail
- THEN lines show SKU, name, quantity, prices, and source

### Requirement: Exchange rate maintenance

The system MUST provide a rate maintenance view editing `rate_to_ars` per currency, MUST NOT allow editing ARS, and MUST trigger recomputation of pending-conversion orders when a rate is saved.

#### Scenario: Rate edited

- GIVEN a USD rate row
- WHEN the owner saves a new rate
- THEN the rate and timestamp are stored
- AND pending-conversion orders recompute their totals

#### Scenario: ARS rate not editable

- GIVEN the ARS currency row
- WHEN the owner tries to edit it
- THEN the edit is rejected

### Requirement: Default margin maintenance

The system MUST expose the default supplier margin (used when `codigo_proveedor` does not map to `suppliers.code`) as an editable setting, seeded at 20%.

#### Scenario: Default margin edited

- GIVEN the default margin setting
- WHEN the owner saves a new value
- THEN subsequent unmapped RAG lines use the new value

#### Scenario: Seed value present

- GIVEN a fresh database
- WHEN the setting is first read
- THEN it equals 20%
