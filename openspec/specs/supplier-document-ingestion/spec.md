# supplier-document-ingestion Specification

## Purpose

Ingest supplier purchase documents (remitos/invoices as photos or PDFs, and price lists as PDF/Excel) by extracting items, quantities, and costs, and update inventory only after explicit confirmation.

## Requirements

### Requirement: Extract items, quantities, and costs

The system MUST extract items, quantities, and supplier costs from supplier remito/invoice documents submitted as photos or PDFs via the RAG/Luna structured-output parse endpoint, returning structured lines with quantity and cost plus source metadata. Document format support (image vs PDF) is resolved in design.

#### Scenario: Remito photo extracted

- GIVEN the owner submits a photo of a supplier remito
- WHEN the document is processed
- THEN the system extracts each item's code/description, quantity, and supplier cost

#### Scenario: Invoice PDF extracted

- GIVEN the owner uploads a supplier invoice PDF
- WHEN the document is processed
- THEN the same structured fields are extracted from the PDF

### Requirement: Confirm before writing inventory

The system MUST NOT write extracted data to inventory until the owner confirms it.

#### Scenario: Preview then confirm

- GIVEN extraction produced a set of item rows
- WHEN the owner reviews the preview grid
- THEN no inventory or cost update occurs until the owner confirms ("Confirmar e Ingresar a Inventario")

#### Scenario: Owner corrects before confirm

- GIVEN the owner edits an extracted field before confirming
- WHEN the correction is made and then confirmed
- THEN the corrected values, not the raw extraction, are written to inventory

### Requirement: Handle parse failure

The system MUST detect RAG/Luna parse failure and route affected lines for manual resolution rather than silently writing bad data. Parse errors MUST produce no write.

#### Scenario: Parse fails

- GIVEN a document the RAG/Luna parser cannot process
- WHEN parsing errors or yields no usable fields
- THEN the owner is notified and no inventory write occurs

#### Scenario: Partial extraction flagged

- GIVEN only some lines parse cleanly
- WHEN the document is processed
- THEN uncertain lines are flagged as unresolved for manual resolution

### Requirement: Parse supplier price lists

The system MUST parse supplier price-list documents (PDF or Excel) extracting code, description, and supplier cost, and store supplier-to-internal SKU mappings.

#### Scenario: Price list PDF parsed

- GIVEN a supplier price-list PDF is uploaded
- WHEN the document is processed
- THEN code, description, and supplier cost are extracted per line

#### Scenario: Price list Excel parsed

- GIVEN a supplier price-list Excel file is uploaded
- WHEN the document is processed
- THEN the same fields are extracted and supplier SKU mappings are stored

### Requirement: Reject illegible handwriting

The system MUST NOT attempt to process illegible handwritten documents in the MVP.

#### Scenario: Illegible handwriting encountered

- GIVEN a handwritten document of very low legibility
- WHEN the document is processed
- THEN the system does not attempt to extract it
- AND treats it as out of scope (deferred to a later version)

#### Scenario: Legible document still processed

- GIVEN a clearly legible printed or handwritten document
- WHEN the document is processed
- THEN it proceeds through normal extraction

### Requirement: Reduce manual entry time

The system SHALL reduce supplier remito/invoice data-entry time by at least 80% compared to manual entry.

#### Scenario: Entry time measured

- GIVEN supplier documents ingested during the pilot
- WHEN entry time is compared against the manual baseline
- THEN automated ingestion reduces entry time by at least 80%

### Requirement: Refuse inactive suppliers at confirmation

The system MUST refuse INACTIVO suppliers in `confirm_items` and MUST NOT write inventory for them.

#### Scenario: confirm_items refuses INACTIVO

- GIVEN a document whose supplier is INACTIVO
- WHEN the owner confirms entry
- THEN the system rejects the confirmation and writes no inventory
