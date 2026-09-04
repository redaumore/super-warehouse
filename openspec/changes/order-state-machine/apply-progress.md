# Apply Progress: order-state-machine — Slice 1 (PR 1)

**Mode**: Standard (`strict_tdd: false`)
**Delivery**: `auto-chain`, `stacked-to-main`, 2000-line review budget
**Status**: Tasks 1.1–1.3 implemented and slice-1 tests green; **STOP at slice
boundary** — the full suite cannot be green without Phases 2–8 (evidence below).
PR not opened: CI would be red; orchestrator decides next step.
**Branch**: `feat/order-state-machine` (pushed to origin, base `main`)
**Issue**: https://github.com/redaumore/super-warehouse/issues/13
**Commits**:

- `0b61738 feat(db): migrate order_estado to six states with draft index`
- `85a1523 feat(db): align OrderEstado model with the six-state migration`
- `f380e8d test(db): cover six-state enum and migration downgrade safety`
- `34d3c0c docs(sdd): add order state machine planning artifacts and state diagram doc`

## Completed Tasks

- [x] 1.1 Create `alembic/versions/f2b2570aed04_order_state_machine.py`
  (down_revision `7d2f4a1e8b90`): RENAME VALUE ×4, ADD DRAFT+PICKING
  (autocommit block — PG<12 cannot run ADD VALUE in a transaction; PG>=12
  keeps new values unusable until commit), partial unique index
  `uq_orders_one_draft_per_customer ON orders(customer_id) WHERE estado='DRAFT'`,
  guarded downgrade (reconcile DRAFT→CONFIRMED and PICKING→READY_FOR_DELIVERY,
  reverse renames, drop index; extra enum labels remain — documented).
- [x] 1.2 Update `src/db/models.py`: `OrderEstado` six values
  (DRAFT, CONFIRMED, PICKING, READY_FOR_DELIVERY, CANCELED, CLOSED), default
  `DRAFT`, partial index in `Order.__table_args__`, `delivery_date` stays the
  nullable `Date` column already modeled/migrated.
- [x] 1.3 Extend `tests/test_db_models.py`: six enum values via `db_inspector`,
  downgrade safety (one-step downgrade + guarded re-upgrade), partial-index
  lock, both enum-cardinality locks updated to six states.

## Work Unit Evidence

| Unit | Focused test command and exact result | Runtime harness command and exact result | Rollback boundary |
|------|----------------------------------------|------------------------------------------|-------------------|
| 1.1 Migration | `.venv/bin/python -m pytest tests/test_db_models.py -q` — **23 passed** in 3.24s (includes migration round-trip tests) | `command.upgrade("head")` then one-step `downgrade("7d2f4a1e8b90")` then guarded re-upgrade on the disposable `ferreteria_test` DB — enum has 6 labels after downgrade (4 legacy + DRAFT/PICKING leftovers), index dropped, rows reconciled, re-upgrade succeeds | Revert `0b61738`; `downgrade()` reverses the renames, drops the index, reconciles rows; `delivery_date` untouched (predates this migration) |
| 1.2 Model | Same focused command — model enum locks and partial-index lock pass | `db_inspector` confirmed `order_estado` has exactly the six labels and the index exists with `WHERE estado = 'DRAFT'` | Revert `85a1523`; restores the four-state enum and removes `__table_args__` index without touching the migration |
| 1.3 Tests | Same focused command — **23 passed** including the new downgrade-safety test | `make test-docs` regenerated `docs/escenarios-testeados.md` (307 scenarios; repo hook rejects a stale inventory) | Revert `f380e8d`; test-only changes plus the generated scenario doc |

## Slice Boundary Conflict (evidence)

Making the full suite green is **impossible** within slice 1: the enum rename
and the removed transition functions are consumed by `src/` modules and tests
that belong to Phases 2–8. Full run on the final slice commit:

```
45 failed, 541 passed, 46 errors
```

| Test file | Failure root cause | Required phase |
|-----------|--------------------|----------------|
| `tests/test_order_lifecycle.py` | tests removed `approve_order`/`reject_order`/`mark_dispatched`; `OrderEstado.PENDING_APPROVAL/APPROVED/IN_DISPATCH/REJECTED` gone | Phase 2 (`confirm_order`, `start_picking`, `complete_picking`, `deliver_order`) |
| `tests/test_approval.py` | `approve_and_register` flow (removed ceremony) | Phase 4 (`confirm_and_register`) |
| `tests/test_dispatch.py`, `tests/test_dispatch_handler.py` | `apply_decision` approve/reject handlers reference removed enum members/functions | Phase 7 |
| `tests/test_pipeline_owner.py`, `tests/test_session_rehydrate_owner.py` | `REJECTED`/awaiting-approval semantics | Phase 7/8 |
| `tests/test_sweeper.py` | `OrderEstado.PENDING_APPROVAL` direct ref (`:176`) | Phase 8 |
| `tests/test_purchasing_accumulate.py`, `tests/test_sourcing_persistence.py` | new `DRAFT` default + `uq_orders_one_draft_per_customer` rejects the second order for the same customer created without an explicit state (AD4 working as designed) | Phase 3/5 (persist DRAFT / set explicit states) |

`src/` consumers of removed members that would need later-slice behavior:
`src/order_lifecycle/state.py` (Phase 2), `src/orchestrator/approval.py`
(Phase 4), `src/sourcing/case_a.py` + `draft_order.py` (Phase 3), and the
chat/session modules (Phase 7).

Per the apply contract, slice 1 was not half-extended into Phase 2; the
orchestrator decides how to proceed (e.g. expand slice 1 to include Phase 2,
reorder slices, or accept a red-CI PR).

## Deviations and Risks

- **`delivery_date` was NOT re-added by the migration**: it already exists as a
  nullable `sa.Date()` column from `a0bf3bd210f8` and is modeled as such; the
  task's "ADD COLUMN `delivery_date TIMESTAMP NULL`" would have failed with a
  duplicate column. The migration documents this; downgrade does not drop it.
- **ADD VALUE uses an autocommit block unconditionally**: PG<12 rejects
  in-transaction ADD VALUE; PG>=12 accepts it but the new value is unusable
  until commit — the partial index needs `DRAFT` (verified:
  `UnsafeNewEnumValueUsage` when used in-transaction). Server-version detection
  is documented; no version branch is needed because the block covers both.
- **ADD VALUE is guarded against leftover labels**: a downgrade leaves
  DRAFT/PICKING behind (PG cannot drop enum values); re-upgrade would fail with
  `DuplicateObject` without the guard (verified empirically). The downgrade→
  re-upgrade cycle is exercised by the test suite.
- **The legacy deep round-trip test was replaced**: `test_customer_order_migration_
  round_trips_and_keeps_case_a_persistable` asserted the removed
  `OrderEstado.PENDING_APPROVAL` and a write path Phase 3 rewrites; it is now
  `test_order_state_machine_migration_downgrade_safety` (one-step downgrade +
  guarded re-upgrade). The deep downgrade path is still exercised by
  `test_migration_seeded_default_margin_is_read_by_pricing`.
- **No PR opened**: branch pushed, issue #13 created, but the PR would fail CI
  (`45 failed, 46 errors`). Per the slice contract the orchestrator decides.
- Migration head is now `f2b2570aed04`; the conftest session fixture upgrades to
  it, and tests that hard-coded head `7d2f4a1e8b90` were updated accordingly.

## Pending Tasks

- [ ] Phase 2 (PR 2): forward transitions — `confirm_order`, `start_picking`,
      `complete_picking`, `deliver_order`; rewrite `tests/test_order_lifecycle.py`
- [ ] Phase 3 (PR 3): draft persistence; explicit states for multi-order tests
- [ ] Phase 4 (PR 4): confirm ceremony
- [ ] Phase 5 (PR 5): cancel/modify
- [ ] Phase 6 (PR 6): backoffice actions
- [ ] Phase 7 (PR 7): chat handlers + rehydration
- [ ] Phase 8 (PR 8): sweep remaining test/docs references; full green suite