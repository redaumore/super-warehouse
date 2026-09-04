# clients-and-price-lists Specification

## Purpose

Identify the customer by their WhatsApp phone number and resolve their commercial condition — assigned price list and particular discount — so pricing can be applied correctly.

## Requirements

### Requirement: Identify client by phone

The system MUST identify a customer by their WhatsApp phone number before quoting.

#### Scenario: Known phone identified

- GIVEN a customer with a registered phone number sends an order
- WHEN the intake resolves the sender's number
- THEN the system matches the number to the stored customer record
- AND uses that record for pricing

#### Scenario: Phone formatting differences reconciled

- GIVEN the same number arrives with varying country-code or spacing formats
- WHEN the number is matched
- THEN the system normalizes the number and resolves it to the same customer

### Requirement: Handle unknown phones

The system MUST handle an unknown phone number gracefully.

#### Scenario: Unknown phone falls back to default list

- GIVEN an order arrives from an unregistered number
- WHEN the client is resolved
- THEN the system treats the sender under the default/base price list
- AND flags the sender for later registration

#### Scenario: Unknown phone flagged for onboarding

- GIVEN an unregistered number is encountered
- WHEN the order is processed
- THEN the number is recorded as a new-client candidate
- AND surfaced in the backoffice for the owner to register

### Requirement: Resolve assigned price list

The system MUST resolve the customer's assigned price list from the set Gremio A / Gremio B / Base.

#### Scenario: Client has an assigned list

- GIVEN a customer is assigned "Gremio B"
- WHEN their commercial condition is resolved
- THEN the system uses the Gremio B list discount for that customer's pricing

#### Scenario: Client on Base list

- GIVEN a customer is assigned the "Base" list
- WHEN their condition is resolved
- THEN the system applies no list discount
- AND prices are computed from the base price

### Requirement: Apply particular discount

The system MUST apply the customer's particular (individual) discount when pricing.

#### Scenario: Particular discount applied

- GIVEN a customer has a registered particular discount percentage
- WHEN their order is priced
- THEN the particular discount is applied to the final price

#### Scenario: No particular discount configured

- GIVEN a customer has no particular discount configured
- WHEN their order is priced
- THEN no particular discount is applied (treated as zero)

### Requirement: Exclude credit and payment conditions

The system MUST NOT model client credit limits or payment conditions in the MVP.

#### Scenario: Pricing ignores credit terms

- GIVEN any customer places an order
- WHEN the order is priced
- THEN credit limits and payment terms are not considered or stored as business rules

#### Scenario: No credit gate blocks ordering

- GIVEN a customer with no credit record
- WHEN they place an order
- THEN the flow proceeds without a credit check
- AND no payment-condition field participates in the quotation

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
