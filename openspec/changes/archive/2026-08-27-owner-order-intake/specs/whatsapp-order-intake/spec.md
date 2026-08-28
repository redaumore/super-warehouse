# Delta for whatsapp-order-intake

## MODIFIED Requirements

### Requirement: Ingest text and voice orders

The system MUST accept incoming orders submitted by the owner as WhatsApp/Telegram text messages and as voice notes in the formats produced by WhatsApp (e.g. .ogg / .mp3). The customer for the order is resolved by name, not by the sender's phone.

(Previously: accepted customer-submitted orders with the customer identified by the sender's phone.)

#### Scenario: Text order received

- GIVEN a known channel is active and the owner sends a text message describing an order for a customer
- WHEN the message arrives at the intake endpoint
- THEN the system accepts the message as an order candidate
- AND proceeds to parsing without manual intervention

#### Scenario: Voice order received

- GIVEN the owner sends a WhatsApp voice note describing an order
- WHEN the audio arrives at the intake endpoint
- THEN the system accepts the audio as an order candidate
- AND routes it for speech-to-text transcription

### Requirement: Ephemeral acknowledgement under 5 seconds

The system MUST send an ephemeral acknowledgement to the owner within 5 seconds of receiving an order, before heavy processing (transcription, search, pricing) completes.

(Previously: acknowledged the customer.)

#### Scenario: ACK sent promptly

- GIVEN an order has just arrived from the owner
- WHEN the intake endpoint receives it
- THEN the owner receives an acknowledgement such as "Recibí tu pedido, ya te lo estoy cotizando..."
- AND the acknowledgement is delivered in under 5 seconds

#### Scenario: Heavy processing does not block the ACK

- GIVEN transcription and catalog search will take longer than 5 seconds
- WHEN the order is accepted
- THEN the ACK is still delivered within 5 seconds
- AND heavy processing continues asynchronously in the background

## ADDED Requirements

### Requirement: Restrict senders to the owner

The system MUST gate every inbound message against an explicit owner-sender allowlist (configured owner WhatsApp phone and/or owner Telegram chat id) and MUST NOT treat any other sender as the owner.

#### Scenario: Owner sender passes the gate

- GIVEN a message whose normalized sender id matches a configured owner sender
- WHEN the message arrives
- THEN it is processed as an owner order

#### Scenario: Non-owner sender rejected politely

- GIVEN a message from a sender not in the owner allowlist
- WHEN the message arrives
- THEN the system replies with a polite rejection
- AND does not create, quote, or approve any order

### Requirement: Resolve customer by name

The system MUST resolve the customer for an order by matching the parsed customer name against `Cliente.nombre_comercial`: exact match first, then accent/case-folded containment. One match auto-selects; two or more prompt the owner to disambiguate; zero offer in-chat creation. `telefono_norm` remains the database unique key.

#### Scenario: Exact name auto-selects

- GIVEN the parsed customer name exactly equals one client's `nombre_comercial`
- WHEN the customer is resolved
- THEN that client is selected without further prompt

#### Scenario: Folded containment matches one

- GIVEN a name that matches one `nombre_comercial` under accent/case folding
- WHEN the customer is resolved
- THEN that client is selected

#### Scenario: Multiple matches prompt disambiguation

- GIVEN a name that folded-contains two or more clients
- WHEN the customer is resolved
- THEN the owner is asked to choose the intended client

#### Scenario: No match offers creation

- GIVEN a name matching no client
- WHEN the customer is resolved
- THEN the owner is offered in-chat creation

### Requirement: Create client in chat

The system MUST support creating a client in chat via the command `nuevo cliente <nombre> <teléfono>`, reusing `backoffice.clients.create_client`, with `telefono_norm` as the unique key.

#### Scenario: New client created

- GIVEN the owner sends `nuevo cliente Ferretería Don Juan 1133445566`
- WHEN the command is processed
- THEN a `Cliente` is created with that name and normalized phone
- AND it can be resolved by name on subsequent orders

#### Scenario: Duplicate phone reported

- GIVEN the phone is already used by an existing client
- WHEN the command is processed
- THEN the system reports the existing client instead of creating a duplicate
