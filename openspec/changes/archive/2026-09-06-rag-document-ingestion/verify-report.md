```yaml
schema: gentle-ai.verify-result/v1
evidence_revision: sha256:87fd0a3ad267aef810b272cf293e0e31ae385421670a879329bc6586c00e1efb
verdict: pass_with_warnings
blockers: 0
critical_findings: 0
requirements: 14/14
scenarios: 30/30
test_command: .venv/bin/python -m pytest -q
test_exit_code: 0
test_output_hash: sha256:fb04cd268391d77c00f6ea657155f3c114ce3d5c38c5da945b5ab9068b64ca07
build_command: .venv/bin/ruff check src tests
build_exit_code: 0
build_output_hash: sha256:82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18
```

## Verification Report

**Change**: rag-document-ingestion
**Version**: branch `rag-document-ingestion` @ `baa0595` (apply HEAD `fe2a2b1` + verify remediation commit)
**Mode**: Standard (strict_tdd=false)

**Spec count note**: the authoritative native heading count is **14 requirements / 30 scenarios** (`### Requirement:` / `#### Scenario:` across the 5 delta-spec domains). The orchestrator briefing stated 13/24 — a mismatch named here per contract; totals in this report use the counted 14/30. The 14th requirement is the REMOVED one in `supplier-document-ingestion` (verified as implemented removal, no scenarios).

### Completeness
| Metric | Value |
|--------|-------|
| Tasks total | 19 |
| Tasks complete | 19 |
| Tasks incomplete | 0 |

### Build & Tests Execution
**Build/static**: ✅ ruff clean (exit 0); mypy `src` = 8 errors, ALL pre-existing — baseline verified on `main` tip `473378f` via worktree = 10 errors, 0 introduced by this change.
```text
.venv/bin/ruff check src tests → All checks passed! (exit 0)
.venv/bin/mypy src → Found 8 errors in 2 files (baseline 10 on main; dispatch.py/app.py pre-existing)
```

**Tests**: ✅ 842 passed / 0 failed / 0 skipped (Postgres `super-warehouse-db` pgvector:pg16 was UP — DB-dependent integration tests RAN, not skipped). rag-api suite: 32 passed via `services/rag-api/.venv` (pymupdf present there).
```text
.venv/bin/python -m pytest -q → 842 passed, 10 warnings in 39.29s (exit 0)
services/rag-api/.venv/bin/python -m pytest services/rag-api/tests -q → 32 passed
```

**Apply-claim comparison**: apply claimed 841 passed / rag-api 28 / ruff clean / mypy 8 pre-existing. Independent re-run: 841 → 842 and 28 → 32 because this verification ADDED 5 covering tests (remediation `baa0595`, +120 changed lines, within the 300-line budget). All other claims reproduced exactly.

### Spec Compliance Matrix
Statuses: ✅ COMPLIANT (covering test passed at runtime). Remediated = scenario had no covering test at apply HEAD; covered by verify remediation commit `baa0595`.

| Domain | Requirement | Scenario | Covering test | Result |
|---|---|---|---|---|
| rag-document-ingestion | REQ-1 Supplier-first ingestion selection | Supplier selected first | `tests/test_backoffice.py > test_active_supplier_choices_lists_activo_by_business_name` (REMEDIATED) | ✅ |
| rag-document-ingestion | REQ-1 | No free numeric ID | `tests/test_backoffice.py > test_build_app_ingestion_tab_has_dropdown_and_no_numeric_id` | ✅ |
| rag-document-ingestion | REQ-2 RAG document parse endpoint | Successful parse | `services/rag-api/tests/test_ingestion.py > test_ingest_parse_success_returns_lines_and_writes_nothing` | ✅ |
| rag-document-ingestion | REQ-2 | Parse failure returns error without writes | `services/rag-api/tests/test_ingestion.py > test_ingest_parse_failure_returns_structured_error_and_writes_nothing` | ✅ |
| rag-document-ingestion | REQ-3 Exact-code-first resolution | Exact hit resolves directly | `tests/test_backoffice.py > test_resolve_lines_exact_hit_resolves_without_hybrid` (asserts `query_calls == []`) | ✅ |
| rag-document-ingestion | REQ-3 | Exact miss falls back to hybrid | `tests/test_backoffice.py > test_resolve_lines_exact_miss_falls_back_to_hybrid_scoped` | ✅ |
| rag-document-ingestion | REQ-4 Review grid + gating | Grid shows resolved and pending | `tests/test_backoffice.py > test_app_ingest_parse_returns_grid_with_resolved_and_pending` | ✅ |
| rag-document-ingestion | REQ-4 | Confirmation blocked while unresolved | `tests/test_backoffice.py > test_app_ingest_confirm_blocked_while_pending` (message lists pending codes) | ✅ |
| rag-document-ingestion | REQ-4 | All lines resolved unlocks confirmation | `tests/test_backoffice.py > test_app_ingest_confirm_unblocked_when_all_resolved` | ✅ |
| rag-document-ingestion | REQ-5 Provenanced persistence | Stock written from matched row | `tests/test_backoffice.py > test_ingest_updates_existing_stock_keeps_origen_and_audits`; `tests/test_e2e_ingestion.py > test_e2e_receipt_flow_writes_stock_with_node_id_provenance` | ✅ |
| rag-document-ingestion | REQ-5 | No creation from model output | `tests/test_e2e_ingestion.py > test_e2e_unmatched_line_blocks_confirm_and_creates_nothing`; `test_ingest_unresolved_positive_line_fails_closed` | ✅ |
| rag-document-ingestion | REQ-5 | Transactional stock update | `tests/test_backoffice.py > test_ingest_embed_failure_rolls_back_whole_confirmation` (seed committed, 2-line batch, rollback restores exact state) | ✅ |
| rag-document-ingestion | REQ-6 RAG/Luna unavailability | RAG/Luna down | `tests/test_e2e_ingestion.py > test_e2e_rag_down_shows_honest_error_and_writes_nothing`; `tests/test_rag.py > test_parse_document_timeout_raises_domain_error` | ✅ |
| supplier-document-ingestion | REQ-1 Extract items/quantities/costs | Remito photo extracted | `services/rag-api/tests/test_ingestion.py > test_ingest_parse_photo_extracts_lines_through_real_parser` (REMEDIATED: real parser + image render) | ✅ |
| supplier-document-ingestion | REQ-1 | Invoice PDF extracted | `services/rag-api/tests/test_ingestion.py > test_ingest_parse_pdf_extracts_lines_through_real_parser` + `test_render_pages_pdf_renders_page_by_page_with_text_and_image` (REMEDIATED: was UNTESTED — real 2-page PDF through `_render_pages` + real parser) | ✅ |
| supplier-document-ingestion | REQ-2 Handle parse failure | Parse fails | `test_ingest_parse_failure_returns_structured_error_and_writes_nothing` (422, zero writes); e2e down test | ✅ |
| supplier-document-ingestion | REQ-2 | Partial extraction flagged | `test_app_ingest_parse_returns_grid_with_resolved_and_pending` + `test_resolve_lines_no_match_stays_pending` | ✅ |
| supplier-document-ingestion | REQ-3 (REMOVED: Map items/SKU suggestions) | — (no scenarios) | `confirm_items`/`extract_document_items`/`to_grid_rows`/`_find_existing_product` verified absent from `src/`; no-creation e2e proves supersession | ✅ |
| manual-rag-product-resolution | REQ-1 Per-line manual search | Manual search returns candidates | `tests/test_backoffice.py > test_app_ingest_manual_search_and_assign_fix_pending` | ✅ |
| manual-rag-product-resolution | REQ-1 | Selection attaches the row | same test (`node_id` attached, grid resolved) | ✅ |
| manual-rag-product-resolution | REQ-2 Resolution never creates | No match keeps line pending | `test_resolve_lines_no_match_stays_pending` + `test_e2e_unmatched_line_blocks_confirm_and_creates_nothing` | ✅ |
| rag-product-query | REQ-1 Parse + exact lookup client | Parse via client | `tests/test_rag.py > test_parse_document_maps_lines_and_sends_multipart` | ✅ |
| rag-product-query | REQ-1 | Exact lookup returns provenance | `tests/test_rag.py > test_exact_lookup_returns_all_matches_with_node_id` | ✅ |
| rag-product-query | REQ-1 | Transport failure raises domain error | `test_parse_document_transport_failure_raises_domain_error` + `test_exact_lookup_transport_failure_raises_domain_error` | ✅ |
| rag-product-query | REQ-1 | Client call respects bounded timeout | `test_parse_document_timeout_raises_domain_error` + `test_exact_lookup_timeout_raises_domain_error` + settings-derived timeout test (`rag_timeout_seconds`, src/config.py:73) | ✅ |
| rag-product-query | REQ-2 Product lookup route by SKU | Price lookup returns single row | `services/rag-api/tests/test_ingestion.py > test_products_sku_single_row` + `tests/test_rag.py > test_price_lookup_200_maps_price_and_supplier_query_parameter` | ✅ |
| rag-product-query | REQ-2 | Price lookup 404 maps to None | `test_products_sku_404_when_no_match` + `test_price_lookup_404_returns_none` | ✅ |
| rag-product-query | REQ-2 | Ingestion resolution returns all matches | `test_products_sku_all_matches_with_node_id` + `test_exact_lookup_404_returns_empty_tuple` | ✅ |
| backoffice | REQ-1 Supplier document ingestion module | Supplier-first upload and preview | `test_build_app_ingestion_tab_has_dropdown_and_no_numeric_id` + `test_app_ingest_parse_returns_grid_with_resolved_and_pending` | ✅ |
| backoffice | REQ-1 | Manual code search fixes pending lines | `test_app_ingest_manual_search_and_assign_fix_pending` + `tests/test_e2e_ingestion.py > test_e2e_manual_search_and_assign_fixes_pending_line` | ✅ |
| backoffice | REQ-1 | Confirm entry to inventory gated | `test_app_ingest_confirm_unblocked_when_all_resolved` + `test_e2e_receipt_flow_writes_stock_with_node_id_provenance` | ✅ |

**Compliance summary**: 30/30 scenarios compliant (28 at apply HEAD + 2 remediated in `baa0595`).

### Correctness (Static Evidence)
| Rule under verification | Status | Evidence |
|---|---|---|
| Exact-code-first before hybrid | ✅ | `src/backoffice/ingestion.py:131-142` `_auto_resolve`: exact → 1 hit returns, miss → hybrid; test asserts `query_calls == []` on exact hit |
| Duplicate >1 → pending, never auto-picked | ✅ | `ingestion.py:138-139` returns None on >1; `test_resolve_lines_duplicate_exact_stays_pending` (also asserts no hybrid run) |
| UPPER(TRIM) normalization | ✅ | Server: `endpoints/ingestion.py:75` `UPPER(TRIM(codigo_orig)) = UPPER(TRIM(%s))`; client: `ingestion.py:133` `.strip().upper()`; `test_resolve_lines_normalizes_codigo_orig_uppercase_trim` asserts `("CLV-001","MSA")` from `"  clv-001  "` |
| Confirmation blocked while positive-qty line unresolved | ✅ | `app.py:385-390` UI gate + `ingestion.py:185-195` `UnresolvedLineError` fail-closed backstop; both tested |
| No Catalogo creation from model output | ✅ | `_adopt_new` requires a resolved RAG `product` (identity/provenance from RAG row, never model-only); unresolved → blocked; e2e asserts no `MSA-PINT-001` row |
| node_id provenance, origen write-once | ✅ | Update path never touches `origen` (`_bump_stock`); adopt path writes `origen={"rag":{node_id,archivo_origen,pagina_origen}}` and requires node_id (`ingestion.py:259-260` fail-closed) |
| Single-transaction rollback | ✅ | One `session.flush()` (`ingestion.py:212`), caller commits (`app.py:400`); rollback test proves full-batch reversal |
| Domain errors, never raw httpx | ✅ | `rag.py:328-332,400-404` `httpx.HTTPError → RagProductError` (parse + exact_lookup); timeout/HTTP-500 mapped too |
| Bounded timeout rag_timeout_seconds | ✅ | `rag.py:193` client built with `settings.rag_timeout_seconds`; settings-derived test asserts 3.5s propagated |
| GET /api/v1/products/{sku} dual contract | ✅ | Route returns array of ALL matches + 404 on empty (`endpoints/ingestion.py:181-184`); `price_lookup` unwraps single row, 404→None (`rag.py:283-284`); `_rag_price` (`customer.py:483`) contract unchanged and tested |
| Parse endpoint no-writes on failure | ✅ | Bytes in memory only (`endpoints/ingestion.py:131`); 422/500 structured errors; `_uploads_count()` unchanged in both failure tests |
| Supplier dropdown ACTIVO-only, business_name, no numeric ID | ✅ | `app.py:224-236` ACTIVO filter + business_name→id; tab has no supplier-ID input (label test); choices content test (remediation) |
| price_lookup / adoption untouched | ✅ | `adoption.py` not in diff; `test_adoption.py` green; price_lookup tests unchanged and passing |

### Coherence (Design)
| Decision | Followed? | Notes |
|---|---|---|
| D1 page-render adapter (PDF pymupdf @200dpi / image → 1 page) | ✅ | `document_parser.py:102-124`; now runtime-tested (remediation) |
| D2 ingestion-only schema (DocumentLine/ParsedDocument, not ExtractedProduct) | ✅ | `schemas/ingest.py` new types; rag-api catalog pipeline untouched |
| D3 new `ingest_receipt_lines` use case; reuse `build_sku` only | ✅ | `ingestion.py:167-213`; `adopt_product` unmodified |
| D4 duplicate/normalization/manual-candidate policy | ✅ | As specified; hybrid supplier-scope achieved via client-side `codigo_proveedor` post-filter (endpoint unscoped) — documented in `hybrid_candidates` docstring, behavior tested |
| D5 add GET /products/{sku}, reuse for exact lookup | ✅ | Route + client reuse implemented |
| W1→W5 work units | ⚠️ | W3+W4 merged into one commit (documented: shared import coupling made them non-independently committable); 4 commits instead of 5 |
| Task 5.2 ocr.py dead-code deferral | ✅ | Deferral is the task as written; removal flagged for PR follow-up |

### Issues Found
**CRITICAL**: none open. Two CRITICAL-UNTESTED gaps were found at apply HEAD and remediated in this verification (commit `baa0595`, test-only, 120 changed lines, all suites re-run green):
1. [sup-doc R1 "Invoice PDF extracted"] `_render_pages` PDF/image path had zero runtime coverage (parse tests patched above rendering). → Added real 2-page PDF + image render tests and real-parser endpoint tests (PDF per-page provenance, photo fields).
2. [rag-doc R1 "Supplier selected first"] `_active_supplier_choices` ACTIVO-only/business_name→id content was never asserted (only dropdown presence). → Added choices-content test.

**WARNING**:
1. mypy `src` reports 8 errors — all pre-existing (baseline 10 verified on main worktree; the 2 old `_ingest_confirm` errors disappeared with the rewrite). Repo carries a non-zero mypy baseline.
2. W3+W4 merged commit (process deviation vs tasks' W-unit plan) — documented, no spec impact.
3. Hybrid supplier-scoping is a client-side post-filter, not a server-side scope — matches D4's observable contract and is tested, but depends on `top_n` recall being large enough to contain the supplier's row (bounded risk, honest docstring).
4. `src/supplier/ocr.py` remito helpers + `tests/test_ocr.py:54-102` are dead code kept per task 5.2 deferral — follow-up cleanup owed in the PR.

**SUGGESTION**:
1. `GET /catalog/exact` and `GET /products/{sku}` are duplicate lookup surfaces; the production client only uses `/products/{sku}` — consider consolidating in a follow-up.
2. `_ingest_confirm`'s catch-all `Exception` handler (app.py:409) could mask unexpected bugs as UI text; acceptable for a UI boundary, monitor in review.

### Verdict
PASS WITH WARNINGS — 14/14 requirements and 30/30 scenarios verified with passing runtime evidence (Postgres up, DB tests executed); 2 coverage gaps remediated in-verification (commit `baa0595`); remaining warnings are non-spec risks.
