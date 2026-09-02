# Tasks: RAG-backed product queries (local-first → RAG-fallback)

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~1050 authored (range 950–1150) + ~400 generated |
| 400-line budget risk | High |
| Chained PRs recommended | No (single-pr; needs `size:exception`) |
| Suggested split | Single PR, work-unit commits W1→W4 |
| Delivery strategy | single-pr |
| Chain strategy | size-exception |

Decision needed before apply: Yes
Chained PRs recommended: No
Chain strategy: size-exception
400-line budget risk: High

W1≈380, W2≈400, W3≈240, W4≈80 (excludes generated test-docs). Needs `size:exception`.
Delivery: exception-ok accepted by owner (single PR ~1050 lines) — recorded in apply-progress.

### Suggested Work Units

| Unit | Goal | Focused test command | Runtime harness | Rollback boundary |
|------|------|----------------------|-----------------|-------------------|
| W1 | RAG client + SKU hygiene + settings | `pytest tests/test_rag.py -q` | `pytest -q` | Revert `src/integrations/rag.py` + settings |
| W2 | Precedence chain + `parse_product_add` | `pytest tests/test_product_search.py -q` | `pytest -q` | Revert `src/agents/product_search.py` |
| W3 | Note rendering + state + add-intent | `pytest tests/test_customer.py tests/test_pipeline.py -q` | `pytest -q` | Revert `customer.py` note logic + state fields |
| W4 | Wiring + E2E + docs regen | `pytest tests/test_pipeline_owner.py -q && make check-test-docs` | `make db-up && pytest -q` | Revert `pipeline.py` wiring to `DbCatalogSearcher` |

Threat matrix N/A per design.md.

## Phase 1: W1 — RAG client foundation

- [x] 1.1 Add `rag_base_url` (default `http://localhost:8001`), `rag_timeout_seconds` (10), `rag_top_n` (3), `rag_threshold` (0.45), `rag_table_name`, `rag_model` to `Settings` + `.env.example`.
- [x] 1.2 RED `tests/test_rag.py`: parametrized `normalize_rag_sku` (`AMX-AMX-AT-5044`→`AMX-AT-5044`, no-double).
- [x] 1.3 GREEN `src/integrations/rag.py`: `RagProduct`, `RagProductError`/`RagProductNotConfigured`, lazy `_ClientHolder`, `RagProductClient.query(text)` POST `/api/v1/query` `structured_json=true`; injectable transport.
- [x] 1.4 RED→GREEN `tests/test_rag.py` via `httpx.MockTransport`: success mapping; refusal→empty; `ConnectError`/`ReadTimeout`/500→`RagProductError`.

## Phase 2: W2 — Precedence chain + parser

- [x] 2.1 RED `tests/test_product_search.py`: parametrized `parse_product_add` — `agregalo`→(0,1), `sumá 5 de eso`→(0,5), `el 2`→(1,1).
- [x] 2.2 GREEN `src/agents/product_search.py`: `ProductSource` (LOCAL|RAG|NONE|ERROR), `ProductEntry`, `ProductSearchResult`, `LocalSearcher` Protocol, `PrecedenceProductSearcher(local, client, floor=0.65)`.
- [x] 2.3 RED→GREEN precedence: local hit skips RAG; empty+RAG→RAG; empty+refusal→NONE; empty+`RagProductError`→ERROR.

## Phase 3: W3 — Customer note + state + add-intent

- [x] 3.1 Add `product_options`, `draft_items` to `ConversationState` in `src/orchestrator/session.py`.
- [x] 3.2 Modify `src/agents/customer.py`: seam→`ProductSearcher`; rename `catalog_context_note`→`product_context_note(query, result)` per ADR 5 templates (RAG cheapest-first + supplier-catalog footer; LOCAL `nombre_oficial (sku)`; dual local-first + labels; NONE/ERROR no stock claim); chain never raises; SQLAlchemyError from local hop still calls RAG.
- [x] 3.3 Add-intent short-circuit: `parse_product_add(text, state.product_options)` match — no `order_id`→offer-to-create reply; with `order_id`→append `(entry,qty)` to `draft_items`; clear `product_options`.
- [x] 3.4 RED→GREEN `tests/test_customer.py` + `tests/test_pipeline.py`: fake `ProductSearcher`→`ProductSearchResult`; assert LOCAL/RAG/NONE/ERROR/dual-source shapes per spec scenarios; refusal→NONE + reformulation; AMX-AMX-AT-5044 normalized; `agregalo`/`sumá 5`/`el 2`/no-open-order paths; update 3 note tests; add RAG-fallback E2E.

## Phase 4: W4 — Wiring + E2E + docs

- [x] 4.1 `src/pipeline.py`: default searcher=`PrecedenceProductSearcher(DbCatalogSearcher(), RagProductClient())`; keep injectable.
- [x] 4.2 `tests/test_pipeline_owner.py`: inject fake `ProductSearcher`; assert no regression in owner-flow tests.
- [x] 4.3 `make test-docs && make check-test-docs` (pre-commit).
- [x] 4.4 One paragraph in `docs/architecture.md` on local-first→RAG + ERROR≠"no stock"; flag 8000↔8001 drift.
- [x] 4.5 Final: `make db-up && pytest -q && make lint && make typecheck && make check-test-docs`.