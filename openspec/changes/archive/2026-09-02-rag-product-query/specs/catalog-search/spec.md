# Delta for catalog-search

## MODIFIED Requirements

### Requirement: Report no-match results

The system MUST consult the supplier-catalog RAG when local catalog search returns no candidates, and MUST render a source-aware note for the RAG results. When neither local nor RAG finds a match, the system MUST tell the customer the item was not found and request clarification; it MUST NOT claim the item is out of stock.

(Previously: reported no-match from local search only, with a note the customer could read as "no stock".)

#### Scenario: No product found

- GIVEN an item matches nothing in the local catalog and the RAG also returns no products
- WHEN the search completes with no candidates
- THEN the system tells the customer the item was not found
- AND requests clarification or an alternative name

#### Scenario: Empty local search falls back to RAG

- GIVEN the local catalog search returns no candidates
- WHEN the search completes
- THEN the system queries the supplier-catalog RAG
- AND renders the RAG results with source-aware note fields

#### Scenario: No-match does not block the rest of the order

- GIVEN an order contains one unmatched item and several matched items
- WHEN the unmatched item is reported
- THEN the matched items continue through quotation
- AND the unmatched item is left out pending customer clarification
