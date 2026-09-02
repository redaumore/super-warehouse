# Apply Progress — rag-product-query

- **Phase**: sdd-apply (hybrid persistence: OpenSpec + Engram)
- **Date**: 2026-09-02
- **Mode**: Standard (strict_tdd=false per openspec/config.yaml + Engram #104)
- **Delivery**: exception-ok — owner accepted `size:exception` (single PR ~1050 authored lines; review budget 2000)
- **Baseline**: full suite green before work (docker `super-warehouse-db` up); unrelated uncommitted backoffice work left untouched
- **Note**: task 4.1 (pipeline wiring) was implemented in the W3 commit — the `build_handler` seam change to `ProductSearcher` requires the wiring change in the same commit for `mypy` to stay green (work-unit commits must leave the repo sensible).

## Task Status

### Phase 1: W1 — RAG client foundation (commit `47c8470`)

- [x] 1.1 `rag_*` settings in `src/config.py` + `.env.example` RAG section (base URL default `http://localhost:8001`, timeout 10, top_n 3, threshold 0.45, table `catalogo_productos_rag`, model `gpt-4o`)
- [x] 1.2 `tests/test_rag.py::test_normalize_rag_sku` — 5 parametrized cases (double/triple prefix, no-double, empty provider)
- [x] 1.3 `src/integrations/rag.py` — `RagProduct`, `RagProductError`/`RagProductNotConfigured`, lazy `_ClientHolder`, `RagProductClient.query(text)` POST `/api/v1/query` with `structured_json=true`; injectable client AND transport; maps `structured_json.productos[]` (SKU hygiene: prefer `codigo_orig`, fallback normalized `codigo`)
- [x] 1.4 `httpx.MockTransport` tests: success mapping + request payload assertion; refusal→empty; empty products→empty; missing name skipped; `ConnectError`/`ReadTimeout`/HTTP 500/malformed JSON→`RagProductError`; no base URL→`RagProductNotConfigured`; injected client used directly

### Phase 2: W2 — Precedence chain + parser (commit `b7f8b9d`)

- [x] 2.1 `tests/test_product_search.py::test_parse_product_add` — 12 parametrized cases (`agregalo`→(0,1), `sumá 5 de eso`→(0,5), `el 2`→(1,1), variants, out-of-range/empty→None)
- [x] 2.2 `src/agents/product_search.py` — `ProductSource` (LOCAL|RAG|NONE|ERROR), `ProductEntry`, `ProductSearchResult`, `LocalSearcher`/`ProductSearcher` Protocols, `PrecedenceProductSearcher(local, client, floor=0.65)`
- [x] 2.3 Precedence tests: local hit (≥floor) skips RAG; below-floor→RAG; empty+RAG→RAG with field mapping; empty+refusal→NONE; empty+`RagProductError`→ERROR; local `SQLAlchemyError`→RAG fallback; both down→ERROR; real `RagProductClient` through the chain normalizes `AMX-AMX-AT-5044`

### Phase 3: W3 — Customer note + state + add-intent (commit `43347b8`)

- [x] 3.1 `ConversationState.product_options` / `draft_items` in `src/orchestrator/session.py`
- [x] 3.2 `customer.py`: seam→`ProductSearcher`; `catalog_context_note`→`product_context_note(query, result, draft=())` per ADR 5 (LOCAL `nombre (sku)`; RAG numbered cheapest-first + fields + footer; dual local-first labeled; NONE reformulation; ERROR unavailability; no stock claim); chain never raises; local `SQLAlchemyError` still falls back to RAG (chain-level)
- [x] 3.3 Add-intent short-circuit: `parse_product_add(text, state.product_options)` — no `order_id`→`OFFER_TO_CREATE_REPLY`; with `order_id`→append `(entry, qty)` to `draft_items`; `product_options` cleared; LLM bypassed
- [x] 3.4 Tests: 25 in `test_customer.py` (LOCAL/RAG/NONE/ERROR/dual shapes, cheapest-first sort, footer, SKU hygiene in draft path, add-intent paths, note placement, DB-error skip), RAG-fallback E2E in `test_pipeline.py`; `test_webhook.py` seam rename fixed

### Phase 4: W4 — Wiring + E2E + docs (commit `4834454`; 4.1 in `43347b8`)

- [x] 4.1 `pipeline.py` default searcher `PrecedenceProductSearcher(DbCatalogSearcher(), RagProductClient())`; `build_orchestrator(searcher=...)` injectable
- [x] 4.2 `test_pipeline_owner.py` injects `FakeProductSearcher`; 3 owner-flow tests pass unchanged (no regression)
- [x] 4.3 `make test-docs && make check-test-docs` green; `test_rag`/`test_product_search` registered in `scripts/gen_test_scenarios.py` DOMAINS (279 scenarios)
- [x] 4.4 `docs/architecture.md` "Product queries: local-first → RAG" paragraph + 8000↔8001 drift flag
- [x] 4.5 Final: `pytest -q` 548 passed; `make lint` clean; `make check-test-docs` clean; `ruff format --check` clean on all 12 touched files; `make typecheck` = 3 PRE-EXISTING errors in `src/backoffice/app.py` (verified pre-existing on HEAD via detached worktree — caused by unrelated uncommitted working-tree changes, NOT this change)

## Work Unit Evidence

| Unit | Focused test command + exact result | Runtime harness command/scenario + exact result | Rollback boundary |
|------|--------------------------------------|--------------------------------------------------|-------------------|
| W1 | `pytest tests/test_rag.py -q` → 17 passed | `pytest -q` (full suite after W1+W2) → green; live RAG service NOT hit (transport stubbed by design) | Revert `src/integrations/rag.py` + the `rag_*` settings in `src/config.py`/`.env.example` |
| W2 | `pytest tests/test_product_search.py -q` → 20 passed | `pytest -q` → green | Revert `src/agents/product_search.py` |
| W3 | `pytest tests/test_customer.py tests/test_pipeline.py -q` → 48 passed | `pytest -q` → 548 passed (full) | Revert `customer.py` note logic + `product_options`/`draft_items` fields in `session.py` |
| W4 | `pytest tests/test_pipeline_owner.py -q` → 3 passed (Postgres-gated, DB up) + `make check-test-docs` → al día | `make db-up && pytest -q` → 548 passed | Revert `pipeline.py` wiring to `DbCatalogSearcher` |

Runtime note: the RAG HTTP boundary is exercised via `httpx.MockTransport` at the client/chain level; no live call to `localhost:8001` is made (service not required for the suite).

## Deviations

1. **Task 3.2 vs design data-flow note**: design says local `SQLAlchemyError` propagates to `build_handler` ("RAG is not a DB-outage fallback"); task 3.2 says "chain never raises; SQLAlchemyError from local hop still calls RAG". Implemented the TASK behavior at chain level (local `SQLAlchemyError` → logged → RAG fallback). The handler still catches a direct `SQLAlchemyError` from any searcher (skips note, keeps reply) per the design's handler-level contract. Flagged for orchestrator/verify.
2. **Task 4.1 moved into W3 commit** — required for mypy-green work-unit commits (seam change and wiring are one unit).
3. **`product_context_note` signature** extended with `draft=` kwarg (design shows `(query, result)`) — required by ADR 1 (dual-source notes arise from accumulated drafts across queries) and spec scenario "Dual-source note lists local first".
4. **RAG note does not display SKU** (ADR 5 template has no `sku` field) — the SKU-hygiene spec scenario is proven at client + chain level, and at customer level via the draft path (entry carries normalized `AMX-AT-5044`, never `AMX-AMX-AT-5044`).

## Issues Found

- `make typecheck` fails with 3 errors in `src/backoffice/app.py` — PRE-EXISTING (present on HEAD before this change; caused by unrelated uncommitted working-tree changes). This change's files are mypy-clean.
- No other blockers; no spec/design contradiction beyond deviation 1.