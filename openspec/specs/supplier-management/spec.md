# supplier-management Specification

## Purpose

Manage supplier master data: English domain naming, validated contact fields, a 3-character code, and an ACTIVO/INACTIVO soft-delete lifecycle consumed by catalog, sourcing, purchase orders, and document ingestion.

## Requirements

### Requirement: Supplier master data and CRUD

The system MUST persist suppliers in `suppliers`/`Supplier` (PK `id`) with `business_name`, `contact_name`, `phone`, `default_margin_pct`, `terms`, `cuit`, `address`, `email`, `whatsapp`, `code`, `iva_condition`, and `status`; supplier-SKU links in `supplier_sku_mappings`/`SupplierSkuMapping` with `supplier_sku_code`, `raw_description`, `internal_sku`, and `confidence`; and FK `supplier_id` on catalogo, supplier_purchase_orders, sourcing_needs, and supplier_sku_mappings. The system MUST provide full CRUD: create, list, edit, and soft-delete via `status` (INACTIVO; default ACTIVO).

#### Scenario: Create supplier

- GIVEN valid supplier fields
- WHEN the owner saves the supplier
- THEN it is persisted with English column names and status ACTIVO

#### Scenario: Edit supplier

- GIVEN an existing supplier
- WHEN the owner edits its fields
- THEN the changes are persisted

### Requirement: Supplier code

The system MUST generate a 3-character uppercase `code` reactively from `business_name`, keep it user-editable before save, enforce uniqueness, and make it immutable once linked to a Catalogo, SupplierPurchaseOrder, SourcingNeed, or SupplierSkuMapping row.

#### Scenario: Code suggested from name

- GIVEN a business name
- WHEN the owner starts creating a supplier
- THEN a 3-character code is suggested and remains editable before save

#### Scenario: Code collision

- GIVEN the requested code already exists
- WHEN the owner saves
- THEN a collision variant is suggested and an alert is shown

#### Scenario: Code immutable once linked

- GIVEN a supplier whose code is referenced by a catalog/PO/need/mapping row
- WHEN the owner tries to change the code
- THEN the change is rejected

### Requirement: Supplier validation

The system MUST validate: `cuit` with mod-11 (nullable; legacy rows may lack it), `email` per RFC 5322 via `email-validator`, `phone` as strict E.164, and `whatsapp` as the WhatsApp form via `normalize_phone`.

#### Scenario: Invalid CUIT rejected

- GIVEN a CUIT failing mod-11
- WHEN the owner saves
- THEN the save is rejected with a validation error

#### Scenario: Phone normalized

- GIVEN `phone` and `whatsapp` inputs
- WHEN the supplier is saved
- THEN `phone` is normalized to strict E.164 and `whatsapp` to the WhatsApp form

### Requirement: Supplier uniqueness

The system MUST enforce DB uniqueness on `cuit` (partial unique index, NULLable) and `code` (unique, uppercase, 3 chars).

#### Scenario: Duplicate CUIT rejected

- GIVEN two suppliers with the same non-null CUIT
- WHEN both are saved
- THEN the database rejects the duplicate

#### Scenario: Duplicate code rejected

- GIVEN two suppliers with the same code
- WHEN both are saved
- THEN the database rejects the duplicate

### Requirement: Supplier status lifecycle

The system MUST soft-delete suppliers via `status` and MUST exclude INACTIVO suppliers from order sourcing, purchase-order creation, and document ingestion.

#### Scenario: INACTIVO excluded from sourcing

- GIVEN a supplier set to INACTIVO
- WHEN the sourcing flow queries candidate suppliers
- THEN the supplier is not listed

### Requirement: Legacy data deletion

The migration MUST DELETE legacy supplier rows and their dependents (no backfill, no preservation).

#### Scenario: Migration removes legacy rows

- GIVEN the rename-and-delete migration runs
- WHEN it upgrades
- THEN legacy supplier rows and dependents are deleted

### Requirement: Default margin scope

The supplier `default_margin_pct` MUST be consumed for future catalog products at ingestion AND at order time for RAG-sourced order lines resolved via `codigo_proveedor` → `suppliers.code`. Editing a margin MUST NOT re-price existing catalog rows or already-persisted order lines.
(Previously: consumed only for future catalog products at ingestion.)

#### Scenario: Margin applies to future ingestion

- GIVEN a supplier with a default margin
- WHEN a future product is ingested for that supplier
- THEN the product's base price uses the supplier's current default margin

#### Scenario: Margin edit does not re-price

- GIVEN existing catalog products priced with an old margin
- WHEN the supplier's default margin is edited
- THEN existing product prices are unchanged

#### Scenario: Margin applies to RAG order lines

- GIVEN a RAG line whose `codigo_proveedor` maps to the supplier
- WHEN the order is priced at finalize
- THEN the line base uses the supplier's current default margin

#### Scenario: Persisted orders stay frozen

- GIVEN an order persisted with a supplier margin
- WHEN the supplier's default margin is edited afterwards
- THEN the persisted order lines keep their snapshot prices
