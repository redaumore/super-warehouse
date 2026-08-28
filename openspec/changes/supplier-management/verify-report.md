```yaml
schema: gentle-ai.verify-result/v1
evidence_revision: sha256:be0e524444cf815a3accbb694dbf065f2740dcbce7d21669cc6089a3cce3179f
verdict: fail
blockers: 0
critical_findings: 0
requirements: 11/11
scenarios: 26/26
test_command: pytest -q
test_exit_code: 1
test_output_hash: sha256:be0e524444cf815a3accbb694dbf065f2740dcbce7d21669cc6089a3cce3179f
build_command: ruff check . && mypy src
build_exit_code: 0
build_output_hash: sha256:28ac19aeb6bc9961c10736add69b98064a9de0c489ba5d4ac768daf5c2fab662
```

## Verification Report

**Change**: supplier-management
**Version**: N/A
**Mode**: Standard (strict_tdd: false)

### Completeness

| Metric | Value |
|--------|-------|
| Tasks total | 34 |
| Tasks complete | 34 |
| Tasks incomplete | 0 |

### Build & Tests Execution

**Build**: ✅ Passed
```text
$ ruff check .
All checks passed!
$ ruff format --check .
180 files already formatted
$ mypy src
Success: no issues found in 55 source files
```

**Tests**: ✅ 485 passed / ❌ 3 failed / ⚠️ 0 skipped
```text
$ pytest -q
3 failed, 485 passed in 16.69s
```

The 3 failures are pre-existing and env-dependent (NOT caused by this change):
- `tests/test_pipeline.py::test_handle_inbound_routes_persists_and_replies`
- `tests/test_pipeline.py::test_second_message_resumes_context`
- `tests/test_pipeline.py::test_voice_routes_to_perception_reply`

Verified identical at base commit `f4568c8` — same 3 tests fail with the same assertions. Root cause: local `.env` sets `OWNER_TELEGRAM_CHAT_ID=2074034510`, which triggers the owner-gate path in the pipeline, returning a hardcoded Spanish message instead of the mocked responder output. CI (without that `.env`) passes.

**Coverage**: ➖ Not available (no `--cov` threshold configured)

### Spec Compliance Matrix

| Requirement | Scenario | Test | Result |
|-------------|----------|------|--------|
| **supplier-management: Supplier master data and CRUD** | Create supplier | `test_suppliers_backoffice.py > test_create_supplier_persists_with_code_and_active_status` | ✅ COMPLIANT |
| | Edit supplier | `test_suppliers_backoffice.py > test_update_supplier_edits_fields` | ✅ COMPLIANT |
| **supplier-management: Supplier code** | Code suggested from name | `test_supplier_validation.py > test_suggest_code_*` (5 parametrized) + UI wiring in `src/backoffice/app.py` | ✅ COMPLIANT |
| | Code collision | `test_suppliers_backoffice.py > test_create_supplier_rotates_code_on_collision` | ✅ COMPLIANT |
| | Code immutable once linked | `test_suppliers_backoffice.py > test_update_supplier_code_blocked_when_linked_to_catalog` | ✅ COMPLIANT |
| **supplier-management: Supplier validation** | Invalid CUIT rejected | `test_suppliers_backoffice.py > test_create_supplier_rejects_invalid_cuit[20111111110\|123\|abc]` | ✅ COMPLIANT |
| | Phone normalized | `test_supplier_validation.py > test_e164_mobile_keeps_strict_e164_without_whatsapp_9` + `test_whatsapp_uses_whatsapp_form` | ✅ COMPLIANT |
| **supplier-management: Supplier uniqueness** | Duplicate CUIT rejected | `test_suppliers_backoffice.py > test_duplicate_cuit_rejected_by_database` | ✅ COMPLIANT |
| | Duplicate code rejected | `test_suppliers_backoffice.py > test_duplicate_code_rejected_by_database` | ✅ COMPLIANT |
| **supplier-management: Supplier status lifecycle** | INACTIVO excluded from sourcing | `test_supplier_validation.py > test_fake_searcher_excludes_inactive_candidates` | ✅ COMPLIANT |
| **supplier-management: Legacy data deletion** | Migration removes legacy rows | `test_db_models.py > test_migration_creates_all_tables` + migration `46bdbdc4a575` DELETE statements | ✅ COMPLIANT |
| **supplier-management: Default margin scope** | Margin applies to future ingestion | `test_backoffice.py > test_confirm_items_creates_new_product_for_unknown_sku` (uses `default_margin_pct`) | ✅ COMPLIANT |
| | Margin edit does not re-price | `test_suppliers_backoffice.py > test_margin_edit_does_not_reprice_existing_catalog_rows` | ✅ COMPLIANT |
| **supplier-document-ingestion: Refuse inactive at confirmation** | confirm_items refuses INACTIVO | `test_e2e_ingestion.py > test_confirm_items_refuses_inactive_supplier_and_writes_nothing` | ✅ COMPLIANT |
| **supplier-catalog-search: Searcher seam** | Candidates returned for missing item | `test_classify.py` (multiple tests use `FakeSupplierCatalogSearcher` with candidates) | ✅ COMPLIANT |
| | No supplier offers the item | `test_classify.py` (empty searcher returns empty tuple) + `test_supplier_validation.py > test_fake_searcher_excludes_inactive_candidates` (INACTIVO-only set yields no results) | ✅ COMPLIANT |
| | Inactive supplier excluded | `test_supplier_validation.py > test_fake_searcher_excludes_inactive_candidates` | ✅ COMPLIANT |
| | Seam decouples RAG | `FakeSupplierCatalogSearcher` used across `test_case_a.py`, `test_case_b.py`, `test_case_c.py`, `test_classify.py`, `test_sourcing_persistence.py`, `test_supplier_validation.py` | ✅ COMPLIANT |
| **purchase-order-lifecycle: Refuse inactive suppliers** | PO creation refuses INACTIVO | `test_purchasing_accumulate.py > test_open_or_create_po_refuses_inactive_supplier` | ✅ COMPLIANT |
| | Accumulation refuses INACTIVO | `test_purchasing_accumulate.py > test_accumulate_need_refuses_inactive_supplier` | ✅ COMPLIANT |
| **backoffice: Supplier management module** | List with quick search and filter | `test_suppliers_backoffice.py > test_list_suppliers_filters_by_query` + `test_list_suppliers_filters_by_status` | ✅ COMPLIANT |
| | Toggle status | `test_suppliers_backoffice.py > test_toggle_status_flips_and_back` | ✅ COMPLIANT |
| | Create with reactive validation | `test_suppliers_backoffice.py > test_create_supplier_*` (7 parametrized) + `test_update_supplier_revalidates_contact_fields` | ✅ COMPLIANT |

**Compliance summary**: 26/26 scenarios compliant

### Correctness (Static Evidence)

| Requirement | Status | Notes |
|------------|--------|-------|
| Supplier master data and CRUD | ✅ Implemented | `src/db/models.py` Supplier with 13 columns; `src/backoffice/suppliers.py` CRUD functions |
| Supplier code | ✅ Implemented | `src/supplier/validation.py` suggest_code + resolve_code; `_assert_code_not_linked` in suppliers.py:266 |
| Supplier validation | ✅ Implemented | `validate_cuit` (mod-11), `normalize_e164_phone` (strict E.164), `normalize_whatsapp` (reuses `normalize_phone`), `validate_email` |
| Supplier uniqueness | ✅ Implemented | Migration: `uq_suppliers_code` unique index + `uq_suppliers_cuit` partial unique `WHERE cuit IS NOT NULL` |
| Supplier status lifecycle | ✅ Implemented | `src/supplier/guards.py` ensure_active_supplier; searcher.py:81 filters INACTIVO |
| Legacy data deletion | ✅ Implemented | Migration `46bdbdc4a575` lines 54-59: child-first DELETE from sourcing_needs → proveedores |
| Default margin scope | ✅ Implemented | `src/backoffice/ingestion.py` uses `supplier.default_margin_pct`; `update_supplier` never touches catalogo |
| Refuse inactive at confirmation | ✅ Implemented | `src/backoffice/ingestion.py:78` calls `ensure_active_supplier` first |
| Searcher seam | ✅ Implemented | `SupplierCatalogSearcher` protocol + `FakeSupplierCatalogSearcher` with INACTIVO filter |
| Refuse inactive in PO/accumulation | ✅ Implemented | `src/purchasing/accumulate.py:36,68` calls `ensure_active_supplier` |
| Supplier management module | ✅ Implemented | `src/backoffice/app.py` sixth tab; `src/backoffice/suppliers.py` full CRUD |

### Coherence (Design)

| Decision | Followed? | Notes |
|----------|-----------|-------|
| Codegen/collision: deterministic prefix+rotate | ✅ Yes | `src/supplier/validation.py` suggest_code + resolve_code with A-Z0-9 rotation |
| CUIT uniqueness: partial unique index WHERE NOT NULL | ✅ Yes | Migration line 97-103 |
| Active guard home: `src/supplier/guards.py` | ✅ Yes | Used in ingestion.py:78, accumulate.py:36,68 |
| Immutability: service existence checks on 4 models | ✅ Yes | `_assert_code_not_linked` counts Catalogo, SupplierPurchaseOrder, SourcingNeed, SupplierSkuMapping |
| Searcher status: add status to SupplierCandidate; Fake filters INACTIVO | ✅ Yes | searcher.py:32 default="ACTIVO", line 81 filters |
| Margin edit: future-ingestion only | ✅ Yes | `update_supplier` never touches catalogo; test confirms |

### DB-Level Guarantees

| Guarantee | Evidence |
|-----------|----------|
| `Supplier.code` unique index | Migration line 96: `op.create_index("uq_suppliers_code", "suppliers", ["code"], unique=True)` |
| `Supplier.cuit` partial unique index | Migration lines 97-103: `postgresql_where=sa.text("cuit IS NOT NULL")` |
| `SupplierStatus` enum (ACTIVO/INACTIVO) | `src/db/models.py` + `test_db_models.py > test_supplier_status_enum_values` |
| `IvaCondition` enum (5 values) | `src/db/models.py` + `test_db_models.py > test_iva_condition_enum_values` |
| Destructive legacy deletion | Migration lines 54-59: child-first DELETE |
| FK retargets to `suppliers(id)` | Migration renames `proveedor_id`→`supplier_id` on catalogo, supplier_purchase_orders, sourcing_needs, supplier_sku_mappings |
| ACTIVO guard at 3 touchpoints | `ingestion.py:78`, `accumulate.py:36`, `accumulate.py:68` |
| Code-immutability guard | `suppliers.py:266` `_assert_code_not_linked` |
| whatsapp uses normalize_phone (WhatsApp form) | `validation.py:80` `return normalize_phone(raw)` |
| phone uses strict E.164 helper | `validation.py:62` `normalize_e164_phone` (phonenumbers, no WhatsApp 9) |

### Issues Found

**CRITICAL**: None

**WARNING**: None

**SUGGESTION**:
1. The 3 pre-existing `test_pipeline.py` failures are caused by the local `.env` `OWNER_TELEGRAM_CHAT_ID=2074034510`. Consider adding a `.env.test` or documenting the env requirement for local runs.

### Verdict

**FAIL** (envelope) / **PASS** (substantive assessment) — All 34 tasks complete. All 11 requirements implemented. 26/26 scenarios have passing covering tests (the previously PARTIAL searcher-scope scenarios are now covered by `test_fake_searcher_excludes_inactive_candidates` in `tests/test_supplier_validation.py`). No CRITICAL findings. No regressions from this change. The envelope records `fail` because `pytest` exits non-zero due solely to 3 pre-existing env-dependent failures in `test_pipeline.py`, verified identical at base `f4568c8`; those failures are NOT caused by this change — they exist at the base commit with the same local `.env` setting `OWNER_TELEGRAM_CHAT_ID=2074034510`.
