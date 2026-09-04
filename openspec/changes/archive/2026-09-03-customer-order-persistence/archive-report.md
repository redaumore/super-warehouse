# Archive Report: customer-order-persistence

**Archived**: 2026-09-03
**Archive path**: `openspec/changes/archive/2026-09-03-customer-order-persistence/`
**Artifact store mode**: hybrid (OpenSpec files + Engram)
**Final-state authority**: this report is the terminal record of the cycle and reflects the state of the change AT CLOSE, per the Final-State Authority hierarchy (persisted tasks artifact > orchestrator launch facts > intermediate snapshots).

## Cycle Summary

- **Proposal → Archive**: full SDD cycle completed — proposed, specified (5 delta capability specs), designed, planned (21 tasks), implemented (21/21 complete), verified (verify run #3), archived.
- **Delivery strategy**: `single-pr` (no chaining). Strict TDD OFF (Standard mode, `strict_tdd: false` in `openspec/config.yaml`).
- **Repo base for the change**: `e261524`.

## Task Completion Gate

- Tasks artifact read: `openspec/changes/customer-order-persistence/tasks.md` (pre-move) and re-validated in the archived copy.
- **21/21 tasks checked, 0 unchecked** — gate PASSED, no stale checkboxes, no archive-time reconciliation needed.

## Final Verification State (at close)

Source: `verify-report.md`, verify run #3 (schema `gentle-ai.verify-result/v1`, `evidence_revision: sha256:705cc770dd075da9919cd5a0b0f833ef142e559af7d2b2134f803910d3319c17`).

| Metric | Final value |
|--------|-------------|
| Verdict | `pass_with_warnings` |
| CRITICAL findings | 0 |
| Blockers | 0 |
| Requirements compliant | 14/14 |
| Scenarios compliant | 33/33 (0 PARTIAL, 0 UNTESTED) |
| Tests | 577 passed / 0 failed / 0 skipped (exit 0) |
| `ruff check src tests` | exit 0 (clean) |
| `mypy src` | exactly 3 pre-existing baseline errors (`src/backoffice/app.py:17,192,196`), zero new |
| Remediation outstanding | none (`remediationState.required: false`) |

The 5 PARTIAL scenarios from verify run #2 were closed by 5 covering tests that landed in the uncommitted working-tree batch AFTER `apply-progress.md`'s last refresh; verify run #3 executed against the current working tree including that batch.

## Final Size (supersedes apply-progress figure)

- Final authored diff vs base `e261524` (per verify run #3): **2493 insertions / 48 deletions = 2541 authored changed lines** (generated `docs/escenarios-testeados.md` excluded; 2569 total including the 28-line generated-docs delta).
- `apply-progress.md` records a stale "fresh measurement" of 2369 authored lines; that figure is superseded by the run #3 measurement above. The 172-line delta is exactly the 5 covering tests added after the apply-progress refresh.
- **WARNING 1 (carried, not fixed)**: the owner earlier accepted a ~2083-line size exception; the current 2541 exceeds it. verify-report records this as WARNING 1 with no trimming. Recorded here as a warning; no artifact was modified or trimmed. Applies to PR delivery, not archive.

## Implementation State (git, at close)

- 5 implementation commits landed: `90881fe`, `dde0188`, `eacbd00`, `2eed076`, `404a1b2`.
- An uncommitted surgical-fix batch remains in the working tree: 12 modified files, 316 insertions / 27 deletions (includes the 5 covering tests that closed the run #2 PARTIALs).
- Committing the uncommitted batch and opening the PR are separate human-owned delivery steps under ordinary repository policy — out of scope for archive. Recorded for accuracy.

## Spec Sync (delta → main specs)

| Domain | Action | Requirements (final in main spec) |
|--------|--------|-----------------------------------|
| backoffice | Merge — 3 ADDED | 9 requirements / 20 scenarios |
| clients-and-price-lists | Merge — 1 ADDED | 6 requirements / 13 scenarios |
| customer-order-persistence | NEW domain — mechanical shell copy (delta is full spec) | 6 requirements / 12 scenarios |
| pricing-engine | Merge — 1 MODIFIED + 2 ADDED | 6 requirements / 14 scenarios |
| supplier-management | Merge — 1 MODIFIED | 7 requirements / 15 scenarios |

- No REMOVED or RENAMED requirements in any delta; no destructive merges (config rule "Warn before merging destructive deltas" — nothing to warn about).
- MODIFIED merges replaced the matching requirement block with the delta's full updated requirement (all scenarios preserved per delta content). Untouched requirements preserved verbatim.
- Mechanical Copy Contract honored: the NEW domain copy and the archive folder move used shell-only `cp`/`mv` with mandatory empty `diff -r` readbacks (verbatim outputs in the phase result). Content merges are the documented model-mediated merge step.
- Archived `tasks.md` re-validated: 0 unchecked, 21 checked.

## Archive Contents

- `proposal.md` ✅
- `specs/` (5 domains) ✅
- `design.md` ✅
- `tasks.md` ✅ (21/21 complete)
- `verify-report.md` ✅ (run #3)
- `apply-progress.md` ✅ (size figures stale — superseded by this report)
- `explore.md` ✅ (present)
- `archive-report.md` ✅ (this file, additive-only)

## Traceability

Source artifacts were read from OpenSpec filesystem paths (openspec mode locators), not Engram observations, so no Engram observation IDs were read for source artifacts. Files read: `openspec/changes/customer-order-persistence/{proposal.md, specs/backoffice/spec.md, specs/clients-and-price-lists/spec.md, specs/customer-order-persistence/spec.md, specs/pricing-engine/spec.md, specs/supplier-management/spec.md, design.md, tasks.md, apply-progress.md, verify-report.md}` and `openspec/config.yaml`. The Engram copy of this archive report is observation ID **252** (sync `obs-308c3d2641bdcbb6`), saved via hybrid persistence on 2026-09-03.

## Intentional-Override / Warning Notes

- Size exception exceeded (WARNING 1 above) — recorded, not trimmed, per verify policy; archive proceeds because there are no CRITICALs and no blockers.
- No other intentional partial-archive or stale-checkbox reconciliation was requested or performed.
- No unrankable contradictions: the only snapshot-vs-final discrepancy (apply-progress 2369 vs final 2541) is explicitly corrected by the higher-ranked verify-report run #3.