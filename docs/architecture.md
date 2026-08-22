# Architecture — Ferretería Multi-Agent MVP

This is the mental model of the whole system: one intake, six agents, one
database, one append-only output. Read this before touching `src/`.

## The pipeline at a glance

```
WhatsApp / Telegram ──> webhook ──> ACK (<5 s) ──> client
                          └── background task ──> Orchestrator
                                                   Perception (STT / Vision)
                                                   Customer & Context
                                                   Disambiguation (hybrid search)
                                                   Inventory & Pricing (soft-lock)
                                                   Conversational Sales (quote)
                                                   Dispatch & Owner (approve/reject)
                                                       approve ──> Sheets + stock deduct + confirm
                                                       reject  ──> release reservations
```

Heavy work (transcription, vision, search, pricing) never blocks the intake:
the webhook ACKs immediately and hands the message to a FastAPI background
task. The orchestrator routes each message to the agent that owns that step
and carries conversation context between them.

## The six agents

| Agent | Owns | Where | Key behavior |
|---|---|---|---|
| Perception | STT + vision | `src/agents/perception.py`, `src/integrations/openai.py` | Whisper transcript with flagged low-confidence fragments; GPT-4o Vision description; failures become clear errors, never silent guesses |
| Customer & Context | Phone identity | `src/agents/customer.py` | Normalizes AR numbers to WhatsApp E.164 form; KNOWN / UNKNOWN / INVALID — never guesses |
| Disambiguation | Catalog resolution | `src/agents/disambiguation.py` | Hybrid rapidfuzz + pgvector cosine; auto-maps high-confidence, menus on ambiguity, reports not-found |
| Inventory & Pricing | Soft-lock + availability | `src/agents/inventory.py` | `available = stock − Σ(active, unexpired reservations)`; TTL enforced at read time |
| Conversational Sales | Quotes + adjustments | `src/agents/sales.py`, `src/pricing/engine.py` | Compound discounts via the pure pricing function; per-line owner adjustments |
| Dispatch & Owner | Notify + decide | `src/agents/dispatch.py`, `src/orchestrator/approval.py` | Quote to owner; approve/reject parsing (accent-safe); approval → convert → Sheets → stock → confirm |

## Data flow details

1. **Intake** (`src/api/webhook.py`): HMAC signature gate (`X-Hub-Signature-256`),
   channel verification, normalized `InboundMessage`, instant `ACK`.
2. **Route** (`src/orchestrator/router.py`): voice/image → Perception;
   awaiting-decision replies → Dispatch; in-progress orders → Sales or
   Disambiguation; fresh messages → Customer. Context lives in an in-memory
   store with a 30-minute TTL (`src/orchestrator/session.py`).
3. **Reserve** (`src/agents/inventory.py`): each quoted line soft-locks stock
   with a TTL; the sweeper (`src/scheduler/sweeper.py`) makes expiry durable
   (EXPIRED + `needs_requote`).
4. **Approve** (`src/orchestrator/approval.py`): the lifecycle transition
   (`src/order_lifecycle/state.py`) refuses stale orders (`RequiresRequoteError`);
   registration converts ACTIVE→CONVERTED reservations, appends the order row
   to Google Sheets (quarantined on failure, never blocking), deducts stock and
   confirms to the owner.

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