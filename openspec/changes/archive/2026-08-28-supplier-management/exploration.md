# Exploration: supplier-management

## Current State

Ferretería MVP on Python 3.12+ (running 3.14 locally), SQLAlchemy 2.0 + Alembic +
Postgres/pgvector, pytest (355 test functions across 46 files), ruff + mypy strict, CI on
GitHub Actions with a `pgvector/pgvector:pg16` service. The backoffice is a Gradio app
(`src/backoffice/app.py`, port 7860) with five tabs: Catalog, Clients, Orders/Monitor,
Purchase Orders, Ingestion.

Suppliers exist today only as a minimal `Proveedor` table used by catalog, ingestion, and
purchase orders. There is NO supplier management module: no CRUD UI, no code field, no
state, no CUIT/email/address fields, no validation beyond nothing. The supplier catalog
searcher used by the sourcing flow is a Protocol with only an in-memory Fake
implementation (`src/supplier/searcher.py`); the production pipeline wires
`FakeSupplierCatalogSearcher()` with an empty candidate list (`src/pipeline.py`).

## Data Layer (objective 1)

**`Proveedor` model** (`src/db/models.py`, lines 112-124, table `proveedores`):

| Column | Type | Notes |
|---|---|---|
| `proveedor_id` | Integer PK | |
| `razon_social` | String(200) NOT NULL | the only required field |
| `contacto` | String(200) NULL | commercial agent name |
| `telefono` | String(32) NULL | RAW — not normalized |
| `margen_predeterminado` | Numeric(5,2) NOT NULL default 0 | percent, stored as Decimal |
| `condiciones` | Text NULL | free-text purchase/payment conditions |

- **No unique constraints, no indexes on `proveedores` at all.**
- Missing vs the brief: CUIT, dirección, email, whatsapp, código (3-char), estado,
  IVA/fiscal condition enum. `condiciones` (Text) is NOT the "condición de IVA" — it is
  free-text commercial conditions; keep them separate.
- **Naming collision risk**: `proveedor_sku_mapping.codigo_proveedor` (String 64) already
  means "the supplier's code for a product". The new 3-char supplier identifier needs a
  different column name (e.g. `codigo`) to avoid semantic confusion.
- State/soft-delete precedent: enums as `class XEstado(str, enum.Enum)` + `mapped_column(
  Enum(XEstado, name=..., values_callable=lambda e: [m.value for m in e]), default=...)`
  (see `OrderEstado`, `ReservationEstado`, `SourcingState`, `SupplierPurchaseOrderState`).
  Follow the same pattern for a `SupplierEstado` enum (ACTIVO/INACTIVO) and the IVA
  condition enum.
- **Alembic**: head is `5f304e18a765` (supplier_purchase_orders); chain
  `26a4a1b103fe → b2f353dfc3d2 → a0bf3bd210f8 → 5f304e18a765`. Conventions: autogenerate
  with `make migrate-new`, hand-adjust; re-reference existing PG enums with
  `create_type=False`; explicit `op.create_index` calls; `server_default` for enum columns.
- **Seed**: the live dev DB has `proveedor_id=1`, razon_social "Distribuidora Ferretería
  Centro S.A.", contacto "Juan Pérez", telefono "11 5555 0101" (raw), margen 0.00,
  condiciones NULL. It was inserted manually — there is NO supplier seed script in the
  repo (scripts/seed_inventory.py only backfills Inventory). A migration adding
  NOT NULL columns must backfill existing rows (estado=ACTIVO, generated code) or the
  upgrade fails on the live DB. CUIT can be NULLable for legacy rows (Postgres unique
  index allows multiple NULLs) while new records validate it.

## Backoffice (objective 2)

- Gradio Blocks in `src/backoffice/app.py`; `build_app()` builds the tree (never starts a
  server), `launch()` on 127.0.0.1:7860; gated by `require_fase(4, cfg)`.
- Tabs are `with gr.Tab("<label>")` blocks; a new Suppliers tab slots in as a sixth tab.
  **`tests/test_backoffice.py::test_build_app_creates_five_tabs_with_expected_labels`
  asserts the exact five-tab list and must be updated.**
- Established CRUD pattern (clients.py is the model to copy): pure DB functions taking a
  `session` in a module file (`list_*`, `create_*`, `update_*`), a module-level
  `InvalidClientDataError` domain exception, `session.flush()` inside the function and the
  caller commits; `app.py` adds thin `_suppliers_grid` / action wrappers that open
  `SessionLocal()` and return `f"Error: {exc}"` strings on failure.
- Existing UI patterns: `gr.Dataframe` for grids with a "Refrescar" button re-invoking the
  grid function; `gr.Dropdown` for price lists; `gr.Number`/`gr.Textbox` for inputs;
  status `gr.Textbox(interactive=False)` for action results.
- **No quick search and no filter controls exist anywhere today** — every grid lists all
  rows. Quick search (CUIT/name/code) and estado filter are new UI patterns (e.g.
  `gr.Textbox` + `gr.Radio`/`gr.Dropdown` driving a filtered grid call).
- No reactive per-field validation exists; validation is backend-only, surfaced as error
  strings. "Reactive front validation" in the brief implies new Gradio event wiring
  (`.change`/`.input` handlers) or at minimum backend errors per field.
- Purchase Orders tab uses explicit COMMIT inside actions (po.py) because Gradio's
  short-lived session would roll back otherwise — follow that for supplier state toggles.

## Catalog & Pricing (objective 3)

- Pricing is pure: `src/pricing/engine.py` — `compute_base(cost, margin)` = cost × (1 +
  margin) and `compute_final(base, list_discount, particular_discount)` (multiplicative).
  Decimals quantized with ROUND_HALF_UP to 2dp.
- **The supplier default margin already plugs into pricing** at
  `src/backoffice/ingestion.py::confirm_items` (line ~108): new catalog products created
  from document ingestion use `proveedor.margen_predeterminado` +
  `compute_base(costo_final, margen)`. Editing the supplier's margin in the new module
  changes future ingestions only (existing products keep `margen_aplicado_pct`) — decide
  and document whether editing a supplier's margin also re-prices its existing catalog
  rows (probably NOT, matching catalog.py semantics).
- `src/backoffice/catalog.py::update_margin` recomputes base price from cost + margin via
  the same engine.

## Order Sourcing / Purchases (objective 4)

Supplier touchpoints and where an ACTIVO-only filter is needed:

1. **Sourcing quotes / missing-item quotes**: `src/sourcing/classify.py::classify_case`
   calls `searcher.search(sku, description)` (the `SupplierCatalogSearcher` Protocol).
   Only `FakeSupplierCatalogSearcher` exists — no DB-backed implementation to filter yet.
   The change should define the ACTIVE-only contract (and implement a DB-backed searcher
   if in scope, filtering by estado=ACTIVO; the Fake must also filter if seeded with
   suppliers).
2. **New purchases / PO accumulation**: `src/purchasing/accumulate.py::open_or_create_po`
   and `accumulate_need` accept any `supplier_id`; `src/sourcing/case_b.py::confirm_selection`
   accumulates selections. Guard: refuse INACTIVO suppliers in
   `open_or_create_po`/`accumulate_need` (or at `confirm_selection`).
3. **Case B candidate presentation**: `format_case_b_reply` in `src/agents/customer.py`
   lists candidate business names — candidates must come from the filtered searcher.
4. **Ingestion**: `src/backoffice/ingestion.py::confirm_items` resolves the supplier by ID
   and uses its margin — should refuse INACTIVO suppliers (or warn).
5. **PO listing**: `src/backoffice/po.py::list_purchase_orders` shows `razon_social` —
   historical POs must keep working after a supplier is deactivated (no FK cascade;
   nothing physically deleted — verified: FKs have no ondelete rules).
6. **Replenishment searches**: no replenishment module exists anywhere in src/ (grep
   found nothing). The brief's "replenishment searches" map to the searcher seam above —
   the closest real artifact is the sourcing search.
7. `Catalogo.proveedor_id` is NOT NULL FK — products never lose their supplier row on
   soft delete. Good; no migration risk beyond adding columns.

**Immutability detection** ("code immutable once linked to products/purchases"): check
existence of `Catalogo` rows with `proveedor_id == X`, `SupplierPurchaseOrder` rows with
`supplier_id == X`, `SourcingNeed.supplier_id == X`, and/or `ProveedorSkuMapping`
references. A pure helper + service-level guard covers it; no DB trigger needed.

## Validation & Normalization (objective 5)

- **Phone/E.164 precedent**: `src/agents/customer.py::normalize_phone(raw, region="AR")`
  using `phonenumbers` — but it returns the **WhatsApp-specific** form
  (`_to_whatsapp_e164` injects the `9` trunk prefix for AR mobiles: +54 9 11 ...), which
  is NOT strict E.164. `phonenumbers` is already a dependency. The brief wants whatsapp
  in "E.164 normalized" form — decide: reuse `normalize_phone` (consistent with clientes,
  WhatsApp-ready) vs a strict `format_number(E164)` variant for the supplier `telefono`
  field. Both can coexist; document the decision.
- **CUIT**: NO existing utility. Mod-11 validation must be new (pure function +
  parametrized tests). No dependency needed.
- **Email (RFC 5322)**: NO existing utility, and `email-validator` is NOT installed.
  Options: add `email-validator` dep, or a pragmatic regex. Recommend the dependency
  (matching the "verify technical claims" bar of the repo) — flag as an open question for
  the proposal.
- **Error handling convention**: module-level domain exceptions
  (`InvalidClientDataError`, `KeyError`, `ValueError`, `SelectionExecutedError`,
  `IllegibleDocumentError`, `InsufficientStockError`); backoffice catches broadly and
  renders `Error: {exc}` strings; tests use `pytest.raises(..., match=...)`.
- There is no shared validation module — a new `src/supplier/validation.py` (or
  extending an existing seam) is the natural home for CUIT/email/code-gen pure functions.

## Testing (objective 6)

- Runner: `make test` → `.venv/bin/python -m pytest`; `pyproject.toml` sets
  `testpaths=["tests"]`, `asyncio_mode="auto"`. CI runs `ruff check`, `ruff format
  --check`, then `pytest` against a Postgres/pgvector service
  (`DATABASE_URL=postgresql+psycopg://ferreteria@localhost:5432/ferreteria`).
- `tests/conftest.py`: session-scoped `db_engine` (drop + `Base.metadata.create_all` —
  tests do NOT run Alembic; pgvector extension created manually), `db_session`
  transactional fixture, `clean_schema` truncation fixture.
  `TRUNCATE_TABLES` in conftest must gain nothing new (no new tables expected), but the
  model change touches `proveedores` — existing truncate list already includes it.
- Test layout: `tests/test_<module>.py` per source module; Spanish scenario docstrings,
  English function names; `pytestmark = pytest.mark.skipif(not _postgres_up(), ...)` in
  DB-heavy files; `shop_ctx`-style seeded fixtures.
- **Strict TDD is NOT configured** (openspec/config.yaml `tdd: false`, `test_command: ""`
  — and the whole config.yaml context block is stale, claiming "no source code"). Tests
  are written alongside implementation. Coverage: `pytest-cov` available;
  `--cov-fail-under` gate documented in runbook (85%) but NOT wired in CI.
- Gotcha: `test_build_app_creates_five_tabs_with_expected_labels` breaks when the
  Suppliers tab is added (asserts exact tab list).
- Gotcha: `clean_schema` truncates `proveedores RESTART IDENTITY CASCADE` — tests
  constructing `Proveedor(proveedor_id=1, ...)` (many do) must add the new NOT NULL
  fields if they become NOT NULL (estado has a default so ORM-level fine; a NOT NULL
  `codigo` without a default would break ~15 test files).

## Extension Points

- New module `src/backoffice/suppliers.py` (mirror clients.py) + sixth Gradio tab in
  `app.py`.
- New pure module for code generation + CUIT/email validation (e.g.
  `src/supplier/codes.py` / `src/supplier/validation.py`).
- New Alembic migration `{head}+1` altering `proveedores` (columns, unique indexes,
  estado enum with backfill, code backfill for existing rows).
- ACTIVE-only filter points: DB-backed searcher (new or future),
  `open_or_create_po`/`accumulate_need`, `confirm_items`, PO grid label.
- Specs: new delta domain (e.g. `supplier-management`) — none exists today;
  `backoffice` spec gains the supplier module requirement.

## Approaches

1. **Single suppliers module + new tab, mirroring clients.py** — all CRUD/codegen/
   validation logic in `src/backoffice/suppliers.py` + `src/supplier/` pure helpers,
   one migration, filters applied at the four touchpoints. Low architectural risk, high
   consistency with existing patterns. Effort: Medium.

2. **Full domain layer (src/supplier/manager.py) + thin backoffice wrapper** — richer
   separation (service functions reusable by chat/pipeline), more files, more tests.
   Slightly cleaner long-term but heavier than the codebase's current flat pattern.
   Effort: Medium-High.

## Recommendation

Approach 1, with the pure helpers (codegen, CUIT mod-11, email) isolated in `src/supplier/`
for easy parametrized testing. Keep state transitions (ACTIVO↔INACTIVO) as a small enum +
guard functions, mirroring `SupplierPurchaseOrderState`. Wire the ACTIVE-only guard into
`open_or_create_po`/`accumulate_need` and `confirm_items`; define the searcher contract
for the future DB-backed implementation; update the Gradio tabs test; one Alembic
migration with backfill for the existing seeded supplier row.

## Risks

- Live DB row (proveedor_id=1) must be backfilled by the migration (estado default,
  code generation) or the upgrade fails; CUIT must stay NULLable for legacy rows.
- `codigo_proveedor` naming collision with `ProveedorSkuMapping.codigo_proveedor` (product
  code, not supplier code) — must pick a distinct column name.
- Small 3-char alphanumeric space (36³=46656; letters-only 17576) → collision variants
  needed from day one; also accent/ñ handling when deriving letters from razon_social.
- WhatsApp normalization utility returns the +54 9 WhatsApp form, not strict E.164 —
  must decide which semantics each field uses.
- `test_build_app_creates_five_tabs_with_expected_labels` and ~15 test files constructing
  `Proveedor(...)` directly will need updates for new NOT NULL columns.
- The sourcing searcher seam has no DB-backed implementation — the ACTIVO-only rule for
  quotes is a contract on the seam today, only enforceable in the Fake; risk of the rule
  being forgotten when the real RAG lands.
- openspec/config.yaml is stale (context claims "no source code, no framework"; tdd/test
  config empty) — should be corrected as part of this change or flagged to sdd-init.
- Inactive suppliers must not break historical views (POs, catalog) — no FK cascades
  exist, verified; keep it that way.

## Ready for Proposal

Yes. Open questions for the proposal phase:
1. CUIT for legacy rows: NULLable vs synthetic backfill?
2. Email validation: add `email-validator` dependency or regex?
3. Strict E.164 vs WhatsApp-form for `telefono` vs `whatsapp` fields?
4. Does editing a supplier's default margin re-price its existing catalog products?
   (recommend: no)
5. Where exactly does the 3-char code show in PO/ingestion views (replace numeric ID)?
6. Should the DB-backed searcher be implemented in this change or left as a contract?
