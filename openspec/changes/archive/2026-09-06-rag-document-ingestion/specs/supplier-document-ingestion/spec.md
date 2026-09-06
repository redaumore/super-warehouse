# Delta for supplier-document-ingestion

## MODIFIED Requirements

### Requirement: Extract items, quantities, and costs

The system MUST extract items, quantities, and supplier costs from supplier remito/invoice documents submitted as photos or PDFs via the RAG/Luna structured-output parse endpoint, returning structured lines with quantity and cost plus source metadata. Document format support (image vs PDF) is resolved in design.
(Previously: extraction used the GPT-4o Vision/regex path.)

#### Scenario: Remito photo extracted
- GIVEN the owner submits a photo of a supplier remito
- WHEN the document is processed
- THEN the system extracts each item's code/description, quantity, and supplier cost

#### Scenario: Invoice PDF extracted
- GIVEN the owner uploads a supplier invoice PDF
- WHEN the document is processed
- THEN the same structured fields are extracted from the PDF

### Requirement: Handle parse failure

The system MUST detect RAG/Luna parse failure and route affected lines for manual resolution rather than silently writing bad data. Parse errors MUST produce no write.
(Previously: "Handle OCR failure" routed extraction failures for manual entry.)

#### Scenario: Parse fails
- GIVEN a document the RAG/Luna parser cannot process
- WHEN parsing errors or yields no usable fields
- THEN the owner is notified and no inventory write occurs

#### Scenario: Partial extraction flagged
- GIVEN only some lines parse cleanly
- WHEN the document is processed
- THEN uncertain lines are flagged as unresolved for manual resolution

## REMOVED Requirements

### Requirement: Map items to existing SKUs or suggest new ones

(Reason: unknown-product creation is superseded — every ingested product MUST pre-exist in `catalogo_productos_rag`.)
(Migration: resolve lines via exact `codigo_orig` lookup then manual RAG product-code search; never create from model output.)
