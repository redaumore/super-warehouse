# Apply Progress — rag-document-ingestion

Delivery: single PR, work-unit commits W1→W5 on branch `rag-document-ingestion` (not pushed).
Mode: Standard (strict_tdd=false). Store: openspec mirror (native) + Engram.
Budget: 2500 changed lines (forecast 1950–2400) — no size exception.

## Batch Log

| Batch | Scope | Focused test | Result | Commit |
|-------|-------|--------------|--------|--------|
| W1 | rag-api endpoints + parser + schemas + tests | `pytest services/rag-api/tests/test_ingestion.py` | pending | — |

## Test Environment

- Postgres + pgvector running in docker (`super-warehouse-db`, port 5432). Integration tests run.
- Root venv `.venv` (pytest 9.1.1): pymupdf NOT installed. rag-api tests must run via
  `services/rag-api/.venv` (has pymupdf 1.28.2, fastapi, openai 3.8). pytest installed there for this run.
- RAG service localhost:8001 not required — rag-api tests use TestClient + monkeypatched OpenAI;
  main-suite rag.py tests use httpx.MockTransport.

## Deviations / Notes

- (to fill as work progresses)

## Blockers

- none
