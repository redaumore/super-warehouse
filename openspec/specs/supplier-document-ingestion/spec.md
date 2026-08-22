# supplier-document-ingestion Specification

## Purpose

Ingest supplier purchase documents (remitos/invoices as photos or PDFs, and price lists as PDF/Excel) by extracting items, quantities, and costs, and update inventory only after explicit confirmation.

## Requirements

### Requirement: Extract items, quantities, and costs

The system MUST extract items, quantities, and supplier costs from supplier remito/invoice documents submitted as photos or PDFs.

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

### Requirement: Map items to existing SKUs or suggest new ones

The system MUST map each extracted item to an existing catalog SKU, or suggest creating a new SKU when no match exists.

#### Scenario: Item maps to an existing SKU

- GIVEN an extracted item matches an existing SKU (including via supplier mapping)
- WHEN the document is processed
- THEN the item is linked to that SKU for inventory update

#### Scenario: Item suggests a new SKU

- GIVEN an extracted item has no matching SKU
- WHEN the document is processed
- THEN the system proposes a new SKU
- AND the proposal is presented for owner confirmation

#### Scenario: Ambiguous mapping highlighted

- GIVEN an extracted item maps to multiple possible SKUs
- WHEN the document is processed
- THEN the ambiguity is highlighted for the owner to resolve before entry

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

### Requirement: Handle OCR failure

The system MUST detect extraction failure and route the document for manual handling rather than silently writing bad data.

#### Scenario: Extraction fails

- GIVEN a document cannot be reliably extracted
- WHEN the extraction errors or yields no usable fields
- THEN the owner is notified
- AND the document is queued for manual entry
- AND no inventory write occurs

#### Scenario: Partial extraction flagged

- GIVEN only some lines of a document extract cleanly
- WHEN the document is processed
- THEN the uncertain lines are flagged
- AND only confirmed lines are eligible for entry

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
