# Archive Report: order-sourcing-workflow

**Change**: order-sourcing-workflow
**Status**: ARCHIVED — SDD cycle complete
**Archived**: 2026-08-27
**Evidence revision**: `acff28b` (commit `acff28b docs(tasks): mark all order-sourcing-workflow tasks complete`; work-unit commits `379df67..acff28b` landed on main)
**Persistence**: hybrid (OpenSpec files + Engram `sdd/order-sourcing-workflow/archive-report`)
**Review gate**: `disabled/unmanaged` — no native review governed this change. No ledger/receipt/transaction exists for `order-sourcing-workflow` (no `review/` directory, no Engram review observations); per the project convention recorded in the terminal archive report of the prior cycle (`2026-08-22-mvp-ferreteria/archive-report.md`: "no native review governed this change... kill switch off"), the native gate is disabled while the kill switch is off. No explicit review artifact failed validation; the gate does not manufacture `allow`.

## Change Summary

Customers describe orders in free language. This change built customer-order creation and sourcing on top of the MVP intake pipeline: structured NL parsing (customer, items/quantities, fuzzy delivery date), a real `Inventory` table as the single on-hand stock source, a sourcing decision that routes each parsed order to Case A (full stock → `PENDING_ASSEMBLY` through the unchanged quotation/approval flow), Case B (partial → supplier selection persisted on the order, accumulating one OPEN `SupplierPurchaseOrder` per supplier → `IN_PREPARATION`), or Case C (no supplier → `REJECTED` + sourcing `CANCELLED` + notify). New `SupplierPurchaseOrder` entity with its own state machine (OPEN → SENT → PARTIALLY_RECEIVED → FULLY_RECEIVED, CANCELLED), a `SupplierCatalogSearcher` protocol seam for the external RAG, DB-row rehydration so Case B multi-turn selection survives the 30-minute in-memory TTL, and a backoffice Purchase Orders view with send/receive/cancel execution.

## Final Quality Numbers (close-of-cycle)

From `verify-report.md` (evidence revision `acff28b`, dated 2026-08-27) — highest-ranked source covering final state; no later work changed these figures:

- **Verify verdict**: **PASS WITH WARNINGS** — blockers 0, critical findings 0
- **Requirements**: 7/7 domain specs complete; **Scenarios**: 36/36 passing across 7 domains
- **Tests**: 354 passed (exit 0) via `.venv/bin/pytest -q --cov=src --cov-fail-under=85`
- **Coverage**: 94.31% (threshold 85%)
- **Lint**: `.venv/bin/ruff check src/ tests/` exit 0
- **Type check**: `.venv/bin/mypy src/` exit 0 (50 source files)
- **Tasks**: 37/37 complete in `tasks.md` — no stale unchecked implementation tasks

## Final-State Notes

- **No post-verify fixes**: no blockers were resolved after verification and no fixes landed beyond what `verify-report.md` already records (per orchestrator final-state handoff; corroborated by commit range `379df67..acff28b`).
- **`verify-report.md` is currently UNTRACKED in git** (not part of commit `acff28b`). It is scheduled to be committed in a later session together with the `owner-order-intake` work; the archive phase performs no git operations. The file's on-disk content is authoritative and is preserved in this archive folder.

## Open WARNINGS at Close (non-blocking, deferred)

Per `verify-report.md` issues section, three WARNING items remain open — no current drift, maintenance risk only; none was fixed after verification:

1. **Barcode dual-write drift risk** — `src/barcode/decoder.py:131-138` writes both `Catalogo.stock_disponible` and `Inventory.quantity_on_hand`. Design intended `Inventory` as the single on-hand source; the legacy counter update should be retired.
2. **Backoffice catalog dual-write** — `src/backoffice/catalog.py:59-66` dual-writes the same two counters.
3. **Backoffice ingestion dual-write** — `src/backoffice/ingestion.py:92-103,122-126` dual-writes on update and create.

Plus one non-blocking SUGGESTION: production wires `FakeSupplierCatalogSearcher` (intentional safe degradation for MVP); replace with the real RAG-backed searcher before scaling.

## Specs Synced to Source of Truth

Delta specs under `openspec/changes/order-sourcing-workflow/specs/` were synced into `openspec/specs/{domain}/spec.md`:

| Domain | Action | Requirements | Scenarios | Details |
|--------|--------|--------------|-----------|---------|
| backoffice | Merged (ADDED) | 4 → 5 | 8 → 11 | Appended "Purchase order view and execution" (3 scenarios) |
| local-inventory | Copied (NEW) | 3 | 6 | Full spec → base |
| order-lifecycle | Merged (MODIFIED) | 8 | 15 → 16 | Replaced "Track order state machine" with sourcing-axis + delivery_date version; added "Sourcing axis is independent of approval" scenario. All other 7 requirements preserved untouched |
| order-sourcing | Copied (NEW) | 7 | 14 | Full spec → base — prerequisite capability for the `owner-order-intake` change's MODIFIED delta |
| purchase-order-lifecycle | Copied (NEW) | 3 | 5 | Full spec → base |
| supplier-catalog-search | Copied (NEW) | 1 | 3 | Full spec → base |
| whatsapp-order-intake | Merged (ADDED) | 4 → 5 | 8 → 10 | Appended "Extract structured order fields" (2 scenarios) |
| **TOTAL** | 3 merged + 4 copied | **32** | **65** | |

### Destructive-merge check (config.yaml `rules.archive`)

No REMOVED or RENAMED sections exist in any delta; the single MODIFIED (`order-lifecycle` / "Track order state machine") extends the requirement and adds a scenario — nothing is deleted. No destructive merge occurred, so no warning is required by `rules.archive`.

Merge fidelity notes:
- Requirements were matched by name; all requirements not mentioned in the deltas were preserved byte-for-byte.
- The delta's `(Previously: tracked only the four approval states...)` parenthetical is a change-note, not normative requirement text; it was dropped from the merged main spec. Recorded here for traceability.

## Archive Contents

- `proposal.md` ✅
- `exploration.md` ✅
- `design.md` ✅
- `tasks.md` ✅ (37/37 complete)
- `verify-report.md` ✅
- `specs/` ✅ (7 domains: backoffice, local-inventory, order-lifecycle, order-sourcing, purchase-order-lifecycle, supplier-catalog-search, whatsapp-order-intake)
- `archive-report.md` ✅ (this file)

## Intentional Archive Notes

None — clean archive. No partial-archive and no stale-checkbox reconciliation was required. Archive is intentional, not intentional-with-warnings. Three non-blocking WARNING items (dual-write maintenance risk) are carried forward from `verify-report.md` as deferred follow-ups, per the Final-State Authority hierarchy (verify snapshot is the highest-ranked source for these; no later fix exists).

## Prerequisite for `owner-order-intake`

`openspec/specs/order-sourcing/spec.md` now exists as a base capability spec. The `owner-order-intake` change's `order-sourcing` MODIFIED delta can be merged against it during that change's archive.

## Traceability

Engram observations (project `super-warehouse`): explore #153, proposal #154, spec #155, design #156, tasks #158, apply-progress #159, verify-report #161, plus English-only naming preference #157. `verify-report` on disk is authoritative; Engram #161 is its mirror. This report: topic_key `sdd/order-sourcing-workflow/archive-report`.