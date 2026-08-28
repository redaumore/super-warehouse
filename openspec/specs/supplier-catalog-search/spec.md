# supplier-catalog-search Specification

## Purpose

Expose a seam to query which suppliers can offer a missing item, consuming the external supplier-catalog RAG without coupling the sourcing workflow to its implementation.

## Requirements

### Requirement: Supplier catalog searcher seam

The system MUST expose a `SupplierCatalogSearcher` protocol whose search takes a missing SKU or free-text description and returns candidate suppliers with their offered item and quantity.

#### Scenario: Candidates returned for a missing item

- GIVEN a missing item that one or more suppliers offer
- WHEN the searcher is queried
- THEN it returns each candidate supplier with its offered item and quantity

#### Scenario: No supplier offers the item

- GIVEN a missing item no supplier offers
- WHEN the searcher is queried
- THEN it returns an empty candidate list

#### Scenario: Seam decouples the external RAG

- GIVEN the supplier-catalog RAG is unavailable or not yet built
- WHEN the sourcing workflow runs
- THEN it depends only on the `SupplierCatalogSearcher` protocol, not on the RAG implementation
- AND a fake searcher MAY stand in for tests
