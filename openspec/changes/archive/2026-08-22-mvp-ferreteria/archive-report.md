# Archive Report: mvp-ferreteria — Hardware Store Multi-Agent MVP

**Change**: mvp-ferreteria
**Status**: ARCHIVED — SDD cycle complete
**Archived**: 2026-08-22
**Source branch**: `feat/mvp-ferreteria-pr4` (HEAD `42ffdd3`)
**Persistence**: hybrid (OpenSpec files + Engram `sdd/mvp-ferreteria/archive-report`)
**Review gate**: `disabled/unmanaged` — no native review governed this change (no ledger/receipt/transaction exist; native `sdd-status` routes `nextRecommended: archive`, `blockedReasons: []`; kill switch off).

## Change Summary

Greenfield Python MVP for a ferretería (hardware store) wholesale operations system: WhatsApp text/voice order intake, hybrid fuzzy+vector catalog search, phone-based client identification with price lists, dynamic pricing, owner approval loop with soft-lock reservations, Google Sheets registration, supplier document OCR ingestion, barcode stock operations, and a Gradio backoffice.

Delivered across 4 chained PRs (stacked-to-main, `size:exception` approved for the final slice, merged locally — not pushed):

| PR | Scope | Key Deliverables |
|----|-------|------------------|
| PR1 | Repo + DB + channels | `pyproject.toml`, docker-compose (Postgres+pgvector), ORM models (all entities, `vector(1536)`), Alembic, channel ABC + Telegram, config Settings, webhook skeleton + signature + ACK <5 s, conftest + RED models tests |
| PR2 | Pricing + clients + search | Pure pricing engine (`compute_base`/`compute_final`, HALF_UP), customer agent (phone normalize), disambiguation agent (pgvector + rapidfuzz), inventory agent (soft-lock), unit tests (pricing 100% coverage) |
| PR3 | Orchestrator + orders | Perception protocols, sales quote/adjustments, dispatch notify/decide, orchestrator router+session, order state machine (+`needs_requote`), APScheduler sweeper, RED tests (TTL release, reject release, stale approve refused) |
| PR4 | Integrations + tests + docs | WhatsApp Cloud API adapter, OpenAI clients (lazy), Sheets append-only writer with quarantine, approval orchestration, Gradio backoffice (4 tabs), barcode decoder, supplier OCR, feature flags, E2E tests, README/docs, mypy strict + ruff clean |

## Final Quality Numbers (close-of-cycle)

- **Tests**: 234 passed / 0 failed / 0 skipped in 6.52s
- **Coverage**: 96% overall (threshold 85%); `src/pricing` 100%, `src/barcode/decoder.py` 100%
- **Lint**: ruff clean
- **Type check**: mypy strict clean (0 errors, 39 source files)
- **Verify verdict**: **PASS** — 0 CRITICAL, 0 WARNING, 3 non-blocking SUGGESTIONs
- **Spec compliance**: 46/46 requirements, 90/90 scenarios (9 domain specs)

## Verification Evidence

- Verify report: `verify-report.md` — verdict `pass`, evidence revision `sha256:fb552c5b…` (git `318957f`)
- Test output hash: `sha256:b190064e…`; mypy output hash: `sha256:8b09188c…`
- Tasks: 39/39 complete (tasks.md, all `[x]` — no stale unchecked implementation tasks)

## Resolved Warning (W1)

W1 (barcode-stock-ops "Record audited stock adjustments" had no implementation) was resolved in the PR4 remediation batch:

- `StockAdjustment` model (`stock_adjustments`) + Alembic migration `b2f353dfc3d2` (creates only `stock_adjustments`)
- `adjust_stock_by_barcode()` + `BarcodeAdjustmentError`/`BarcodeAdjustmentErrorKind` in `src/barcode/decoder.py`
- 5 tests in `tests/test_barcode.py` (increase, decrease, duplicate, unknown, negative)

Full suite after remediation: 234 passed; lint + mypy strict clean.

## Remaining SUGGESTIONs (non-blocking, deferred)

- **S1** — barcode-stock-ops: `Catalogo` has no `location` field (spec says "where available"). Response omits location until the field exists.
- **S2** — whatsapp-order-intake: no E2E test for the full voice → transcription failure → customer notification path (unit-covered).
- **S3** — whatsapp-order-intake: partial-transcription confirmation flow not E2E tested (field + unit coverage exist).

## Accepted Deviations

1. Webhook dispatch moved to FastAPI `BackgroundTasks` (design said "BackgroundTasks + APScheduler"; PR1 skeleton dispatched synchronously — PR4 wired the real background handoff, proven by recording ASGI transport).
2. Dispatch notifier is sync; WhatsApp send is async — bridged at the pipeline edge via an async-notifier adapter.
3. `register_approved_order` vs `approve_and_register` — adjustment-approvals go through `apply_decision` first then the registration half; calling `approve_and_register` after `apply_decision` double-approves (`InvalidTransitionError`, documented).
4. OpenAI clients build lazily (openai 3.x raises at `OpenAI()` construction without a key; importing the backoffice never needs credentials).
5. Price-list ingestion is Vision/text based — no PDF/Excel parsing library added; native PDF/Excel parsing is an extension point.
6. Feature flags: `require_fase` is raise-or-proceed, not a predicate — the webhook catches it so the ACK contract holds at the boundary.
7. Sheet quarantine message is Spanish ("cuarentena") — intentional owner-facing copy (established owner UX strings); all code/identifiers/docstrings are English.

## Known Non-Blocking Notes

- pyzbar needs the system zbar library (`brew install zbar` on macOS) — documented in the runbook.
- Pre-existing `httpx/starlette TestClient` deprecation warning in test_webhook remains (harmless).

## Specs Synced to Source of Truth

All 9 delta specs were full specifications (no ADDED/MODIFIED/REMOVED deltas); base spec tree was empty. Copied directly into `openspec/specs/{domain}/spec.md` (byte-identical):

| Domain | Requirements | Scenarios |
|--------|--------------|-----------|
| agent-orchestration | 4 | 8 |
| backoffice | 4 | 8 |
| barcode-stock-ops | 5 | 9 |
| catalog-search | 5 | 10 |
| clients-and-price-lists | 5 | 10 |
| order-lifecycle | 8 | 15 |
| pricing-engine | 4 | 8 |
| supplier-document-ingestion | 7 | 14 |
| whatsapp-order-intake | 4 | 8 |
| **TOTAL** | **46** | **90** |

## Archive Contents

- `proposal.md` ✅
- `exploration.md` ✅
- `design.md` ✅
- `tasks.md` ✅ (39/39 complete)
- `apply-progress.md` ✅
- `verify-report.md` ✅
- `specs/` ✅ (9 domains)
- `archive-report.md` ✅ (this file)

## Intentional Archive Notes

None — clean archive, no partial-archive or stale-checkbox reconciliation was required. Archive is intentional, not intentional-with-warnings.

## Traceability

Engram observations (project `super-warehouse`): explore #105, proposal #106, spec #107, design #108, tasks #109, apply-progress #110, W1 remediation #132. `verify-report` is authoritative on disk (native status `done`); no Engram topic was persisted for it. This report: topic_key `sdd/mvp-ferreteria/archive-report`.