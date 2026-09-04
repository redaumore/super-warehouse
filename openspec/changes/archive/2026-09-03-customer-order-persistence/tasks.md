# Tasks: Customer Order Persistence

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~1300–1500 (6 new + 4 modified files; 4 new + 3 modified tests) |
| 2000-line budget risk | Medium — under owner-approved budget |
| Chained PRs recommended | No — single PR fits; WUs become commits inside it |
| Delivery strategy | single-pr |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: pending
2000-line budget risk: Medium

### Work units (commits inside one PR)

| Unit | Test | Rollback |
|------|------|----------|
| WU1 DB+pricing | `pytest tests/test_order_pricing.py tests/test_pricing.py` | Migration `down()` + revert model cols + drop new tables |
| WU2 RAG+persist | `pytest tests/test_rag.py tests/test_draft_order.py` | Revert rag.py + drop draft_order.py; engine/Case A untouched |
| WU3 Finalize+routing | `pytest tests/test_customer.py tests/test_product_search.py tests/test_router_sourcing.py` | Revert customer/product_search/router; restore `order_id is None` gate |
| WU4 Backoffice tab | `pytest tests/test_backoffice.py` | Drop new tab + customer_orders.py; restore six-tab test |

## Pinned decisions (do NOT re-open)

Local base = `costo_proveedor × (1 + margen_aplicado_pct)` via `engine.compute_base`; never `precio_lista_base`. RAG base = `offer_price → ARS → ×(1 + supplier default_margin_pct)`; unmapped → `app_settings.default_margin_pct` (seeded 20). New pure `src/pricing/order_pricing.py` with `RateSource = Callable[[str], Decimal|None]` feeding `compute_base`/`compute_final`.

DB: Order subtotal/total nullable ARS + conversion_pending bool; OrderItem name/source/supplier/moneda/precio_original snapshot; exchange_rates seeded ARS=1.0000; app_settings seeded `default_margin_pct=20`. Single additive reversible migration.

RAG client: `GET /api/v1/products/{codigo}?codigo_proveedor=…` (200/404/error), mirrors `openai.py`; retain `codigo_proveedor` on `RagProduct`. Save side effects (UPDATED): LOCAL reserves stock at save; Sheets sync stays on `register_approved_order` approval — NOT at save.

Finalize: `parse_finalize` in `src/agents/product_search.py`; customer resolved by name (reuse `customers.py`); `src/sourcing/draft_order.py:persist_draft_order` mirrors `persist_case_a_order` (LOCAL→reserve_stock; RAG skip). `route_message` routes draft-carrying state → CUSTOMER; drop `order_id is None` gate (`customer.py:579`); retire `OFFER_TO_CREATE_REPLY` for finalize.

Backoffice: 7th tab "Customer Orders" + rate/margin UI; six-tab test → seven. Pending-conversion orders blocked until rate load recomputes totals. `descuento_particular_pct` OUT of scope.

## Phase 1: DB + pricing foundation

- [x] 1.1 Migration `alembic/versions/xxxx_customer_orders.py`: Order subtotal/total/conversion_pending; OrderItem name/source/supplier/moneda/precio_original; exchange_rates + app_settings tables; seed ARS=1.0000 + default_margin_pct=20; reversible `down()`.
- [x] 1.2 `src/db/models.py`: new Order/OrderItem columns; add ExchangeRate + AppSetting ORM.
- [x] 1.3 `src/pricing/order_pricing.py`: PricedLine/PricedOrder, MissingRateError, `compute_order` (LOCAL→compute_base; RAG→convert→compute_base; final via compute_final).
- [x] 1.4 RED `tests/test_order_pricing.py`: LOCAL base; RAG mapped/unmapped; missing-rate raises; non-ARS conversion + subtotal; final totals.
- [x] 1.5 Append `exchange_rates, app_settings` to `TRUNCATE_TABLES` in `tests/conftest.py`.

## Phase 2: RAG price lookup + persist_draft_order

- [x] 2.1 `src/integrations/rag.py`: `RagProductClient.price_lookup(sku, codigo_proveedor=None)` + `RagPrice(price, currency)`; retain `RagProduct.codigo_proveedor`; 404→None; transport/5xx→RagProductError.
- [x] 2.2 `src/sourcing/draft_order.py`: `persist_draft_order(session, customer, priced, delivery_date=None)` — Order (PENDING_APPROVAL, conversion_pending), OrderItem snapshots, reserve LOCAL stock only, fill subtotal/total when priced.
- [x] 2.3 RED `tests/test_rag.py::test_price_lookup_*` (200/404/transport via httpx.MockTransport).
- [x] 2.4 RED `tests/test_draft_order.py`: LOCAL reserves + Sheets NOT called; RAG skip stock + no catalogo FK; subtotal/total persisted; pending-conversion leaves NULL totals + flag True.

## Phase 3: Customer finalize intent + routing

- [x] 3.1 `src/agents/product_search.py`: add `parse_finalize(text, draft_items)` + `ProductEntry.codigo_proveedor`.
- [x] 3.2 `src/agents/customer.py`: finalize branch — `resolve_customer_name`; AMBIGUOUS keeps menu+parsed; NOT_FOUND offers `parse_create_client_command` → Base; match → `compute_order` → `persist_draft_order` → clear draft_items → awaiting_decision. Drop `base.order_id is None` gate (~line 579). Retire `OFFER_TO_CREATE_REPLY` for finalize.
- [x] 3.3 `src/orchestrator/router.py`: route draft-carrying state → CUSTOMER after existing branches.
- [x] 3.4 RED: finalize happy path; ambiguous keeps state; unknown offers creation; router routes draft state; add-intent works without order_id.

## Phase 4: Backoffice Customer Orders tab

- [x] 4.1 `src/backoffice/customer_orders.py`: `list_customer_orders`, `order_detail`, `list_exchange_rates`, `set_exchange_rate` (reject ARS), `get_default_margin`/`set_default_margin`, `recompute_pending_conversion`.
- [x] 4.2 `src/backoffice/app.py`: append 7th "Customer Orders" tab — orders grid + line detail + rate table (ARS read-only) + default-margin numeric; recompute on rate save.
- [x] 4.3 `tests/test_backoffice.py`: 7-tab labels; set_exchange_rate rejects ARS + persists USD; recompute clears conversion_pending + fills totals; set_default_margin round-trips.
- [x] 4.4 Block pending-conversion at `register_approved_order` (PendingConversionError).

## Phase 5: Verification

- [x] 5.1 `pytest tests/` green (focused per WU first).
- [x] 5.2 E2E chat: search → add RAG line → finalize new customer → backoffice snapshot; missing rate → set USD rate → recompute clears flag.
- [x] 5.3 Sheet sync fires only at approval (log + unit).
- [x] 5.4 Migration `down()` round-trip; legacy Case A orders still persistable.
