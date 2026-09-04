# Delta for clients-and-price-lists

## ADDED Requirements

### Requirement: Attach customer by name at finalization

The system MUST resolve the customer for a finalized draft by name (exact match, then case-folded match, then an ambiguity menu), MUST attach the resolved `clientes` record to the order, and MUST offer in-chat minimal creation (name and phone, Base price list) when no match exists. Phone-based identification MUST NOT be used for the draft-finalize flow.

#### Scenario: Exact name match

- GIVEN a finalize naming an existing client
- WHEN the customer is resolved
- THEN the exact match is attached to the order

#### Scenario: Ambiguous name offers menu

- GIVEN two clients sharing the name
- WHEN the customer is resolved
- THEN the system asks the customer to pick one before persisting

#### Scenario: No match offers creation

- GIVEN a name with no client record
- WHEN resolution fails
- THEN the system offers minimal creation on the Base list