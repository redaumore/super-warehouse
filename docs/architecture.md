# Architecture — Ferretería Multi-Agent MVP

This is the mental model of the whole system: one owner, one intake, six
agents, one database, one append-only output. Read this before touching `src/`.

## The pipeline at a glance

```
WhatsApp / Telegram ──> webhook ──> ACK (<5 s) ──> client
                          └── background task ──> OWNER GATE (allowlist)
                                                     │ non-owner → polite rejection
                                                     └─ owner ──> Orchestrator
                                                                  Perception (STT / Vision)
                                                                  Customer & Context (resolve by NAME)
                                                                  Disambiguation (hybrid search)
                                                                  Inventory & Pricing (soft-lock)
                                                                  Conversational Sales (quote)
                                                                  Dispatch (wired approve/reject)
                                                                      approve ──> Sheets + stock deduct + confirm (in chat)
                                                                      reject  ──> release reservations
                                                                  Sourcing (Case B supplier selection)
```

Heavy work (transcription, vision, search, pricing) never blocks the intake:
the webhook ACKs immediately and hands the message to a FastAPI background
task. The owner gate (`src/orchestrator/owner.py`) runs FIRST, at the pipeline
edge: only the configured owner sender (Telegram chat id / WhatsApp phone) is
routed; anyone else gets a polite rejection and no order is ever created,
quoted or approved for them. The orchestrator routes each message to the agent
that owns that step and carries conversation context between them.

## The six agents

| Agent | Owns | Where | Key behavior |
|---|---|---|---|
| Perception | STT + vision | `src/agents/perception.py`, `src/integrations/openai.py` | Whisper transcript with flagged low-confidence fragments; GPT-4o Vision description; failures become clear errors, never silent guesses |
| Customer & Context | Owner chat + customer-by-name | `src/agents/customer.py`, `src/agents/customers.py` | Owner-assistant persona; resolves the customer by `nombre_comercial` (exact → folded containment; 1 auto-picks, ≥2 numbered menu, 0 offers in-chat creation) |
| Disambiguation | Catalog resolution | `src/agents/disambiguation.py` | Hybrid rapidfuzz + pgvector cosine; auto-maps high-confidence, menus on ambiguity, reports not-found |
| Inventory & Pricing | Soft-lock + availability | `src/agents/inventory.py` | `available = stock − Σ(active, unexpired reservations)`; TTL enforced at read time |
| Conversational Sales | Quotes + adjustments | `src/agents/sales.py`, `src/pricing/engine.py` | Compound discounts via the pure pricing function; per-line owner adjustments |
| Dispatch & Owner | Approve/reject (wired) | `src/agents/dispatch.py`, `src/orchestrator/approval.py` | `parse_decision` + `apply_decision` + `register_approved_order` (Sheets); `#N` override; Sheets failure rolls the approval back |

## Data flow details

1. **Intake** (`src/api/webhook.py`): HMAC signature gate (`X-Hub-Signature-256`),
   channel verification, normalized `InboundMessage`, instant `ACK`.
2. **Gate** (`src/pipeline.py`): `is_owner_sender` compares the normalized
   sender against the configured owner allowlist BEFORE routing; a non-owner is
   rejected with `rejection_reply()` and the orchestrator never sees the turn.
3. **Route** (`src/orchestrator/router.py`): voice/image → Perception;
   awaiting-decision replies → wired Dispatch; customer-name menu picks and
   supplier-selection replies → Customer / Sourcing; in-progress orders →
   Sales or Disambiguation; fresh messages → Customer (after the parse step).
   Context lives in an in-memory store with a 30-minute TTL
   (`src/orchestrator/session.py`), rehydrated from the DB (latest open order
   across all customers — the owner-keyed rule).
4. **Resolve** (`src/agents/customers.py`): the parsed customer name is matched
   against `Cliente.nombre_comercial` (exact → folded containment); `nuevo
   cliente <nombre> <teléfono>` creates a client in chat reusing
   `backoffice.clients.create_client`.
5. **Reserve** (`src/agents/inventory.py`): each quoted line soft-locks stock
   with a TTL; the sweeper (`src/scheduler/sweeper.py`) makes expiry durable
   (EXPIRED + `needs_requote`).
6. **Approve** (`src/orchestrator/approval.py`): the lifecycle transition
   (`src/order_lifecycle/state.py`) refuses stale orders (`RequiresRequoteError`);
   registration converts ACTIVE→CONVERTED reservations, appends the order row
   to Google Sheets, deducts stock and returns the in-chat confirmation. A
   quarantined Sheets write RAISES `SheetsRegistrationError` so the caller rolls
   the approval back — the order stays PENDING (never half-registered).

Quotes, cancellations and approvals are in-chat replies: the legacy
`_ChannelNotifier` push to `owner_phone` was removed (the owner sender IS the
chat).

## State machine

```
PENDING_APPROVAL ──approve──> APPROVED ──dispatch──> IN_DISPATCH
       │
       └──reject / TTL expiry──> REJECTED / needs_requote (re-quote before approve)
```

`orders.estado` is one of `PENDING_APPROVAL | APPROVED | IN_DISPATCH | REJECTED`
plus a `needs_requote` flag. TTL expiry is a reservation property, not a fifth
state.

## Pricing

Pure function (`src/pricing/engine.py`), no I/O:

```
Base  = cost × (1 + margin)                  # HALF_UP to the cent
Final = Base × (1 − list_discount) × (1 − particular_discount)   # compounds, never sums
```

Lists: Base = 0%, Gremio A = 10%, Gremio B = 20% (owner-configurable). Every
agent delegates to this function — no agent re-derives a price.

## Backoffice

Gradio Blocks (`src/backoffice/app.py`) with four tabs: Catalog (stock/price/
margin edits — margin recomputes list price), Clients (registration with phone
normalization), Orders/Monitor (state + soft-lock + Sheets sync) and Ingestion
(upload → Vision preview grid → confirm to inventory). Supplier OCR
(`src/supplier/ocr.py`) rejects illegible documents; barcode decoding
(`src/barcode/decoder.py`) flags duplicate mappings for manual resolution.

## Feature flags

`FASE1..4_ENABLED` (`src/features.py`) gate each phase at its boundary: a
disabled fase refuses to run with `FeatureDisabledError` instead of
half-working — the webhook still ACKs without dispatching, the backoffice
refuses to build.

## Checklist: how to verify a change is safe

- [ ] The new behavior has a test with a Spanish first-line docstring (feeds `docs/escenarios-testeados.md`)
- [ ] `make test` passes (unit + integration + E2E)
- [ ] `make lint && make typecheck` clean
- [ ] External calls (OpenAI, Sheets, WhatsApp) are mocked in tests
- [ ] DB-dependent tests skip cleanly when Postgres is down (`skipif` guard)