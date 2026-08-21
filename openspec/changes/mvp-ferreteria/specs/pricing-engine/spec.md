# pricing-engine Specification

## Purpose

Compute base and final prices for catalog items by applying cost margin and the customer's list and particular discounts, in a defined order.

## Requirements

### Requirement: Compute base price from cost and margin

The system MUST compute the base price as `Base = cost × (1 + margin%)`, where margin is expressed as a percentage.

#### Scenario: Base price computed

- GIVEN an item with cost 925.92 and margin 35%
- WHEN the base price is computed
- THEN the base price is `925.92 × (1 + 0.35) = 1249.99` (HALF_UP to cents)

#### Scenario: Zero margin yields cost

- GIVEN an item with margin 0%
- WHEN the base price is computed
- THEN the base price equals the cost

### Requirement: Compute final price with list and particular discounts

The system MUST compute the final price as `Final = Base × (1 − list_discount) × (1 − particular_discount)`.

#### Scenario: Both discounts applied

- GIVEN a base price, a list discount, and a particular discount
- WHEN the final price is computed
- THEN the final price equals `Base × (1 − list_discount) × (1 − particular_discount)`

#### Scenario: Only list discount present

- GIVEN a customer with a list discount and no particular discount
- WHEN the final price is computed
- THEN the final price equals `Base × (1 − list_discount)`

### Requirement: Apply discounts multiplicatively in order

The system MUST apply discounts multiplicatively, list discount first then particular discount, and MUST NOT add the percentages.

#### Scenario: Discounts compound, not sum

- GIVEN a base price of 1000, list discount 20%, particular discount 10%
- WHEN the final price is computed
- THEN the result is `1000 × 0.80 × 0.90 = 720`
- AND is NOT `1000 × (1 − 0.30) = 700`

#### Scenario: Order of application is list then particular

- GIVEN both discounts are present
- WHEN the final price is computed
- THEN the list discount is applied to the base price first
- AND the particular discount is applied to the list-discounted price second

### Requirement: Treat absent discounts as zero

The system SHALL treat any absent discount as zero percent.

#### Scenario: No discounts configured

- GIVEN an item with neither list nor particular discount
- WHEN the final price is computed
- THEN the final price equals the base price

#### Scenario: Missing list discount only

- GIVEN a customer with a particular discount but no list discount
- WHEN the final price is computed
- THEN the list discount is treated as 0%
- AND the particular discount is applied to the base price
