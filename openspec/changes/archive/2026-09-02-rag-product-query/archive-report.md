# Archive Report: RAG-Backed Product Queries

**Change**: `rag-product-query`
**Archived**: 2026-09-02
**Archive path**: `openspec/changes/archive/2026-09-02-rag-product-query/`
**Artifact store**: hybrid (OpenSpec files + Engram)
**Status**: ✅ ARCHIVED — intentional, complete (no warnings required)

---

## 1. Gates

| Gate | Result | Evidence |
|------|--------|----------|
| Native Review Receipt | ✅ PASS (relaxation) | `reviewGate.delivery: disabled/unmanaged` — receipt-driven development is DISABLED clone-local for this repo (user decision; kill switch off; no review governs this change). No review artifacts exist (`reviews/` dir absent; Engram search for `sdd/rag-product-query/review` topics returned none). No fabricated approvals. |
| Task Completion | ✅ PASS | Persisted `tasks.md` 16/16 checkboxes `[x]` across 4 phases (W1–W4); `rg '^- \[ \]'` → zero unchecked implementation tasks. Engram topic #225 agrees. |
| CRITICAL findings | ✅ PASS | `verify-report.md` §Issues: `### CRITICAL — None.` Verdict `PASS`, 0 blockers. |
| Action context | ✅ PASS | `actionContext.mode: repo-local`; all archive ops inside workspace root `openspec/`. Unrelated uncommitted working-tree changes (`src/backoffice/app.py`, `tests/test_suppliers_backoffice.py`, `docs/especificacion-catalogo-e-inventario.md`) left untouched. |

**RDD-off footnote (non-blocking)**: the native sdd-attempt runtime ledger for this change records the apply attempt as passed with the budget flag raised (1700 changed lines vs 1200 declared budget), a maintainer reset applying the owner-approved `size:exception` (2000 lines), and the verify attempt settled `complete`. Recorded for traceability only — RDD-off was the user's explicit choice, same posture as the `supplier-management` and `owner-order-intake` cycles; not a blocker.

## 2. Final State (authority-ranked)

Per the Final-State Authority hierarchy, the following is the state of the change AT CLOSE:

| Fact | Value | Source rank |
|------|-------|-------------|
| Tasks | 16/16 complete | Persisted tasks artifact + Engram #225 (rank 2) |
| Verify verdict | **PASS** — 9/9 requirements, 16/16 scenarios compliant | launch prompt (rank 3) + verify-report #227 (rank 4); structural scenario count across both deltas = 16 (13 + 3), matching exactly — no contradiction |
| Tests (final) | **548 passed**, 2 warnings (alembic deprecation), 15.46s | launch prompt + verify-report #227 (rank 3/4) |
| Lint / format / docs | `make lint` (ruff) clean; `ruff format --check` clean on 12 touched files; `make check-test-docs` clean (279 escenarios) | verify-report #227 (rank 4) |
| Typecheck | `make typecheck` exits 1 — **3 PRE-EXISTING mypy errors** in `src/backoffice/app.py` (lines 17, 183, 187), verified identical on stashed HEAD; caused by unrelated uncommitted work, NOT this change. All 16 change files mypy-clean. | verify-report #227 + apply-progress #226 + launch prompt (rank 3/4) |
| Live smoke | RAG `http://localhost:8001/api/v1/query` reachable (HTTP 200); query "tarugos" returned valid refusal envelope (`is_refusal=True`, `productos=[]`) | verify-report #227 (rank 4) |
| Work-unit commits | `47c8470` (W1 feat(rag)), `b7f8b9d` (W2 feat(product-search)), `43347b8` (W3 feat(customer) — incl. task 4.1 wiring), `4834454` (W4 test(pipeline) E2E pins + docs). On branch `main` at HEAD. NOT pushed, no PR. | launch prompt + git log |
| Review gate | `disabled/unmanaged` (RDD-off clone-local, user decision) | launch prompt + absent review artifacts |
| Deviations | 4 adjudicated as coherent/tested: (1) chain-level `SQLAlchemyError` → RAG fallback (task 3.2 wins over design note; handler still catches direct errors); (2) `product_context_note` `draft=` kwarg per ADR 1; (3) task 4.1 folded into W3 commit for mypy-green units; (4) SKU hygiene proven at client/chain/draft level (RAG note template has no SKU field per ADR 5) | verify-report #227 (rank 4) |
| SUGGESTION findings | 1 open: update remaining port-8000 doc references (8000↔8001 drift now flagged in `docs/architecture.md`) | verify-report #227, carried as follow-up (non-blocking) |

**Contradiction log**: None. All sources agree; scenario counts match delta structure exactly (9 requirements / 16 scenarios), unlike the supplier-management cycle. No unrankable contradictions recorded.

**Snapshot-derived claims** (attributed to source and time, not bare present facts):
- `apply-progress` #226 (2026-09-02 13:00) reported "live RAG service NOT hit (transport stubbed by design)" — accurate at apply time; the later verify phase (13:07) performed the live smoke check against `:8001`, which succeeded. Final state carries the live result.
- `apply-progress` #226 reported `make typecheck` failing on the same 3 pre-existing `src/backoffice/app.py` errors — consistent with the final state; both rank-4 snapshots and the rank-3 launch prompt agree.

## 3. Spec Sync (delta → main)

| Domain | Action | Details |
|--------|--------|---------|
| rag-product-query | **Created** | NEW capability — no main spec existed. The delta IS a full spec: copied verbatim to `openspec/specs/rag-product-query/spec.md` (8 requirements, 13 scenarios). Byte-identical to the archived delta (verified via `diff`). |
| catalog-search | Updated | 1 MODIFIED replaced (`Report no-match results`) — requirement now mandates RAG consultation on empty local search + source-aware note + explicit no-stock-claim; 2 pre-existing scenarios carried inside the replaced block (`No product found` updated GIVEN to include RAG, `No-match does not block the rest of the order` preserved) plus new scenario `Empty local search falls back to RAG`. The delta's `(Previously: ...)` change-note was dropped from the merged main spec per repo convention (Engram #206 / `order-sourcing-workflow` precedent). 4 untouched requirements preserved (`Hybrid fuzzy and vector search`, `Auto-map high-confidence matches`, `Disambiguation menu on ambiguity`, `Identification precision target`). |

No `REMOVED` or top-level `RENAMED` requirement sections existed in any delta, so no requirement deletion required `(Reason: ...)` / `(Migration: ...)` notes.

**Destructive-delta warning (per `openspec/config.yaml` `rules.archive`)**: none triggered — the only merge was a single-requirement replacement (3 scenarios) and a verbatim new-spec copy; no large sections were removed from any main spec.

## 4. Source of Truth Updated

- `openspec/specs/rag-product-query/spec.md` (created)
- `openspec/specs/catalog-search/spec.md` (1 requirement replaced)

## 5. Archive Contents

- `proposal.md` ✅
- `exploration.md` ✅
- `design.md` ✅
- `specs/{rag-product-query,catalog-search}/spec.md` ✅ (delta specs, unmodified audit trail)
- `tasks.md` ✅ (16/16 complete, no unchecked implementation tasks)
- `apply-progress.md` ✅
- `verify-report.md` ✅
- `archive-report.md` ✅ (this file)

Active changes directory no longer contains `rag-product-query` (only `archive/` remains).

## 6. Engram Traceability (hybrid store)

Change-artifact observations persisted in Engram (project `super-warehouse`):

| Artifact | Engram observation | sync_id |
|----------|--------------------|---------|
| explore | #221 | `obs-1e39d24b03f467d7` |
| proposal | #222 | `obs-fc60380b3616d814` |
| spec (deltas) | #223 | `obs-caba60d030f7ac30` |
| design | #224 | `obs-2aae78d85c8c4ca1` |
| tasks | #225 | `obs-dd5ff9accd1dbe48` |
| apply-progress | #226 | `obs-4ba4b642299d9764` |
| verify-report | #227 | `obs-59ff32b0e2bb1cbb` |
| archive-report | topic `sdd/rag-product-query/archive-report` | (persisted at archive time) |

No review-topic observations exist — consistent with RDD-off.

## 7. Notes & Follow-ups

**Follow-ups (recorded, NOT fixed):**
1. Verify SUGGESTION carried forward: update remaining port-8000 references in docs (drift now flagged in `docs/architecture.md`).
2. `openspec/config.yaml` context block remains stale (claims greenfield, no tooling) — carried from the supplier-management archive; still recommends a `sdd-init` refresh.

**RDD-off footnote**: see §1 — the native sdd-attempt ledger records the apply budget-flag + maintainer reset and a settled verify attempt; not a blocker under `reviewGate.delivery: disabled/unmanaged`.

**Branch note**: work-unit commits are on `main` (HEAD = `4834454`), NOT pushed, no PR created — the orchestrator owns delivery. The change folder itself was untracked before the archive move; the archive is likewise uncommitted, consistent with prior cycles where the orchestrator commits.

**Unrelated working-tree changes** (`src/backoffice/app.py`, `tests/test_suppliers_backoffice.py`, `docs/especificacion-catalogo-e-inventario.md`) are the source of the 3 pre-existing mypy errors and were deliberately left untouched by this phase.

**Rollback** of the change itself: revert `pipeline.py` wiring to `DbCatalogSearcher`, delete `src/integrations/rag.py`. DB untouched (no migrations).

## 8. SDD Cycle Complete

The `rag-product-query` change has been fully planned, implemented, verified, and archived. Ready for the next change.