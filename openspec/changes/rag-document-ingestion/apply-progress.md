# Apply Progress — rag-document-ingestion

Delivery: single PR, work-unit commits W1→W5 on branch `rag-document-ingestion` (not pushed).
Mode: Standard (strict_tdd=false). Store: openspec mirror (native) + Engram.
Budget: 2500 changed lines (forecast 1950–2400) — no size exception.

## Batch Log

| Batch | Scope | Focused test | Result | Commit |
|-------|-------|--------------|--------|--------|
| W1 | rag-api endpoints + parser + schemas + tests | `.venv/bin/python -m pytest tests/test_ingestion.py` (rag-api venv) | 9 passed (+19 existing rag-api = 28) | `f863b36` |
| W2 | client `parse_document`/`exact_lookup` | `.venv/bin/pytest tests/test_rag.py -k "parse or exact"` | 9 passed (full file 33 passed) | `f820992` |
| W3+W4 | ingestion.py rewrite + Ingestion tab UI | W3: `pytest tests/test_backoffice.py -k "receipt or resolve or ingest"` = 20 passed; W4: `-k "app_ingest"` = 6 passed; full file 76 passed | green | `??` |
| W5 | E2E rewrite + cleanup + full suite | `tests/test_e2e_ingestion.py` | pending | — |

## Test Environment

- Postgres + pgvector running in docker (`super-warehouse-db`, port 5432). Integration tests run.
- Root venv `.venv` (pytest 9.1.1): pymupdf NOT installed. rag-api tests run via
  `services/rag-api/.venv` (pymupdf 1.28.2). pytest installed there for this run.
- Pre-commit hook `test-scenario-docs` regenerates `docs/escenarios-testeados.md` from test files —
  test changes require running `.venv/bin/python scripts/gen_test_scenarios.py` before commit.
- RAG service localhost:8001 not required — rag-api tests use TestClient + monkeypatched OpenAI;
  main-suite rag.py tests use httpx.MockTransport.

## Deviations / Notes

- W1: product display `nombre`/`descripcion` are not columns of `catalogo_productos_rag`; the
  endpoint parses them from the row `text_content` YAML lines and `archivo_origen` from `metadata`
  (matches how `chunker.py` builds nodes). Product route returns an ARRAY of all matches; 404 on
  empty (rag-product-query spec "all matches" scenario). `price_lookup` client updated to unwrap
  the array while keeping its dict/404 contract + unchanged tests.
- W2 test additions regenerated `docs/escenarios-testeados.md` (426 scenarios) via the repo script.
- **W3+W4 merged into one commit (deviation):** task 3.2's mandated removal of
  `confirm_items`/`to_grid_rows`/`extract_document_items` breaks `src/backoffice/app.py:51` import
  (and the Ingestion tab) until the W4 tab rewrite lands — the phases are not independently
  committable while keeping the repo green per work-unit-commits. Both focused commands were run
  against the combined state (20 + 6 passed; full file 76 passed). E2E file (`test_e2e_ingestion.py`)
  still imports removed names — it is rewritten in W5 before any full-suite run.
- `ingest_receipt_lines` raises `UnresolvedLineError` on any positive-qty unresolved line (fail-closed
  backstop for the UI gating). `resolve_lines`/UI gating treat `cantidad <= 0` lines as non-gating.
- New `origen` write-once semantics: the update path never touches `Catalogo.origen`; adopt-new
  writes `{"rag": {node_id, archivo_origen, pagina_origen}}` and requires `node_id` (fail closed).
- Receipt tests seed catalog rows with the `build_sku`-convention SKU (e.g. `MSA-CLV-001`) since
  `ingest_receipt_lines` locates existing products by `build_sku(supplier.code, codigo_orig)`.

## Blockers

- none
