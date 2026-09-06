# manual-rag-product-resolution Specification

## Purpose

Assign unresolved ingestion lines to existing RAG catalog products through a per-line product-code search, without creating products.

## Requirements

### Requirement: Per-line manual product-code search

The system MUST let the owner search RAG catalog products by product code for any pending line and select a match.

#### Scenario: Manual search returns candidates
- GIVEN an unresolved line in the review grid
- WHEN the owner searches a product code
- THEN candidate RAG products are listed for selection

#### Scenario: Selection attaches the row
- GIVEN candidate products for a pending line
- WHEN the owner selects one
- THEN the line is resolved to that row with its `node_id`

### Requirement: Resolution never creates products

The system MUST NOT create a catalog product during manual resolution; an unresolved line that yields no match MUST remain pending.

#### Scenario: No match keeps line pending
- GIVEN a manual search with no matching product
- WHEN the search returns empty
- THEN the line stays unresolved
- AND confirmation remains blocked
