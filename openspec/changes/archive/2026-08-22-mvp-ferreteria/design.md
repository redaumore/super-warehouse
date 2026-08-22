# Design: mvp-ferreteria — Hardware Store Multi-Agent MVP

## Technical Approach

Greenfield Python service. FastAPI accepts WhatsApp (primary) and Telegram (demo) via a shared channel abstraction, ACKs in <5 s, then hands heavy work (Whisper STT, GPT-4o Vision, embeddings, hybrid search) to background processing via an internal orchestrator over six agents. PostgreSQL + pgvector is the source of truth; Google Sheets is append-only output; owner approves via WhatsApp with soft-lock reservations; Gradio serves the backoffice.

## Architecture Decisions

### Decision: Stack

| Option | Tradeoff | Decision |
|--------|----------|----------|
| pgvector (in Postgres) | One DB, no extra service; small catalog | **Choose** |
| Qdrant | Scales, but extra ops for a 4-week MVP | Reject |
| Managed vector store | Zero ops, cost + lock-in + latency | Reject |
| Gradio backoffice | Dataframe/Image Components fit the remito confirm grid | **Choose** |
| Streamlit/NiceGUI | Simpler CRUD, more custom grid code | Reject |
| BackgroundTasks + APScheduler sweeper | ACK + async work + periodic TTL release | **Choose** |
| Celery + Redis | Full queue, heavy infra | Reject |

Base stack (kept): Python 3.12, FastAPI, PostgreSQL 16, OpenAI SDK, SQLAlchemy 2.0 + Alembic, Pydantic v2, Google Sheets API, pytest, pyzbar.
**Rationale**: the doc prescription is sound; only Qdrant (unnecessary ops) and BackgroundTasks-for-TTL (can't schedule) are weak. TTL correctness does NOT depend on the sweeper — reservations are filtered by expiry at read time — the sweeper is best-effort cleanup.

### Decision: State machine on `orders.estado`

**Choice**: enum `{PENDING_APPROVAL, APPROVED, IN_DISPATCH, REJECTED}` + `needs_requote` flag. TTL expiry releases reservations and sets `needs_requote`; approval on a stale order re-quotes first.
**Rationale**: spec fixes the four states; expiry is a reservation property, not a fifth order state.

### Decision: Pricing as pure function

**Choice**: `Base = cost × (1 + margin)`; `Final = Base × (1 − list_discount) × (1 − particular_discount)` in `pricing/engine.py`, no I/O.
**Rationale**: fixed by spec; a pure function is the highest-value unit test and prevents agents re-deriving prices.

## Data Flow

```
WhatsApp → webhook → ACK(<5s) → client
                 └→ BackgroundTasks → Orchestrator
                      Perception → Customer&Context → Disambiguation → Inventory&Pricing
                                      (soft-lock + quote) → Sales → Dispatch&Owner
                                        → Owner WhatsApp (approve/reject)
                                            approve → Sheets + deduct + confirm
                                            reject  → release reservations
```

## File Changes

| File | Action | Purpose |
|------|--------|---------|
| `src/api/webhook.py` | Create | Webhook + signature + ephemeral ACK |
| `src/orchestrator/` | Create | Routing + session state |
| `src/agents/{perception,customer,disambiguation,inventory,sales,dispatch}.py` | Create | Six agents, each owns its tools |
| `src/pricing/engine.py` | Create | Pure pricing function |
| `src/db/{models,migrations}.py` | Create | SQLAlchemy models + Alembic |
| `src/channels/{base,telegram,whatsapp}.py` | Create | Channel abstraction: common in/out interface + Telegram & WhatsApp adapters |
| `src/integrations/{openai,sheets}.py` | Create | SDK adapters |
| `src/backoffice/` | Create | Gradio modules |
| `tests/` | Create | pytest unit + integration |

## Interfaces / Contracts

```
lista_precios(lista_id, nombre, descuento_lista_pct)          # Base = 0%; Gremio A = 10%; Gremio B = 20% (owner-configurable)
clientes(customer_id, nombre_comercial, contacto, telefono_norm,
         lista_precios_id→lista_precios, descuento_particular_pct)   # no credit/payment
catalogo(id, codigo_interno, codigo_barras, proveedor_id, nombre_oficial,
         costo_proveedor, margen_aplicado_pct, precio_lista_base,
         stock_disponible, sinonimos[], embedding vector(1536))
proveedores(proveedor_id, razon_social, contacto, telefono, margen_predeterminado, condiciones)
proveedor_sku_mapping(mapping_id, proveedor_id, codigo_proveedor, descripcion_raw, sku_interno, confianza)
stock_reservations(reservation_id, sku, customer_id, order_id, cantidad, timestamp, ttl_minutes,
                   estado: ACTIVE|CONVERTED|RELEASED|EXPIRED)
orders(order_id, customer_id, estado, needs_requote, created_at, approved_at, rejected_at)
order_items(id, order_id, sku, cantidad, base_price, final_price, adjustment)
```

Available stock = `stock_disponible − Σ(active, unexpired reservations)`.

## Testing Strategy

| Layer | What | Approach |
|-------|------|----------|
| Unit | Pricing, state transitions, phone normalization, TTL expiry | pytest pure functions |
| Integration | Soft-lock RED (reserve→expiry releases; reject releases), hybrid search, ACK <5 s | pytest + Postgres/pgvector |
| E2E | Order → approval → Sheets row | WhatsApp mock |

RED tests: TTL auto-release; reject releases; expired order cannot approve silently; discounts compound (not sum).

## Threat Matrix

N/A — no routing, shell, subprocess, VCS/PR automation, executable-classification, or process-integration boundary. Greenfield business app; "agents" are the product's domain agents, not the SDD harness.

## Migration / Rollout

No migration (greenfield). Per-Fase feature flags; Fase boundaries are safe stop points. Sheets append-only with order IDs; corrupt rows quarantined.

## Resolved Decisions (user-confirmed)

- **Stack**: CONFIRMED — pgvector in Postgres, Gradio backoffice, FastAPI BackgroundTasks + APScheduler sweeper. Full dev environment runs locally (Docker/Homebrew); only OpenAI/Sheets/WhatsApp are external and mocked in dev/tests.
- **Price-list discounts**: Base = 0%, Gremio A = 10%, Gremio B = 20% (owner-configurable in backoffice).
- **Channels**: WhatsApp primary; Telegram in-MVP as the demo channel. Both behind a shared `channels/` abstraction so implementation order is irrelevant (Telegram-first is lower-friction).
- **Duplicate barcode**: flag to owner for manual resolution.
