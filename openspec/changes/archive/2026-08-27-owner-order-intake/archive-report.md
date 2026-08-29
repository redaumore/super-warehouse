# Archive Report: Owner Order Intake

**Change**: `owner-order-intake`
**Archived**: 2026-08-27
**Archive path**: `openspec/changes/archive/2026-08-27-owner-order-intake/`
**Artifact store**: hybrid (OpenSpec files + Engram)
**Status**: ✅ ARCHIVED — intentional, complete (no warnings required)

---

## 1. Gates

| Gate | Result | Evidence |
|------|--------|----------|
| Native Review Receipt | ✅ PASS (relaxation) | `reviewGate.delivery: disabled/unmanaged` (project convention, kill switch off). No explicit review artifacts exist for this change (`reviews/` absent; native `sdd-status` lists no reviewPolicy/ledger/receipt). |
| Task Completion | ✅ PASS | `tasks.md` 22/22 checkboxes `[x]`; native `sdd-status` `taskProgress: {total: 22, completed: 22, allComplete: true}`. No stale unchecked implementation tasks. |
| CRITICAL findings | ✅ PASS | `verify-report.md` verdict `pass`, `critical_findings: 0`, `blockers: 0`. |
| Action context | ✅ PASS | `actionContext.mode: repo-local`; `allowedEditRoots: [workspace root]`; archive ops inside root. |

Native dispatcher: `nextRecommended: archive`, `blockedReasons: []`.

## 2. Final State (authority-ranked)

Per the Final-State Authority hierarchy, the following is the state of the change AT CLOSE:

| Fact | Value | Source rank |
|------|-------|-------------|
| Tasks | 22/22 complete | Persisted tasks artifact + native `sdd-status` (rank 2) |
| Verify verdict | **PASS**, 0 blockers, 0 critical | `verify-report` #180 (rank 4) **confirmed current** by launch-prompt final-state facts (rank 3): no post-verify fixes were made |
| Requirements | 11/11 compliant (whatsapp-order-intake 5, agent-orchestration 3, order-sourcing 3) | verify-report #180 + launch prompt |
| Scenarios | 26/26 compliant (12 + 10 + 4) | verify-report #180 + launch prompt |
| Tests | 421 passed | verify-report #180 + launch prompt |
| Coverage | 94.79% (threshold 85%) | verify-report #180 + launch prompt |
| Lint / types | ruff clean, mypy clean (52 source files) | verify-report #180 + launch prompt |
| Delivery | `exception-ok` — single PR, maintainer pre-approved `size:exception` | tasks.md forecast + launch prompt |
| Review gate | `disabled/unmanaged` (project convention) | launch prompt + absent review artifacts |
| SUGGESTION findings | 1 open: `FakeSupplierCatalogSearcher` in production pipeline (documented safe MVP degradation, `src/pipeline.py:90`) | verify-report #180, carried as final state (non-blocking) |

**Contradiction log**: None. All sources agree; `verify-report` #180 is current (launch prompt explicitly states no post-verify fixes). No unrankable contradictions recorded.

**Snapshot-derived claims**: `apply-progress` #176 (rank 4, written 2026-08-27 20:44) reported all 22/22 tasks complete with a note that the launch brief said 21 — the persisted tasks artifact (rank 2) shows 22 tasks, all `[x]`; verification (rank 4, later) counted 22/22. No conflict.

## 3. Spec Sync (delta → main)

| Domain | Action | Details |
|--------|--------|---------|
| whatsapp-order-intake | Updated | 2 MODIFIED replaced (`Ingest text and voice orders`, `Ephemeral acknowledgement under 5 seconds`), 3 ADDED appended (`Restrict senders to the owner`, `Resolve customer by name`, `Create client in chat`), 3 untouched preserved (`Transcribe voice notes`, `Handle transcription failure`, `Extract structured order fields`). Purpose updated to owner-first wording. |
| agent-orchestration | Updated | 2 MODIFIED replaced (`Route inbound messages to the correct agent` — 4 scenarios; `Orchestrator coordinates the end-to-end flow` — 3 scenarios), 1 ADDED appended (`Wire DISPATCH to the approval flow` — 3 scenarios), 2 untouched preserved (`Six specialized agents`, `Run heavy processing asynchronously`). |
| order-sourcing | Updated | 3 MODIFIED replaced (`Case A creates order via quotation flow` — scenario renamed to `Full-stock order confirmed in owner chat`; `Case B lists missing items and suppliers`; `Case C notifies unavailability`), 4 untouched preserved (`Classify sourcing case from availability`, `Multi-turn supplier selection persisted on the order`, `Case B creates or accumulates purchase orders`, `Capture delivery date`). Merged against the base spec created by archiving `order-sourcing-workflow` (2026-08-27). |

No `REMOVED` or top-level `RENAMED` requirement sections existed in any delta, so no requirement deletion required `(Reason: ...)` / `(Migration: ...)` notes.

**Destructive-delta warning (per `openspec/config.yaml` rules.archive)**: ⚠️ One scenario was superseded inside a MODIFIED block: `agent-orchestration` / `Orchestrator coordinates the end-to-end flow` dropped the old scenario `Human-in-the-loop wait is handled` (phone-keyed resume of the correct order). It is replaced by the two new delta scenarios `Latest open order wins on rehydration` and `pedido #N overrides to a specific order`, which describe the new owner-keyed resume behavior. Verification (26/26 scenarios) covers the replacement set; the old scenario described the removed phone-keyed behavior. Not a large-section removal; merged without halt, recorded here per config rule.

**Preserved-requirement note (non-blocking)**: The delta left `whatsapp-order-intake` / `Handle transcription failure` untouched; its scenarios still say "notifies the customer" / "prompts the customer". Under the owner pivot the recipient is the owner. Preserved verbatim per delta boundary (skill rule: preserve requirements not mentioned in the delta). Recommend a follow-up spec-cleanup change to reword this requirement. `Extract structured order fields` remains correct as written (the order's *customer* is still resolved by name).

## 4. Source of Truth Updated

- `openspec/specs/whatsapp-order-intake/spec.md`
- `openspec/specs/agent-orchestration/spec.md`
- `openspec/specs/order-sourcing/spec.md`

## 5. Archive Contents

- `proposal.md` ✅
- `exploration.md` ✅
- `design.md` ✅
- `specs/{whatsapp-order-intake,agent-orchestration,order-sourcing}/spec.md` ✅ (delta specs, unmodified audit trail)
- `tasks.md` ✅ (22/22 complete, no unchecked implementation tasks)
- `verify-report.md` ✅
- `archive-report.md` ✅ (this file)

Active changes directory no longer contains `owner-order-intake`.

## 6. Engram Traceability (hybrid store)

Change-artifact observations persisted in Engram (project `super-warehouse`):

| Artifact | Engram observation | sync_id |
|----------|--------------------|---------|
| exploration | #164 | `obs-02458b805901a778` |
| proposal | #166 | `obs-a68138dddf9f9062` |
| spec (deltas) | #168 | `obs-7d07cfd285149fb3` |
| design | #172 | `obs-9404d4e11e0ecdae` |
| tasks | #175 | `obs-07105ac532d54176` |
| apply-progress | #176 | `obs-a4aaf85966b07bdd` |
| verify-report | #180 | `obs-7e4768f1dc7d45c2` |
| related decisions | #167, #173, #174 | `obs-f058aee52ade6418`, `obs-d9e4a21c178e94ac`, `obs-f23dd8000c968ee8` |
| related discovery | #165 | `obs-4c00d917f89fe8b3` |
| archive-report | see `sdd/owner-order-intake/archive-report` topic | (persisted at archive time) |

## 7. Notes

- Implementation is complete in the working tree but NOT committed; the orchestrator will commit later. No git commands were run by the archive phase.
- Rollback of the change itself: revert + redeploy; `owner_phone` stays parseable; clearing the two owner keys returns to legacy customer intake (per design.md).
- Open SUGGESTION carried forward (non-blocking): replace `FakeSupplierCatalogSearcher` with the real RAG searcher before scaling.

## 8. SDD Cycle Complete

The `owner-order-intake` change has been fully planned, implemented, verified, and archived. Ready for the next change.