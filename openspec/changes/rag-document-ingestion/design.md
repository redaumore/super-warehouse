# Design: RAG-backed Document Ingestion

## Technical Approach
`catalogo_productos_rag` becomes authoritative for supplier receipt ingestion. The backoffice Ingestion tab moves supplier selection first, then a RAG multipart parse endpoint (Luna Structured Outputs) returns lines with code/description/quantity/cost and NO writes. A two-pass resolver (exact `codigo_orig` lookup scoped to supplier → hybrid fallback) attaches `node_id` provenance per line; unresolved lines stay pending and gate confirmation. Confirmation persists stock in one transaction via a new receipt use case (update-already-adopted / adopt-new), never creating a product from model output alone.

## Architecture Decisions

### D1 — Image vs PDF (open Q1)
| Option | Tradeoff | Decision |
|---|---|---|
| Restrict to PDF | Breaks "remito photo" scenario | Reject |
| Reuse DirectLunaCatalogProcessor | Catalog prompt filters unpriced/unavailable; no quantity field | Reject |
| New parser + page-render adapter; image wrapped as 1-page doc | Smallest; clean contract | **Choose** |

Adapter in `document_parser.py`: PDF → pymupdf page-by-page (text + base64 PNG @200dpi); image → single page (`pagina=1`, empty text layer). Dedicated receipt prompt/schema, NOT the catalog prompt.

### D2 — Ingestion-only schema (open Q2)
**Choose** a new ingestion-only Pydantic schema (`DocumentLine`: `codigo_orig`, `codigo`, `descripcion`, `cantidad:int`, `costo:float|None`, `pagina:int`; `ParsedDocument` wrapper). **Rejected**: extending `ExtractedProduct`/`PageExtractionResult` — those are the Fases 0–3 catalog pipeline contract (`process_catalog` filters by price/availability, has no quantity); contaminating them risks regression. rag-api's catalog ingestion stays untouched.

### D3 — Receipt use case vs reuse adopt_product (open Q3)
**Choose** a new `ingest_receipt_lines` use case (rewrites `src/backoffice/ingestion.py`). `adopt_product` is insert-only (raises `SkuCollisionError` on existing), single-stock, resolves supplier by CODE, hardcodes reason `product_adoption` — it cannot update already-adopted rows or carry a per-line quantity. Reuse only the public `build_sku` helper.
Per-line: `sku = build_sku(supplier.code, codigo_orig)`; **exists in Catalogo** → bump `stock_disponible`, mirror `Inventory.quantity_on_hand`, `StockAdjustment(+qty, reason="receipt_ingestion")`, keep `origen` write-once (reject overwrite); **only in RAG** → embed (fail-closed, like adoption), create `Catalogo(origen={"rag":{node_id,archivo_origen,pagina_origen}})` + Inventory + StockAdjustment. One `flush`; caller commits (session-in / caller-commits). `ensure_active_supplier` guard first.

### D4 — Duplicate/normalization/manual-candidate policy (open Q4)
- **Normalization**: `UPPER(TRIM(codigo_orig))` both sides (matches `_normalize_sku_part`).
- **Duplicates**: exact lookup returns ALL rows for (codigo_orig, supplier); resolver: 1 → auto-resolve, 0 → hybrid, >1 → pending (ambiguous, never silently pick).
- **Manual candidates**: hybrid `POST /api/v1/query` (structured_json) scoped to supplier; rank = existing RRF/rerank order (client already best-first). Selection attaches `node_id`; empty stays pending.

### D5 — price_lookup route (open Q on non-existent GET /api/v1/products/{sku})
**Choose**: add the route (not remove/repoint). `price_lookup` is live in `src/agents/customer.py:483` (`_rag_price`); the route is genuinely missing, silently returning 404→None in draft pricing. The new `GET /api/v1/products/{sku}?codigo_proveedor=` returns the full row (incl. `node_id`); `price_lookup` keeps its URL/404 contract, and a new `exact_lookup` client method reuses the same route but returns all matches for ambiguity detection.

## Data Flow
```
upload(file, supplier) ─▶ POST /api/v1/ingest/parse ─▶ lines[]  (no writes)
  per line:
    exact_lookup(codigo_orig, supplier) ─▶ 1 hit ─▶ resolved
                                       ─▶ 0    ─▶ hybrid query ─▶ 1 ─▶ resolved
                                       ─▶ >1   ─▶ pending        └▶ 0/>1 ─▶ pending
  review grid (state) ─▶ manual search/assign ─▶ all +qty resolved ─▶ confirm
confirm ─▶ ingest_receipt_lines(session, lines) ─▶ Catalogo+Inventory+StockAdjustment (1 tx)
```

## File Changes
| File | Action | Functions/classes |
|---|---|---|
| `services/rag-api/app/core/ingestion/document_parser.py` | Create | `DocumentLine`, `ParsedDocument`, `DocumentLineParser.parse()` + render adapter |
| `services/rag-api/app/api/v1/endpoints/ingestion.py` | Create | `POST /ingest/parse`, `GET /catalog/exact`, `GET /products/{sku}` |
| `services/rag-api/app/api/schemas/ingest.py` | Create | `DocumentParseResponse`, `ExactLookupResponse`, `ProductLookupResponse` |
| `services/rag-api/app/api/v1/router.py` | Modify | include `ingestion.router` |
| `src/integrations/rag.py` | Modify | +`DocumentLine` dataclass, `parse_document()`, `exact_lookup()` (reuse `RagProductError`, `RagProduct`) |
| `src/backoffice/ingestion.py` | Rewrite | remove `confirm_items`/`extract_document_items`/`to_grid_rows`/`_find_existing_product`; +`ReceiptLine`, `ResolvedLine`, `IngestResult`, `resolve_lines()`, `ingest_receipt_lines()` |
| `src/backoffice/app.py` | Modify | Ingestion tab: supplier dropdown (`_active_supplier_choices`), `_ingest_parse`, `_ingest_manual_search`, `_ingest_assign`, `_ingest_confirm` (gated); remove `_ingest_preview` |
| `tests/test_rag.py` | Modify | +parse_document, +exact_lookup (MockTransport); price_lookup tests unchanged |
| `tests/test_backoffice.py` | Modify | replace `test_confirm_items_*`/`test_extract_document_items_*`/`test_to_grid_rows_*`/`test_app_ingest_*` with receipt-flow tests |
| `tests/test_e2e_ingestion.py` | Rewrite | RAG-backed flow (mock RagProductClient) |
| `services/rag-api/tests/test_ingestion.py` | Create | endpoint tests (OpenAI mocked via monkeypatch) |
| `tests/test_ocr.py`, `tests/test_adoption.py` | Unchanged | ocr.py remito helpers become dead (flag cleanup); adopt_product untouched |

## Interfaces / Contracts
```python
# rag.py additions
@dataclass(frozen=True) class DocumentLine:
    codigo_orig: str|None; codigo: str|None; descripcion: str
    cantidad: int; costo: float|None; pagina: int
def parse_document(self, *, filename: str, content: bytes, codigo_proveedor: str) -> tuple[DocumentLine, ...]
def exact_lookup(self, codigo_orig: str, codigo_proveedor: str) -> tuple[RagProduct, ...]
```
`ingest_receipt_lines(session, supplier_id, lines: list[ResolvedLine], owner_ctx, embedder) -> IngestResult`. All transport failures → `RagProductError`.

## Testing Strategy
| Layer | What | Approach |
|---|---|---|
| Unit (rag.py) | parse/exact lookup mapping, transport→domain errors | `httpx.MockTransport` |
| Unit (ingestion.py) | two-pass resolution, duplicate→pending, per-line update/adopt | DB session + fake embedder |
| Integration | single-transaction rollback, provenance write-once, no-creation-from-model | Postgres (skips when down) |
| E2E | upload→parse→resolve→confirm writes stock with node_id | mock RAG client |
| rag-api | parse no-writes, exact lookup scoping, 404 | FastAPI TestClient + mocked OpenAI |

## Threat Matrix
N/A — no shell, subprocess, VCS/PR automation, executable-file classification, or process integration. New HTTP routing is ordinary FastAPI; the parse endpoint reads `UploadFile` bytes in memory and never persists (no-writes guarantee).

## Migration / Rollout
No DB migration (existing `catalogo.origen`/`inventory`/`stock_adjustments` reused). Revert = restore UI/client/endpoint code without touching RAG data.

## Open Questions
- [ ] Whether to delete now-dead `src/supplier/ocr.py` remito helpers (`extract_document`/`parse_line_items`) — deferred, flag in apply.

## Review Workload Forecast
Estimated ~1000–1200 changed lines (rag-api ~350, rag.py +80, ingestion.py rewrite ~200, app.py ~180, tests ~450). **Under the 2500-line budget — NOT at risk.** Single PR viable. Risk: rag-api has no OpenAI-mocked test precedent (existing tests are live-integration), so parse-endpoint RED tests need a new monkeypatch pattern — small but new.
