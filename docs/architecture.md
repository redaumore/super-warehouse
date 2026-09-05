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
Dispatch (wired confirm/cancel)
                                                                       confirm ──> classify + convert + Sheets + stock deduct (in chat)
                                                                       cancel  ──> release reservations / restore stock
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
| Dispatch & Owner | Confirm/cancel (wired) | `src/agents/dispatch.py`, `src/orchestrator/approval.py` | `parse_decision` + `apply_decision` + `confirm_and_register` (classify → convert → Sheets → deduct); `#N` override; Sheets quarantine is tolerated (the order stays CONFIRMED) |

## Data flow details

1. **Intake** (`src/api/webhook.py`): HMAC signature gate (`X-Hub-Signature-256`),
   channel verification, normalized `InboundMessage`, instant `ACK`.
2. **Gate** (`src/pipeline.py`): `is_owner_sender` compares the normalized
   sender against the configured owner allowlist BEFORE routing; a non-owner is
   rejected with `rejection_reply()` and the orchestrator never sees the turn.
3. **Route** (`src/orchestrator/router.py`): voice/image → Perception;
   awaiting-decision replies → wired Dispatch; customer-name menu picks and
   supplier-selection replies → Customer / Sourcing; in-progress orders →
   Sales or Disambiguation; fresh messages → Customer. The GUIDED agent owns
   the scripted order-creation flow (session reset "hola bob"): it is the
   ONLY order-creation path — the legacy free-form parsed intake was removed.
   Context lives in an in-memory store with a 30-minute TTL
   (`src/orchestrator/session.py`), rehydrated from the DB (latest open order
   across all customers — the owner-keyed rule).
4. **Resolve** (`src/agents/customers.py`): the customer name is matched
    against `Cliente.nombre_comercial` (exact → folded containment); `nuevo
    cliente <nombre> <teléfono>` creates a client in chat reusing
    `backoffice.clients.create_client`.
5. **Reserve** (`src/agents/inventory.py`): each quoted line soft-locks stock
   with a TTL; the sweeper (`src/scheduler/sweeper.py`) makes expiry durable
   (EXPIRED + `needs_requote`).
6. **Confirm** (`src/orchestrator/approval.py`): the confirm ceremony — the
   lifecycle transition (`src/order_lifecycle/state.py`) refuses stale quotes
   (`RequiresRequoteError`); the order is re-classified from the latest
   availability (Case C cancels, Case B persists sourcing needs and hands the
   supplier selection back); registration converts ACTIVE→CONVERTED
   reservations, appends the order row to Google Sheets, deducts stock and
   returns the in-chat confirmation. A quarantined Sheets write is TOLERATED:
   the order stays CONFIRMED and the failure is surfaced in chat (spec:
   "the order MUST remain Confirmed").

Quotes, cancellations and confirmations are in-chat replies: the legacy
`_ChannelNotifier` push to `owner_phone` was removed (the owner sender IS the
chat).

## State machine

```
DRAFT ──confirm──> CONFIRMED ──start picking──> PICKING ──complete──> READY_FOR_DELIVERY ──deliver──> CLOSED
  │                    │                            │                        │
  └──────cancel────────┴──────cancel────────────────┴────────cancel──────────┘
        Draft/Confirmed: release ACTIVE reservations
        Picking/Ready:   restore deducted stock + StockAdjustment (audit)
CONFIRMED ──modify──> DRAFT   (restores stock, releases converted locks)
DRAFT ──add/remove item──> DRAFT   (the draft persists; an empty draft stays DRAFT)
```

`orders.estado` is one of `DRAFT | CONFIRMED | PICKING | READY_FOR_DELIVERY |
CANCELED | CLOSED` plus a `needs_requote` flag. `SourcingState` is a separate
informational axis (PENDING_ASSEMBLY / IN_PREPARATION / CANCELLED) that never
drives the order state. TTL expiry is a reservation property, not a seventh
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

Gradio Blocks (`src/backoffice/app.py`) with seven tabs: Catalog (stock/price/
margin edits — margin recomputes list price), Clients (registration with phone
normalization), Orders/Monitor (state + soft-lock + Sheets sync), Purchase
Orders (send/receive/cancel), Ingestion (upload → Vision preview grid →
confirm to inventory), Suppliers (master data) and Customer Orders (order
lines, exchange rates, default margin, fulfillment actions). Supplier OCR
(`src/supplier/ocr.py`) rejects illegible documents; barcode decoding
(`src/barcode/decoder.py`) flags duplicate mappings for manual resolution.

## Feature flags

`FASE1..4_ENABLED` (`src/features.py`) gate each phase at its boundary: a
disabled fase refuses to run with `FeatureDisabledError` instead of
half-working — the webhook still ACKs without dispatching, the backoffice
refuses to build.

## Product queries: local-first → RAG

Conversational product queries (a plain chat turn) resolve
through a precedence chain (`src/agents/product_search.py`): the local catalog
search (`DbCatalogSearcher`) runs first, and only a zero-candidate local result
— or a local database error — falls back to the supplier-catalog RAG
(`RagProductClient`, the sibling `fase-0-pdf-parsing` service). The chain never
raises: a RAG timeout or outage resolves to the `ERROR` source and the customer
note says the supplier catalogs could not be consulted — it NEVER claims the
item is out of stock; a RAG refusal or empty result resolves to `NONE` ("not
found in current catalogs", suggest a synonym or reformulation). RAG results
are numbered, cheapest first, with provider, price, specs and source page/PDF,
and close with the footer "These are supplier-catalog items, not own stock."
Note: the RAG service runs on port **8001** while some docs still say 8000 —
`RAG_BASE_URL` defaults to 8001, which is the live port.

## Checklist: how to verify a change is safe

- [ ] The new behavior has a test with a Spanish first-line docstring (feeds `docs/escenarios-testeados.md`)
- [ ] `make test` passes (unit + integration + E2E)
- [ ] `make lint && make typecheck` clean
- [ ] External calls (OpenAI, Sheets, WhatsApp) are mocked in tests
- [ ] DB-dependent tests skip cleanly when Postgres is down (`skipif` guard)