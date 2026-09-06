# Archive Report: RAG-Backed Document Ingestion

**Change**: `rag-document-ingestion`
**Archived**: 2026-09-06
**Archive path**: `openspec/changes/archive/2026-09-06-rag-document-ingestion/`
**Artifact store**: hybrid (OpenSpec mirror + Engram)
**Status**: ✅ ARCHIVED — complete, PASS WITH WARNINGS (0 CRITICAL open)

---

## 1. Gates

| Gate | Result | Evidence |
|------|--------|----------|
| Task Completion | ✅ PASS | Persisted `tasks.md` 19/19 checkboxes `[x]` across 5 phases (W1–W5); zero unchecked implementation tasks (`rg '^- \[ \]'` → none). Native `sdd-status` confirms `taskProgress: 19/19, allComplete: true`. |
| CRITICAL findings | ✅ PASS | Verify verdict **PASS WITH WARNINGS**, 0 CRITICAL open (native validator admitted via `gentle-ai sdd-verify-validate --requirements 14 --scenarios 30` → `valid:true`). Remediation commit `baa0595` (test-only, 120 lines) landed after the verify snapshot. |
| Archive readiness | ✅ PASS | `gentle-ai sdd-status rag-document-ingestion --json`: `dependencies.archive: ready`, `nextRecommended: archive`, `blockedReasons: []`, all dependencies `all_done`. |
| Action context | ✅ PASS | `actionContext.mode: repo-local`; all archive operations inside `allowedEditRoots` (workspace root). |

## 2. Final State (authority-ranked)

Per the Final-State Authority hierarchy, the following is the state of the change AT CLOSE. The launch prompt's final-state facts outrank the intermediate `apply-progress` / `verify-report` snapshots.

| Fact | Value | Source rank |
|------|-------|-------------|
| Tasks | 19/19 complete (persisted artifact) | rank 1 |
| Verify verdict | **PASS WITH WARNINGS** — 14 requirements / 30 scenarios compliant; native validator admission `gentle-ai sdd-verify-validate --requirements 14 --scenarios 30` → `valid:true`; 0 CRITICAL open | launch prompt (rank 2), corroborated by verify-report Engram #362 |
| Remediation | Commit `baa0595` (test-only, 120 lines: real PDF/image page-render tests + ACTIVO supplier-choices test) landed AFTER the apply commits | launch prompt (rank 2) |
| Branch / commits | Branch `rag-document-ingestion`, 5 commits: `f863b36`, `f820992`, `7ca1ce7`, `fe2a2b1`, `baa0595`. HEAD = `baa0595`. Working tree clean. **NOT pushed, NO PR created.** | launch prompt (rank 2) |
| Tests (final) | pytest **842 passed** (Postgres up); rag-api suite **32 passed** | launch prompt (rank 2) |
| Lint | `ruff` clean | launch prompt (rank 2) |
| Typecheck | mypy `src` — **8 pre-existing errors** (baseline 10 on main `473378f`) | launch prompt (rank 2) |
| Review budget | 2498 code-only changed lines vs 2500 budget | launch prompt (rank 2) |

**Snapshot-derived claims** (attributed to source and time, not bare present facts):
- `apply-progress` Engram #360 (2026-09-06 15:54) reported 4 work-unit commits (W1, W2, W3+W4 merged, W5) — accurate at apply time; the later remediation commit `baa0595` brought the branch to 5 commits. Final state carries the 5-commit count.
- `verify-report` Engram #362 (2026-09-06 16:15) reported `pass_with_warnings` 14/30 with the remediation commit included — consistent with the launch prompt; no contradiction.

**Contradiction log**: None. All sources agree on the final state.

## 3. Spec Sync (delta → main)

Conventions followed: `2026-09-02-rag-product-query` archive precedent — MODIFIED replaces the requirement block wholesale, `(Previously: ...)` change-notes stay out of the main spec, ADDED appends to the Requirements section, REMOVED deletes the block (Reason/Migration present in delta).

| Domain | Action | Details |
|--------|--------|---------|
| rag-document-ingestion | **Created** | NEW capability — no main spec existed. Delta IS a full spec: copied verbatim (shell `cp` + `diff -r` readback, empty) to `openspec/specs/rag-document-ingestion/spec.md` (6 requirements, 13 scenarios). |
| manual-rag-product-resolution | **Created** | NEW capability — no main spec existed. Copied verbatim (shell `cp` + `diff -r` readback, empty) to `openspec/specs/manual-rag-product-resolution/spec.md` (2 requirements, 3 scenarios). |
| backoffice | Updated | 1 MODIFIED replaced: `Supplier document ingestion module` — now supplier-first selection by `business_name`, RAG/Luna parse, review grid with per-line manual search, confirmation gated until all positive-quantity lines resolved. 3 scenarios in the replaced block (2 old ones retired with it). 9 untouched requirements preserved. The delta's `(Previously: ...)` note dropped per repo convention. |
| rag-product-query | Updated | 2 ADDED appended: `Document parse and exact supplier-code lookup` (4 scenarios) and `Product lookup route by SKU` (3 scenarios). 8 pre-existing requirements preserved. |
| supplier-document-ingestion | Updated | 2 MODIFIED replaced: `Extract items, quantities, and costs` (now via RAG/Luna structured-output parse endpoint with source metadata) and `Handle parse failure` (renamed-modified from `Handle OCR failure` — the delta's `(Previously: ...)` note explicitly names the old requirement; old block deleted). 1 REMOVED deleted: `Map items to existing SKUs or suggest new ones` (delta carries `(Reason: unknown-product creation is superseded — every ingested product MUST pre-exist in catalogo_productos_rag)` and `(Migration: resolve lines via exact codigo_orig lookup then manual RAG product-code search; never create from model output)`). 5 untouched requirements preserved. |

**Post-merge structural verification**: `backoffice` 10 requirements, `rag-product-query` 10, `supplier-document-ingestion` 7 (8 → 7 after REMOVED); no residual `Handle OCR failure` / `Map items to existing SKUs` text; no `(Previously: ...)` notes from this change's deltas leaked into main specs (pre-existing notes in unrelated requirements untouched).

**Destructive-delta warning**: none triggered — the single REMOVED was a 3-scenario requirement block with documented Reason/Migration; no large sections removed.

## 4. Source of Truth Updated

- `openspec/specs/rag-document-ingestion/spec.md` (created)
- `openspec/specs/manual-rag-product-resolution/spec.md` (created)
- `openspec/specs/backoffice/spec.md` (1 requirement replaced)
- `openspec/specs/rag-product-query/spec.md` (2 requirements appended)
- `openspec/specs/supplier-document-ingestion/spec.md` (2 replaced, 1 removed)

## 5. Open Follow-ups (recorded, NOT fixed)

1. **Task 5.2 deferral** — `src/supplier/ocr.py` remito helpers (`extract_document`, `parse_line_items`) plus their tests are dead code pending a dedicated cleanup (same file's price-list helpers stay live).
2. **Hybrid supplier-scoping** (warning) — client-side `codigo_proveedor` post-filter bounded by `rag_top_n`, not a server-side scope.
3. **`GET /catalog/exact` duplicates `GET /products/{sku}`** (suggestion) — candidate consolidation.
4. **mypy non-zero baseline** (warning) — 8 pre-existing `src` errors hide future type regressions; baseline cleanup recommended.

## 6. Engram Traceability (hybrid store)

Change-artifact observations persisted in Engram (project `super-warehouse`) and read for this archive:

| Artifact | Engram observation | sync_id |
|----------|--------------------|---------|
| proposal | #354 | `obs-7ca7783f012e45ef` |
| spec (5 domains) | #355 | `obs-d28114d59bb61938` |
| design | #356 | `obs-df3928f2b990ed31` |
| tasks | #358 | `obs-e8eb15ce0d0238f6` |
| apply-progress (final) | #360 | `obs-08574c6a0bd86686` |
| verify-report | #362 | `obs-ee6c773797fc2344` |
| archive-report | topic `sdd/rag-document-ingestion/archive-report` | (persisted at archive time) |

## 7. Delivery State

- Branch `rag-document-ingestion` @ `baa0595` (5 commits: `f863b36`, `f820992`, `7ca1ce7`, `fe2a2b1`, `baa0595`), working tree clean.
- **NOT pushed, NO PR created** — delivery decision is human-owned (orchestrator/owner).
- Spec-sync + archive-move commit lands on the same branch as a single conventional commit.

## 8. SDD Cycle Complete

The `rag-document-ingestion` change has been fully planned, implemented, verified, and archived. Ready for the next change.
