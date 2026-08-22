# whatsapp-order-intake Specification

## Purpose

Receive customer orders as WhatsApp text messages or voice notes and acknowledge receipt immediately so the client is not left waiting while heavy processing runs.

## Requirements

### Requirement: Ingest text and voice orders

The system MUST accept incoming customer orders submitted as WhatsApp text messages and as voice notes in the formats produced by WhatsApp (e.g. .ogg / .mp3).

#### Scenario: Text order received

- GIVEN a known channel is active and a customer sends a text message describing an order
- WHEN the message arrives at the intake endpoint
- THEN the system accepts the message as an order candidate
- AND proceeds to transcription/parsing without manual intervention

#### Scenario: Voice order received

- GIVEN a customer sends a WhatsApp voice note describing an order
- WHEN the audio arrives at the intake endpoint
- THEN the system accepts the audio as an order candidate
- AND routes it for speech-to-text transcription

### Requirement: Ephemeral acknowledgement under 5 seconds

The system MUST send an ephemeral acknowledgement to the customer within 5 seconds of receiving an order, before heavy processing (transcription, search, pricing) completes.

#### Scenario: ACK sent promptly

- GIVEN an order has just arrived
- WHEN the intake endpoint receives it
- THEN the customer receives an acknowledgement such as "Recibí tu pedido, ya te lo estoy cotizando..."
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
