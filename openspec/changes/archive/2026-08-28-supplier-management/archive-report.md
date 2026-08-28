# Archive Report: Supplier Management

**Change**: `supplier-management`
**Archived**: 2026-08-28
**Archive path**: `openspec/changes/archive/2026-08-28-supplier-management/`
**Artifact store**: hybrid (OpenSpec files + Engram)
**Status**: ✅ ARCHIVED — intentional, complete (no warnings required)

---

## 1. Gates

| Gate | Result | Evidence |
|------|--------|----------|
| Native Review Receipt | ✅ PASS (relaxation) | `reviewGate.delivery: disabled/unmanaged` — receipt-driven development is DISABLED clone-local for this repo (user decision; kill switch off; no review governs this change). No explicit review artifacts exist (`reviews/` absent). No fabricated approvals. |
| Task Completion | ✅ PASS | `tasks.md` 34/34 checkboxes `[x]` across 6 phases. No stale unchecked implementation tasks. |
| CRITICAL findings | ✅ PASS | `verify-report.md` envelope: `critical_findings: 0`, `blockers: 0`. Envelope `verdict: fail` is solely due to `test_exit_code: 1` from 3 pre-existing env-dependent failures (see §2) — NOT a CRITICAL finding and NOT a regression. |
| Action context | ✅ PASS | `actionContext.mode: repo-local`; archive operations inside workspace root. |

**RDD-off footnote (non-blocking)**: the native sdd-attempt runtime ledger for this change remains in `blocked/maintainer_decision` from the pre-disable accounting (apply attempt exceeded the 2500-line budget at 3306 changed lines; the ledger row was never settled before RDD was disabled). This is recorded for traceability, NOT treated as a blocker: RDD-off was the user's explicit choice, and delivery now follows ordinary repository policy with `reviewGate.delivery: disabled/unmanaged` (same posture as the `owner-order-intake` cycle).

## 2. Final State (authority-ranked)

Per the Final-State Authority hierarchy, the following is the state of the change AT CLOSE:

| Fact | Value | Source rank |
|------|-------|-------------|
| Tasks | 34/34 complete | Persisted tasks artifact (rank 2) |
| Verify envelope | `fail` (test_exit_code 1), 0 blockers, 0 CRITICAL | `verify-report` #203 (rank 4) |
| Substantive verdict | PASS — 11/11 requirements, 26/26 scenarios compliant | `verify-report` #203 + launch-prompt final-state facts (rank 3) |
| Tests (final) | **485 passed / 3 failed** | `verify-report` #203 + launch prompt (rank 3/4) |
| Lint / format / types | ruff clean, `ruff format --check` clean, mypy strict clean (55 source files) | `verify-report` #203 (rank 4) |
| Migration | `46bdbdc4a575` (down_revision `5f304e18a765`): destructive legacy deletion (user-approved), English rename, enums `SupplierStatus` (ACTIVO/INACTIVO) + `IvaCondition` (RESPONSABLE_INSCRIPTO, MONOTRIBUTO, EXENTO, CONSUMIDOR_FINAL, NO_RESPONSABLE), unique `code` index, partial unique `cuit` index | `verify-report` #203 + launch prompt (rank 3/4) |
| Delivery | `single-pr` with maintainer-approved `size:exception` (~3306 changed lines vs 2500 budget) | tasks.md forecast + launch prompt |
| Work-unit commits | `7368a4b` (WU1 schema+migration+helpers), `75a0510` (WU2 backoffice+guards+rename), `9797898` (WU3 test fallout+verification), `41e97a1` (WU4 remediation: `test_fake_searcher_excludes_inactive_candidates` — resolved the 3 PARTIAL scenarios). NOT pushed, no PR created. | launch prompt + git log |
| Review gate | `disabled/unmanaged` (RDD-off clone-local, user decision) | launch prompt + Engram #202 |
| SUGGESTION findings | 1 open: document the local `.env` owner-gate requirement for green local test runs | `verify-report` #203, carried as follow-up (non-blocking) |

**The 3 failing tests — corrected attribution**: `tests/test_pipeline.py::test_handle_inbound_routes_persists_and_replies`, `tests/test_pipeline.py::test_second_message_resumes_context`, `tests/test_pipeline.py::test_voice_routes_to_perception_reply`. Cause: local `.env` sets `OWNER_TELEGRAM_CHAT_ID=2074034510` (config field `owner_telegram_chat_id`), which triggers the owner-gate path in the pipeline and returns a hardcoded Spanish message instead of the mocked responder output. Verified identical at base commit `f4568c8`; CI without that `.env` passes. **NOT regressions.** The verify phase corrected an earlier attribution in `apply-progress` #199 (see contradiction log below).

**Contradiction log** (recorded, not silently resolved):

1. **Scenario count**: the `verify-report` #203 envelope and the launch prompt both report **26/26 scenarios compliant**, but a structural count of `#### Scenario:` headings across the 5 delta specs yields **23** (supplier-management 13, backoffice 3, supplier-catalog-search 4, purchase-order-lifecycle 2, supplier-document-ingestion 1) — and the verify report's own compliance matrix lists 23 scenario rows. The requirements count (11/11) matches the delta structure exactly, so the discrepancy is specific to the scenario total. Both numbers are carried: **26/26** is the verify-reported compliance total (rank 3/4), **23** is the delta heading count.
2. **Test attribution**: `apply-progress` #199 (written 2026-08-28 13:33, rank 4) attributed the 3 failures to `tests/test_pipeline_owner.py` / `tests/test_owner_gate.py`. `verify-report` #203 (written 2026-08-28 14:13, later) and the launch prompt corrected the attribution to `tests/test_pipeline.py` — the owner-gate pipeline tests pass; the failing tests are pipeline tests whose reply differs under the local `.env` owner-gate path. Resolved by rank (later verify report + launch prompt win); the correction is carried in the table above.

**Snapshot-derived claims** (attributed to source and time, not stated as bare present facts):
- `apply-progress` #199 (2026-08-28 13:33) reported **484 passed / 3 failed** — accurate at apply time, before WU4 remediation commit `41e97a1` added `test_fake_searcher_excludes_inactive_candidates` and brought the suite to **485** (final count per `verify-report` #203 and the launch prompt).
- `apply-progress` #199 reported the 3 failures under `test_pipeline_owner.py`/`test_owner_gate.py` — superseded by the corrected attribution above.

## 3. Spec Sync (delta → main)

| Domain | Action | Details |
|--------|--------|---------|
| supplier-management | **Created** | NEW capability — no main spec existed. The delta IS a full spec: copied verbatim to `openspec/specs/supplier-management/spec.md` (7 requirements, 13 scenarios). |
| backoffice | Updated | 1 ADDED appended (`Supplier management module` — 3 scenarios: list/search/filter, toggle status, create with reactive validation). 5 existing requirements preserved untouched. Purpose updated with a minimal wording addition ("supplier master data") to stay current with the capability, matching repo convention. |
| supplier-catalog-search | Updated | 1 MODIFIED replaced (`Supplier catalog searcher seam`) — requirement now includes "Candidate results MUST exclude INACTIVO suppliers" and a new `Inactive supplier excluded` scenario; the 3 pre-existing scenarios carried inside the replaced block. The delta's `(Previously: ...)` change-note was dropped from the merged main spec per repo convention (see `order-sourcing-workflow` archive report). |
| purchase-order-lifecycle | Updated | 1 ADDED appended (`Refuse inactive suppliers` — 2 scenarios: PO creation, accumulation). 3 existing requirements preserved untouched. |
| supplier-document-ingestion | Updated | 1 ADDED appended (`Refuse inactive suppliers at confirmation` — 1 scenario). 7 existing requirements preserved untouched. |

No `REMOVED` or top-level `RENAMED` requirement sections existed in any delta, so no requirement deletion required `(Reason: ...)` / `(Migration: ...)` notes.

**Destructive-delta warning (per `openspec/config.yaml` `rules.archive`)**: none triggered — all syncs were additive appends or a single-requirement replacement; no large sections were removed from any main spec.

## 4. Source of Truth Updated

- `openspec/specs/supplier-management/spec.md` (created)
- `openspec/specs/backoffice/spec.md`
- `openspec/specs/supplier-catalog-search/spec.md`
- `openspec/specs/purchase-order-lifecycle/spec.md`
- `openspec/specs/supplier-document-ingestion/spec.md`

## 5. Archive Contents

- `proposal.md` ✅
- `exploration.md` ✅
- `design.md` ✅
- `specs/{supplier-management,backoffice,supplier-catalog-search,purchase-order-lifecycle,supplier-document-ingestion}/spec.md` ✅ (delta specs, unmodified audit trail)
- `tasks.md` ✅ (34/34 complete, no unchecked implementation tasks)
- `apply-progress.md` ✅ (previously untracked; now under the archived dir, committed with the archive)
- `verify-report.md` ✅
- `archive-report.md` ✅ (this file)

Active changes directory no longer contains `supplier-management` (only `archive/` remains).

## 6. Engram Traceability (hybrid store)

Change-artifact observations persisted in Engram (project `super-warehouse`):

| Artifact | Engram observation | sync_id |
|----------|--------------------|---------|
| proposal | #191 | `obs-38f66fa041a69d43` |
| spec (deltas) | #193 | `obs-5d3cc50ffa8c01d1` |
| design | #194 | `obs-a15051c5dfd3c8b3` |
| tasks | #197 | `obs-7aa41a65cf321845` |
| apply-progress | #199 | `obs-44396e21daf38833` |
| verify-report | #203 | `obs-549cf27b3ee383ba` |
| RDD-off decision (config) | #202 | `obs-c73290d3aae5d20d` |
| exploration | filesystem only | — (optional artifact; no Engram observation) |
| archive-report | topic `sdd/supplier-management/archive-report` | (persisted at archive time) |

## 7. Notes & Follow-ups

**Follow-ups (recorded, NOT fixed):**
1. `openspec/config.yaml` context block is stale — it still claims "greenfield / undecided (no source code, no package manager, no framework)" and empty tooling, while the repo runs Python 3.12+, SQLAlchemy 2.0 + Alembic, Postgres/pgvector, Gradio, pytest, ruff, and mypy. Left untouched to keep the archive commit scoped to the enumerated change; recommend a `sdd-init` refresh.
2. Verify SUGGESTION carried forward: document the local `.env` `OWNER_TELEGRAM_CHAT_ID` owner-gate requirement (e.g., a `.env.test` or runbook note) so local test runs are green without CI's clean environment.

**RDD-off footnote**: see §1 — the native sdd-attempt runtime ledger remains in `blocked/maintainer_decision` from the pre-disable accounting; not a blocker under `reviewGate.delivery: disabled/unmanaged`.

**Branch note**: the work-unit commits live on branch `feat/owner-order-intake` (branch reused from the previous cycle). NOT pushed; no PR created. If a PR is opened later for this change, branch naming should be revisited.

**Rollback**: `alembic downgrade` restores the schema but NOT the deleted legacy supplier rows (user-approved destructive deletion, unrecoverable); reverting the commits restores prior behavior (no cascades).

**Commit hygiene**: the one-line trailing-blank deletion in `docker-compose.yml` (formatting churn only) was folded into the archive commit as a `chore`; no `Co-Authored-By` or AI attribution.

## 8. SDD Cycle Complete

The `supplier-management` change has been fully planned, implemented, verified, and archived. Ready for the next change.