# Delta for backoffice

## MODIFIED Requirements

### Requirement: Supplier document ingestion module

The system MUST provide a backoffice module that selects an active supplier by `business_name` first, uploads a supplier document for RAG/Luna parsing, shows an identified-products review grid with per-line manual RAG product-code search for pending lines, and gates confirmation until all positive-quantity lines are resolved.
(Previously: uploaded a document directly and confirmed extracted data into inventory/catalog.)

#### Scenario: Supplier-first upload and preview
- GIVEN the owner opens the ingestion module
- WHEN they select a supplier and drop a document
- THEN the identified-products grid (code, description, quantity, supplier cost) is displayed with resolved and pending lines

#### Scenario: Manual code search fixes pending lines
- GIVEN pending lines in the review grid
- WHEN the owner runs a per-line RAG product-code search and selects a match
- THEN those lines become resolved

#### Scenario: Confirm entry to inventory gated
- GIVEN a grid where every positive-quantity line is resolved
- WHEN the owner confirms entry
- THEN stock is written for matched lines with RAG provenance
