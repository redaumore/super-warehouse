# Delta for supplier-document-ingestion

## ADDED Requirements

### Requirement: Refuse inactive suppliers at confirmation

The system MUST refuse INACTIVO suppliers in `confirm_items` and MUST NOT write inventory for them.

#### Scenario: confirm_items refuses INACTIVO

- GIVEN a document whose supplier is INACTIVO
- WHEN the owner confirms entry
- THEN the system rejects the confirmation and writes no inventory
