# Design: Owner Order Intake

## Technical Approach

Minimal owner pivot (exploration approach 1). The owner becomes the only chat actor; the customer is resolved by name. Reuse parse → classify → case A/B/C → PO accumulation unchanged. Four seams: (a) owner-sender gate, (b) name resolution, (c) DISPATCH wiring, (d) owner-keyed rehydration. Quotes/cancellations/approvals become in-chat replies; `owner_phone` push and `_ChannelNotifier` are dropped. Additive except where the pivot demands (chat-path phone lookup, notifier pushes).

## Architecture Decisions

| Decision | Alternatives considered | Rationale |
|---|---|---|
| Gate at the pipeline edge (`handle_inbound`), before routing | Gate in router; gate in webhook | Pipeline owns the channel adapter + reply, so rejection is one send there; router stays pure. |
| Two typed owner keys — `owner_telegram_chat_id` + `owner_whatsapp_phone`; `owner_phone` deprecated, parseable | Generic allowlist | Telegram senders are chat ids, WhatsApp senders are phones; two keys match channel reality. |
| Name resolution = pure matcher + DB resolver in new `src/agents/customers.py` | Inline in `customer.py` | Exact→folded-containment matcher is DB-free unit-testable; `customer.py` is ~400 lines. |
| Disambiguation reuses the multi-turn pattern (`customer_disambiguation_pending` state) | Single-turn "re-type name" prompt | Numbered menu matches the spec and mirrors Case B; no router change (state already falls through to CUSTOMER). |
| Approval runs synchronously in the webhook background task; Sheets failure rolls back, order stays PENDING | `_ChannelNotifier` bridging; accept quarantine-as-approved | Reply returns via `AgentOutcome.reply`, removing the bridge. Spec requires PENDING on Sheets failure → atomic approve+register with rollback. |
| In-chat create = single message `nuevo cliente <nombre> <teléfono>`, default Base list | Multi-turn wizard | Reuses `create_client`; a default list avoids a new question. |
| Rehydration = latest open order across all customers (no owner entity) | Add an owner table | Single-owner MVP: latest open order IS the owner's; `#N` override addresses others. |

## Data Flow

Approval (DISPATCH) — the new complex flow:

```
owner "aprobá #3" → gate → route(DISPATCH) → dispatch handler
                                                │
                     parse_decision + parse_order_reference("#3")
                                                │
                    ┌──────────┬────────────────┴──────────────┐
                 UNKNOWN     REJECT                        APPROVE
                 re-ask   apply_decision(release)      apply_decision(approve)
                                                              │
                                                register_approved_order
                                                 (Sheets + deduct stock)
                                                              │
                                      QUARANTINED → rollback → "error, sigue pendiente"
                                      APPENDED    → commit   → confirmation
```

Name resolution:

```
parsed.customer_name → resolve_customer_name ─┬─ 1 match → auto-pick
                                              ├─ ≥2 → numbered disambiguation menu
                                              └─ 0 → offer "nuevo cliente <nombre> <tel>"
```

## File Changes

| File | Action | Description |
|---|---|---|
| `src/config.py` | Modify | Add `owner_telegram_chat_id`, `owner_whatsapp_phone`; deprecate `owner_phone` |
| `src/orchestrator/owner.py` | Create | `is_owner_sender` (pure) + `rejection_reply` |
| `src/agents/customers.py` | Create | `resolve_customer_name`, `match_by_name`, `parse_create_client_command` |
| `src/pipeline.py` | Modify | Gate; wire DISPATCH; drop `_ChannelNotifier`; gate deps on owner sender |
| `src/agents/customer.py` | Modify | Owner persona; name resolution; drop phone gate; intercept `nuevo cliente` |
| `src/agents/dispatch.py` | Modify | `build_dispatch_handler`; `parse_order_reference` |
| `src/orchestrator/session.py` | Modify | Owner-keyed rehydration; disambiguation state fields |
| `src/orchestrator/approval.py` | Modify | Drop `notifier`/`owner_phone`; return confirmation text |
| `src/sourcing/case_a.py` | Modify | Drop `notifier`/`owner_phone` |
| `src/sourcing/case_c.py` | Modify | Drop `notifier`/`owner_phone` |
| `src/backoffice/clients.py` | Modify | `default_price_list_id` helper |
| `tests/` (gate, customers, dispatch, case A/B/C, e2e, customer, approval, rehydrate, pipeline, router) | Create/Modify | Owner-sender + named-customer fixtures |
| `README.md`, `docs/architecture.md`, `docs/sourcing.md` | Modify | Owner-first docs |

## Interfaces / Contracts

```python
class CustomerResolutionKind(enum.Enum): EXACT; FOLDED; AMBIGUOUS; NOT_FOUND

@dataclass(frozen=True)
class CustomerResolution:
    kind: CustomerResolutionKind
    candidate: Cliente | None = None
    candidates: tuple[Cliente, ...] = ()

def match_by_name(name, rows: Sequence[tuple[int, str]]) -> CustomerResolution  # pure
def resolve_customer_name(session, name: str) -> CustomerResolution
def parse_create_client_command(text: str) -> tuple[str, str] | None  # (nombre, tel)
def is_owner_sender(sender_id: str, channel: str, settings) -> bool
def parse_order_reference(text: str) -> int | None
```

`ConversationState` gains `customer_disambiguation_pending` + `customer_candidates`. `register_approved_order`/`approve_and_register` drop `notifier`/`owner_phone`; `ApprovalResult` gains `confirmation_text`.

## Testing Strategy

| Layer | What | Approach |
|---|---|---|
| Unit | `match_by_name` (exact/folded/ambiguous/none), `parse_create_client_command`, `is_owner_sender`, `parse_order_reference` | Parametrized pure tests |
| Unit | Dispatch paths (approve/reject/unknown/#N/Sheets-fail) | Mocked session + `SheetsWriter`; assert rollback on QUARANTINED |
| Integration | Name resolution vs Postgres; owner rehydration; duplicate-phone create | `db_session` fixture; extend TRUNCATE list |
| E2E | Owner turn: gate → parse → resolve → Case A quote in chat → approve → Sheets row | Fake parser/searcher; assert Order/PO state |

## Threat Matrix

N/A — no shell, subprocess, VCS/PR automation, executable-file classification, or process-integration boundary. In-process message routing only; the owner gate is an in-memory normalized allowlist compare covered by unit tests.

## Migration / Rollout

No migrations. `owner_phone` stays parseable (ignored). Rollback = revert + redeploy; clearing the two owner keys returns to legacy customer intake.

## Resolved Open Questions

- [x] WhatsApp posture: owner-only — one persona, gate on both channels.
- [x] Case B selection: answered by the owner sender (the pivot's sender IS the owner).
- [x] Sheets failure: rollback the approval, order stays PENDING, error reply in chat.
