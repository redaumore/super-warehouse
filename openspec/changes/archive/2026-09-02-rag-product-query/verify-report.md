# Verification Report — rag-product-query

- **Change**: rag-product-query
- **Date**: 2026-09-02
- **Mode**: Full artifacts (proposal + specs + design + tasks)
- **Verifier**: sdd-verify phase worker

## Completeness Table

| Dimension | Status | Notes |
|-----------|--------|-------|
| Tasks complete | ✅ All 16/16 checked | 4 work units (W1–W4), all [x] |
| Spec correctness | ✅ Verified | 9 requirements, 16 scenarios — all traced |
| Design coherence | ✅ Verified | 4 deviations adjudicated (see below) |
| Runtime evidence | ✅ 548 passed | Baseline met; no regressions |

## Build / Test / Coverage Evidence

| Command | Exit | Result |
|---------|------|--------|
| `pytest -q` | 0 | **548 passed**, 2 warnings (alembic deprecation), 15.46s |
| `make lint` (ruff check) | 0 | All checks passed |
| `ruff format --check` (12 touched files) | 0 | 12 files already formatted |
| `make typecheck` (mypy src) | 1 | **3 PRE-EXISTING errors** in `src/backoffice/app.py` (lines 17, 183, 187) — verified identical on stashed HEAD; unrelated to this change |
| `make check-test-docs` | 0 | `docs/escenarios-testeados.md` is up to date (279 scenarios) |

**Baseline**: 548 passed (pre-change). Post-change: 548 passed. Delta: 0.

## Spec Compliance Matrix

### rag-product-query (8 requirements, 13 scenarios)

| # | Requirement | Scenario | Test(s) | Status |
|---|-------------|----------|---------|--------|
| 1 | RAG product client contract | Successful product query | `test_rag_client_query_maps_products_and_sends_structured_json` | ✅ PASS |
| 1 | RAG product client contract | Transport failure raises domain error | `test_rag_client_connect_error_raises_domain_error`, `test_rag_client_read_timeout_raises_domain_error`, `test_rag_client_http_500_raises_domain_error`, `test_rag_client_malformed_json_raises_domain_error` | ✅ PASS |
| 2 | Local-first → RAG-fallback precedence chain | Local hit skips RAG | `test_local_hit_skips_rag` | ✅ PASS |
| 2 | Local-first → RAG-fallback precedence chain | Empty local search falls back to RAG | `test_empty_local_falls_back_to_rag_and_maps_fields` | ✅ PASS |
| 3 | Source-discriminated results | Source set per outcome | `test_local_hit_skips_rag` (LOCAL), `test_empty_local_falls_back_to_rag_and_maps_fields` (RAG), `test_empty_local_with_refusal_is_none` (NONE), `test_empty_local_with_rag_error_is_error` (ERROR) | ✅ PASS |
| 4 | Source-aware note rendering | RAG results rendered numbered, cheapest first | `test_rag_results_note_numbered_cheapest_first_with_fields_and_footer` | ✅ PASS |
| 4 | Source-aware note rendering | Dual-source note lists local first | `test_dual_source_note_lists_local_first_labeled` | ✅ PASS |
| 5 | Refusal suggests reformulation | Refusal suggests reformulation | `test_refusal_none_note_suggests_reformulation_without_stock_claim` | ✅ PASS |
| 6 | RAG unavailability notice | RAG down produces unavailability notice | `test_error_note_states_catalogs_unavailable_without_stock_claim` | ✅ PASS |
| 7 | Order-building integration | Natural-phrase add | `test_add_intent_with_open_order_appends_draft_and_clears_options`, `test_add_intent_with_quantity_appends_draft_with_qty` | ✅ PASS |
| 7 | Order-building integration | Numbered reference disambiguates | `test_add_intent_numbered_reference_picks_displayed_result` | ✅ PASS |
| 7 | Order-building integration | No open order offers one | `test_add_intent_without_open_order_offers_to_create` | ✅ PASS |
| 8 | RAG SKU hygiene | Double-prefixed SKU sanitized | `test_normalize_rag_sku` (5 cases), `test_rag_note_never_leaks_raw_double_prefix_codigo`, `test_add_intent_preserves_normalized_rag_sku_in_draft`, `test_chain_normalizes_sku_from_real_client` | ✅ PASS |

### catalog-search (1 requirement, 3 scenarios)

| # | Requirement | Scenario | Test(s) | Status |
|---|-------------|----------|---------|--------|
| 1 | Report no-match results | No product found | `test_product_query_with_none_result_injects_not_found_note` | ✅ PASS |
| 1 | Report no-match results | Empty local search falls back to RAG | `test_rag_fallback_result_reaches_responder_as_source_note` (pipeline E2E) | ✅ PASS |
| 1 | Report no-match results | No-match does not block the rest of the order | Existing sourcing flow tests (`test_pipeline_owner.py`) — RAG change doesn't modify sourcing path; unmatched items handled by existing Case B/C classification | ✅ PASS (preserved) |

**Totals**: 9 requirements, 16 scenarios — **16/16 PASS**.

## Deviation Adjudication

### Deviation 1: Task 3.2 vs design data-flow note (SQLAlchemyError handling)

- **Design**: Local `SQLAlchemyError` propagates to `build_handler` ("RAG is not a DB-outage fallback").
- **Task 3.2**: "chain never raises; SQLAlchemyError from local hop still calls RAG".
- **Implementation**: Chain-level catch (`PrecedenceProductSearcher.search` catches `SQLAlchemyError` → RAG fallback). Handler-level catch (`build_handler` catches `SQLAlchemyError` from any searcher → skips note, keeps reply).
- **Verdict**: ✅ **Spec intent satisfied**. The system degrades gracefully: DB down → RAG fallback at chain level; if both fail → ERROR source → unavailability note. The handler never crashes. `test_local_sqlalchemy_error_still_calls_rag` and `test_searcher_database_error_skips_note_and_keeps_reply` prove both layers.

### Deviation 2: `product_context_note` signature extended with `draft=` kwarg

- **Design**: `(query, result)`.
- **Implementation**: `(query, result, draft=())`.
- **Reason**: ADR 1 — dual-source notes arise from accumulated drafts across queries. Spec scenario "Dual-source note lists local first" requires this.
- **Verdict**: ✅ **ADR 1 and spec scenario satisfied**. `test_dual_source_note_lists_local_first_labeled` proves the dual-source path.

### Deviation 3: Task 4.1 moved into W3 commit

- **Reason**: `build_handler` seam change to `ProductSearcher` requires wiring change in same commit for mypy-green work-unit commits.
- **Verdict**: ✅ **No behavioral impact**. Commit organization only.

### Deviation 4: RAG note does not display SKU field

- **Design ADR 5 template**: No `sku` field in RAG note rendering.
- **Spec scenario**: "Double-prefixed SKU sanitized" — proven at client boundary (`normalize_rag_sku`), chain level (`test_chain_normalizes_sku_from_real_client`), and draft path (`test_add_intent_preserves_normalized_rag_sku_in_draft`).
- **Verdict**: ✅ **Spec scenario satisfied**. The SKU is normalized before it reaches the note; the note never renders raw `codigo`. `test_rag_note_never_leaks_raw_double_prefix_codigo` proves no double-prefix leaks.

## Design Coherence Table

| Design Decision | Implementation | Coherent? |
|----------------|----------------|-----------|
| `RagProductClient` in `integrations/rag.py`; sync httpx; injectable transport | ✅ Matches | Yes |
| `PrecedenceProductSearcher` → `ProductSearchResult(source, entries)` | ✅ Matches | Yes |
| Source enum `LOCAL|RAG|NONE|ERROR` | ✅ Matches | Yes |
| SKU hygiene: `normalize_rag_sku` at client; prefer `codigo_orig` | ✅ Matches | Yes |
| Note: English source-aware templates, numbered, cheapest-first | ✅ Matches | Yes |
| Order-building: `parse_product_add` + `product_options`/`draft_items` | ✅ Matches | Yes |
| Observability: logs at client + chain | ✅ Matches (`logger.info`/`logger.warning`) | Yes |
| Wire `PrecedenceProductSearcher(DbCatalogSearcher(), RagProductClient())` in pipeline | ✅ Matches | Yes |

## Out-of-Scope Check

| Path pattern | Touched? |
|--------------|----------|
| `src/supplier/*` | ❌ Not touched |
| `src/sourcing/*` | ❌ Not touched |
| `alembic/*` | ❌ Not touched |
| `openspec/specs/*` (base specs) | ❌ Not touched |

Changed files (16): `.env.example`, `docs/architecture.md`, `docs/escenarios-testeados.md`, `scripts/gen_test_scenarios.py`, `src/agents/customer.py`, `src/agents/product_search.py`, `src/config.py`, `src/integrations/rag.py`, `src/orchestrator/session.py`, `src/pipeline.py`, `tests/test_customer.py`, `tests/test_pipeline.py`, `tests/test_pipeline_owner.py`, `tests/test_product_search.py`, `tests/test_rag.py`, `tests/test_webhook.py`. All in scope.

## Live Smoke Check (RAG Service)

- **Endpoint**: `http://localhost:8001/api/v1/query`
- **Status**: ✅ Reachable (HTTP 200)
- **Query**: `"tarugos"` with `structured_json=true`
- **Response shape**: `is_refusal=True`, `productos=[]` (0 products) — valid refusal response
- **Observation**: Service is live and returns the expected envelope. The refusal path is exercised by the test suite via `httpx.MockTransport`; the live service confirms the real endpoint responds with the same shape.

## Issues

### CRITICAL

None.

### WARNING

1. **`make typecheck` exits non-zero** — 3 pre-existing mypy errors in `src/backoffice/app.py` (lines 17, 183, 187). Verified identical on stashed HEAD before this change. Not caused by this change; not blocking.

### SUGGESTION

1. **Port drift documentation** — `docs/architecture.md` now flags the 8000↔8001 drift. Consider updating any remaining references to port 8000 in other docs.

## Final Verdict

**PASS**

All 16 spec scenarios are covered by passing tests. All 16 tasks are complete. Design coherence is verified across all 8 decisions. No out-of-scope files were touched. The 3 mypy errors are pre-existing and unrelated. The RAG service is live and returns the expected response shape.
