# rag-product-query Specification

## Purpose

Answer customer product queries through the supplier-catalog RAG as if there were no local inventory, behind a local-first → RAG-fallback precedence chain, with source-discriminated results and honest failure handling.

## Requirements

### Requirement: RAG product client contract

The system MUST provide a `RagProductClient` that queries the supplier-catalog RAG at `POST /api/v1/query` synchronously with `structured_json=true` and maps `structured_json.productos[]` into typed product results (code, name, provider, brand, price, specs, source page/PDF). The HTTP transport MUST be injectable for tests. On timeout or connection failure the client MUST raise a domain error, never a raw transport exception.

#### Scenario: Successful product query

- GIVEN the RAG is reachable and the query matches catalog products
- WHEN the client queries with `structured_json=true`
- THEN typed product results are returned with name, provider, price, specs, and source page/PDF

#### Scenario: Transport failure raises domain error

- GIVEN the RAG is unreachable or times out
- WHEN the client queries
- THEN a domain error is raised
- AND no raw transport exception escapes the client

### Requirement: Local-first → RAG-fallback precedence chain

The system MUST resolve product queries local-first: run the local catalog search, and only when it returns zero candidates MUST it call the RAG. Local hits MUST NOT reach the RAG.

#### Scenario: Local hit skips RAG

- GIVEN the local catalog search returns one or more candidates
- WHEN a product query is resolved
- THEN the RAG is not called
- AND the local candidates are used

#### Scenario: Empty local search falls back to RAG

- GIVEN the local catalog search returns zero candidates
- WHEN a product query is resolved
- THEN the RAG is queried for the same free-form text

### Requirement: Source-discriminated results

The product-query result MUST carry a source discriminator with values `LOCAL`, `RAG`, `NONE`, or `ERROR`.

#### Scenario: Source set per outcome

- GIVEN any product query
- WHEN it resolves
- THEN the result source is LOCAL for local hits, RAG for RAG hits, NONE when nothing matches, and ERROR when the RAG failed

### Requirement: Source-aware note rendering

The customer note MUST render results per source: RAG results list product name, provider, price, specs, and source page/PDF, numbered and sorted cheapest first. When a note mixes entries from both sources, local entries MUST be listed before RAG entries and labeled by source.

#### Scenario: RAG results rendered numbered, cheapest first

- GIVEN the RAG returns multiple products
- WHEN the note is rendered
- THEN results are numbered and sorted by ascending price
- AND each entry shows name, provider, price, specs, and source page/PDF

#### Scenario: Dual-source note lists local first

- GIVEN a note renders entries from both local and RAG sources
- WHEN the note is composed
- THEN local entries appear before RAG entries
- AND each entry is labeled with its source

### Requirement: Refusal suggests reformulation

When the RAG responds with `is_refusal=true` or an empty product list, the system MUST treat it as "not found in current catalogs" and MUST suggest synonyms or a reformulation. It MUST NOT claim stock status either way.

#### Scenario: Refusal suggests reformulation

- GIVEN the RAG returns `is_refusal=true` with no products
- WHEN the note is rendered
- THEN the customer is told the item was not found in current catalogs
- AND is offered synonyms or a reformulation
- AND no stock claim is made

### Requirement: RAG unavailability notice

When the RAG is down or slow (timeout), the system MUST return a structured unavailability notice stating the supplier catalogs could not be consulted. It MUST NOT retry and MUST NOT claim the item is out of stock.

#### Scenario: RAG down produces unavailability notice

- GIVEN the RAG is unreachable or exceeds the timeout
- WHEN a product query resolves
- THEN the note states the supplier catalogs could not be consulted
- AND no retry is performed
- AND no "out of stock" claim is made

### Requirement: Order-building integration

During order building, the system MUST accept adding a product via natural phrases such as "agregalo" or "sumá 5 de eso", and MUST resolve numbered references such as "el 2" to the corresponding displayed result. When no open order exists, the system MUST offer to create one.

#### Scenario: Natural-phrase add

- GIVEN a product was just displayed and an open order exists
- WHEN the customer says "agregalo" or "sumá 5 de eso"
- THEN the displayed product (with quantity when given) is added to the order

#### Scenario: Numbered reference disambiguates

- GIVEN multiple numbered results were displayed
- WHEN the customer replies "el 2"
- THEN the second displayed result is selected

#### Scenario: No open order offers one

- GIVEN no open order exists
- WHEN the customer asks to add a product
- THEN the system offers to create an order instead of adding to none

### Requirement: RAG SKU hygiene

Display MUST NOT trust the raw RAG `codigo` field blindly. The system MUST sanitize or normalize the SKU before showing it, preventing double-prefix artifacts such as `AMX-AMX-AT-5044`.

#### Scenario: Double-prefixed SKU sanitized

- GIVEN the RAG returns a `codigo` with a duplicated provider prefix (e.g. `AMX-AMX-AT-5044`)
- WHEN the note is rendered
- THEN the displayed SKU is normalized to a single-prefix form

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
