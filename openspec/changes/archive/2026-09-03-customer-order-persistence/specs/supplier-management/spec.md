# Delta for supplier-management

## MODIFIED Requirements

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