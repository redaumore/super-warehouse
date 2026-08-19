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

The system MUST route each inbound message to the agent responsible for that step.

#### Scenario: Order routed through the pipeline

- GIVEN an inbound customer order
- WHEN the message is processed
- THEN it is routed to perception, then disambiguation, then pricing, then the owner-assistant flow in the correct order

#### Scenario: Barcode photo routed to perception

- GIVEN a barcode photo from the owner
- WHEN the message is processed
- THEN it is routed to barcode decoding rather than the order-intake path

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

The system MUST use an orchestrator to coordinate state across agents and preserve conversational context for the whole order.

#### Scenario: Cross-agent state preserved

- GIVEN a multi-step order requiring several agents
- WHEN the flow progresses
- THEN the orchestrator carries context (customer, items, reservations) between agents
- AND no step loses the order's identity

#### Scenario: Human-in-the-loop wait is handled

- GIVEN an order is awaiting owner approval
- WHEN the owner responds later
- THEN the orchestrator resumes the correct order and applies the decision
