# session-trace-logging Specification

## Purpose

Capture structured, chronological trace logs for each user session on Telegram, initiated when the user sends "Hola Bob", recording all interactions with Telegram, the orchestrator, product search (local + RAG), and order persistence/lifecycle.

## Requirements

### Requirement: Session lifecycle and identification

The system MUST generate a unique, non-empty `session_id` when an inbound Telegram message triggers a session reset via the canonical greeting "hola bob" (case-insensitive, whole message match). The `session_id` MUST be preserved across turns in the conversation context.

#### Scenario: Session reset generates unique session ID
- GIVEN an active or empty conversation state for a sender
- WHEN the sender transmits "Hola Bob"
- THEN a fresh `ConversationState` is initialized with a newly generated `session_id`
- AND the previous in-memory state is dropped.

#### Scenario: Subsequent messages retain the active session ID
- GIVEN a conversation with an active `session_id`
- WHEN the sender transmits another message that is not a reset command
- THEN the turn executes under the same `session_id`.

### Requirement: Context propagation and correlation

The system MUST propagate the active `session_id` through `contextvars` so that synchronous and asynchronous operations invoked during the turn can record trace events without explicit argument plumbing.

#### Scenario: Subordinate service reads session ID from context
- GIVEN an inbound message being processed within an active session
- WHEN a service (RAG client, catalog searcher, or order manager) emits a trace event
- THEN the event is tagged with the current `session_id` from the context variable.

### Requirement: Structured session trace recording

The system MUST record trace events for:
1. Inbound message receipt and outbound reply transmission.
2. Orchestrator routing decisions and agent handler invocations.
3. RAG queries, execution latency, response status, and product count / refusal / error status.
4. Order lifecycle events (draft creation, product additions/removals, state transitions).

Each event MUST contain an ISO-8601 timestamp, the service name, action name, and structured key-value payload.

#### Scenario: Inbound and outbound Telegram message logged
- GIVEN an incoming message from Telegram
- WHEN the pipeline processes the message and responds
- THEN an event for the inbound message and an event for the outbound reply are appended to the session trace.

#### Scenario: RAG query logged in session trace
- GIVEN a turn where a product query invokes `RagProductClient.query`
- WHEN the query executes
- THEN a trace event is recorded containing the query text, latency in seconds, HTTP status, and product count.

#### Scenario: Order operations logged in session trace
- GIVEN a turn where a draft order is created or updated
- WHEN the order operation executes
- THEN an order trace event is appended with the `order_id` (if assigned) and the item details or transition.

### Requirement: Session log file isolation

The system MUST write each session's trace events to an individual file named `logs/sessions/{session_id}.log`. The directory `logs/sessions/` MUST be created automatically if it does not exist.

#### Scenario: Session file created upon first event
- GIVEN a new session with an assigned `session_id`
- WHEN the first event for that session is recorded
- THEN the file `logs/sessions/{session_id}.log` is created and contains the formatted event.
