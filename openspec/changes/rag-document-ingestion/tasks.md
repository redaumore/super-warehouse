# Tasks: RAG-backed Document Ingestion (rag-document-ingestion)

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~1,950–2,400 (additions+deletions; design's 1,000–1,200 undercounts — excludes ~500–600 deletions and the new rag-api test file) |
| Project review budget | 2500 lines (overrides 400 default) |
| 400-line budget risk | Medium |
| Chained PRs recommended | No |
| Suggested split | Single PR, work-unit commits W1→W5 |
| Delivery strategy | single-pr |
| Chain strategy | pending |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: pending
400-line budget risk: Medium

### Suggested Work Units

| Unit | Goal | Focused test command | Runtime harness | Rollback boundary | Est. lines (additions) |
|------|------|----------------------|-----------------|-------------------|-----------|
| W1 | rag-api endpoints + parser + schemas + tests | `pytest services/rag-api/tests/test_ingestion.py` | FastAPI TestClient, OpenAI monkeypatched | Revert `endpoints/ingestion.py`, `schemas/ingest.py`, `core/ingestion/document_parser.py`, router line | 500–650 |
| W2 | Client `parse_document`/`exact_lookup` | `pytest tests/test_rag.py -k "parse or exact"` | N/A (httpx.MockTransport unit) | Revert `src/integrations/rag.py` additions | 200–260 |
| W3 | ingestion.py rewrite (resolve + persist) | `pytest tests/test_backoffice.py -k "receipt or resolve or ingest"` | docker pgvector + fake embedder | Revert `src/backoffice/ingestion.py` | 380–480 |
| W4 | Backoffice Ingestion tab UI | `pytest tests/test_backoffice.py -k "app_ingest"` | `python -m src.backoffice.app` (gradio) | Revert `src/backoffice/app.py` Ingestion tab | 250–340 |
| W5 | E2E rewrite + cleanup + full suite | `pytest tests/test_e2e_ingestion.py` | docker pgvector, mock RagProductClient | Test-only revert; `src/supplier/ocr.py` untouched (deferral) | 300–420 |

## Phase 1: rag-api endpoints (backend-first)

- [x] 1.1 `services/rag-api/app/api/schemas/ingest.py` (new): `DocumentLine` (codigo_orig, codigo, descripcion, cantidad:int, costo:float|None, pagina:int), `ParsedDocument`, `DocumentParseResponse`, `ExactLookupResponse`, `ProductLookupResponse`. Deps: none. Commit: W1. Accept: [rag-doc R2] parse response = structured lines + source metadata.
- [x] 1.2 `services/rag-api/app/core/ingestion/document_parser.py` (new): `DocumentLineParser.parse()` with dedicated receipt prompt/schema (NOT catalog prompt); page-render adapter — PDF pymupdf page-by-page (text + base64 PNG @200dpi), image → single page (pagina=1, empty text layer); OpenAI via `get_openai_client` pattern (`core/ingestion/embedder.py`). Deps: 1.1. Commit: W1. Accept: [sup-doc R1] remito photo + invoice PDF extract same fields.
- [x] 1.3 RED `services/rag-api/tests/test_ingestion.py` (new; TestClient + monkeypatched OpenAI — new pattern, no live-DB): parse success lines + zero writes; parse failure → structured error + zero writes; exact-lookup scoping; products/{sku} single/404/all. Deps: 1.1–1.2 (fails until 1.4). Commit: W1. Accept: [rag-doc R2] both scenarios; [rag-product-query R2] 3 scenarios.
- [x] 1.4 `services/rag-api/app/api/v1/endpoints/ingestion.py` (new): **POST /ingest/parse** (multipart, UploadFile bytes in memory, never persists), **GET /catalog/exact** (UPPER(TRIM(codigo_orig)) scoped to codigo_proveedor, returns ALL rows with node_id), **GET /products/{sku}?codigo_proveedor=** (single full row incl. node_id for price lookup; 404 when none; exact-lookup callers reuse it for all matches). Deps: 1.1–1.2. Commit: W1. Accept: [rag-product-query R2] 200 single / 404 / array-all-matches-possibly-empty.
- [x] 1.5 `services/rag-api/app/api/v1/router.py`: include `ingestion.router`, tags=["Ingestion"]. Deps: 1.4. Commit: W1.

## Phase 2: client methods (`src/integrations/rag.py`)

- [x] 2.1 RED `tests/test_rag.py` (httpx.MockTransport): parse_document maps lines; exact_lookup all matches + node_id; 404 → empty tuple; transport failure AND timeout → `RagProductError`, never raw httpx exception; bounded by `rag_timeout_seconds` (src/config.py:73). Deps: none (fails until 2.2–2.3). Commit: W2. Accept: [rag-product-query R1] parse/exact/provenance/domain-error/timeout scenarios.
- [x] 2.2 `src/integrations/rag.py`: frozen `DocumentLine` dataclass + `parse_document(self, *, filename, content, codigo_proveedor) -> tuple[DocumentLine, ...]` on `RagProductClient` — multipart POST **/api/v1/ingest/parse**; httpx.HTTPError → RagProductError; timeout = settings.rag_timeout_seconds. Deps: 1.4. Commit: W2. Accept: [rag-product-query R1] parse via client, domain error, bounded timeout.
- [x] 2.3 Same file: `exact_lookup(self, codigo_orig, codigo_proveedor) -> tuple[RagProduct, ...]` — GET **/api/v1/products/{sku}** + codigo_proveedor param; 404 → empty tuple; all matches mapped incl. node_id; failures → RagProductError. Deps: 1.4. Commit: W2. Accept: [rag-product-query R1] exact lookup + provenance; [rag-doc R3] exact hit resolves without hybrid.

## Phase 3: resolution + persistence use case (`src/backoffice/ingestion.py`)

- [ ] 3.1 RED `tests/test_backoffice.py` — replace `test_confirm_items_*` (:322, :336), `test_extract_document_items_*` (:150, :162), `test_to_grid_rows_*` (:138) with receipt-flow tests: two-pass resolution (1→auto, 0→hybrid, >1→pending), UPPER(TRIM) normalization, no-creation-from-model-output, provenance write-once, transactional rollback, `ensure_active_supplier` guard, StockAdjustment reason="receipt_ingestion". Deps: none (fails until 3.2–3.3). Commit: W3. Accept: [rag-doc R3/R5] exact-first, no-creation, rollback; [manual R2] no-match stays pending.
- [ ] 3.2 `src/backoffice/ingestion.py`: add `ReceiptLine`/`ResolvedLine`/`IngestResult`; `resolve_lines(session, rag, lines, supplier_id)` — exact via `exact_lookup`, hybrid via `query()` scoped to supplier ONLY on miss; UPPER(TRIM(codigo_orig)) both sides; 1 hit → resolved, 0 → hybrid, >1 → pending (never silently pick); remove `extract_document_items`/`to_grid_rows`/`_find_existing_product`/`confirm_items`. Deps: 2.2–2.3. Commit: W3. Accept: [rag-doc R3] both scenarios; [sup-doc R2] partial extraction → unresolved lines.
- [ ] 3.3 Same file: `ingest_receipt_lines(session, supplier_id, lines, owner_ctx, embedder) -> IngestResult` — `ensure_active_supplier` first; per line `sku = build_sku(supplier.code, codigo_orig)` (reuse `src/backoffice/adoption.py:101`); exists in Catalogo → bump stock_disponible, mirror Inventory.quantity_on_hand, StockAdjustment(+qty, "receipt_ingestion"), origen write-once (reject overwrite); only-in-RAG → embed fail-closed (node_id required), create `Catalogo(origen={"rag":{node_id, archivo_origen, pagina_origen}})` + Inventory + StockAdjustment; ONE flush, caller commits. Deps: 3.2. Commit: W3. Accept: [rag-doc R5] all 3 scenarios (provenance, no-creation, single-transaction rollback).
- [ ] 3.4 `tests/test_backoffice.py` (unit, after 3.2–3.3 green): 3-row seed; duplicate codigo_orig → pending; per-line update keeps origen; adopt-new writes origen dict; embed-failure rollback; unknown/inactive supplier. Deps: 3.2–3.3. Commit: W3. Accept: [rag-doc R3/R5] duplicate policy + provenance.

## Phase 4: backoffice UI (`src/backoffice/app.py`)

- [ ] 4.1 Rewrite Ingestion tab (:996–1018): supplier dropdown from new `_active_supplier_choices(session)` (ACTIVO only, shows `business_name`, retains ID internally); REMOVE `gr.Number` supplier-ID input (:1006); upload button unchanged. Deps: none. Commit: W4. Accept: [rag-doc R1] dropdown by business_name; no free numeric ID.
- [ ] 4.2 Same file: add `_ingest_parse` (upload → `parse_document` → grid state; RagProductError → honest error, zero writes), `_ingest_manual_search` (hybrid `query()` scoped to supplier → candidates) and `_ingest_assign` (selection attaches node_id); remove `_ingest_preview` (:213). Deps: 2.2–2.3, 4.1. Commit: W4. Accept: [manual R1] search returns candidates; selection attaches row; [rag-doc R6] down → honest error.
- [ ] 4.3 Same file: rewrite `_ingest_confirm` (:234) — gated: blocked while ANY positive-quantity line unresolved, message lists pending lines; delegates to `ingest_receipt_lines`; single commit. Deps: 3.3, 4.2. Commit: W4. Accept: [rag-doc R4] blocked w/ message; unlocked when all resolved.
- [ ] 4.4 `tests/test_backoffice.py` UI tests — replace `test_build_app_ingestion_tab_has_preview_and_confirm` (:119), `test_app_ingest_*` (:609, :620, :631, :644): tab renders dropdown, no numeric input; parse → grid with resolved+pending; manual search fixes pending; confirm blocked/unblocked. Deps: 4.1–4.3. Commit: W4. Accept: [backoffice R1] all 3 scenarios; [rag-doc R4] grid shows resolved and pending.

## Phase 5: E2E + cleanup

- [ ] 5.1 Rewrite `tests/test_e2e_ingestion.py`: RAG-backed flow with mocked RagProductClient — upload → parse → resolve → confirm writes stock with node_id provenance; unmatched line → no Catalogo created + confirm blocked; RAG down/slow → honest error, zero writes. Deps: 3.3, 4.3. Commit: W5. Accept: [rag-doc R5/R6] provenance writes, no-creation, down→no-writes; [sup-doc R2] parse failure → no write.
- [ ] 5.2 Dead-code decision (design open Q): DEFER deletion of `src/supplier/ocr.py` remito helpers (`extract_document` :162, `parse_line_items` :120) + their tests (`tests/test_ocr.py`:54–102) — same file's price-list helpers stay live; flag removal in apply notes/PR for follow-up cleanup. No code change. Deps: 3.2 (helpers become unreferenced). Commit: W5.
- [ ] 5.3 Run `ruff check src tests && mypy src`; full `pytest` (Postgres-dependent tests skip when down); confirm `src/agents/customer.py:483` `_rag_price` price_lookup contract unchanged (404→None) and adoption flow untouched. Deps: all. Commit: W5.

Threat matrix: all rows N/A (no shell/subprocess/VCS/executable/process integration) — no RED tests beyond those above.
