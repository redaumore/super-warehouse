# Delta for purchase-order-lifecycle

## ADDED Requirements

### Requirement: Refuse inactive suppliers

The system MUST refuse INACTIVO suppliers in `open_or_create_po` and `accumulate_need`.

#### Scenario: PO creation refuses INACTIVO

- GIVEN a supplier set to INACTIVO
- WHEN `open_or_create_po` is called for it
- THEN the system rejects the operation

#### Scenario: Accumulation refuses INACTIVO

- GIVEN a supplier set to INACTIVO
- WHEN `accumulate_need` is called for it
- THEN the system rejects the operation
