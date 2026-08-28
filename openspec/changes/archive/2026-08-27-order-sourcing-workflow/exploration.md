# Exploration: order-sourcing-workflow

## Current State

The MVP implements a conversational intake pipeline for a ferretería (hardware store) on
Python 3.12/3.14 + SQLAlchemy 2.0 + Alembic + Postgres/pgvector, tested with pytest
(229 tests), linted with ruff, type-checked with mypy strict.

**Intake & routing.** Inbound messages arrive via FastAPI webhook (`src/api/webhook.py`,
HMAC/secret-token verified, <5s ACK) from Telegram/WhatsApp adapters
(`src/channels/`), and are dispatched in the background to `Orchestrator.handle_inbound`
(`src/orchestrator/router.py`). Routing rules: voice/image → Perception; a reply while
`awaiting_decision` → Dispatch (owner decision); an in-progress order with resolved
items → Sales, without → Disambiguation; fresh text → Customer. Conversation context
(`ConversationState`: sender, customer_id, order_id, resolved items, history) lives in an
in-memory TTL store (`src/orchestrator/session.py`, 30-min TTL). Only the Customer agent
is wired for real (LLM responder + transient catalog context note via `DbCatalogSearcher`);
the other five agents remain walking-skeleton stubs in `src/pipeline.py`, though
disambiguation (`search_catalog`/`resolve_item`), inventory (`available_stock`/
`reserve_stock`), sales (pure quote functions) and dispatch (decision parsing) modules
carry real logic.

**Order model.** `Order` (`src/db/models.py`) has exactly four states
(`OrderEstado`: PENDING_APPROVAL, APPROVED, IN_DISPATCH, REJECTED) plus a
`needs_requote` flag. `OrderItem` holds priced lines. `StockReservation` soft-locks stock
with a 30-min TTL (`ReservationEstado`: ACTIVE/CONVERTED/RELEASED/EXPIRED). There is NO
delivery-date field anywhere. Transitions live in `src/order_lifecycle/state.py`
(approve refuses stale reservations via `RequiresRequoteError`, reject releases
reservations, dispatch, TTL expiry; sweeper in `src/scheduler/sweeper.py`).
`src/orchestrator/approval.py` composes approve → convert reservations → Sheets row →
stock deduct → confirm. The order-lifecycle spec explicitly fixes the four-state machine
("fixed by spec — four states" comment in models.py). Production code never creates
`Order` rows from a conversation — only tests instantiate `Order(...)` directly; the
conversation→persisted-order path is not yet built.

**Supplier side.** There is NO purchase-order/supplier-order entity or scaffolding
anywhere in `src/` or the specs. The only supplier-facing capability is document
ingestion: OCR/Vision extraction of remitos/invoices/price lists
(`src/supplier/ocr.py`, supplier-document-ingestion spec) with owner confirmation in the
backoffice before inventory is updated. Supplier-catalog data today consists of:
`Catalogo.proveedor_id` (which supplier each stocked product comes from) and
`ProveedorSkuMapping` (supplier code/description → internal SKU with confidence). There
is no table of what suppliers can OFFER us (a supplier-offered catalog) — a gap for
Case B ("consult Supplier Catalog for missing items").

**Backoffice.** Gradio app (`src/backoffice/app.py`) with four tabs: Catalog, Clients,
Orders/Monitor (`src/backoffice/monitor.py`), and Ingestion. No purchase-order view.

**Tests & gates.** `tests/` mirrors modules (test_*.py per source file); pytest with
`asyncio_mode=auto`; CI (`.github/workflows/ci.yml`) runs ruff + ruff format --check +
pytest against a pgvector Postgres service. The documented coverage gate is
`pytest --cov=src --cov-fail-under=85` (docs/runbook.md) but is NOT wired into the CI
workflow. Alembic has two migrations (initial schema + stock adjustments).

**Spec landscape.** `openspec/specs/` contains order-lifecycle, catalog-search,
supplier-document-ingestion, whatsapp-order-intake, clients-and-price-lists,
pricing-engine, barcode-stock-ops, backoffice, agent-orchestration. The requested states
(Pendiente de Armado / En Preparación / Cancelado) appear nowhere in specs or code; they
conflict textually with the order-lifecycle requirement "exactly one of the states:
Pending Approval, Approved, In Dispatch, or Rejected".

## Affected Areas

- `src/db/models.py` — new SupplierPurchaseOrder (+items) entity, likely new sourcing
  state enum and delivery-date column on Order; Alembic migration(s).
- `src/order_lifecycle/state.py` — pattern template for the purchase-order state machine
  (own transitions module recommended, e.g. `src/purchasing/`).
- `src/agents/customer.py` + router — NL order parsing (structured extraction of
  customer, items, quantities, delivery date) has no home today; hooks into the Customer
  agent turn or a new intent step before routing.
- `src/orchestrator/session.py` — ConversationState must carry parsed items, delivery
  date, customer identity, and (Case B) missing items + supplier options across turns.
- `src/orchestrator/router.py` — routing for supplier-selection replies (owner vs
  customer conversation) and for the new workflow's turns.
- `src/agents/disambiguation.py` (`search_catalog`) — product mapping over inventory for
  the parsed items; supplier-side search needs a data source (ProveedorSkuMapping /
  Catalogo.proveedor_id, or a new supplier-offerings table).
- `src/backoffice/app.py` / `monitor.py` — owner executes the accumulated purchase order
  (new tab or extended Orders/Monitor).
- `src/agents/dispatch.py` / `src/orchestrator/approval.py` — owner-facing notifications
  and decisions extend to sourcing outcomes (Case B selection, execution).
- `openspec/specs/order-lifecycle/spec.md` — MODIFIED requirements needed if states
  change; new spec domain (e.g. purchase-order-lifecycle or order-sourcing) for the
  outgoing order entity.
- `openspec/config.yaml` — stale ("greenfield / undecided"); should be refreshed with
  the real stack in a later phase (not blocking).

## Approaches

1. **Additive: new sourcing dimension + new SupplierPurchaseOrder entity**
   Add a separate sourcing/fulfillment enum column on Order (PENDING_ASSEMBLY /
   IN_PREPARATION / CANCELLED) plus `delivery_date`, leaving the four-state
   `OrderEstado` intact; introduce `SupplierPurchaseOrder` + items with its OWN state
   enum (accumulating/OPEN → EXECUTED → RECEIVED or CANCELLED), FK to `Proveedor`,
   accumulating items from multiple customer orders; new structured NL-parsing step in
   the intake pipeline; a supplier-catalog data source for missing-item lookup.
   - Pros: respects the existing fixed four-state machine and its consumers (monitor,
     Sheets, approval); matches the requirement that the purchase order has its own
     state flow; additive Alembic migrations; clear separation of concerns (approval axis
     vs sourcing axis).
   - Cons: two state fields on Order require clear semantics (when each applies); more
     tables; existing order-lifecycle spec text ("exactly one of the states") still needs
     a MODIFIED delta or an explicit carve-out; delivery-date and supplier-offerings data
     modeling must be decided.
   - Effort: High

2. **Extend OrderEstado with the three requested values**
   Replace/extend the four-state enum with PENDING_ASSEMBLY / IN_PREPARATION / CANCELLED
   and rework the transition module.
   - Pros: single state field; reads literally like the feature request ("main order
     state").
   - Cons: conflicts head-on with the existing spec and code (approve/reject/dispatch
     guards assume four states; backoffice monitor and Sheets integration key on
     OrderEstado values); mixes two different axes (owner approval vs stock availability)
     in one machine; Postgres `ALTER TYPE` migration with data mapping risk; high
     regression surface in tests.
   - Effort: Medium-High (regression-heavy)

3. **No new ORM entity; accumulate purchase orders in backoffice/Sheets only**
   Keep a JSON/Sheets-based accumulator instead of a persisted entity.
   - Pros: fast to prototype.
   - Cons: violates "accumulates items from multiple orders, open to modification, own
     state flow"; no audit trail; untestable against the DB layer; dead-end for future
     supplier workflows.
   - Effort: Low (rejected on requirements)

## Recommendation

Approach 1 (additive). The existing order-lifecycle spec is explicit that the
customer-order machine is fixed at four states, and its consumers (approval orchestration,
backoffice monitor, Sheets sync) depend on those values; the requested states are a
different axis (fulfillment/sourcing) driven by stock availability, not owner approval.
A separate sourcing enum on Order plus a first-class `SupplierPurchaseOrder` entity with
its own state machine mirrors the codebase's existing patterns (enum in models +
transition-owning module) and satisfies the accumulation requirement naturally. The
purchase-order state flow (open-accumulating → executed by owner → received/cancelled)
becomes its own spec domain alongside order-lifecycle.

Open questions the proposal must resolve: (a) who picks the supplier in Case B — the
owner or the customer (routing differs); (b) supplier-catalog data source for missing
items — reuse ProveedorSkuMapping/Catalogo.proveedor_id or add a supplier-offerings
table fed by price-list ingestion; (c) where structured NL parsing hooks (Customer agent
turn vs a dedicated intent step in the router); (d) whether a Case A order still goes
through quotation/owner approval or jumps straight to Pending Assembly; (e) delivery-date
semantics (agreed date vs promised date) and storage.

## Risks

- **No supplier-offerings catalog exists.** "Consult Supplier Catalog" for missing items
  has no backing table today; only ProveedorSkuMapping + Catalogo.proveedor_id exist.
  The proposal must define this data source or add one.
- **Order creation path from conversation does not exist yet** (walking skeleton; Order
  rows only appear in tests). This feature implicitly includes building the first real
  conversation→Order persistence path.
- **Spec conflict**: order-lifecycle requires exactly four states; adding sourcing states
  needs MODIFIED deltas or a new spec domain, or the main spec becomes contradictory.
- **Postgres enum changes** (new enum column on orders, purchase-order enum) need careful
  Alembic migrations; existing enum types are PostgreSQL-native via SQLAlchemy `Enum`.
- **NL parsing of dates** ("para el viernes a la tarde") — fuzzy date resolution is a new
  capability with ambiguity risk; needs explicit requirements and tests.
- **In-memory conversation store with 30-min TTL** may not survive the multi-turn Case B
  flow (missing-items list → supplier selection → purchase order creation).
- **Coverage gate (85%)** is documented in runbook but not enforced in CI yml; verify
  where the gate is applied before implementation.
- **Stale openspec/config.yaml** ("greenfield/undecided") vs the real Python stack — a
  later phase should refresh it so phase rules match reality.

## Ready for Proposal

Yes. The orchestrator should tell the user: exploration confirms no existing
purchase-order entity, no supplier-offerings catalog, and no conversation→Order
persistence path — the feature is greenfield on those three points and additive elsewhere;
proposal should lock the four open questions above (supplier picker actor, supplier
catalog data source, NL-parsing hook location, Case A approval integration) before spec
writing.
