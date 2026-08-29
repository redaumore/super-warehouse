# Tasks: Owner Order Intake

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~1400 (range 1300-1500) |
| 400-line budget risk | High |
| Chained PRs recommended | No (per explicit `exception-ok` override) |
| Suggested split | Single PR with work-unit commits W1-W8 |
| Delivery strategy | exception-ok |
| Chain strategy | size-exception |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: size-exception
400-line budget risk: High

Maintainer pre-approved `size:exception` ("sin límites"): single PR, no chained slices, no 400-line stop. Risk stays **High** but plan moves as one PR with **work-unit commits** (W1-W8). Reviewer merges the whole stack.

### Suggested Work Units (commits inside the single PR)

| Unit | Goal | Focused test command | Runtime harness | Rollback boundary |
|------|------|----------------------|-----------------|-------------------|
| W1 | Owner gate seam (config + `is_owner_sender`) | `pytest tests/test_owner_gate.py -q` | `pytest -q` | Revert `src/orchestrator/owner.py` + `src/config.py` |
| W2 | Pure name matcher + command parser | `pytest tests/test_customers.py::test_match tests/test_customers.py::test_parse -q` | `pytest -q` | Remove pure funcs in `src/agents/customers.py` |
| W3 | DB name resolver + `default_price_list_id` helper | `pytest tests/test_customers.py -q` | `make db-up && pytest -q` | Drop helper + resolver; `create_client` callers unchanged |
| W4 | In-chat `nuevo cliente` intercept + duplicate-phone path | `pytest tests/test_customers.py::test_create tests/test_customer.py -q` | `make db-up && pytest -q` | Revert command branch in `src/agents/customer.py` |
| W5 | ConversationState disambiguation fields + numbered reply | `pytest tests/test_session_rehydrate_owner.py -q` | `make db-up && pytest -q` | Revert `ConversationState` fields + AMBIGUOUS branch |
| W6 | Owner-keyed rehydration (latest open across customers) | `pytest tests/test_session_rehydrate_owner.py -q` | `make db-up && pytest -q` | Revert `rehydrate_conversation`; phone lookup restored |
| W7 | DISPATCH wiring + Sheets rollback + `parse_order_reference` | `pytest tests/test_dispatch_handler.py tests/test_approval.py -q` | `make db-up && pytest -q` | Remove `build_dispatch_handler`; pipeline wires stub |
| W8 | Pipeline wiring + notifier removal + E2E + docs | `pytest -q` (full regression) | `mkdocs build` if tool present | Revert pipeline; `owner_phone` push restored |

Note (locked input #3, MODIFIED ACK): `tests/test_webhook.py::test_ack_returns_quickly` already proves the 5-second SLA; the spec re-targets the ACK at the owner sender (single actor). Pipeline ACK is `sender_id`-driven, so the existing test carries the requirement — no new task beyond W8.

## Phase 1: Configuration + Owner Gate

- [x] 1.1 Add `owner_telegram_chat_id` + `owner_whatsapp_phone` to `Settings` in `src/config.py`; mark `owner_phone` deprecated (kept parseable).
- [x] 1.2 Create `src/orchestrator/owner.py`: `is_owner_sender(sender_id, channel, settings)` (pure, normalized compare) + `rejection_reply()`.
- [x] 1.3 Modify `src/pipeline.py` `handle_inbound`: gate first; non-owner → send `rejection_reply` and return without routing.
- [x] 1.4 Add `tests/test_owner_gate.py`: parametrized owner vs non-owner matrix on both channels.

## Phase 2: Name Resolution + In-Chat Client Creation

- [x] 2.1 Create `src/agents/customers.py`: `CustomerResolutionKind` enum, `CustomerResolution` dataclass, `match_by_name(name, rows)` pure matcher (EXACT/FOLDED/AMBIGUOUS/NOT_FOUND).
- [x] 2.2 Add `resolve_customer_name(session, name)` (DB) + `parse_create_client_command(text) -> (nombre, tel) | None`.
- [x] 2.3 Add `default_price_list_id(session)` in `src/backoffice/clients.py` (locked input #1: Base list id) + parametrized test.
- [x] 2.4 Add `customer_disambiguation_pending` + `customer_candidates` to `ConversationState` in `src/orchestrator/session.py`.
- [x] 2.5 Modify `src/agents/customer.py`: drop phone gate; resolve by parsed name; intercept `nuevo cliente`; AMBIGUOUS → numbered menu.
- [x] 2.6 Add `tests/test_customers.py`: pure matcher (parametrized), command parser, DB integration (one/many/zero), **duplicate-phone scenario** (locked input #2).

## Phase 3: DISPATCH Wiring + Approval Rewrite

- [x] 3.1 Add `parse_order_reference(text) -> int | None` in `src/agents/dispatch.py`.
- [x] 3.2 Add `build_dispatch_handler(session_factory, sheets)` in `src/agents/dispatch.py`: parse decision → load order (`#N` override or `state.order_id`) → `apply_decision` + `approve_and_register`; Sheets QUARANTINED → rollback + error reply.
- [x] 3.3 Modify `src/orchestrator/approval.py`: drop `notifier`/`owner_phone`; add `ApprovalResult.confirmation_text`; atomic approve+register with rollback.
- [x] 3.4 Add `tests/test_dispatch_handler.py`: approve/reject/unknown/`#N` override/Sheets-fail; mock SheetsWriter; assert rollback on QUARANTINED.

## Phase 4: Owner-Keyed Rehydration + Disambiguation

- [x] 4.1 Modify `rehydrate_conversation` in `src/orchestrator/session.py`: drop phone→customer lookup; latest open Order across all customers.
- [x] 4.2 Wire AMBIGUOUS reply: numbered menu pick resolves to a `Cliente`, clears `customer_disambiguation_pending`.
- [x] 4.3 Add `tests/test_session_rehydrate_owner.py`: latest-wins across customers; pedido `#N` overrides rehydrated `order_id`.

## Phase 5: Pipeline Wiring + Notifier Removal + E2E + Docs

- [x] 5.1 Modify `src/pipeline.py`: drop `_ChannelNotifier`; wire DISPATCH via `build_dispatch_handler`; thread `SheetsWriter`; remove `owner_phone` push.
- [x] 5.2 Modify `src/sourcing/case_a.py` + `case_c.py`: drop `notifier` + `owner_phone` params; reply travels via `AgentOutcome.reply`.
- [x] 5.3 Add `tests/test_pipeline_owner.py`: owner turn → gate → parse → resolve → Case A quote in chat → approve → Sheets row; non-owner → polite rejection.
- [x] 5.4 Update `tests/test_approval.py` + `test_dispatch.py` + `test_case_c.py` callers: drop `notifier`/`owner_phone`; assert `confirmation_text`.
- [x] 5.5 Update `README.md`, `docs/architecture.md`, `docs/sourcing.md`: owner-first docs; `owner_phone` deprecation note; pipeline edge diagram.
