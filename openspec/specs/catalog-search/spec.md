# catalog-search Specification

## Purpose

Resolve free-form customer language (slang, misspellings, informal names, mixed units) into specific catalog products using a hybrid of fuzzy string matching and vector similarity.

## Requirements

### Requirement: Hybrid fuzzy and vector search

The system MUST resolve each order item by combining fuzzy string matching with vector/semantic similarity against the catalog, not by exact-match lookup alone.

#### Scenario: Informal name matches a product

- GIVEN a customer asks for "clavos de 2 pulgadas" while the catalog entry is "Clavos Paris 2 Pulgadas (50mm)"
- WHEN the item is searched
- THEN the hybrid search returns the catalog product as a candidate
- AND the informal phrasing is normalized to the official SKU

#### Scenario: Misspelled request still resolves

- GIVEN a customer misspells or abbreviates a product name
- WHEN the item is searched
- THEN fuzzy matching recovers the intended catalog entry
- AND vector similarity ranks the correct synonym above unrelated products

### Requirement: Auto-map high-confidence matches

The system MUST automatically map an item to a SKU when match confidence is at or above the calibrated high-confidence threshold, without asking the customer.

#### Scenario: Single high-confidence match

- GIVEN a search returns exactly one candidate with confidence at or above threshold
- WHEN the item is resolved
- THEN the system maps the item to that SKU automatically
- AND does not prompt the customer for confirmation

#### Scenario: Unambiguous synonym resolved

- GIVEN a synonym in the catalog maps cleanly to one product
- WHEN the item is searched
- THEN the system auto-maps to the official SKU

### Requirement: Disambiguation menu on ambiguity

The system MUST present a numbered disambiguation menu when confidence is low or multiple candidates match, and MUST accept a digit reply.

#### Scenario: Multiple plausible matches

- GIVEN a search returns several candidates above the ambiguity floor (e.g. "Clavo Paris 2'" vs "Clavo Espiralado 2'")
- WHEN the item is ambiguous
- THEN the system sends a numbered menu listing the candidates
- AND the customer may reply with only the digit of their choice to resolve the item

#### Scenario: Low-confidence single match

- GIVEN a search returns one candidate but below the auto-map threshold
- WHEN the item is ambiguous
- THEN the system presents a numbered menu for explicit confirmation
- AND does not silently guess the SKU

### Requirement: Report no-match results

The system MUST consult the supplier-catalog RAG when local catalog search returns no candidates, and MUST render a source-aware note for the RAG results. When neither local nor RAG finds a match, the system MUST tell the customer the item was not found and request clarification; it MUST NOT claim the item is out of stock.

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

### Requirement: Identification precision target

The system SHALL achieve at least 85% correct identification of products from customer audio and text, measured across real orders.

#### Scenario: Precision measured on pilot orders

- GIVEN a set of real customer orders during the pilot
- WHEN identification outcomes are measured
- THEN at least 85% of items are mapped to the correct catalog SKU

#### Scenario: Precision below target triggers calibration

- GIVEN measured precision falls below 85%
- WHEN the shortfall is detected
- THEN search thresholds and synonym coverage are reviewed and recalibrated
