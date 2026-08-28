# Design: Supplier Management

## Technical Approach

Rename the supplier domain to English in `src/db/models.py`, add a CRUD module (`src/backoffice/suppliers.py`, clients.py pattern), pure validation/codegen helpers under `src/supplier/`, one destructive migration, and ACTIVO guards at the three DB touchpoints. Searcher stays contract-only.

## Architecture Decisions

| Decision | Choice | Rejected | Why |
|---|---|---|---|
| Codegen/collision | Deterministic prefix+rotate in `src/supplier/validation.py` | Random suffix, DB function | Predictable, testable, no DB round-trips |
| CUIT uniqueness | Partial unique index `WHERE cuit IS NOT NULL` | Full unique, app-only | Legacy rows may lack CUIT; DB backs the guarantee |
| Active guard home | `src/supplier/guards.py` `ensure_active_supplier` | Per-call inline | Purchasing+ingestion share one source of truth |
| Immutability | Service existence checks on 4 models | DB trigger | Matches spec; no trigger overhead |
| Searcher status | Add `status` to `SupplierCandidate`; `FakeSupplierCatalogSearcher` filters INACTIVO | New DB query | DB searcher out of scope; contract enforced + testable |
| Margin edit | Future-ingestion only; `update_supplier` never touches catalogo | Re-price loop | User decision |

## Data Flow

```
backoffice form ──validate──► create/update_supplier ──► flush Supplier
        code textbox ◄──suggest_code(business_name)── resolve_code(collision)
sourcing/PO ──► ensure_active_supplier ──► open_or_create_po / accumulate_need
ingestion ──► confirm_items ──► ensure_active_supplier ──► default_margin_pct → Catalogo
```

## File Changes

| File | Action | Description |
|---|---|---|
| `src/db/models.py` | Modify | `Supplier`, `SupplierSkuMapping`; `SupplierStatus`/`IvaCondition` enums; new columns; `Catalogo.supplier` |
| `alembic/versions/xxxx_supplier_management.py` | Create | Delete legacy rows → rename → add columns/enums/indexes |
| `src/supplier/validation.py` | Create | `validate_cuit`, `normalize_e164_phone`, `normalize_whatsapp`, `validate_email`, `suggest_code`, `resolve_code` |
| `src/supplier/guards.py` | Create | `ensure_active_supplier`, `SupplierInactiveError` |
| `src/backoffice/suppliers.py` | Create | `list_suppliers`, `create_supplier`, `update_supplier`, `toggle_status`, immutability check |
| `src/backoffice/app.py` | Modify | Sixth tab; `_ingest_confirm` rename |
| `src/backoffice/ingestion.py` | Modify | `confirm_items`: rename + ACTIVO guard + `default_margin_pct` |
| `src/backoffice/po.py` | Modify | `po.supplier.razon_social` → `business_name` |
| `src/purchasing/accumulate.py` | Modify | Guards in `open_or_create_po`, `accumulate_need` |
| `src/supplier/searcher.py` | Modify | Contract note + `status` field + Fake filter |
| `src/supplier/ocr.py` | Modify | `SupplierSkuMapping` rename |
| `scripts/gen_test_scenarios.py` | Verify | No model usage — only a Spanish label string "OCR de documentos de proveedor"; no change expected |
| `tests/conftest.py`, `~30 test files` | Modify | Rename fallout; TRUNCATE list |

## Migration Plan

One migration (`down_revision = '5f304e18a765'`). Order:

1. **Delete dependents, child-first** (all suppliers are legacy; `catalogo.proveedor_id` is NOT NULL so every catalogo row is a dependent):
   ```sql
   DELETE FROM sourcing_needs WHERE supplier_id IS NOT NULL;
   DELETE FROM supplier_purchase_order_items;
   DELETE FROM supplier_purchase_orders;
   DELETE FROM catalogo;
   DELETE FROM proveedor_sku_mapping;
   DELETE FROM proveedores;
   ```
   (`inventory`/`orders`/`order_items`/`stock_reservations`/`stock_adjustments` reference SKUs by bare string, no FK — left untouched.)
2. **Rename** `proveedores`→`suppliers`, PK `proveedor_id`→`id`; Postgres auto-rewrites FKs to follow. Rename supplier columns: `razon_social`→`business_name`, `contacto`→`contact_name`, `telefono`→`phone`, `margen_predeterminado`→`default_margin_pct`, `condiciones`→`terms`. Rename child FK columns `catalogo.proveedor_id`→`supplier_id`, `proveedor_sku_mapping.proveedor_id`→`supplier_id`. Rename `proveedor_sku_mapping`→`supplier_sku_mappings` and its columns: `mapping_id`→`id`, `codigo_proveedor`→`supplier_sku_code`, `descripcion_raw`→`raw_description`, `sku_interno`→`internal_sku`, `confianza`→`confidence`.
3. **Add** columns (`cuit`, `address`, `email`, `whatsapp`, `code` String(3), `iva_condition`, `status`), `CREATE TYPE supplier_status`/`iva_condition`, unique index on `code`, partial unique index on `cuit`.

**Downgrade**: drop indexes/columns/types, rename back. Deleted data unrecoverable (user-approved).

**Test consistency**: `tests/conftest.py` builds schema via `Base.metadata.create_all` (never Alembic) — models must mirror the migration exactly; update `TRUNCATE_TABLES` to `suppliers`, `supplier_sku_mappings`. The "migration" tests in `test_db_models.py` validate ORM metadata, so rename their expected sets too.

## Backoffice UI (sixth tab)

`gr.Dataframe` grid [ID, Code, Name, CUIT, Contact, Phone, Margin, IVA, Status] fed by `_suppliers_grid(query, status)`. Search box + status `gr.Dropdown` (All/ACTIVO/INACTIVO) trigger the grid via `.change`; `list_suppliers(session, query=None, status=None)` filters `ILIKE` on `business_name`/`cuit`/`code`. Row selection (`gr.Dataframe.select`) stores `supplier_id` in `gr.State` and populates the edit form; "Toggle status" applies to that id. Create/edit form: text fields + `gr.Dropdown(iva_condition)` + `gr.Number(margin)`; code field reacts to `business_name.change` calling `suggest_code`, editable until save. Backend `create_supplier`/`update_supplier` re-validate and surface errors in a status `Textbox` (clients.py `try/except` pattern).

## Guard Placement

- **Immutability** (`src/backoffice/suppliers.py`): `update_supplier` calls `_assert_code_not_linked(session, supplier)` — `SELECT` counts over `Catalogo`, `SupplierPurchaseOrder`, `SourcingNeed`, `SupplierSkuMapping` by `supplier_id`; raises `InvalidSupplierDataError` if any > 0 and `code` changed.
- **ACTIVO** (`src/supplier/guards.py`): `ensure_active_supplier(session, supplier_id) -> Supplier` raises `SupplierInactiveError` when `status == INACTIVO`. Called first in `open_or_create_po`, `accumulate_need`, `confirm_items` (before any write). Seam contract (docstring + Fake filter) excludes INACTIVO.

## Validation Helpers (`src/supplier/validation.py`)

- `validate_cuit(cuit: str) -> bool` — 11 digits, mod-11 verifier (weights 5,4,3,2,7,6,5,4,3,2).
- `normalize_e164_phone(raw: str, region="AR") -> str | None` — `phonenumbers` → strict E.164 (no WhatsApp `9` insertion). Distinct from `normalize_phone`.
- `normalize_whatsapp(raw: str) -> str | None` — reuses `normalize_phone` from `src/agents/customer.py` (WhatsApp `+54 9` form), consistent with the client channel.
- `validate_email(email) -> bool` — `email_validator`.
- `suggest_code(business_name) -> str` — first letter of first 3 tokens, uppercased, padded from first token.
- `resolve_code(session, raw) -> str` — normalize to 3 chars; if free return; else rotate third char over `A-Z0-9` (prefix `code[:2]`), then two-char rotation; raise `CodeCollisionError` when exhausted (owner types manually). DB unique index is the backstop.

## Pricing Integration

`confirm_items` renames `proveedor`→`supplier`, `margen_predeterminado`→`default_margin_pct`; new products still get `margen_aplicado_pct = supplier.default_margin_pct` via `compute_base`. Margin edits only mutate the `Supplier` row — existing catalogo prices untouched.

## Testing Strategy

- Update `~30` files (rename): `conftest.py` TRUNCATE, `test_db_models.py` expected tables, `test_backoffice.py` seed/`Proveedor` imports + tabs test (`five`→`six` incl. "Suppliers"), `test_ocr.py`, `test_e2e_ingestion.py`, `test_purchasing_accumulate.py`, `test_case_b.py`, `test_sourcing_persistence.py`.
- New: `tests/test_supplier_validation.py` (pure), `tests/test_suppliers_backoffice.py` (CRUD/codegen/immutability/toggle), guard tests in accumulate + ingestion suites (ACTIVO refusal, no inventory written).
- DB tests use `db_engine`/`db_session` + `clean_schema`; Postgres-down skip via `pytestmark = skipif(not _postgres_up())` (as `test_backoffice.py`). Strict TDD not configured — write tests alongside implementation.

## Threat Matrix

N/A — no routing, shell, subprocess, VCS/PR automation, executable-file classification, or process-integration boundary.

## Migration / Rollout

`alembic upgrade head` runs the destructive migration; `downgrade` restores schema (not data). No feature flag needed.

## Open Questions

- [x] `IvaCondition` enum values — user-confirmed: `RESPONSABLE_INSCRIPTO`, `MONOTRIBUTO`, `EXENTO`, `CONSUMIDOR_FINAL`, `NO_RESPONSABLE`.

## Next Recommended

`tasks`
