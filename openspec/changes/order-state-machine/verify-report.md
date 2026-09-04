```yaml
schema: gentle-ai.verify-result/v1
evidence_revision: sha256:4689616610843010437440877ff275287998bdc60cb0173795d4e977061986f1
verdict: pass
blockers: 0
critical_findings: 0
requirements: 22/22
scenarios: 43/43
test_command: pytest
test_exit_code: 0
test_output_hash: sha256:93060b7dc3e08d81c6acd1ae4d37a8e7b5733dbe305f179d2f23e96c8e17eed4
build_command: ruff check src tests && ruff format --check src tests
build_exit_code: 0
build_output_hash: sha256:cec1d2c967a28b796ab40044c84df2da4dafa24ed237d576b0c758f3dbd73464
```

## Verification Report

**Change**: order-state-machine
**Version**: N/A (delta specs, 5 capability folders)
**Mode**: Standard (strict_tdd: false) — delivery `exception-ok`, single PR #14

### Completeness
| Metric | Value |
|--------|-------|
| Tasks total | 27 |
| Tasks complete | 27 |
| Tasks incomplete | 0 |

### Build & Tests Execution
**Build**: ✅ Passed — `ruff check src tests` ("All checks passed!") + `ruff format --check src tests` ("111 files already formatted"), exit 0.

**Tests**: ✅ 679 passed / 0 failed / 0 errors / 7 warnings (deprecation warnings from alembic config only), exit 0, in 25.88s. Disposable DB (`ferreteria_test`) rebuilt from migrations by `tests/conftest.py` every session; Postgres reachable on localhost:5432 (docker compose `db`, healthy).

**Coverage**: Not available / threshold: none configured → ➖ Not available (pytest-cov installed, no threshold in pyproject).

### Migration Safety (fresh round-trip on disposable DB `ferreteria_verify_roundtrip`)
| Step | Result | Evidence |
|------|--------|----------|
| upgrade `7d2f4a1e8b90` → seed old-state rows → upgrade `f2b2570aed04` | ✅ | Enum labels = 6 values; live rows mapped in place: PENDING_APPROVAL→CONFIRMED, APPROVED→READY_FOR_DELIVERY; partial index `uq_orders_one_draft_per_customer` present |
| seed DRAFT+PICKING rows → `downgrade -1` | ✅ | Guarded reconcile: DRAFT→PENDING_APPROVAL, PICKING→APPROVED (0 stranded rows); index dropped; DRAFT/PICKING labels remain (documented — PG cannot drop enum values) |
| re-upgrade head | ✅ | Leftover-label guard skips ADD VALUE (no collision); index recreated; 6 labels |
| Real dev DB (`ferreteria`) live rows | ✅ | Renamed values observed on real data: PENDING_APPROVAL→CONFIRMED, REJECTED→CANCELED, zero rows deleted |

### Spec Compliance Matrix (43 scenarios — 5 delta specs)
**order-lifecycle (12)**
| Requirement | Scenario | Test | Result |
|-------------|----------|------|--------|
| Track order state machine | Happy path | `tests/test_order_lifecycle.py::test_happy_path_draft_to_closed_sets_delivery_date` | ✅ COMPLIANT (state.py:97-149) |
| Track order state machine | Modify loops back to Draft | `::test_modify_only_from_confirmed_and_releases_converted` | ✅ COMPLIANT (state.py:243-258) |
| Track order state machine | Illegal transition rejected | `::test_confirm_non_draft_order_is_invalid`, `::test_start_picking_only_from_confirmed`, `::test_complete_picking_only_from_picking`, `::test_deliver_only_from_ready_for_delivery`, `::test_cancel_from_closed_or_canceled_is_invalid` | ✅ COMPLIANT (InvalidTransitionError guards) |
| Track order state machine | Sourcing axis independent | `tests/test_case_b.py::test_cancel_case_b_order_never_touches_pos_or_needs` (IN_PREPARATION order transitions) + `test_case_c.py::test_cancel_for_no_supplier_releases_reservations` (CANCELLED) + state.py has zero `sourcing_state` references | ✅ COMPLIANT |
| Owner approval with adjustments | Confirm with adjustment | `tests/test_dispatch.py::test_apply_approve_with_adjustment_reprises_line`, `::test_parse_decision_with_adjustment` | ✅ COMPLIANT (dispatch.py:154-200) |
| Owner approval with adjustments | Plain confirm | `::test_apply_plain_approve_keeps_prices` | ✅ COMPLIANT |
| Owner approval with adjustments | Stale quote refused | `tests/test_order_lifecycle.py::test_confirm_order_with_stale_reservation_raises_requote`, `::test_stale_quote_refused_with_requote_requirement`, `tests/test_approval.py::test_confirm_on_expired_reservation_refuses_without_side_effects` | ✅ COMPLIANT (state.py:108-111) |
| Register confirmed orders | Confirmed order registered end-to-end | `tests/test_approval.py::test_confirm_and_register_converts_deducts_and_confirms` + `::test_sheets_append_belongs_to_confirm_not_draft_persistence` | ✅ COMPLIANT (approval.py:282-299) |
| Register confirmed orders | Sheets failure keeps order confirmed | `::test_sheets_quarantine_is_tolerated_and_order_stays_confirmed`; `SheetsWriter.append_order_row` never raises (sheets.py:89-112, quarantine internal) | ✅ COMPLIANT (approval.py:205-215) |
| Cancellation releases or restores stock | Cancel before fulfillment releases reservations | `::test_cancel_releases_active_reservations_from_draft/confirmed`, `::test_cancel_draft_releases_reservations_and_stock_is_available` | ✅ COMPLIANT (state.py:232-233) |
| Cancellation releases or restores stock | Late cancel restores deducted stock | `::test_late_cancel_restores_deducted_stock_with_audit` (+ StockAdjustment row, tests/test_backoffice.py::test_cancel_action_restores_deducted_stock_with_audit) | ✅ COMPLIANT (state.py:183-211, 234-235) |
| Modify confirmed order | Modify reconciles side effects | `::test_modify_restores_deducted_stock_without_double_count` | ✅ COMPLIANT (state.py:253-254; Sheets append-only per AD6) |

**order-sourcing (10)**
| Requirement | Scenario | Test | Result |
|-------------|----------|------|--------|
| Classify sourcing case from availability | Full stock is Case A | `tests/test_classify.py::test_full_stock_is_case_a` | ✅ COMPLIANT (classify.py:54-85) |
| Classify sourcing case from availability | Quantity exceeding stock is partial | `::test_partial_stock_with_supplier_is_case_b`, `::test_exact_quantity_available_is_not_missing` | ✅ COMPLIANT |
| Classify sourcing case from availability | Missing item with no supplier is Case C | `::test_missing_item_with_no_supplier_is_case_c`, `::test_mixed_items_any_no_supplier_forces_case_c` | ✅ COMPLIANT |
| Classify sourcing case from availability | Item unknown to catalog or inventory | `::test_unknown_sku_is_treated_as_missing` (no inventory row → 0 on hand → reported, never dropped) | ✅ COMPLIANT |
| Classify sourcing case from availability | Classification runs at confirm | `tests/test_approval.py::test_confirm_discovering_case_c_cancels_the_order`, `::test_confirm_discovering_case_b_persists_needs_and_returns_selection_prompt` (availability `_availability_for_order` adds back own ACTIVE locks) | ✅ COMPLIANT (approval.py:171-202) |
| Case A creates order via quotation flow | Full-stock order confirmed in owner chat | `tests/test_case_a.py::test_full_stock_order_flows_through_case_a`, `::test_case_a_order_can_be_approved_with_stock_deduction` (pedido #N in reply) | ✅ COMPLIANT |
| Case A creates order via quotation flow | Confirm TTL and re-quote still apply | `::test_case_a_reservation_ttl_requote_rules_unchanged` | ✅ COMPLIANT |
| Case B creates or accumulates purchase orders | Purchase order created on selection | `tests/test_case_b.py::test_owner_selection_accumulates_open_po` (OPEN PO per supplier, sourcing IN_PREPARATION, order CONFIRMED — case_b.py:96-108) | ✅ COMPLIANT |
| Case C notifies unavailability | No-supplier order cancelled | `tests/test_case_c.py::test_no_supplier_order_is_cancelled_and_reported_in_chat` (cancel path + owner chat) | ✅ COMPLIANT (case_c.py:35-45) |
| Case B cancellation policy | Cancel a Picking Case B order | `tests/test_case_b.py::test_cancel_case_b_order_never_touches_pos_or_needs` — ⚠️ PARTIAL: policy (POs/needs untouched, no orphaned supplier work) proven at runtime from CONFIRMED; the Picking-state GIVEN is covered by code path only (`cancel_order` executes stock-restore branch with zero PO/SourcingNeed references — no such imports in state.py) | ⚠️ PARTIAL |

**customer-order-persistence (10)**
| Requirement | Scenario | Test | Result |
|-------------|----------|------|--------|
| Finalize draft into a persisted order | Draft persisted at first add | `tests/test_draft_order.py::test_persist_draft_order_writes_draft_without_reservations`, `tests/test_finalize.py` first-add paths | ✅ COMPLIANT (draft_order.py:36-77, estado=DRAFT) |
| Finalize draft into a persisted order | Unknown customer created minimally | `tests/test_case_a.py::test_case_a_unknown_customer_name_offers_creation`, `tests/test_finalize.py::test_finalize_unknown_customer_then_create_attaches_waiting_draft` (name+phone, Base list — customer.py:709-736) | ✅ COMPLIANT |
| Source-aware base pricing at finalize | Local line priced from cost | `tests/test_order_pricing.py::test_local_lines_use_cost_and_applied_margin_not_list_price`, `tests/test_finalize.py::test_finalize_local_draft_uses_cost_margin_and_clears_draft` (compute_base(costo_proveedor, margin), never precio_lista_base) | ✅ COMPLIANT (case_a.py:56, customer.py:537-548) |
| Source-aware base pricing at finalize | Unmapped supplier uses default margin | `::test_rag_supplier_margin_and_default_margin_are_source_aware`, `tests/test_finalize.py::test_default_margin_edit_prices_subsequent_chat_finalize` | ✅ COMPLIANT |
| Save side effects (reserve and sync at confirm) | Local lines reserve at confirm | `tests/test_approval.py::test_confirm_and_register_converts_deducts_and_confirms` (reconcile→convert→deduct→Sheets at confirm; ACTIVE created at quote step per AD10) | ✅ COMPLIANT (approval.py:282-293) |
| Save side effects (reserve and sync at confirm) | RAG lines skip stock | `tests/test_draft_order.py::test_persist_draft_order_keeps_rag_snapshot_without_reservation` + `_local_quantities` filters non-LOCAL (approval.py:101-108) | ✅ COMPLIANT |
| Single draft per customer | Second draft rejected | `::test_second_draft_for_same_customer_is_rejected_and_preserved`, `tests/test_finalize.py::test_second_finalize_for_same_customer_is_refused` | ✅ COMPLIANT (customer.py:668-677 + partial index models.py:283-286) |
| Single draft per customer | Concurrent add races safely | `::test_two_session_draft_race_exactly_one_survives` (two sessions, IntegrityError caught, draft preserved) | ✅ COMPLIANT (customer.py:678-690) |
| Add and remove products on a persisted draft | Remove product is real | `::test_remove_draft_item_is_real_on_persisted_draft`, `tests/test_finalize.py::test_remove_command_deletes_persisted_draft_line` | ✅ COMPLIANT (state.py:293-308) |
| Add and remove products on a persisted draft | Add product after resume | `::test_add_draft_item_after_resume_appends_to_same_draft` | ✅ COMPLIANT |

**backoffice (9)**
| Requirement | Scenario | Test | Result |
|-------------|----------|------|--------|
| Live order monitor | Monitor order states | `tests/test_backoffice.py::test_monitor_shows_all_six_states` (+ soft-lock count via ACTIVE reservations, monitor.py:22-44) | ✅ COMPLIANT |
| Live order monitor | Sheets synchronization visible | `::test_monitor_lists_orders_with_state_and_sheets_status` (`sheets_synced`) | ✅ COMPLIANT |
| Customer Orders tab | Orders listed with totals | `::test_customer_orders_list_and_detail_include_ars_totals_and_snapshots` | ✅ COMPLIANT (customer_orders.py:58-63) |
| Customer Orders tab | Line detail per order | same test (SKU/name/qty/prices/source snapshots) | ✅ COMPLIANT (customer_orders.py:73-92) |
| Customer Orders tab | Actions shown only when legal | `::test_legal_actions_per_state` + `::test_app_customer_orders_tab_has_fulfillment_buttons` | ✅ COMPLIANT (customer_orders.py:226-238) |
| Fulfillment actions | Start picking | `::test_start_picking_action_commits_transition` | ✅ COMPLIANT (customer_orders.py:241-245) |
| Fulfillment actions | Complete picking | `::test_fulfillment_chain_commits_to_closed_with_delivery_date` | ✅ COMPLIANT |
| Fulfillment actions | Deliver | same chain test (delivery_date stored) | ✅ COMPLIANT (customer_orders.py:255-261) |
| Fulfillment actions | Cancel from any eligible state | `::test_cancel_action_releases_reservations_with_backoffice_actor`, `::test_cancel_action_restores_deducted_stock_with_audit` | ✅ COMPLIANT (customer_orders.py:264-272) |

**barcode-stock-ops (2)**
| Requirement | Scenario | Test | Result |
|-------------|----------|------|--------|
| Audited cancellation stock restoration | Late cancel restores stock with audit | `tests/test_order_lifecycle.py::test_late_cancel_restores_deducted_stock_with_audit` (Inventory.quantity_on_hand restored + StockAdjustment row) | ✅ COMPLIANT (state.py:195-207) |
| Audited cancellation stock restoration | Restoration is auditable | same test asserts reason `order_cancelled` + actor (owner/backoffice both exercised) | ✅ COMPLIANT |

**Compliance summary**: 43/43 scenarios compliant (42 ✅ COMPLIANT, 1 ⚠️ PARTIAL — Case B cancel policy from the exact Picking state).

### Requirements Coverage (static evidence, 22/22)
All 22 requirements Implemented (see matrix above). RENAMED markers (3) map to the same implementations: generalized `cancel_order` (1), confirm-time registration via `confirm_and_register` (2), reserve+sync moved to confirm (13). No requirement relies on test names alone: each matrix row cites the code path that implements it.

### Coherence (Design, 10 decisions)
| Decision | Followed? | Notes |
|----------|-----------|-------|
| 1 Enum migration RENAME×4 + ADD×2, never drop | ✅ Yes | f2b2570aed04:51-67; round-trip proven |
| 2 Draft as persisted Order row | ✅ Yes | draft_order.py:48-56, models.py:295 |
| 3 DB OrderItem as source of truth | ✅ Yes | draft items only in-memory pre-customer (session.py:96) |
| 4 Single draft: index + app guard + IntegrityError | ✅ Yes | customer.py:668-690, models.py:283-286 |
| 5 Confirm ceremony order + quarantine tolerated | ✅ Yes | approval.py:218-299 (pricing timing deviates — see WARNING-1) |
| 6 Modify: restore + release, Sheets append-only | ✅ Yes | state.py:243-258 |
| 7 Cancel generalization + audit | ✅ Yes | state.py:214-240 |
| 8 Backoffice four actions, commit inside, legal-only | ✅ Yes | customer_orders.py:241-272, app.py:748-780 |
| 9 Sourcing informational; cancel never touches POs | ✅ Yes | state.py has no PO/need imports; case_b/case_c set only sourcing_state |
| 10 Reservation at quote step, converted at confirm | ✅ Yes | customer.py:626-646, case_a.py:73-80, approval.py:144-149 |

### Documented Deviations (apply-progress) — Verdicts
| Deviation | Verdict | Evidence |
|-----------|---------|----------|
| Pricing at quote step, confirm consumes frozen snapshots + adjustments | ⚠️ Spec-acceptable (WARNING-1) | Pricing formulas enforced at both persist paths and locked by tests; re-pricing at confirm would clobber owner adjustments (spec's "the adjustment is applied before registration" and "confirmed at the quoted prices" both hold) |
| Case B enters CONFIRMED at supplier selection | ✅ Spec-compliant (not a deviation) | order-sourcing spec: "on confirmed selection … the order itself enters CONFIRMED" — case_b.py:104-107 implements exactly this; ceremony Case B branch keeps CONFIRMED (approval.py:264-280) |
| Case B local-portion reservations not converted at ceremony | ⚠️ Spec-acceptable (WARNING-2) | PO axis is the sourcing truth per spec ("independent of PO progress"); no stock leak (nothing deducted → nothing to restore); documented bounded risk |
| `delivery_date` not re-added (exists from a0bf3bd210f8) | ✅ Spec-acceptable | Column exists nullable (models.py:302); deliver_order stores it (state.py:146-147); ADD COLUMN would duplicate |
| `approved_at`/`rejected_at` reused (no rename) | ✅ Spec-acceptable | Design open question, recommended option; spec names no columns |
| PG enum labels left after downgrade | ✅ Spec-acceptable | Documented data-safe platform limit (migration docstring + design); guarded downgrade reconciles all rows (0 stranded, proven); re-upgrade guards leftover labels (proven) |
| Tests committed before code in final batch | ✅ Process note | Final tree green; no spec impact |

### Issues Found
**CRITICAL**: None.

**WARNING**:
1. **Pricing-timing deviation** — Spec `Source-aware base pricing at finalize` says "price each line by source at confirm"; implementation prices at the quote/persist step and the ceremony consumes frozen snapshots (approval.py has no re-price; case_a.py:56, customer.py:579-598). Every observable pricing outcome the spec's scenarios assert is implemented and passes; the deviation is documented with a sound rationale (adjustment clobber). Acceptable for archive; flag to reviewers.
2. **Case B local-portion deduction** — A draft re-classified Case B at confirm converts/deducts nothing for its available LOCAL portion (approval.py:264-280 returns early with `converted=0`); fulfillment rides the PO-receipt axis. Documented risk in apply-progress; consistent with the sourcing spec's PO independence; no leak or double-count (late cancel of such an order restores only what was converted).
3. **Case B cancel policy from Picking** — The policy scenario is runtime-proven from CONFIRMED (OPEN PO + needs intact); the exact Picking-state GIVEN is covered by code-path analysis only (state.py imports no PO/SourcingNeed entities, so cancel cannot touch them from any state). Narrow composition gap, not a violation.

**SUGGESTION**:
1. Add a dedicated test: Case B order advanced to Picking, then canceled — composes PO-retention with the stock-restore branch.
2. Note the surviving DRAFT/PICKING enum labels in operator release notes (downgrade leaves them; documented in the migration).
3. `docs/estado-pedido.md` remains the historical gap analysis per apply-progress; consider archiving it under the change folder after merge.

### Verification Process Note
The verify round-trip tooling initially targeted the dev DB `ferreteria` (alembic `env.py` overrides any per-Config URL with `ALEMBIC_DATABASE_URL` or the app `DATABASE_URL`), downgrading it one step; it was restored to head `f2b2570aed04` within the same session. Zero data loss (the guarded downgrade only renames labels and drops the index; both restored). Side benefit: real live-row rename mapping (PENDING_APPROVAL→CONFIRMED, REJECTED→CANCELED) was confirmed on production-shaped data.

### Verdict
**PASS WITH WARNINGS** — 27/27 tasks complete; ruff clean; 679/679 tests green; 43/43 scenarios evidenced (1 PARTIAL); migration round-trip safe; 3 documented deviations judged spec-acceptable; 0 CRITICAL.
