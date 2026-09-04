# Archive Report: order-state-machine

**Archived**: 2026-09-04
**Archive path**: `openspec/changes/archive/2026-09-04-order-state-machine/`
**Artifact store mode**: openspec (native dispatcher) + Engram mirror (session preflight `both`)
**Final-state authority**: this report is the terminal record of the cycle and reflects the state of the change AT CLOSE, per the Final-State Authority hierarchy (persisted tasks artifact > orchestrator launch facts > intermediate snapshots).

## Cycle Summary

- **Proposal → Archive**: full SDD cycle completed — proposed, specified (5 delta capability specs), designed, planned (27 tasks), implemented (27/27 complete across 12 commits), verified (PASS WITH WARNINGS, 0 CRITICAL), archived.
- **Branch**: `feat/order-state-machine` (base `main`), HEAD `17be566` at close (pushed).
- **Delivery strategy**: `exception-ok` — maintainer-approved single PR #14 with `size:exception` (~3,318 changed lines; maintainer-approved reset to 3,600, actor redaumore). No chained PRs.
- **Issue**: #13 (open — references this change).
- **Migration head**: `f2b2570aed04` (`order_estado` six values + `uq_orders_one_draft_per_customer` partial unique index + `delivery_date` reused from `a0bf3bd210f8` — no duplicate column added).

## Task Completion Gate

- Tasks artifact read: `openspec/changes/order-state-machine/tasks.md` (pre-move) and re-validated in the archived copy.
- **27/27 tasks checked, 0 unchecked** — gate PASSED, no stale checkboxes, no archive-time reconciliation needed.

## Final Verification State (at close)

Source: `verify-report.md` (schema `gentle-ai.verify-result/v1`, `evidence_revision: sha256:4689616610843010437440877ff275287998bdc60cb0173795d4e977061986f1`), committed AFTER apply-progress in `17be566` ("docs(sdd): record order-state-machine verify report (pass with warnings)").

| Metric | Final value |
|--------|-------------|
| Verdict | `pass` (PASS WITH WARNINGS) |
| CRITICAL findings | 0 |
| Blockers | 0 |
| Requirements compliant | 22/22 |
| Scenarios compliant | 43/43 (42 COMPLIANT, 1 PARTIAL) |
| Tasks | 27/27 complete |
| Tests | `pytest` 679 passed / 0 failed / 0 errors / 7 warnings (alembic deprecation only), exit 0 |
| `ruff check src tests` | exit 0 (clean) |
| `ruff format --check src tests` | exit 0 (111 files already formatted) |
| `gentle-ai sdd-verify-validate` | valid:true pass |
| Migration round-trip | fresh-DB upgrade/downgrade/re-upgrade safe; real dev-DB live rows renamed in place, zero deleted |

**WARNINGs (3, spec-acceptable — carried to archive)**:
1. Pricing-at-quote-step deviation — pricing runs at the quote/persist step; confirm consumes frozen source-priced snapshots + owner adjustments (re-pricing at confirm would clobber adjustments). Every observable pricing outcome the spec scenarios assert is implemented and passes.
2. Case B local-portion deduction deferred — a draft re-classified Case B at confirm converts/deducts nothing for its available LOCAL portion; fulfillment rides the PO-receipt axis (PO independence per sourcing spec; no leak or double-count).
3. Case B cancel policy runtime-proven from CONFIRMED — the exact Picking-state GIVEN is covered by code-path analysis only (`state.py` imports no PO/SourcingNeed entities, so cancel cannot touch them from any state). Narrow composition gap, not a violation.

**SUGGESTIONs (3, advisory)**:
1. Add a dedicated test: Case B order advanced to Picking, then canceled.
2. Note surviving DRAFT/PICKING enum labels in operator release notes (downgrade leaves them; PG cannot drop enum values).
3. `docs/estado-pedido.md` remains the historical gap analysis; consider archiving it under the change folder — recorded here, NOT executed: moving repo docs was outside the archive scope authorized by the orchestrator.

**Issues**: no open implementation defects. No unrankable contradictions between sources: `verify-report` and the orchestrator's final-state handoff agree; `apply-progress.md` (written before the final verify run) does not claim any task incomplete, so no stale snapshot claims needed correction.

## Implementation State (git, at close)

12 implementation commits on `feat/order-state-machine` (base `main`), all pushed:

`0b61738` (migration) · `85a1523` (model) · `f380e8d` (db tests) · `34d3c0c` (planning artifacts + docs/estado-pedido.md) · `e504eb1` (slice-1 progress) · `f0612da` (six-state transitions) · `843b892` (suite migration) · `01700ac` (draft persistence + remove-product) · `310273d` (confirm ceremony) · `c56090c` (backoffice fulfillment) · `fce755f` (sweep + progress) · `17be566` (verify report).

## Native Runtime Ledger

Full-change objective PASSED (3,318 changed lines; maintainer-approved reset to 3,600, actor redaumore); verify objective COMPLETE. No pending ledger blocks.

## Delivery State (out of archive scope)

- **PR #14**: open, base `main`, `Closes #13`, label `type:feature` — NOT merged. Merging/PR delivery remains a separate human decision under ordinary repository policy.
- **Issue #13**: open.
- Archive did not merge or close PR #14 / issue #13, and did not touch `src/` or `tests/`.

## Spec Sync (delta → main specs)

| Domain | Action | Delta requirements | Final in main spec |
|--------|--------|--------------------|--------------------|
| order-lifecycle | Merge — 4 MODIFIED + 2 RENAMED + 1 ADDED | 5 blocks / 12 scenarios | 9 requirements / 20 scenarios |
| order-sourcing | Merge — 4 MODIFIED + 1 ADDED | 5 blocks / 10 scenarios | 8 requirements / 15 scenarios |
| customer-order-persistence | Merge — 3 MODIFIED + 1 RENAMED + 2 ADDED | 5 blocks / 10 scenarios | 8 requirements / 15 scenarios |
| backoffice | Merge — 2 MODIFIED + 1 ADDED | 3 blocks / 9 scenarios | 10 requirements / 25 scenarios |
| barcode-stock-ops | Merge — 1 ADDED | 1 block / 2 scenarios | 6 requirements / 11 scenarios |

- RENAMED: `Rejection releases reservations → Cancellation releases or restores stock`; `Register approved orders → Register confirmed orders` (order-lifecycle); `Save side effects (stock now, Sheets at approval) → Save side effects (reserve and sync at confirm)` (customer-order-persistence).
- No REMOVED requirements in any delta; no destructive merges (config rule "Warn before merging destructive deltas" — nothing to warn about).
- MODIFIED merges replaced the matching requirement block with the delta's full updated requirement; ADDED requirements appended. Untouched requirements preserved verbatim.
- Byte-identity verified: every delta requirement body present verbatim in the merged main spec (scripted block comparison, zero differences); renamed old names absent, new names present.
- Mechanical Copy Contract honored: the archive folder move used shell-only `git mv` with a pre-move recursive snapshot and a MANDATORY empty `diff -r` readback (verbatim output in the phase result: exit 0, no differences). Content merges are the documented model-mediated merge step.
- Archived `tasks.md` re-validated: 0 unchecked, 27 checked.

## Archive Contents

- `exploration.md` ✅
- `proposal.md` ✅
- `specs/` (5 domains) ✅
- `design.md` ✅
- `tasks.md` ✅ (27/27 complete)
- `apply-progress.md` ✅ (intermediate snapshot, superseded by this report)
- `verify-report.md` ✅ (final, PASS WITH WARNINGS)
- `archive-report.md` ✅ (this file, additive-only)

## Traceability

Source artifacts were read from OpenSpec filesystem paths (openspec mode locators), not Engram observations, so no Engram observation IDs were read for source artifacts. Files read: `openspec/changes/order-state-machine/{exploration.md, proposal.md, design.md, tasks.md, apply-progress.md, verify-report.md, specs/{order-sourcing,barcode-stock-ops,order-lifecycle,customer-order-persistence,backoffice}/spec.md}` and `openspec/specs/{domain}/spec.md` (5) and `openspec/config.yaml`. The Engram mirror of this archive report is observation ID **285** (sync `obs-f7634476bdf3b29e`, topic_key `sdd/order-state-machine/archive-report`), saved via the session `both` preflight on 2026-09-04.

## Intentional-Override / Warning Notes

- No intentional partial-archive or stale-checkbox reconciliation was requested or performed.
- 3 spec-acceptable WARNINGs and 3 advisory SUGGESTIONs carried from verify-report (recorded above); no CRITICALs, so nothing blocks archive.
- No unrankable contradictions recorded — all sources agree on the final state at close.
