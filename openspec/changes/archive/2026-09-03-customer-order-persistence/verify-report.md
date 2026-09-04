```yaml
schema: gentle-ai.verify-result/v1
evidence_revision: sha256:705cc770dd075da9919cd5a0b0f833ef142e559af7d2b2134f803910d3319c17
verdict: pass_with_warnings
blockers: 0
critical_findings: 0
requirements: 14/14
scenarios: 33/33
test_command: .venv/bin/python -m pytest tests/
test_exit_code: 0
test_output_hash: sha256:5cb77a54d5d85d83968f5b9f7cd31528c1a616351da16513c692f2abd0f7233d
build_command: .venv/bin/python -m ruff check src tests
build_exit_code: 0
build_output_hash: sha256:82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18
```

## Verification Report

**Change**: customer-order-persistence (final verify re-run with fresh evidence against the current working tree, uncommitted surgical-fix batch included)
**Version**: N/A (change delta specs, 5 capability files)
**Mode**: Standard (strict_tdd: false)

### Re-Check of Previous Findings (verify run #2 → run #3)
| Previous finding | Re-check evidence | Status |
|------------------|-------------------|--------|
| CRITICAL 1–3 (runs #1–#2 resolved) — seed value, frozen persisted lines, ask-before-persist | Covering tests still present and passing inside the 577: `tests/test_db_models.py > test_migration_seeded_default_margin_is_read_by_pricing`, `tests/test_draft_order.py > test_supplier_margin_edit_keeps_persisted_order_lines_frozen`, `tests/test_finalize.py > test_finalize_without_customer_name_asks_before_persisting` | ✅ STILL RESOLVED |
| PARTIAL (a) — session-customer finalize branch never exercised | New `tests/test_finalize.py > test_finalize_session_customer_persists_without_asking_name`: `customer_id=1` set, name-less "cerrá el pedido" → no ask, order persisted with `customer_id == 1`, subtotal/total 135.00, draft cleared | ✅ CLOSED |
| PARTIAL (b) — no doubled-prefix SKU through `persist_draft_order` | New `tests/test_draft_order.py > test_persist_draft_order_normalizes_doubled_prefix_sku`: `AMX-AMX-AT-5044` priced order persisted → stored `OrderItem.sku == "AMX-AT-5044"` via `_stored_sku` | ✅ CLOSED |
| PARTIAL (c) — no handler-level finalize of a price-less RAG entry | New `tests/test_finalize.py > test_finalize_rag_without_price_falls_back_to_endpoint_lookup`: RAG entry without price, rag client mocked at the boundary, `price_lookup.assert_called_once_with("AT-5044", "AMX")`, priced 135.5 × 1.20 = 162.60 ARS, snapshot stored | ✅ CLOSED |
| PARTIAL (d) — rate-edit `updated_at` not asserted; app-level save→recompute wiring untested | New `tests/test_backoffice.py > test_app_rate_save_updates_timestamp_and_recomputes_pending_order`: app handler `_save_exchange_rate` called twice with patched clock; rates grid shows `datetime(2024,1,1)` then `datetime(2024,6,1)` (timestamp bumped); messages "recomputed 1 pending order(s)" then "recomputed 0"; reloaded order `conversion_pending=False`, totals 24000.00 | ✅ CLOSED |
| PARTIAL (e) — no `set_default_margin` → new chat finalize chain | New `tests/test_finalize.py > test_default_margin_edit_prices_subsequent_chat_finalize`: `set_default_margin(27.50)` committed, then a NEW chat finalize for an unmapped supplier prices 100.00 → 127.50 | ✅ CLOSED |
| WARNING — ruff clean (was 13 errors at run #1) | `ruff check src tests` → "All checks passed!", exit 0 (output hash identical to run #2) | ✅ STILL CLEAN |
| WARNING — mypy baseline | `mypy src` → exactly the 3 pre-existing baseline errors (`src/backoffice/app.py:17,192,196`), zero new; output hash identical to run #2's baseline | ✅ STILL BASELINE-ONLY |
| WARNING — size over exception | Fresh measurement: authored diff e261524 → working tree = 2493 insertions / 48 deletions = **2541 authored changed lines** (docs excluded) — larger than run #2's 2369 because the 5 covering tests landed after the apply-progress refresh; see WARNING 1 | ⚠️ REMAINS (figure corrected upward) |

### Completeness
| Metric | Value |
|--------|-------|
| Tasks total | 21 |
| Tasks complete | 21 |
| Tasks incomplete | 0 |

### Build & Tests Execution
**Build**: ✅ Passed — the repo's quality gate (`make lint` = `ruff check src tests`; no compile step exists) is clean at exit 0. The separate advisory `make typecheck` target (`mypy src`) exits 1 solely from the 3 pre-existing baseline errors that already exist at base e261524; zero new type errors — recorded as baseline context, not a gate regression of this change.
```text
$ .venv/bin/python -m ruff check src tests
All checks passed! — exit 0 (sha256:82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18)

$ .venv/bin/python -m mypy src   (advisory baseline context; output sha256:77b07e6dddbbf56da880fac72ba3c5480d9b8750edf05b071ec8231ca2b23351)
src/backoffice/app.py:17: error: Library stubs not installed for "pandas"  [import-untyped]
src/backoffice/app.py:192: error: "object" has no attribute "itertuples"  [attr-defined]
src/backoffice/app.py:196: error: Argument 2 to "confirm_items" has incompatible type "object"; expected "list[list[object]]"  [arg-type]
Found 3 errors in 1 file (checked 60 source files) — exit 1 (pre-existing baseline, not introduced by this change).
```

**Tests**: ✅ 577 passed / ❌ 0 failed / ⚠️ 0 skipped (DB-backed integration tests ran against disposable Postgres)
```text
$ .venv/bin/python -m pytest tests/
577 passed, 8 warnings in 18.58s — exit 0 (output sha256:5cb77a54d5d85d83968f5b9f7cd31528c1a616351da16513c692f2abd0f7233d).
The 8 warnings are the pre-existing Alembic path_separator DeprecationWarning. Suite grew 572 → 577
(+5: the three test_finalize.py covering tests, the doubled-prefix persist test, and the app-level
rate-save test). A dedicated focused re-run of the 5 new covering tests individually reported
5 passed in 3.31s, proving per-test runtime evidence for every previously PARTIAL scenario.
```

**Coverage**: ➖ Not available (pytest-cov not configured in this project)

**Environment note (known risk)**: RAG at `localhost:8001` is slow/known — no verification claim depends on the live RAG service. The new endpoint-fallback test mocks the client at the client boundary (`unittest.mock.Mock` on `RagProductClient.price_lookup`), exactly per the declared risk rule; the endpoint mapping itself is covered by `httpx.MockTransport` unit tests (`tests/test_rag.py > test_price_lookup_*`).

### Spec Compliance Matrix
Statuses: ✅ COMPLIANT = covering test passed at runtime; ⚠️ PARTIAL = passing test covers only part of the scenario; ❌ UNTESTED = no covering test found.

**customer-order-persistence (spec)** — 6 requirements, 12 scenarios
| Requirement | Scenario | Test | Result |
|-------------|----------|------|--------|
| Finalize draft into a persisted order | Session customer attached | `tests/test_finalize.py > test_finalize_session_customer_persists_without_asking_name` (customer_id=1 set → order persisted, customer attached, draft cleared, no ask) | ✅ COMPLIANT |
| Finalize draft into a persisted order | Customer asked at finalization | `tests/test_finalize.py > test_finalize_without_customer_name_asks_before_persisting` (ask reply, draft preserved, no Order row) | ✅ COMPLIANT |
| Finalize draft into a persisted order | Unknown customer created minimally | `tests/test_finalize.py > test_finalize_unknown_customer_then_create_attaches_waiting_draft` (client created on Base list `lista_precios_id == 1`, order attached, draft cleared) | ✅ COMPLIANT |
| Source-aware base pricing at finalize | Local line priced from cost | `tests/test_order_pricing.py > test_local_lines_use_cost_and_applied_margin_not_list_price` + `tests/test_finalize.py > test_finalize_local_draft_uses_cost_margin_and_clears_draft` (base 135.00 vs `precio_lista_base` 999.00) | ✅ COMPLIANT |
| Source-aware base pricing at finalize | Unmapped supplier uses default margin | `tests/test_order_pricing.py > test_rag_supplier_margin_and_default_margin_are_source_aware` (100 → 120.00) + `tests/test_db_models.py > test_migration_seeded_default_margin_is_read_by_pricing` (seeded default feeds `compute_order`) + `tests/test_finalize.py > test_default_margin_edit_prices_subsequent_chat_finalize` (edited default 27.50 → 127.50) | ✅ COMPLIANT |
| Frozen RAG line snapshots | Snapshot without catalog link | `tests/test_draft_order.py > test_persist_draft_order_reserves_local_and_keeps_rag_snapshot` (name/source/supplier/moneda/precio_original snapshot fields, no catalogo FK) | ✅ COMPLIANT |
| Frozen RAG line snapshots | SKU normalized | `tests/test_draft_order.py > test_persist_draft_order_normalizes_doubled_prefix_sku` (`AMX-AMX-AT-5044` persisted → stored `AMX-AT-5044`) + `tests/test_rag.py > test_normalize_rag_sku[double-prefix-collapsed]` + `test_rag_client_normalizes_double_prefix_codigo` + `tests/test_customer.py > test_add_intent_preserves_normalized_rag_sku_in_draft` | ✅ COMPLIANT |
| Exchange-rate conversion | Non-ARS line converted | `tests/test_order_pricing.py > test_non_ars_conversion_happens_before_margin_and_totals` | ✅ COMPLIANT |
| Exchange-rate conversion | Missing rate defers conversion | `tests/test_order_pricing.py > test_missing_non_ars_rate_raises` + `tests/test_finalize.py > test_finalize_rag_without_rate_saves_pending_snapshot` + `tests/test_draft_order.py > test_persist_draft_order_keeps_totals_null_when_conversion_is_pending` + `tests/test_backoffice.py > test_recompute_pending_conversion_clears_flag_and_fills_totals` (24000.00 after rate load) | ✅ COMPLIANT |
| Save side effects (stock now, Sheets at approval) | Local lines reserve stock at save | `tests/test_draft_order.py > test_persist_draft_order_reserves_local_and_keeps_rag_snapshot` (reservations exactly `[("LOCAL-1", 2)]`) + `tests/test_approval.py > test_sheets_append_belongs_to_approval_not_draft_persistence` (0 Sheets calls at save, 1 at approval) | ✅ COMPLIANT |
| Save side effects (stock now, Sheets at approval) | RAG lines skip stock | Same two tests (no reservation for the RAG finalize order; Sheets untouched at save) | ✅ COMPLIANT |
| Order retrieval with lines and totals | Order detail retrieved | `tests/test_backoffice.py > test_customer_orders_list_and_detail_include_ars_totals_and_snapshots` (total 256.50; line source/name snapshot fields) | ✅ COMPLIANT |

**pricing-engine (delta)** — 3 requirements, 8 scenarios
| Requirement | Scenario | Test | Result |
|-------------|----------|------|--------|
| Compute base price from cost and margin (MODIFIED) | Base price computed | `tests/test_pricing.py > test_compute_base[925.92-0.35-1249.99]` (HALF_UP to cents, matches spec value 1249.99) + `test_base_price_rounds_half_up_to_cent` | ✅ COMPLIANT |
| Compute base price from cost and margin (MODIFIED) | Zero margin yields cost | `tests/test_pricing.py > test_compute_base[1000-0-expected1]` | ✅ COMPLIANT |
| Compute base price from cost and margin (MODIFIED) | List price never used as base | `tests/test_order_pricing.py > test_local_lines_use_cost_and_applied_margin_not_list_price` | ✅ COMPLIANT |
| Compute base price for RAG-sourced items (ADDED) | Supplier margin applied to RAG line | `tests/test_order_pricing.py > test_rag_supplier_margin_and_default_margin_are_source_aware` (100 → 125.00) + `tests/test_draft_order.py > test_supplier_margin_edit_keeps_persisted_order_lines_frozen` (supplier row margin 0.25 → 125.00) | ✅ COMPLIANT |
| Compute base price for RAG-sourced items (ADDED) | Fallback endpoint price used | `tests/test_finalize.py > test_finalize_rag_without_price_falls_back_to_endpoint_lookup` (handler-level, price-less entry, client mocked at boundary, `price_lookup("AT-5044","AMX")` supplies price 135.5 ARS) + `tests/test_rag.py > test_price_lookup_200_maps_price_and_supplier_query_parameter` | ✅ COMPLIANT |
| Compute base price for RAG-sourced items (ADDED) | Unmapped supplier falls back to default | `tests/test_order_pricing.py > test_rag_supplier_margin_and_default_margin_are_source_aware` (100 → 120.00) + `tests/test_finalize.py > test_default_margin_edit_prices_subsequent_chat_finalize` (unmapped supplier priced with edited setting) | ✅ COMPLIANT |
| Persist order subtotal and total (ADDED) | Totals persisted on save | `tests/test_draft_order.py > test_persist_draft_order_reserves_local_and_keeps_rag_snapshot` (757.80) + `tests/test_finalize.py > test_finalize_local_draft_uses_cost_margin_and_clears_draft` (270.00) | ✅ COMPLIANT |
| Persist order subtotal and total (ADDED) | Pending-conversion totals deferred | `tests/test_backoffice.py > test_recompute_pending_conversion_clears_flag_and_fills_totals` (24000.00 after rate load) + `tests/test_backoffice.py > test_app_rate_save_updates_timestamp_and_recomputes_pending_order` (24000.00 after app-level rate save) | ✅ COMPLIANT |

**backoffice (delta)** — 3 requirements, 6 scenarios
| Requirement | Scenario | Test | Result |
|-------------|----------|------|--------|
| Customer Orders tab | Orders listed with totals | `tests/test_backoffice.py > test_customer_orders_list_and_detail_include_ars_totals_and_snapshots` (total 256.50, conversion_pending False) + seven-tab structure `test_build_app_creates_seven_tabs_with_expected_labels` | ✅ COMPLIANT |
| Customer Orders tab | Line detail per order | `tests/test_backoffice.py > test_customer_orders_list_and_detail_include_ars_totals_and_snapshots` (SKU/name/quantity/prices/source per line) | ✅ COMPLIANT |
| Exchange rate maintenance | Rate edited | `tests/test_backoffice.py > test_exchange_rate_rejects_ars_and_persists_usd` (USD 950.12345 stored at 4dp) + `tests/test_backoffice.py > test_app_rate_save_updates_timestamp_and_recomputes_pending_order` (timestamps 2024-01-01 → 2024-06-01 asserted; app-level save triggers recompute; DB totals filled) | ✅ COMPLIANT |
| Exchange rate maintenance | ARS rate not editable | `tests/test_backoffice.py > test_exchange_rate_rejects_ars_and_persists_usd` (`pytest.raises(ValueError, match="ARS.*read-only")`) | ✅ COMPLIANT |
| Default margin maintenance | Default margin edited | `tests/test_backoffice.py > test_default_margin_round_trips` (set 27.50 persists) + `tests/test_finalize.py > test_default_margin_edit_prices_subsequent_chat_finalize` (new chat finalize priced 100 → 127.50 with the edited value) | ✅ COMPLIANT |
| Default margin maintenance | Seed value present | `tests/test_db_models.py > test_migration_seeded_default_margin_is_read_by_pricing` (fresh migration, first read equals 20, no test-inserted row, feeds `compute_order`) | ✅ COMPLIANT |

**clients-and-price-lists (delta)** — 1 requirement, 3 scenarios
| Requirement | Scenario | Test | Result |
|-------------|----------|------|--------|
| Attach customer by name at finalization | Exact name match | `tests/test_customers.py > test_resolve_exact_name_auto_picks` + `tests/test_finalize.py > test_finalize_local_draft_uses_cost_margin_and_clears_draft` | ✅ COMPLIANT |
| Attach customer by name at finalization | Ambiguous name offers menu | `tests/test_finalize.py > test_finalize_ambiguous_customer_keeps_menu_and_draft` + `tests/test_customers.py > test_resolve_ambiguous_name_lists_candidates` | ✅ COMPLIANT |
| Attach customer by name at finalization | No match offers creation | `tests/test_finalize.py > test_finalize_unknown_customer_then_create_attaches_waiting_draft` + `tests/test_customers.py > test_resolve_unknown_name_offers_creation` | ✅ COMPLIANT |

**supplier-management (delta)** — 1 requirement, 4 scenarios
| Requirement | Scenario | Test | Result |
|-------------|----------|------|--------|
| Default margin scope (MODIFIED) | Margin applies to future ingestion | `tests/test_backoffice.py > test_confirm_items_creates_new_product_for_unknown_sku` (3200 × 1.10 supplier margin) | ✅ COMPLIANT |
| Default margin scope (MODIFIED) | Margin edit does not re-price | `tests/test_suppliers_backoffice.py > test_margin_edit_does_not_reprice_existing_catalog_rows` | ✅ COMPLIANT |
| Default margin scope (MODIFIED) | Margin applies to RAG order lines | `tests/test_order_pricing.py > test_rag_supplier_margin_and_default_margin_are_source_aware` + `tests/test_finalize.py > test_default_margin_edit_prices_subsequent_chat_finalize` + wiring `_supplier_margin_source` (customer.py:442) | ✅ COMPLIANT |
| Default margin scope (MODIFIED) | Persisted orders stay frozen | `tests/test_draft_order.py > test_supplier_margin_edit_keeps_persisted_order_lines_frozen` (margin 0.25 → persist 125.00 → edit to 0.50 → snapshots and totals unchanged) | ✅ COMPLIANT |

**Compliance summary**: 33/33 scenarios compliant (0 PARTIAL, 0 UNTESTED); requirements fully covered 14/14.

### Correctness (Static Evidence)
| Requirement | Status | Notes |
|------------|--------|-------|
| Finalize draft into a persisted order | ✅ Implemented | `_run_finalize_turn` (customer.py): create-command interception, disambiguation pick, session-customer branch, name resolution, `MissingRateError` → `pending_order`; persist via `persist_draft_order` + draft cleared; all four customer-resolution branches handler-tested |
| Source-aware base pricing at finalize | ✅ Implemented | LOCAL: `costo_proveedor` × `margen_aplicado_pct` via `engine.compute_base`; RAG: offer price → ARS → supplier margin (`codigo_proveedor` → `suppliers.code`) or default margin setting; `_rag_price` endpoint fallback wired (customer.py:455-466) and handler-proven |
| Frozen RAG line snapshots | ✅ Implemented | `PricingLine`/`PricedLine` carry snapshot fields; `OrderItem` name/source/supplier/moneda/precio_original nullable columns; no `catalogo` FK; `_stored_sku` (draft_order.py:21) normalizes doubled prefixes at persist — now persisted-level proven |
| Exchange-rate conversion | ✅ Implemented | `_rate_source` reads `exchange_rates`; missing non-ARS rate → `MissingRateError` → pending order with NULL totals + `conversion_pending=True`; `recompute_pending_conversion` fills totals |
| Save side effects | ✅ Implemented | `persist_draft_order` reserves stock only for LOCAL; Sheets stays on `register_approved_order`; `PendingConversionError` blocks approval of pending orders |
| Order retrieval with lines and totals | ✅ Implemented | `list_customer_orders` / `order_detail` in `src/backoffice/customer_orders.py` |
| pricing-engine deltas | ✅ Implemented | Pure `src/pricing/order_pricing.py`, injectable rate/margin sources, HALF_UP to cents, `compute_order`/`pending_order` |
| backoffice deltas | ✅ Implemented | 7th "Customer Orders" tab, ARS read-only, rate/margin maintenance, app-level `_save_exchange_rate` → `recompute_pending_conversion` wiring (app.py:398) now directly tested |
| clients-and-price-lists delta | ✅ Implemented | Name resolution (exact → case-folded → menu) reusing `resolve_customer_name`; minimal creation on Base list; phone-based identification not used for finalize |
| supplier-management delta | ✅ Implemented | `default_margin_pct` consumed at ingestion and order time; edits touch only the supplier row; persisted snapshots proven frozen |

### Coherence (Design)
| Decision | Followed? | Notes |
|----------|-----------|-------|
| Local margin source = `Catalogo.margen_aplicado_pct` (never `precio_lista_base`, no double margin) | ✅ Yes | `precio_lista_base` 999 vs base 135 proven at runtime |
| RAG base = offer → ARS → ×(1+supplier margin); unmapped → setting | ✅ Yes | order_pricing.py converts before margin; `_as_fraction` normalizes points/fractions (documented deviation in apply-progress) |
| Pending conversion = `conversion_pending` bool + nullable totals | ✅ Yes | Disambiguates from legacy NULL-total Case A orders |
| Default-margin storage in `app_settings`, seed 20 | ✅ Yes | Migration seeds ARS=1.0000 + margin=20; seed test proves the row reaches pricing; edit test proves edited value prices subsequent chat finalizes |
| Sheets sync deferred to `register_approved_order` | ✅ Yes | `test_sheets_append_belongs_to_approval_not_draft_persistence` pins the boundary |
| RAG price fallback via sibling-service endpoint (not direct DB) | ✅ Yes | `RagProductClient.price_lookup` + `_rag_price` wiring, now exercised handler-level with the client mocked (owner decision honored; live RAG never required) |
| Draft reachability: route draft-carrying state → CUSTOMER; drop `order_id is None` gate | ✅ Yes | `test_draft_carrying_state_routes_to_customer_before_sales_or_disambiguation`; `test_add_intent_without_open_order_starts_draft` |
| Additive reversible migration | ✅ Yes | `test_customer_order_migration_round_trips_and_keeps_case_a_persistable` (down to `46bdbdc4a575`, up to `7d2f4a1e8b90`, legacy Case A persists) |

### Issues Found
**CRITICAL**: None (all 3 historical CRITICALs remain resolved; all 5 PARTIAL scenarios from run #2 are now closed with passing runtime coverage; quality gates clean/baseline).

**WARNING**:
1. Size — authored working-tree diff vs e261524 (generated `docs/escenarios-testeados.md` excluded) is now **2493 insertions / 48 deletions = 2541 changed lines**, above the owner-accepted ~2083-line exception and above the 2369 figure recorded in apply-progress.md. The 172-line delta is exactly the 5 covering tests added after that refresh; apply-progress.md's "fresh measurement" is stale. No trimming was attempted per apply policy (verify phase does not fix artifacts).

**SUGGESTION**:
1. Refresh apply-progress.md's size figures to the current measurement (2541 authored lines, 2569 total including the 28-line generated-docs delta) before opening the PR.
2. Keep mocking RAG at the client boundary in CI; `localhost:8001` remains slow/known and no verification claim depends on it (already honored by the new fallback test).

### Verdict
PASS WITH WARNINGS — fresh runtime evidence: 577/577 tests passed (0 failed, 0 skipped), `ruff check src tests` clean at exit 0, `mypy src` at exactly its 3 pre-existing baseline errors (zero new). All 33 scenarios across 5 capability specs now have passing covering tests at runtime (14/14 requirements fully covered); every finding from verify runs #1 and #2 is re-checked and resolved, with the sole remaining warning being the owner-accepted size exception being exceeded further (reported with corrected figures, not fixed here). The change is ready for archive from the verification standpoint.
