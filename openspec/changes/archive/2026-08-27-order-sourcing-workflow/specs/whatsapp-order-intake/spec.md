# Delta for whatsapp-order-intake

## ADDED Requirements

### Requirement: Extract structured order fields

The system MUST, after transcription, extract structured order fields from the message: customer name, a list of items with quantities, and an optional delivery date.

#### Scenario: Order message extracted

- GIVEN a transcribed or text order message
- WHEN the message is parsed
- THEN the system produces customer name, item lines (description + quantity), and a delivery date when present

#### Scenario: Missing delivery date

- GIVEN a message with no delivery date
- WHEN the message is parsed
- THEN the delivery date is left empty without failing extraction
