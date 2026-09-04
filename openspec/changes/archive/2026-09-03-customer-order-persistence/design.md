# Design: Customer Order Persistence

## Technical Approach

Explore approach 3: a pure `src/pricing/order_pricing.py` (injectable rate source) feeds a new
finalize intent in the CUSTOMER handler; a parallel persistence step mirrors `persist_case_a_order`.
The chat draft (`ConversationState.draft_items`) is priced source-aware, persisted as a
`PENDING_APPROVAL` Order + OrderItems, then routed through the UNCHANGED approval flow
(which owns Sheets sync). RAG lines are frozen snapshots with no `catalogo` FK; local lines
reserve stock. Missing exchange rate → `conversion_pending` order, recomputed on rate load.

## Architecture Decisions

| Decision | Choice | Alternatives rejected | Rationale |
|---|---|---|---|
| Local margin source (PIN) | `Catalogo.margen_aplicado_pct` | `Supplier.default_margin_pct`; `precio_lista_base` | `precio_lista_base` can be overwritten by `update_price` independent of cost (`catalog.py:68-75`), so it is never trusted. `default_margin_pct` is the supplier-level ingestion default, not the product's actual applied margin (which diverges via `update_margin`). `margen_aplicado_pct` is the per-product order-time margin: `compute_base(costo_proveedor, margen_aplicado_pct)` reuses `engine.compute_base`, applies margin once, avoids double-counting. |
| RAG base price | `offer_price → ARS → ×(1+supplier margin)` | apply margin before conversion; use local margin | Margin is a percentage — order of conversion vs margin is commutative, but spec fixes "convert non-ARS before subtotal". Supplier margin = `codigo_proveedor → suppliers.code → default_margin_pct`; unmapped → setting (20%). |
| Pending conversion | `Order.conversion_pending` bool + nullable `subtotal`/`total` | infer from NULL total | Legacy Case A orders also have NULL totals (migration adds nullable cols); the flag disambiguates them from pending-conversion orders. |
| Default-margin storage | `app_settings` key/value table, seed `('default_margin_pct','20')` | env var; dedicated single-row table | DB-backed, backoffice-administrable per owner decision; key/value is extensible for future settings. |
| Sheets at finalize | Defer to approval flow (`register_approved_order`) | sync at finalize | `persist_case_a_order` does NOT sync Sheets — Sheets writes at approval. Mirroring it keeps engine/approval untouched (rollback plan). Spec wording "sync on save" is read as "via Case A", i.e. the unchanged approval path. |
| RAG price fallback | New RAG endpoint + client method | direct DB read (rejected by owner) | Owner decision: sibling service owns price lookup. |
| Draft reachability | `route_message` routes draft-carrying state to CUSTOMER; add-intent appends regardless of `order_id` | draft-state flag | Explore's "draft-state flag routes back to draft owner". The `order_id is None` gate (`customer.py:579`) makes the draft unreachable — relax it so the product-query flow can build a draft from a fresh conversation. |

## Data Flow

```
search ──▶ CUSTOMER (product_options set)
"agregalo" ──▶ add-intent ──▶ draft_items += (entry, qty)
"cerrá el pedido para <cliente>" ──▶ parse_finalize ──▶ resolve_customer_name
        ──▶ order_pricing.compute_order(draft, rate_source, margin_resolver)
              LOCAL: costo×margen_aplicado ─┐
              RAG: offer ─▶ ARS ─▶ ×supplier margin ─┘ ─▶ compute_final(list disc)
        ──▶ persist_draft_order ──▶ Order+OrderItems, local→reserve_stock
        ──▶ reply quote ──▶ awaiting_decision ──▶ (unchanged approval/Sheets)
missing rate ──▶ Order(conversion_pending=True); rate load ──▶ recompute totals
```

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `src/pricing/order_pricing.py` | Create | Pure pricing: `compute_order`, per-source base, conversion, margin chain, subtotal/total. |
| `src/db/models.py` | Modify | `Order`: `subtotal`, `total` (nullable), `conversion_pending` (bool). `OrderItem`: `name`, `source`, `supplier`, `moneda`, `precio_original` (all nullable). New `ExchangeRate`, `AppSetting` models. |
| `alembic/versions/xxxx_customer_orders.py` | Create | Additive + reversible: new columns, `exchange_rates`, `app_settings`; seed ARS rate + default margin. |
| `src/agents/customer.py` | Modify | `parse_finalize`; finalize branch: resolve customer, price, persist, clear draft. |
| `src/orchestrator/router.py` | Modify | Route draft-carrying state → CUSTOMER. |
| `src/agents/product_search.py` | Modify | `parse_finalize`; `ProductEntry.codigo_proveedor`; relax add-intent gate. |
| `src/integrations/rag.py` | Modify | Retain `codigo_proveedor` on `RagProduct`; add `RagProductClient.price_lookup`. |
| `src/sourcing/draft_order.py` | Create | `persist_draft_order` (mirror `persist_case_a_order`). |
| `src/backoffice/customer_orders.py` | Create | `list_customer_orders`, `order_detail`, rate/margin maintenance ops, recompute. |
| `src/backoffice/app.py` | Modify | New "Customer Orders" tab (7th) + rate/margin UI. |
| `tests/conftest.py` | Modify | Add `exchange_rates`, `app_settings` to `TRUNCATE_TABLES`. |

## Interfaces / Contracts

**RAG price endpoint** (sibling `fase-0-pdf-parsing`, port 8001):
```
GET /api/v1/products/{codigo}?codigo_proveedor={code}
200: {"codigo": str, "precio": float|null, "moneda": str|null,
      "nombre": str|null, "unidad_venta": str|null,
      "archivo_origen": str|null, "pagina": int|null}
404: {"detail": "not found"}
```
Client mirrors `openai.py`: `RagProductClient.price_lookup(sku, codigo_proveedor=None) -> RagPrice(price, currency) | None`; transport/5xx → `RagProductError`, 404 → `None`.

**RateSource** (injectable): `Callable[[str], Decimal | None]` — currency → ARS rate; DB adapter reads `exchange_rates`.

**Pricing signatures** (pure, no I/O):
```python
@dataclass PricedLine: sku; cantidad; base_ars; final_ars; moneda; source; name; supplier; precio_original
def compute_order(lines, *, rate, supplier_margin, default_margin,
                  list_discount, particular_discount=0) -> PricedOrder  # raises MissingRateError
```

**Schema deltas**: `ExchangeRate(currency PK String(3), rate_to_ars Numeric(14,4), updated_at)`; `AppSetting(key PK String(64), value String(255))`. Migration `down()` drops new columns/tables.

## Testing Strategy

| Layer | What | How |
|-------|------|-----|
| Unit | `order_pricing` per-source base, conversion, missing-rate, margin chain | Parametrized, injectable rate (mirror `test_pricing.py`). |
| Unit | `parse_finalize`, routing decision for draft state | `test_product_search.py`, `test_router`-style. |
| Integration | `persist_draft_order` (local reserve + RAG skip), recompute on rate load | Postgres, mirror `test_case_a.py`; `clean_schema`. |
| Integration | RAG `price_lookup` (200/404/error) | `httpx.MockTransport` (mirror existing RAG tests). |
| Backoffice | New tab label, line detail, rate/margin edit, ARS reject | `test_backoffice.py`. |

`tests/conftest.py:43-47` `TRUNCATE_TABLES` gains `exchange_rates, app_settings`.
`test_backoffice.py:57` six-tab test becomes seven tabs (append "Customer Orders").

## Threat Matrix

N/A — no shell, subprocess, VCS/PR automation, executable-file classification, or process-integration boundary. In-app message routing is covered by ordinary unit tests.

## Migration / Rollout

Single additive migration; `down()` drops new columns/tables and seeds. Reversible; revert routing + tab wiring.

## Open Questions

None blocking.
