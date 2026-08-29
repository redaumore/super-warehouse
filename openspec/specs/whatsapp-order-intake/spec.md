# whatsapp-order-intake Specification

## Purpose

Receive owner orders as WhatsApp text messages or voice notes and acknowledge receipt immediately so the owner is not left waiting while heavy processing runs.

## Requirements

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

### Requirement: Transcribe voice notes

The system MUST transcribe voice-note audio into text using speech-to-text before order parsing.

#### Scenario: Clean audio transcribed

- GIVEN a voice note with clearly spoken product requests
- WHEN the audio is processed
- THEN the system produces a text transcript of the spoken order
- AND the transcript is passed to order parsing

#### Scenario: Noisy audio still yields a usable transcript

- GIVEN a voice note with background noise or dialect variance
- WHEN the audio is processed
- THEN the system produces a best-effort transcript
- AND any low-confidence fragments are flagged for disambiguation downstream rather than silently dropped

### Requirement: Handle transcription failure

The system MUST detect transcription failure and recover without losing the customer.

#### Scenario: Transcription fails outright

- GIVEN a voice note cannot be transcribed (silent, corrupted, or unsupported audio)
- WHEN the transcription step errors
- THEN the system notifies the customer that the audio could not be understood
- AND asks the customer to resend as text or a new voice note

#### Scenario: Partial transcription is confirmed

- GIVEN only part of an audio could be transcribed with confidence
- WHEN ambiguity remains
- THEN the system does not proceed to quotation on guessed items
- AND prompts the customer to confirm or correct the unclear items

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