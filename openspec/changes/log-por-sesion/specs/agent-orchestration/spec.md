# agent-orchestration Specification (Delta: log-por-sesion)

## Purpose

Maintain and propagate user session identifiers across message turns and route execution, initiating a new session upon receiving the canonical session-reset greeting "hola bob".

## Requirements

### Requirement: Session lifecycle integration in Orchestrator

The `Orchestrator` MUST initialize a new session identifier on `ConversationState` when a reset greeting (`is_session_reset`) is detected, and preserve the active session identifier during normal turn handling.

#### Scenario: Orchestrator assigns session ID on reset
- GIVEN an inbound message matching `is_session_reset`
- WHEN `handle_inbound` drops existing state and initializes fresh state
- THEN the fresh `ConversationState` has a non-empty `session_id`.

#### Scenario: Orchestrator preserves session ID across turns
- GIVEN a `ConversationState` with an active `session_id`
- WHEN a non-reset message is processed
- THEN the resulting updated `ConversationState` retains the same `session_id`.
