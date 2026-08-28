# Tasks: Supplier Management

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | 2400–2800 (rename churn across ~20 files) |
| 400-line budget risk | High |
| 2500-line review budget risk | High (at maintainer threshold) |
| Chained PRs recommended | Yes |
| Delivery strategy | `single-pr` (maintainer) |
| Chain strategy | `size-exception` — maintainer-approved (single PR) |

Decision needed before apply: No — size:exception approved by maintainer (single PR, ~2400-2800 lines)
Chained PRs recommended: Yes
Chain strategy: size-exception (approved)
400-line budget risk: High
2500-line review budget risk: High

### Work Units (chained fallback if `size:exception` denied)

| Unit | Goal | PR | Focused test | Runtime harness | Rollback |
|------|------|----|--------------|-----------------|----------|
| 1 | Schema + destructive migration + pure helpers | PR 1 | `pytest tests/test_db_models.py tests/test_supplier_validation.py -q` | `alembic upgrade head && alembic downgrade -1` round-trip | Drop new migration, revert `src/db/models.py` + `src/supplier/{validation,guards}.py`; head back to `5f304e18a765`. |
| 2 | Backoffice CRUD + sixth tab + rename fallout + ACTIVO guards | PR 2 | `pytest tests/test_backoffice.py tests/test_suppliers_backoffice.py tests/test_purchasing_accumulate.py -q` | `python -c "from src.backoffice.app import build_app; build_app()"` smoke | Revert `src/backoffice/{suppliers,app,ingestion,po}.py`, `src/purchasing/accumulate.py`, `src/supplier/{ocr,searcher}.py`, `src/sourcing/case_b.py`; PR1 migration stays. |
| 3 | Test updates + new validation/backoffice/guard tests | PR 3 | `pytest -q` (full suite) | `ruff check . && mypy src && pytest` (CI triad) | Revert touched `tests/*.py`; no production code touched. |

## Phase 1: Schema, Enums, Destructive Migration

- [x] 1.1 Add `email-validator>=2` to `pyproject.toml` and install.
- [x] 1.2 Add `SupplierStatus` (ACTIVO, INACTIVO) and `IvaCondition` (5 values) enums in `src/db/models.py`.
- [x] 1.3 Rename `Proveedor`→`Supplier`, `proveedores`→`suppliers`, `proveedor_id`→`id`; rename columns per design.
- [x] 1.4 Add `Supplier` columns: `cuit` String(13) NULL, `address`, `email`, `whatsapp`, `code` String(3) NOT NULL, `iva_condition`, `status` default ACTIVO.
- [x] 1.5 Add unique index on `Supplier.code` + partial unique `WHERE cuit IS NOT NULL` on `Supplier.cuit`.
- [x] 1.6 Rename `ProveedorSkuMapping`→`SupplierSkuMapping`, `proveedor_sku_mapping`→`supplier_sku_mappings`; rename columns per design.
- [x] 1.7 `Catalogo.proveedor`→`Catalogo.supplier`; rename FK `proveedor_id`→`supplier_id`; update `SupplierPurchaseOrder` + `SourcingNeed` FK targets to `suppliers.id`.
- [x] 1.8 Create `alembic/versions/{rev}_supplier_management.py` (`down_revision='5f304e18a765'`): delete child rows, rename tables/columns, add columns/enums/indexes; matching `downgrade`.
- [x] 1.9 RED: update `tests/test_db_models.py` expected sets and add new enum value assertions.

## Phase 2: Validation + Guard Helpers

- [x] 2.1 `src/supplier/validation.py`: `validate_cuit` (mod-11 weights 5,4,3,2,7,6,5,4,3,2) and `validate_email` (`email_validator`).
- [x] 2.2 `normalize_e164_phone` (phonenumbers, strict E.164, no `9`) and `normalize_whatsapp` (reuses `src.agents.customer.normalize_phone`).
- [x] 2.3 `suggest_code` (first letter of first 3 tokens, uppercased, padded) and `resolve_code` (3-char normalize, rotate `A-Z0-9`; raise `CodeCollisionError` when exhausted).
- [x] 2.4 `src/supplier/guards.py`: `ensure_active_supplier(session, supplier_id) -> Supplier` + `SupplierInactiveError`.
- [x] 2.5 `src/supplier/searcher.py`: add `status` to `SupplierCandidate`; `FakeSupplierCatalogSearcher.search` filters INACTIVO; docstring notes seam contract.
- [x] 2.6 `tests/test_supplier_validation.py` (pure unit): CUIT valid/invalid, e164 phone (mobile/landline/`9`/unparseable), whatsapp, email valid/malformed, `suggest_code` (1/2/3+ tokens + accents), `resolve_code` (free/rotate/exhaust).

## Phase 3: Backoffice CRUD + Sixth Tab UI

- [x] 3.1 `src/backoffice/suppliers.py`: `InvalidSupplierDataError`, `list_suppliers(query=None, status=None)`, `create_supplier`, `update_supplier`, `toggle_status`; create/update re-validate CUIT/email/phone; `_assert_code_not_linked` counts the 4 linked models and blocks `code` change when any > 0.
- [x] 3.2 Add sixth `gr.Tab("Suppliers")` in `src/backoffice/app.py`: grid (ID, Code, Name, CUIT, Contact, Phone, Margin, IVA, Status) fed by `_suppliers_grid(query, status)`; search + status filter; row select stores `supplier_id` in `gr.State`; create/edit form with `gr.Dropdown(iva_condition)` + `gr.Number(margin)`; code reactive to `business_name.change`; errors in status `gr.Textbox` (clients.py pattern).
- [x] 3.3 `tests/test_suppliers_backoffice.py` (DB-skipping pytestmark): create, list with query/status filters, toggle, code-blocked when linked, code-allowed when unlinked, margin edit does NOT re-price existing catalog rows, invalid CUIT/email/phone rejected.
- [x] 3.4 `tests/test_backoffice.py`: rename `Proveedor`→`Supplier` in `shop_ctx`; update seed; rename `..._five_tabs_...`→`..._six_tabs_...`; add `"Suppliers"` label assertion.

## Phase 4: Sourcing / Purchasing / Ingestion Rename + ACTIVO Guards

- [x] 4.1 `src/backoffice/ingestion.py`: rename `proveedor`→`supplier`, `margen_predeterminado`→`default_margin_pct`; call `ensure_active_supplier` as first line in `confirm_items`; new `Catalogo` rows use `supplier.default_margin_pct`.
- [x] 4.2 `src/purchasing/accumulate.py`: call `ensure_active_supplier` at start of `open_or_create_po` and `accumulate_need`.
- [x] 4.3 `src/supplier/ocr.py`: rename `ProveedorSkuMapping`→`SupplierSkuMapping` + columns (`proveedor_id`→`supplier_id`, `codigo_proveedor`→`supplier_sku_code`, `descripcion_raw`→`raw_description`, `sku_interno`→`internal_sku`, `confianza`→`confidence`).
- [x] 4.4 `src/backoffice/po.py`: `po.supplier.razon_social`→`po.supplier.business_name`.
- [x] 4.5 `src/sourcing/case_b.py`: reply string `"proveedor {po.supplier_id}"`→`"supplier {po.supplier_id}"`.
- [x] 4.6 `tests/test_purchasing_accumulate.py`: rename `Proveedor`→`Supplier`; add guard test asserting `SupplierInactiveError` for INACTIVO supplier in both `open_or_create_po` and `accumulate_need`.
- [x] 4.7 `tests/test_e2e_ingestion.py`: rename `Proveedor`→`Supplier`, update seed; add test: `confirm_items` raises `SupplierInactiveError` for INACTIVO supplier and writes no `Catalogo`/`Inventory`.

## Phase 5: Test Updates Across the Suite

- [ ] 5.1 `tests/conftest.py`: swap `proveedores, proveedor_sku_mapping`→`suppliers, supplier_sku_mappings` in `TRUNCATE_TABLES`; same swap in inline truncates in `test_backoffice.py`, `test_e2e_ingestion.py`, `test_approval.py`, `test_inventory.py`.
- [ ] 5.2 Across `test_approval.py`, `test_inventory.py`, `test_case_a.py`, `test_pipeline_owner.py`, `test_backoffice_po.py`, `test_barcode.py`, `test_case_c.py`, `test_classify.py`, `test_customers.py`, `test_dispatch.py`, `test_dispatch_handler.py`, `test_e2e_order.py`, `test_order_lifecycle.py`, `test_search.py`, `test_session_rehydrate_owner.py`, `test_sweeper.py`, `test_ocr.py`, `test_case_b.py`, `test_sourcing_persistence.py`: rename `Proveedor`→`Supplier`, `proveedor_id`→`id` (seed), `razon_social`→`business_name`, `margen_predeterminado`→`default_margin_pct`; add `code` + `status=ACTIVO` to every `Supplier(...)` constructor.
- [ ] 5.3 In `test_ocr.py` and `test_case_b.py` rename any local `ProveedorSkuMapping`; confirm tabs `5`→`6` and `"Suppliers"` label in `test_backoffice.py`.

## Phase 6: Final Verification

- [ ] 6.1 `ruff check .` — resolve all violations.
- [ ] 6.2 `mypy src` (strict) — resolve all type errors.
- [ ] 6.3 `pytest -q` — full suite green including new `test_supplier_validation.py` + `test_suppliers_backoffice.py`.
- [ ] 6.4 `alembic upgrade head && alembic downgrade -1` round-trip on throwaway DB; verify `down_revision='5f304e18a765'` and no legacy `proveedores`.
- [ ] 6.5 Grep verify: no remaining `Proveedor`/`proveedor` (allowlist: `docs/escenarios-testeados.md`, `scripts/gen_test_scenarios.py`, `alembic/versions/26a4a1b103fe_initial_schema_with_pgvector.py`).
