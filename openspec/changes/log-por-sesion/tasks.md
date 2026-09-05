# Tasks: Log por Sesión de Telegram

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~450 authored |
| 400-line budget risk | Medium |
| Chained PRs recommended | No (single-pr) |
| Suggested split | Single PR, work-unit commits W1→W4 |
| Delivery strategy | single-pr |

### Suggested Work Units

| Unit | Goal | Focused test command | Runtime harness | Rollback boundary |
|------|------|----------------------|-----------------|-------------------|
| W1 | Observability core + ContextVar | `pytest tests/test_session_logger.py -q` | `pytest -q` | Delete `src/observability/` |
| W2 | Session lifecycle in Orchestrator & Pipeline | `pytest tests/test_orchestrator.py tests/test_pipeline.py -q` | `pytest -q` | Revert `router.py` + `session.py` + `pipeline.py` |
| W3 | Service tracing (RAG + Orders) + E2E trace test | `pytest tests/test_pipeline_session_trace.py -q` | `pytest -q` | Revert trace hooks in `rag.py` + `draft_order.py` |
| W4 | Backoffice session viewer + full verification | `pytest -q && make lint && make typecheck` | `pytest -q` | Revert `src/backoffice/sessions.py` |

## Phase 1: W1 — Observability core & ContextVar

- [x] 1.1 Create `src/observability/__init__.py` and `src/observability/session_logger.py` with `current_session_id`, `generate_session_id`, `set_current_session_id`, `get_current_session_id`, and `log_session_event`.
- [x] 1.2 Create `tests/test_session_logger.py` testing session ID format, ContextVar isolation, log file creation under `logs/sessions/`, and JSON event structure.

## Phase 2: W2 — Session lifecycle in Orchestrator & Pipeline

- [x] 2.1 Add `session_id: str | None = None` to `ConversationState` in `src/orchestrator/session.py`.
- [x] 2.2 Modify `src/orchestrator/router.py`: generate `session_id` on `is_session_reset` ("hola bob") and preserve `session_id` in subsequent turns.
- [x] 2.3 Modify `src/pipeline.py`: activate `current_session_id` contextvar, log inbound Telegram messages, routing decisions, and outbound replies.
- [x] 2.4 Update orchestrator/pipeline unit tests to assert `session_id` persistence and reset behavior.

## Phase 3: W3 — Service tracing (RAG & Orders)

- [x] 3.1 In `src/integrations/rag.py`: add trace logging in `query` (query text, latency, HTTP status, product count, refusal/error) and `price_lookup`.
- [x] 3.2 In `src/sourcing/draft_order.py`: add trace logging on draft order creation, item addition, and item removal.
- [x] 3.3 Create `tests/test_pipeline_session_trace.py`: verify end-to-end trace from "Hola Bob" through RAG query and order modification, asserting log events in `logs/sessions/{session_id}.log`.

## Phase 4: W4 — Backoffice session viewer & verification

- [x] 4.1 Create `src/backoffice/sessions.py` helper to list session files and load events.
- [x] 4.2 Add "Sesiones" tab to `src/backoffice/app.py` displaying session files and formatted trace event tables.
- [x] 4.3 Run full verification: `pytest`, `make lint`, `make typecheck`.
