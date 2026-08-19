# backoffice Specification

## Purpose

Provide a lightweight web interface for the owner to manage supplier document ingestion, catalog and stock, clients and price lists, and live order monitoring.

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
