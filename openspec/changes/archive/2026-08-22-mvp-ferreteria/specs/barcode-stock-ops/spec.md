# barcode-stock-ops Specification

## Purpose

Decode product barcodes from photos and perform stock queries and audited stock adjustments from WhatsApp or the web interface.

## Requirements

### Requirement: Decode barcode photos

The system MUST decode barcode images (EAN-13, UPC, QR, or internal codes) submitted as photos.

#### Scenario: Barcode photo decoded

- GIVEN the owner photographs a product box with a barcode
- WHEN the photo is processed
- THEN the barcode is decoded to a value that identifies the product

#### Scenario: Unreadable barcode image

- GIVEN a barcode photo that is blurred or unreadable
- WHEN the photo is processed
- THEN the system reports it cannot decode the image
- AND asks the owner to retake the photo

### Requirement: Return product and stock on query

The system MUST return the product identity and current available stock when queried by barcode.

#### Scenario: Stock query by barcode

- GIVEN the owner sends a barcode photo and asks "¿cuánto stock queda de esto?"
- WHEN the barcode is decoded and queried
- THEN the system responds with the product name and available quantity

#### Scenario: Location included in query response

- GIVEN the catalog records a warehouse location for the product
- WHEN a barcode stock query is answered
- THEN the response includes the product's location where available

### Requirement: Record audited stock adjustments

The system MUST record stock increases/decreases by barcode with a reason, maintaining an audit trail.

#### Scenario: Stock increase recorded

- GIVEN the owner sends "sumá 50 cajas que llegaron" with a barcode
- WHEN the adjustment is applied
- THEN the stock is increased by the stated quantity
- AND the adjustment is recorded with the reason

#### Scenario: Stock decrease recorded

- GIVEN the owner adjusts stock downward by barcode
- WHEN the adjustment is applied
- THEN the stock is decreased
- AND the change is recorded with reason and actor

### Requirement: Handle duplicate barcode mappings

The system MUST flag when one barcode maps to multiple SKUs and require disambiguation.

#### Scenario: One barcode, multiple SKUs

- GIVEN a barcode value is associated with more than one SKU
- WHEN the barcode is decoded
- THEN the system does not silently pick one
- AND presents the candidates for the owner to choose

#### Scenario: Ambiguity resolved by owner choice

- GIVEN the owner selects one SKU from a duplicate-barcode list
- WHEN the choice is made
- THEN subsequent operations use the chosen SKU

### Requirement: Notify on unknown barcodes

The system MUST notify the owner when a barcode is not recognized.

#### Scenario: Unknown barcode

- GIVEN a barcode that does not match any catalog entry
- WHEN the barcode is decoded and looked up
- THEN the system reports the barcode is unknown
- AND surfaces it to the owner for manual resolution
