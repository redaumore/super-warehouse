# Exploration: customer-order-persistence

> Phase: sdd-explore — read-only investigation. 2026-09-02.
> Base branch: `feat/rag-product-query` (open PR #10, merged chain commits 47c8470..4834454).
> Working tree note: 3 unrelated uncommitted changes exist (`M src/backoffice/app.py` supplier-form echo/clear rework, `M tests/test_suppliers_backoffice.py`, `?? docs/especificacion-catalogo-e-inventario.md`). Read but not touched; this change must not depend on or modify them.

## Exploration: Persisting a customer order from the chat product-selection draft

### Current State

**The interactive draft (rag-product-query change) never reaches the DB.**

- `ConversationState.draft_items: tuple[tuple[ProductEntry, int], ...]` (`src/orchestrator/session.py:97`) accumulates `(entry, qty)` pairs from the add-intent short-circuit in the CUSTOMER handler (`src/agents/customer.py:574-591`). Each entry is a `ProductEntry` (`src/agents/product_search.py:45-59`): `sku`, `name`, `source` (LOCAL|RAG), `provider`, `brand`, `price` (float|None), `currency` (str|None), `unit`, `specs`, `source_file`, `page`.
- Add intents (`parse_product_add`, `product_search.py:160-182`: "agregalo", "sumá 5 de eso", "el 2") only append to `draft_items` when `base.order_id is not None` (`customer.py:585-591`). Otherwise the handler replies `OFFER_TO_CREATE_REPLY` ("mandá el pedido completo…") and the draft stays empty (`customer.py:579-584`).
- **Routing gap (dead path in production)**: `route_message` (`src/orchestrator/router.py:99-111`) sends a turn to CUSTOMER only when there is no pending decision, no pending sourcing selection, and `state.order_id is None`. Once an order exists, turns go to DISPATCH (`awaiting_decision`), SOURCING, or SALES/DISAMBIGUATION — and SALES/DISAMBIGUATION are registered as `_stub_agent` (`src/pipeline.py:127-128`). The ADDED_TO_ORDER branch is unit-tested (`tests/test_customer.py:371-388` seeds `ConversationState(order_id=5)` directly) but unreachable through real routing today. Net effect: **no flow today can convert `draft_items` into an Order** — the only persistence path is the free-text sourcing turn.
- Customer identity in chat: resolved by NAME only in the sourcing turn — `resolve_customer_name` (`src/agents/customers.py:91-110`, EXACT→FOLDED→AMBIGUOUS menu→NOT_FOUND), with in-chat creation `nuevo cliente <nombre> <teléfono>` assigning the Base list (`src/agents/customer.py:348-397`, `default_price_list_id` `src/backoffice/clients.py:38-52`). `ConversationState.customer_id` is set only after Case A/B/C persist (`customer.py:482-521`). The draft flow (fresh conversation) has no `customer_id` — a "finalize order" step must resolve/attach the customer itself.
- The sourcing turn persists: `_run_sourcing_turn` (`customer.py:400-525`) → `classify_case` → `persist_case_a_order` (Case A: Order + OrderItems + reservations + quote) / `persist_case_b_order` (Order IN_PREPARATION + SourcingNeed rows, **no OrderItems**) / `persist_case_c_order` (cancelled). Everything commits in that turn (`customer.py:524`).

**Order/OrderItem schema (what exists to reuse).**

- `Order` (`src/db/models.py:267-303`): `order_id`, `customer_id` (FK, NOT NULL), `estado` (4-state enum, default PENDING_APPROVAL), `sourcing_state` (3-state enum, default PENDING_ASSEMBLY), `delivery_date` (nullable), `needs_requote`, `created_at`, `approved_at`, `rejected_at`. **No subtotal/total/currency columns.**
- `OrderItem` (`models.py:306-319`): `id`, `order_id` (FK), `sku` (String(64), **no FK to catalogo**), `cantidad`, `base_price`, `final_price`, `adjustment` (absolute discount). **No currency, no product name/description, no unit, no supplier reference.**
- `persist_case_a_order` (`src/sourcing/case_a.py:29-89`) fills lines from `Catalogo.precio_lista_base` (raises `UnknownSkuError` for SKUs without a catalog row), quotes via `quote_order` with the customer's list + particular discounts, reserves stock per line, then writes OrderItems. Totals are never stored: `order_total()` (`src/orchestrator/approval.py:63-68`) sums `final_price × cantidad` on read, and `Quote.total` (`src/agents/sales.py:62-66`) likewise. Both render `"… ARS"` hardcoded in replies (`approval.py:117`, `customer.py:283`, `sales.py:60` `currency: str = "ARS"`).

**Pricing provenance per source (the double-margin hazard).**

- LOCAL: `Catalogo.costo_proveedor` → `margen_aplicado_pct` → `precio_lista_base` — **the base price ALREADY includes the business margin** (`compute_base`, `src/pricing/engine.py:37-44`). `Supplier.default_margin_pct` is consumed ONLY at ingestion for NEW products (`src/backoffice/ingestion.py:111,119-120`; locked by the supplier-management spec: "consumed only for future catalog products at ingestion"). `update_margin`/`update_price` in `src/backoffice/catalog.py:68-85` recompute or overwrite the base.
- RAG: `RagProduct.price`/`currency` (`src/integrations/rag.py:54-67`) is the **supplier's offer price** (what the business would pay) — no business margin applied. Display-only today (`customer.py:169-192`).
- Owner's formula ("base price in denomination currency → apply supplier margin → subtotal → list discount → total") is only consistent for RAG items. For local items, applying `Supplier.default_margin_pct` again would double-count (base already margin-marked), and there is no currency column anywhere in `catalogo` (implicit ARS). **"What is the base price per source?" is the #1 product decision this change must resolve.**

**Currency/exchange rate: nothing exists.**

- Grep across the repo confirms: no currency table, no exchange-rate column, no rate source, no conversion logic. `ProductEntry.currency`/`RagProduct.currency` are strings from RAG `moneda` (e.g. "USD") used only in note rendering. Options (proposal-phase decision):
  1. **Manual rate table in DB** (e.g. `exchange_rates(currency, rate_to_ars, updated_at)`) + backoffice maintenance: deterministic, offline, auditable; owner burden, stale-rate risk.
  2. **External API** (e.g. dolarapi — free, no key): always fresh; new integration, a failure mode (rate unavailable → block quote or fall back to last-known), latency in a sync handler.
  3. **ARS-only for now**: zero scope; silently misprices USD-denominated RAG items (observed in the RAG corpus) — quote trust risk for the owner.
  4. Hybrid (table + optional API refresh) — most scope. Settings pattern exists for an API section (`src/config.py:56-64`, `.env.example`).

**Customer resolution + discount lists.**

- `Cliente.lista_precios_id` NOT NULL FK (`models.py:125-127`); `ListaPrecios.descuento_lista_pct` (Base=0 / Gremio A=10 / Gremio B=20 per docstring). Populated via backoffice `create_client` (`clients.py:71-96`) and in-chat creation (Base default). **There is no ListaPrecios CRUD UI** — the Clients tab only offers the existing-list dropdown (`app.py:404-410`), and no migration/seed creates list rows (tests seed them per-fixture; production depends on manually inserted rows). `compute_final` consumes the list discount multiplicatively through `quote_order` (`case_a.py:55-59`).
- Spec drift note: `openspec/specs/clients-and-price-lists` still describes phone-based customer identification; the implemented owner pivot resolves by NAME (`src/agents/customers.py`). A delta for this change should not blindly extend the stale phone-identification requirement.

**Backoffice extension pattern.**

- Modules = pure DB ops returning dict rows (`src/backoffice/monitor.py:18-47`, `clients.py`, `catalog.py`, `po.py`, `suppliers.py`). `build_app` (`src/backoffice/app.py:346-598`) wires one `gr.Tab` per module: `gr.Dataframe(headers=…, datatype=…, value=_grid_fn)` + "Refrescar" button (`refresh.click(_grid_fn, outputs=grid)`). A "Customer Orders" tab would mirror Orders/Monitor plus a detail view; reusable today: `list_orders` (monitor) and `order_total` (approval). **No line-item view exists**, and `OrderItem` lacks a name/description — a detail view needs either a join to `catalogo.nombre_oficial` (local) or a stored name for RAG lines. Test `tests/test_backoffice.py:57` (`test_build_app_creates_six_tabs_with_expected_labels`) counts tabs.

**Migrations & tests.**

- Handwritten Alembic migrations (curated, autogenerate-capable env): `alembic/versions/26a4a1b103fe` (initial), `b2f353dfc3d2` (stock adjustments), `a0bf3bd210f8` (sourcing axis — enum via `sa.Enum(…).create(bind, checkfirst=True)`, server_default backfill), `46bdbdc4a575` (renames), `5f304e18a765` (POs). Conftest builds the test schema from migrations on a disposable `ferreteria_test` DB (`tests/conftest.py:67-79`); `TRUNCATE_TABLES` (conftest.py:43-47) must include new tables.
- Test surface for estimation: `test_pricing.py`/`test_sales.py` (pure pricing math, parametrized), `test_case_a/b/c.py`, `test_sourcing_persistence.py`, `test_customer.py` (FakeSearcher/FakeResponder, no DB), `test_product_search.py`, `test_pipeline.py` (injectable `build_orchestrator(searcher=…)`), `test_backoffice.py` (shop_ctx/client_ctx fixtures), `test_e2e_order.py` (Postgres-gated E2E), `test_db_models.py` (column presence). `FakeSupplierCatalogSearcher` is used in 9 files (supplier seam — untouched by this change).

### Affected Areas

- `src/agents/customer.py` — draft flow / add-intent / new "finalize order" step; customer attachment for the draft path; quote reply rendering.
- `src/orchestrator/session.py` — `draft_items`/`product_options` lifecycle (clear on finalize), possibly per-item currency/pricing metadata.
- `src/agents/product_search.py` — `ProductEntry` may need supplier-id resolution (RAG entries carry `provider` name, not `Supplier.id`).
- `src/sourcing/case_a.py` / `src/agents/sales.py` / `src/pricing/engine.py` — reuse or extension for currency conversion + subtotal/total computation (engine is spec-locked pure pricing; a conversion step likely sits before it).
- `src/db/models.py` + `alembic/versions/` — new columns (currency per line? subtotal/total on Order? name/description on OrderItem?) and possibly a new `exchange_rates` table.
- `src/backoffice/monitor.py` + `src/backoffice/app.py` (+ new module) — "Customer Orders" tab with orders, line items, totals.
- `src/orchestrator/router.py` / `src/pipeline.py` — routing/wiring so the finalize intent reaches a handler (the current routing never sends an order-carrying state back to CUSTOMER).
- OpenSpec: `pricing-engine` (MODIFIED — currency/subtotal/total semantics), `order-sourcing` or a new `customer-order-persistence` domain (ADDED), `backoffice` (MODIFIED — new tab). `rag-product-query`/`catalog-search`/`supplier-catalog-search` untouched.
- Tests: `test_customer.py`, `test_pricing.py`, `test_sales.py`, `test_case_a.py`, `test_backoffice.py`, `test_db_models.py`, `test_e2e_order.py`, `conftest.py` (TRUNCATE_TABLES).

### Approaches

1. **Finalize intent inside the CUSTOMER handler (extend the existing add-intent short-circuit)**
   - A new phrase (e.g. "cerrá el pedido para <cliente>") parses to a finalize intent; the handler resolves the customer by name (reusing `customers.py`), prices `draft_items` (source-aware), persists Order + OrderItems, and replies with the quote. Requires loosening `route_message` so a draft-carrying conversation still reaches CUSTOMER (today it would never).
   - Pros: smallest conceptual jump; reuses name resolution and the draft UI the owner already knows; no new agent.
   - Cons: mixes order persistence into an already-long handler; draft state has no customer until the final phrase (or needs an earlier "cliente: X" attachment step); RAG items have no DB row (pricing must be self-contained from `ProductEntry`).
   - Effort: Medium.

2. **Bridge the draft into the existing sourcing turn (`_run_sourcing_turn`)**
   - Treat the finalize phrase as a parsed-order-like turn: convert `draft_items` into `ParsedItem`s and reuse the Case A/B/C machinery where possible (Case A at least). But local resolution would re-lookup by description/SKU and lose RAG price/currency — RAG lines have no `catalogo` row, so `persist_case_a_order`'s `UnknownSkuError` fires for them.
   - Pros: reuses reservation/quote/approval machinery (stock, approvals, Sheets).
   - Cons: sourcing classification assumes catalog-backed availability (RAG items have no stock concept); forces RAG lines into a local-SKU shape they don't have; would still need a parallel persistence path — little reuse in practice.
   - Effort: Medium-High.

3. **Dedicated finalize/pricing module + a light routing change (likely the cleanest)**
   - A new pure pricing step (e.g. `src/pricing/order_pricing.py`) computes per-line: base in denomination currency → ARS conversion (injectable rate source) → supplier margin (RAG items only) → subtotal → list/particular discounts (existing `compute_final`) → total; a new persist function writes Order + OrderItems with the extra columns; the CUSTOMER handler (or a new agent) orchestrates. Routing: allow a draft-carrying state to keep reaching the flow that owns the draft.
   - Pros: keeps the pricing-engine spec intact (conversion/margin feed INTO `compute_base`/`compute_final`, not into them); source-aware pricing is unit-testable in isolation; persistence mirrors `persist_case_a_order` patterns; room for the supplier-id mapping (RAG `provider` → `Supplier.code`?) as its own seam.
   - Cons: biggest new surface (new module, routing decision, handler step); the supplier-margin application rule must be pinned down first (double-margin hazard above).
   - Effort: Medium-High.

### Recommendation

**Approach 3**, with the exchange-rate decision explicitly deferred to the proposal phase: a source-aware pricing/persistence step that (a) converts RAG prices to ARS via an injectable rate source, (b) applies the supplier margin only where the source price does not already include it, (c) reuses `compute_final` for discounts, (d) persists subtotal/total and per-line currency on Order/OrderItem, and (e) renders the quote in chat. Before spec work, the orchestrator should get the owner's answers on: the per-source base-price definition (the double-margin question), the exchange-rate source (manual table vs API vs ARS-only), how the customer is attached to a draft (final phrase vs earlier step), and whether RAG-sourced items must survive as lines without a `catalogo` row (they must — they are the primary path today).

### Risks

- **Double margin**: local `precio_lista_base` already includes `margen_aplicado_pct`; naively applying `Supplier.default_margin_pct` on top inflates prices. The proposal must pin one rule per source.
- **RAG items are DB orphans**: `OrderItem.sku` has no FK and RAG SKUs have no `catalogo` row; `persist_case_a_order` raises `UnknownSkuError` for them. A parallel persistence path (or a relaxed one) is unavoidable.
- **Routing dead-end**: the add-intent "open order" branch is unreachable through production routing today; any finalize flow must fix routing or build its own conversation state (e.g. a draft-state flag that routes back to the owner of the draft).
- **No ListaPrecios seed in production**: list rows (Base/Gremio A/B) exist only in test fixtures; `default_price_list_id` raises when none exists. The change should not assume lists are present in the owner's dev DB.
- **Exchange-rate failure mode**: if an API/table rate is missing or stale, quoting either blocks or silently misprices — needs an explicit, owner-visible behavior.
- **Spec drift**: `clients-and-price-lists` (phone identification) does not match the implemented name-based owner pivot; deltas must not enshrine the stale requirement.
- **Working-tree hygiene**: the change must branch off `feat/rag-product-query` without entangling the 3 uncommitted files (especially the `app.py` supplier-form rework — a future "Customer Orders" tab edits the same file).
- **RAG SKU/price hygiene**: observed double-prefix SKUs (`AMX-AMX-AT-5044`) and free-form currency strings — persistence should reuse `normalize_rag_sku` and validate/whitelist currency codes rather than storing raw strings blindly.

### Ready for Proposal

**Yes**, with four owner decisions to collect first (base-price semantics per source; exchange-rate source; customer attachment to the draft; RAG lines without catalog rows). The exploration identified the exact seams (draft state, pricing engine, case_a persistence, backoffice tab pattern) and the blocking product ambiguities. The orchestrator should proceed to `sdd-propose` after surfacing those decisions.
