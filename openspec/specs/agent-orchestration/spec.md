# agent-orchestration Specification

## Purpose

Coordinate six specialized agents through an orchestrator that routes work and keeps heavy processing asynchronous so the conversational SLA is met.

## Requirements

### Requirement: Six specialized agents

The system MUST be composed of six specialized agents: Perception, Customer & Context, Disambiguation, Inventory & Pricing, Conversational Sales, and Dispatch & Owner Assistant.

#### Scenario: Agents present and bounded

- GIVEN the system is running
- WHEN an agent's responsibility is inspected
- THEN each of the six agents owns a distinct capability (perception, customer context, disambiguation, inventory/pricing, conversation, dispatch/owner assistance)
- AND no capability is owned by two agents

#### Scenario: Agent invoked for its domain

- GIVEN a transcription task
- WHEN the orchestrator dispatches it
- THEN the Perception agent handles transcription rather than a pricing or sales agent

### Requirement: Route inbound messages to the correct agent

The system MUST gate inbound messages to the owner sender, then route each message to the agent responsible for that step. An order awaiting a decision MUST route to a wired Dispatch agent that runs the real approval flow, and a supplier-selection-pending order routes to Sourcing.

(Previously: routing had no owner gate, and Dispatch was a stub that swallowed approval replies.)

#### Scenario: Order routed through the pipeline

- GIVEN an inbound owner order
- WHEN the message is processed
- THEN it is gated, parsed, then routed to the customer/sourcing flow in the correct order

#### Scenario: Barcode photo routed to perception

- GIVEN a barcode photo from the owner
- WHEN the message is processed
- THEN it is routed to barcode decoding rather than the order-intake path

#### Scenario: Approval reply routed to wired Dispatch

- GIVEN an order is awaiting a decision
- WHEN the owner replies "aprobá" or "rechazá"
- THEN it is routed to Dispatch, which runs the real approval flow

#### Scenario: Non-owner sender gated before routing

- GIVEN a message from a sender not in the owner allowlist
- WHEN the message arrives
- THEN it is rejected before any agent routing

### Requirement: Run heavy processing asynchronously

The system MUST run heavy processing (speech-to-text, vision, search) asynchronously so webhooks are not blocked.

#### Scenario: Heavy work does not block intake

- GIVEN a voice order arrives
- WHEN transcription and search run
- THEN the intake returns immediately with an ACK
- AND heavy processing continues in the background

#### Scenario: Background completion continues the flow

- GIVEN asynchronous processing finishes
- WHEN results are ready
- THEN the orchestrator resumes the flow (quote, soft-lock, owner notification) without a new client message

### Requirement: Orchestrator coordinates the end-to-end flow

The system MUST use an orchestrator to coordinate state across agents and preserve conversational context for the whole order. Rehydrating the owner's conversation MUST load the latest open order for any customer, with an explicit `pedido #N` reference overriding to a specific order.

(Previously: rehydration was keyed to the customer's phone, so the owner sender had no recoverable conversation state.)

#### Scenario: Cross-agent state preserved

- GIVEN a multi-step order requiring several agents
- WHEN the flow progresses
- THEN the orchestrator carries context (customer, items, reservations) between agents
- AND no step loses the order's identity

#### Scenario: Latest open order wins on rehydration

- GIVEN the owner has multiple pending orders across customers
- WHEN the owner's conversation is rehydrated after the TTL
- THEN the latest open order becomes the active order

#### Scenario: pedido #N overrides to a specific order

- GIVEN the owner references `pedido #3` in a decision
- WHEN the decision is applied
- THEN it targets order #3 regardless of which order is latest

### Requirement: Wire DISPATCH to the approval flow

The system MUST wire the Dispatch agent to `parse_decision` → `apply_decision` → `approve_and_register` (with `SheetsWriter`) so approval and rejection decisions are actually executed.

#### Scenario: Approval registers end-to-end

- GIVEN the owner approves a quoted order
- WHEN the decision is applied
- THEN the order is approved and registered via `approve_and_register`

#### Scenario: Rejection releases reservations

- GIVEN the owner rejects a quoted order
- WHEN the decision is applied
- THEN the order is rejected and its reservations are released

#### Scenario: Sheets failure keeps order pending

- GIVEN the Sheets write fails during registration
- WHEN approval is applied
- THEN the owner receives an error reply in chat
- AND the order remains pending rather than half-registered