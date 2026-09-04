# Exploration: rag-product-query

> Phase: sdd-explore — read-only investigation. 2026-09-02.

## Exploration: RAG-backed product queries with local-first precedence

### Current State

**Product-query path (Telegram/WhatsApp chat, non-sourced turn)**

1. `src/api/webhook.py` ACKs the webhook (<5 s SLA) and dispatches work to a background task — the reply path never touches the ACK latency budget.
2. `src/pipeline.py::handle_inbound` gates the owner, then `ORCHESTRATOR.handle_inbound` routes via `src/orchestrator/router.py::route_message`. A plain chat turn lands on `AgentName.CUSTOMER` (a parsed order turn with `sourcing` wired goes to `_run_sourcing_turn` instead).
3. `src/agents/customer.py::build_handler` builds the message list: `SYSTEM_PROMPT` → history → **transient catalog note** → latest user turn. The note comes from `catalog_context_note(text, candidates)` and is never persisted into history.
4. The searcher seam is the `CatalogSearcher` Protocol (`search(query) -> tuple[SearchCandidate, ...]`). Production wires `DbCatalogSearcher`, which opens a short-lived `SessionLocal` and calls `search_catalog(session, query, limit=3)` from `src/agents/disambiguation.py` (fuzzy rapidfuzz token-sort over `catalogo.nombre_oficial` + `sinonimos`, optionally boosted by pgvector cosine when a query embedding is passed — in production it is not).
5. The handler catches `SQLAlchemyError` and skips the note; any other exception would propagate out of the handler (no catch-all today).
6. `OpenAIResponder` (`src/integrations/openai.py`, gpt-4o-mini) answers over the full list. Session rehydration (`src/orchestrator/session.py::rehydrate_conversation`) re-calls a searcher only for **supplier** candidates (`SupplierCatalogSearcher`), never the catalog searcher.

**Concrete behavior today**: `catalogo` has 0 rows, so `search_catalog` returns `[]` for every query, and the note tells the assistant "sin resultados… decíselo al dueño" (not in stock). This is exactly the behavior the owner wants replaced: product queries must be answered through the RAG "as if there were no inventory", with a seam whose precedence is **local first, RAG fallback**, so a future populated inventory DB wins without refactoring.

**RAG service contract** (sibling repo `fase-0-pdf-parsing`, running on `http://localhost:8001` — the docs in both repos say 8000; the orchestrator confirmed 8001 and `/health` responds there: 160 products, providers AMX/PZF/SON).

- `POST /api/v1/query`, no auth headers. Request (`app/api/schemas/query.py::QueryRequest`): `query` (str, min 1, required), `table_name` (default `catalogo_productos_rag`), `top_n` (default 3, 1–20), `threshold` (default 0.45), `structured_json` (default **false** — must be set `true` for the typed product list), `audit` (default false), `model` (default `gpt-4o`).
- Response: `query`, `response_text`, `is_refusal`, `status` (`SUCCESS | REFUSAL_GROUNDED | EMPTY_CONTEXT | CITATION_MISMATCH`), `citations[]`, `is_fully_grounded`, `structured_json{respuesta_narrativa, consulta_respondida, productos[]}`, `context_chunks[]`, `total_latency_ms`, `model_name`, `evaluation`.
- `productos[]` fields: `codigo` (= `codigo_proveedor`+`codigo_orig`), `codigo_orig`, `codigo_proveedor`, `nombre_proveedor`, `marca`, `nombre`, `categoria_padre/categoria/subcategoria`, `precio`, `moneda`, `unidad_venta`, `empaque`, `especificaciones`, `archivo_origen`, `pagina`, `fragmento_id`.
- Errors: any pipeline exception → HTTP 500 with Spanish detail. `is_refusal: true` + `productos: []` means "not in current catalogs" (RAG ingests only priced, in-stock-at-ingest products — "no existe" and "agotado" are the same, per `docs/especificacion-catalogo-e-inventario.md` §5.3).
- Latency measured live: hit ≈ 4.97 s (first call after cold start ≈ 15 s), refusal ≈ 2.4 s. Pipeline = hybrid BM25+vector → cross-encoder reranker → LLM generation with citation audit (temp 0.0, `max_tokens` 800).
- Data-quality quirk observed: one returned `codigo` was `AMX-AMX-AT-5044` (double provider prefix). The consumer must not assume perfect SKU hygiene.

**Local search behavior today**: with `catalogo` empty, `search_catalog` always returns `[]`. `src/db/models.py`: `Catalogo` (codigo_interno, nombre_oficial, sinonimos, stock_disponible, embedding vector(1536) never populated), `Inventory` (sku_id PK, quantity_on_hand), `Supplier` (8 rows). `src/agents/inventory.py::available_stock` returns 0 for unknown SKUs. Table `products` does not exist (three-layer model is backlog).

**HTTP client pattern**: `httpx>=0.27` is already a dependency. `src/channels/whatsapp.py` uses `httpx.AsyncClient(timeout=30)` inside async code and maps `httpx.HTTPError` to a domain error. `src/integrations/openai.py` is the better template for a RAG client: injectable SDK client in the constructor, lazy build from `Settings` (`_ClientHolder`), domain-specific exceptions (`ResponderNotConfigured`/`ResponderError`), plain sync calls. The Customer handler runs sync code, so a sync `httpx.Client` fits (mirrors `OpenAIResponder`).

**Config**: `src/config.py::Settings` (pydantic-settings, `.env`). No RAG vars today. `.env.example` has sections for each integration — a RAG section (`RAG_BASE_URL`, timeout, top_n) would follow the pattern.

**Test surface**:
- `tests/test_customer.py`: `FakeSearcher` (records queries, configured candidates, can raise) drives note-shape tests (`test_product_query_with_empty_catalog_injects_no_stock_note`, `test_searcher_database_error_skips_note_and_keeps_reply`). No network, no DB.
- `tests/test_search.py`: Postgres-gated integration tests for `search_catalog`/`resolve_item`.
- `tests/test_openai.py`: `MagicMock(spec=OpenAI)` + `SimpleNamespace` fake-SDK pattern; the same pattern works for a mocked RAG client (or `respx`/`httpx.MockTransport`, not yet used in the repo).
- 9 files use `FakeSupplierCatalogSearcher` (supplier seam): `test_case_a/b/c`, `test_classify`, `test_customers`, `test_pipeline_owner`, `test_session_rehydrate_owner`, `test_sourcing_persistence`, `test_supplier_validation`. **Untouched by this change** if we do not touch the supplier seam.
- `tests/test_pipeline.py` builds orchestrators with `FakeSearcher`; production wiring changes must keep `build_orchestrator(searcher=...)` injectable.

### Affected Areas

- `src/agents/customer.py` — `CatalogSearcher` protocol, `build_handler` note injection, `catalog_context_note` rendering; the seam this change extends.
- `src/agents/disambiguation.py` — `SearchCandidate` (sku, nombre_oficial, confidence): the candidate shape the note renders; possibly extended with RAG-sourced fields.
- `src/pipeline.py` — production wiring: today `build_orchestrator(searcher=None) → DbCatalogSearcher()`; would wire the composite/precedence searcher.
- `src/config.py` + `.env.example` — new RAG settings (base URL default `http://localhost:8001`, timeout, top_n).
- `src/integrations/` (new module, e.g. `rag.py`) — HTTP client for `POST /api/v1/query` mirroring `openai.py` patterns.
- `tests/test_customer.py`, `tests/test_pipeline.py` — extended for precedence/failure cases; new tests for the RAG client and composite searcher.
- OpenSpec: `openspec/specs/catalog-search/spec.md` (MODIFIED — search precedence + note behavior), new domain spec `rag-product-query` (ADDED — RAG client contract, precedence, failure handling). `supplier-catalog-search`, `order-sourcing`, `local-inventory` untouched.
- NOT affected: sourcing flow, supplier seam, `resolve_item`, session rehydration.

### Approaches

1. **RagCatalogSearcher behind the existing `CatalogSearcher` protocol, composed with local search**
   - A composite class (e.g. `PrecedenceProductSearcher`) implements `search(query) -> tuple[SearchCandidate, ...]`: call `DbCatalogSearcher`; if empty, call a `RagCatalogSearcher` (httpx client mapping `structured_json.productos[]` → `SearchCandidate(sku=codigo, nombre_oficial=nombre, confidence≈1.0)`); return the local result otherwise.
   - Pros: smallest blast radius — only `pipeline.py` wiring + one new class + note rendering tweaks; existing `FakeSearcher` tests stay valid; precedence lives in exactly one place; `build_orchestrator(searcher=...)` keeps full testability.
   - Cons: `SearchCandidate` is too thin for RAG products — price, brand, specs, source PDF would be lost in the note unless the dataclass is extended (backward-compatible with defaults) or the note renderer branches on source; the "empty result" signal conflates "RAG found nothing" with "RAG failed/timeout" (needs a distinct outcome to avoid telling the owner "not in stock" when the service is down).
   - Effort: Low-Medium.

2. **Dedicated ProductSearchService/strategy with explicit precedence chain (local → RAG)**
   - New `src/agents/product_search.py` (or similar) exposing a `ProductSearchResult` with a `source` discriminator (`LOCAL | RAG | NONE | ERROR`), plus a `RagProductClient` in `src/integrations/rag.py`. `build_handler` renders a source-aware note: local candidates → current note; RAG products → name/brand/price/specs note; `NONE` → honest "not found in catalogs"; `ERROR` → "couldn't consult supplier catalogs" (no stock claim).
   - Pros: cleanest fit for the owner's requirement — precedence policy is explicit and unit-testable in isolation; richer DTO preserves RAG fields (§5.2 mapping: price, currency, brand, specs) that matter for order generation; failure handling is first-class (RAG down/timeout → structured note, never a wrong "sin stock" claim); `SearchCandidate` stays untouched (sourcing/`resolve_item` unaffected).
   - Cons: bigger blast radius (new module, `customer.py` note logic refactor, pipeline wiring); two search concepts coexist (CatalogSearcher for sourcing resolution, ProductSearchService for the conversational note) — mitigated because `CatalogSearcher` can remain the local hop inside the chain.
   - Effort: Medium.

3. **Extend the supplier seam only (Case B) and leave plain product queries local**
   - This is the currently documented architecture (`docs/especificacion-catalogo-e-inventario.md` §5.1): "el RAG solo participa cuando el ítem no puede satisfacerse con stock propio (Caso B)" — `RagSupplierCatalogSearcher` behind `SupplierCatalogSearcher` replaces `FakeSupplierCatalogSearcher`.
   - Pros: follows the existing documented plan; fixes the long-standing "FakeSupplierCatalogSearcher in production" suggestion from two archived verify-reports.
   - Cons: **conflicts with the owner's step-1 direction** — with `catalogo`/`inventory` empty, plain product queries would keep answering "not in stock", which is precisely what the owner wants changed now ("answer through the RAG as if there were no inventory"). It also entangles sourcing classification with RAG latency (~5 s per missing item) before the conversational query path is proven. The owner's explicit decision wins: local-first→RAG-fallback for **product queries** is the fixed requirement; the supplier seam upgrade remains a natural follow-up (Case B) but is out of scope here.
   - Effort: Medium (but does not satisfy the change).

### Recommendation

**Approach 2**, implemented to reuse approach 1's cheap parts: keep the `CatalogSearcher` protocol as the local hop, add a `RagProductClient` (sync `httpx`, injectable transport, timeout from settings, domain errors) and a `PrecedenceProductSearcher`-style chain returning a source-discriminated result. `build_handler` renders the transient note per source. Rationale:

- **Precedence explicitness**: one seam owns "local first, RAG fallback" — when `catalogo`/`inventory` gets populated, local hits simply stop reaching the RAG. No refactor later.
- **Failure honesty**: a timeout/down RAG must produce "couldn't consult catalogs", not the current empty-result note that claims "not in stock" — the empty-catalog note text already conflates "not found" with "no stock", and with a dead RAG that conflation becomes a lie to the owner.
- **Latency**: measured ~2.5–5 s per RAG call is acceptable because the webhook ACKs before the background handler runs (SLA untouched); the reply is delayed, not blocked. Recommend an httpx timeout (≈8–10 s) and keeping the call sync inside the already-background handler.
- **OpenSpec delta**: new `rag-product-query` spec (ADDED requirements) + MODIFIED `catalog-search` (note/precedence behavior). `supplier-catalog-search` untouched; Case B real-searcher upgrade can be a later change.
- **Design decisions to fix in proposal/spec**: fallback trigger (recommend: fallback when local returns zero candidates at/above `search_ambiguity_floor`, honoring existing thresholds); RAG request params (`structured_json=true`, `top_n` from settings); note language (Spanish rioplatense product copy, fields from §5.2 mapping); whether `structured_json.respuesta_narrativa` or the product list feeds the note (recommend product list + key fields; narrative is LLM output with citations aimed at machines, not the assistant persona).

### Risks

- **Latency creep**: 5 s per RAG hit, ~15 s on cold start; stacking with the OpenAI responder call doubles turn latency. Mitigation: timeout settings, note the ACK-first architecture makes this a UX issue, not an SLA one.
- **`catalogo.embedding` never populated**: the local pgvector channel is dead code today; do not "fix" it in this change (out of scope, would widen the diff).
- **RAG data hygiene**: observed `AMX-AMX-AT-5044` double-prefix SKU — note rendering should not trust `codigo` blindly for display.
- **Port drift**: docs (both repos) say 8000, service runs on 8001. Default the setting to 8001 and flag the docs inconsistency.
- **"Not found" vs "no stock" conflation**: with `is_refusal=true` meaning "not in current catalogs", the note must not claim stock status either way (RAG knows nothing about business inventory).
- **Spec coupling**: `order-sourcing`/`supplier-catalog-search` must not be accidentally modified; the delta touches only the conversational query path.

### Ready for Proposal

**Yes.** The fixed requirement (local-first → RAG-fallback seam) has a clear implementation home, a measured API contract, and a recommended approach. The orchestrator can proceed to `sdd-propose` with: the precedence policy as a non-negotiable requirement, approach 2 as the chosen direction, and the design decisions listed above to resolve during proposal/spec.
