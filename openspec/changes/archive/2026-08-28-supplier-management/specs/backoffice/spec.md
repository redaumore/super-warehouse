# Delta for backoffice

## ADDED Requirements

### Requirement: Supplier management module

The system MUST provide a sixth "Suppliers" tab listing suppliers with quick search by CUIT/business_name/code and a status filter, edit and toggle-status row actions, and a create/edit form with reactive front-and-back validation, a code assistant field, and default margin and IVA condition inputs.

#### Scenario: List with quick search and filter

- GIVEN the owner opens the Suppliers tab
- WHEN they type a CUIT, business name, or code, or select a status
- THEN the list filters to matching suppliers

#### Scenario: Toggle status

- GIVEN a supplier row
- WHEN the owner toggles its status
- THEN the supplier becomes ACTIVO or INACTIVO

#### Scenario: Create with reactive validation

- GIVEN the create/edit form
- WHEN the owner enters code, margin, or IVA condition
- THEN validation reacts on the front and backend before save
