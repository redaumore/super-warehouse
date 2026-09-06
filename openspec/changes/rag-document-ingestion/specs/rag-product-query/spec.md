# Delta for rag-product-query

## ADDED Requirements

### Requirement: Document parse and exact supplier-code lookup

The RAG client MUST add a parse call that submits a multipart document to the RAG parse endpoint and returns structured lines, and an exact lookup that queries `catalogo_productos_rag.codigo_orig` scoped to the supplier code and returns the matched row with `node_id`. Both calls MUST carry a bounded timeout configured from `rag_timeout_seconds` (src/config.py:73) and MUST raise a domain error, never a raw transport exception, on timeout or connection failure.

#### Scenario: Parse via client
- GIVEN a supplier document
- WHEN the client parses it
- THEN structured lines are returned

#### Scenario: Exact lookup returns provenance
- GIVEN a supplier code and `codigo_orig`
- WHEN the client performs exact lookup
- THEN the matched row and its `node_id` are returned

#### Scenario: Transport failure raises domain error
- GIVEN the RAG is unreachable
- WHEN the client parses or looks up
- THEN a domain error is raised and no raw exception escapes

#### Scenario: Client call respects bounded timeout
- GIVEN the RAG is slow beyond `rag_timeout_seconds`
- WHEN the client parses or looks up
- THEN the call times out within the configured bound
- AND a domain error is raised, never a raw transport exception

### Requirement: Product lookup route by SKU

The RAG service MUST mount `GET /api/v1/products/{sku}`. For supplier-price lookup by SKU, the route MUST return a single row and MUST return `404` when no row matches, which the client maps to `None`. For exact-code ingestion resolution, the route MUST return ALL matching rows — possibly an empty list — each carrying `node_id`.

#### Scenario: Price lookup returns single row
- GIVEN a SKU matching exactly one catalog row
- WHEN the route is queried for supplier-price lookup
- THEN `200` with a single-row payload is returned

#### Scenario: Price lookup 404 maps to None
- GIVEN a SKU with no matching row
- WHEN the route is queried for supplier-price lookup
- THEN `404` is returned
- AND the client maps it to `None` without raising

#### Scenario: Ingestion resolution returns all matches
- GIVEN a `codigo_orig` matching zero, one, or many rows
- WHEN the route is queried for ingestion resolution
- THEN `200` with an array of all matching rows (possibly empty), each with `node_id`, is returned
