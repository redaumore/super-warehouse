# rag-document-ingestion Specification

## Purpose

Parse supplier receipt/invoice documents through the RAG/Luna service, resolve each line to an existing `catalogo_productos_rag` row, review and manually resolve unmatched lines, and persist stock only from matched rows carrying RAG `node_id` provenance.

## Requirements

### Requirement: Supplier-first ingestion selection

The system MUST present active suppliers in a dropdown displaying `business_name` (ACTIVO only) before document upload, MUST retain the supplier ID internally, and MUST NOT expose a free-text numeric supplier ID field.

#### Scenario: Supplier selected first
- GIVEN the owner opens the ingestion tab
- WHEN the active-supplier dropdown renders
- THEN it lists ACTIVO suppliers by `business_name`
- AND the selected supplier's ID is retained internally

#### Scenario: No free numeric ID
- GIVEN the ingestion form
- WHEN it renders
- THEN no free-text supplier-ID input is present

### Requirement: RAG document parse endpoint

The system MUST expose a multipart RAG endpoint accepting a supplier document and supplier code, returning structured lines (code, description, quantity, supplier cost) with source metadata. The endpoint MUST return a structured error and MUST NOT write on parse failure.

#### Scenario: Successful parse
- GIVEN a valid supplier document and supplier code
- WHEN the parse endpoint is called
- THEN structured lines with quantity and cost plus source metadata are returned
- AND no inventory or catalog rows are written

#### Scenario: Parse failure returns error without writes
- GIVEN a document the parser cannot process
- WHEN the parse endpoint is called
- THEN a structured error is returned
- AND no write occurs

### Requirement: Exact-code-first resolution with hybrid fallback

The system MUST resolve each parsed line by exact lookup on `catalogo_productos_rag.codigo_orig` scoped to the supplier code, and MUST run hybrid RAG retrieval only for lines the exact lookup missed.

#### Scenario: Exact hit resolves directly
- GIVEN a line whose supplier code and `codigo_orig` match an indexed row
- WHEN the line is resolved
- THEN the matched row (with `node_id`) is attached
- AND no hybrid retrieval runs for that line

#### Scenario: Exact miss falls back to hybrid
- GIVEN a line with no exact `codigo_orig` match
- WHEN the line is resolved
- THEN hybrid RAG retrieval is queried for that line only

### Requirement: Review grid and confirmation gating

The system MUST present identified products in a review grid with per-line manual RAG code search for unresolved lines, and MUST block confirmation while any positive-quantity line remains unresolved.

#### Scenario: Grid shows resolved and pending lines
- GIVEN parsed lines after resolution
- WHEN the review grid renders
- THEN matched lines show their product and pending lines are flagged for manual search

#### Scenario: Confirmation blocked while unresolved
- GIVEN at least one positive-quantity line unresolved
- WHEN the owner attempts to confirm
- THEN confirmation is blocked
- AND a message indicates which lines remain unresolved

#### Scenario: All lines resolved unlocks confirmation
- GIVEN every positive-quantity line is resolved
- WHEN the owner confirms
- THEN confirmation proceeds

### Requirement: Provenanced persistence

The system MUST persist stock only from matched RAG rows carrying `node_id` provenance, MUST NOT create a catalog product from model output, and MUST update stock and inventory in a single transaction.

#### Scenario: Stock written from matched row
- GIVEN a resolved line whose RAG row has a `node_id`
- WHEN the owner confirms
- THEN stock is written with that `node_id` provenance

#### Scenario: No creation from model output
- GIVEN a line with no matching `catalogo_productos_rag` row
- WHEN ingestion proceeds
- THEN no `Catalogo`/product row is created
- AND the line remains unresolved

#### Scenario: Transactional stock update
- GIVEN a multi-line confirmation
- WHEN any write fails
- THEN the whole confirmation rolls back and no partial stock is committed

### Requirement: RAG/Luna unavailability

The system MUST time out and return an honest error when the RAG/Luna service is unavailable or slow, and MUST write nothing.

#### Scenario: RAG/Luna down
- GIVEN the RAG/Luna service is unreachable or exceeds the timeout
- WHEN the owner submits a document
- THEN an honest error is shown
- AND no inventory or catalog write occurs
