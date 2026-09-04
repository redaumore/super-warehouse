# Exploration: Order state machine alignment (Draft persisted, 6 renamed states, backoffice picking)

Change: `order-state-machine` — 2026-09-04. Owner decisions per `docs/estado-pedido.md`: Draft becomes a persisted DB state; at most one Draft per customer; states renamed to the diagram names (Draft, Confirmed, Picking, Ready for delivery, Canceled, Closed); picking/delivery triggered by owner or warehouse via chat or backoffice, first target backoffice.

## Current State

- `OrderEstado` (src/db/models.py:50-56) is a **native PG enum** `order_estado` (column def models.py:279-283; created in alembic/versions/26a4a1b103fe:99 with `sa.Enum("PENDING_APPROVAL", "APPROVED", "IN_DISPATCH", "REJECTED", ...)`). Migration head is `7d2f4a1e8b90`.
- Transitions owned by src/order_lifecycle/state.py: `approve_order` (PENDING_APPROVAL→APPROVED, TTL guard → `RequiresRequoteError` + `needs_requote`), `reject_order` (PENDING_APPROVAL→REJECTED, releases ACTIVE reservations only), `mark_dispatched` (APPROVED→IN_DISPATCH, **no production caller**), `expire_reservations`.
- Draft is memory-only (`ConversationState.draft_items`, session.py:97; appended at customer.py:921). `persist_draft_order` (src/sourcing/draft_order.py:28-79) creates the `Order` at finalize **already as PENDING_APPROVAL** with `OrderItem` snapshots + reservations for LOCAL lines (draft_order.py:55-62). No remove-product, no draft cancel, no DB Draft state.
- `Order.customer_id` is **NOT NULL** (models.py:278); `subtotal`/`total` are nullable; items are `OrderItem` children (models.py:306-329). Customer is resolved at finalize, not at first add (customer.py:617-644, disambiguation menu session.py:90-91).
- Approval flow (dispatch.py → approval.py): `register_approved_order` converts reservations ACTIVE→CONVERTED, appends Google Sheets row (quarantine → rollback keeps order pending), deducts stock from `Inventory.quantity_on_hand` (approval.py:100-116). **No stock-restore path exists anywhere**; `reject_order` only releases ACTIVE reservations, CONVERTED ones are untouched.
- Backoffice: Customer Orders tab is read-only (app.py:660-711 renders `_customer_orders_grid` from customer_orders.py:48-53). The PO tab shows the established action pattern: pure action functions in src/backoffice/po.py wrapping src/purchasing/state.py transitions that COMMIT inside (po.py:51-73), wired in app.py with try/except handlers (app.py:112-133).
- Sourcing axis `SourcingState` (models.py:59-69) is set once at creation: Case A→PENDING_ASSEMBLY (case_a.py:63), Case B→IN_PREPARATION (case_b.py:53,101), Case C→CANCELLED (case_c.py:41-42). Rehydration uses it to derive routing flags (session.py:204-230). Case B creates the order **without estado** (default PENDING_APPROVAL) plus `SourcingNeed` rows and POs.
- Sweeper (src/scheduler/sweeper.py:38-62) only marks past-TTL ACTIVE reservations EXPIRED and flags `needs_requote` — orthogonal to renamed states.

## Affected Areas

- `src/db/models.py:50-56, 267-329` — enum rename + (likely) partial unique index for the draft invariant.
- `alembic/versions/*` — new migration: `ALTER TYPE order_estado RENAME VALUE` ×4 + `ADD VALUE` ×2 (head `7d2f4a1e8b90`).
- `src/order_lifecycle/state.py` — all transitions renamed/replaced; cancel must move to a generalized `cancel_order` (Draft/Confirmed/Picking/Ready-for-delivery → Canceled) with stock restore; `mark_dispatched` replaced by picking/deliver transitions.
- `src/sourcing/draft_order.py` — create Order as DRAFT (not PENDING_APPROVAL); reservations move to confirm time (or stay at confirm); add/remove-item functions.
- `src/agents/customer.py:617-644, 905-923` — finalize becomes Draft→Confirmed; draft creation at first add; remove-product command.
- `src/orchestrator/session.py:97, 160-247` — `draft_items` persists to DB instead; rehydration must load DRAFT orders; routing flags derived from new states.
- `src/agents/dispatch.py` + `src/orchestrator/approval.py` — approval/Sheets/deduction must attach to a transition of the new machine (see Risk 1).
- `src/backoffice/app.py:660-711` + `src/backoffice/customer_orders.py` — add action buttons mirroring the PO tab pattern; new action functions (start picking / complete picking / deliver / cancel).
- `src/sourcing/case_a.py:62, case_b.py:51-61, case_c.py:20-42` — creation states (Case C cancels via the new cancel path).
- `src/backoffice/monitor.py:41` — display-only, follows `.value` automatically.
- `tests/` — 14 files reference the states (~70 lines, see Test Blast Radius).

## Approaches

### 1. Enum rename + migration
- **A. In-place ALTER TYPE (recommended)** — one migration: `ALTER TYPE order_estado RENAME VALUE 'PENDING_APPROVAL' TO 'CONFIRMED'`, `'APPROVED' TO 'READY_FOR_DELIVERY'`, `'IN_DISPATCH' TO 'CLOSED'`, `'REJECTED' TO 'CANCELED'`; then `ADD VALUE 'DRAFT'`, `ADD VALUE 'PICKING'`. Existing rows land in the diagram-equivalent state; no data rewrite; matches the project convention (all 5 state machines are native enums).
  - Pros: preserves semantics of every existing row; reversible (RENAME VALUE is reversible); minimal diff.
  - Cons: requires PG ≥10 (RENAME VALUE) and ADD VALUE outside a transaction block on PG <12 (verify server version); the old enum members disappear from code — any missed reference fails loudly at import/compile time, which is good.
  - Effort: Low.
- **B. Recreate the type** — alter column to varchar, drop type, create new type with 6 values, alter back with USING cast.
  - Pros: full control of the final value set in one shot.
  - Cons: verbose, risky on live data, downgrade is painful; no benefit over A.
  - Effort: Medium.
- **C. Switch column to varchar** — drop DB-level constraint, validate in app.
  - Pros: trivially flexible.
  - Cons: deviates from the repo convention (every other state machine is a native enum); loses integrity guarantees the partial unique index needs. Not viable for the draft invariant.
  - Effort: Low, but wrong direction.

### 2. Draft persistence shape
- **A. Order row with estado=DRAFT (recommended)** — at first product add, create `Order(customer_id=..., estado=DRAFT)`; add/remove = `OrderItem` insert/delete; confirm = DRAFT→CONFIRMED (price + reserve at confirm, mirroring today's finalize side effects). The schema already fits: `customer_id` NOT NULL, `subtotal/total` nullable, `OrderItem` snapshots exist (draft_order.py:63-77).
  - Pros: the diagram models Draft as a state of the SAME order; no new table; `order_id` available in chat from the first add; rehydration can resume drafts.
  - Cons: forces customer resolution at draft creation (today it happens at finalize) — a chat-flow behavior change; reservations shift from creation-time to confirm-time (they already are, since today the order row is created at finalize).
  - Effort: Medium.
- **B. Separate drafts + draft_items tables** — new tables, converted into an Order at confirm.
  - Pros: drafts stay out of the orders table; no nullable semantics pressure.
  - Cons: duplicates Order/OrderItem structure; contradicts the diagram (Draft is one of the order's states); extra mapping and a second lifecycle to maintain; more migration surface.
  - Effort: High.

### 3. Single-draft invariant
- **A. Partial unique index + app guard (recommended)** — `CREATE UNIQUE INDEX uq_orders_one_draft_per_customer ON orders (customer_id) WHERE estado = 'DRAFT'` (customer_id is NOT NULL, so the WHERE clause is just the estado). Precedent: `uq_suppliers_cuit` partial unique index (models.py:150-155). App-level guard in the draft-add path for a friendly message.
  - Pros: DB is the authoritative backstop; the app guard gives the UX; pattern already exists in the codebase.
  - Cons: an `IntegrityError` must be caught at the add path; index must be created in the same migration as the enum change.
  - Effort: Low.

### 4. Backoffice picking & delivery actions
- **A. Mirror the PO tab pattern (recommended)** — new action functions in `src/backoffice/customer_orders.py` (or a new `fulfillment.py`) wrapping new lifecycle transitions in `src/order_lifecycle/state.py`, each COMMITting (po.py:51-73); Gradio buttons in the Customer Orders tab next to the existing Order ID box (app.py:679-681): "Start picking" (CONFIRMED→PICKING), "Complete picking" (PICKING→READY_FOR_DELIVERY), "Deliver" (READY_FOR_DELIVERY→CLOSED), "Cancel order" (from Confirmed/Picking/Ready-for-delivery).
  - Pros: follows the established, tested pattern; pure DB functions are unit-testable without Gradio.
  - Cons: four buttons + a status textbox; grid must refresh after each action.
  - Effort: Low-Medium.

### 5. Late cancellation (stock restore)
- **A. Restore Inventory + audit row (recommended)** — on cancel from Ready-for-delivery (and Picking), add back each converted reservation's quantity to `Inventory.quantity_on_hand` and write a `StockAdjustment` row (reason `order_cancelled`, actor) — the audit table exists (models.py:248-265) and is the barcode-stock-ops convention. For Confirmed-cancel, keep releasing ACTIVE reservations (existing `reject_order` behavior).
  - Pros: no stock leak; auditable; reuses existing infrastructure; sweeper untouched (it only touches ACTIVE reservations).
  - Cons: new logic + tests; must decide whether cancel at Picking also cancels OPEN/SENT supplier POs and their `SourcingNeed` rows (design question — `cancel_po` exists in src/purchasing/state.py).
  - Effort: Medium.

### 6. needs_requote / TTL placement
- The TTL guard and `RequiresRequoteError` protect the window between quoting and the owner's go-ahead. Options for where that lands in the new machine: (a) `confirm` stays the current finalize (Draft→Confirmed) and approval+Sheets+deduction become a precondition of `start picking` (Confirmed→Picking refuses on stale reservations); (b) approval moves into Draft→Confirmed ("confirm" is owner-gated); (c) approval is dissolved, Sheets registration happens at deliver. Option (a) preserves today's semantics with the smallest diff; the proposal must pick one — see Risk 1.

### 7. SourcingState relationship
- (a) Keep the axis as informational: sourcing (A/B/C + PO progress) stays a separate column; Picking/Ready-for-delivery are order-axis states; Case B enters PICKING on the owner's trigger regardless of PO receipt. (b) Fold SourcingState into the order axis (PICKING subsumes IN_PREPARATION): removes a column/enum/rehydration logic, but loses the case classification and contradicts the documented independence (spec.md:94). (c) Keep the axis but only as a creation-time case tag. Recommendation for the proposal: (a) — smallest blast radius; the diagram does not model supplier procurement.

### 8. Case A/B/C under the new machine
- Case A: order created DRAFT at first add; at confirm → CONFIRMED with pricing + reservations (current `persist_case_a_order` side effects move to the confirm transition). Case B: created DRAFT then CONFIRMED with sourcing IN_PREPARATION + SourcingNeed rows (case_b.py:51-61); supplier selection continues unchanged. Case C: created DRAFT then immediately Canceled via the new cancel path (case_c.py:33-42) — or created directly CANCELED; the diagram's `Draft → Canceled` covers both. Classification runs at confirm, where availability can change.

### 9. Chat triggers (backoffice-first scope)
- Extend `src/agents/commands.py` vocabulary with warehouse commands (e.g. start picking / complete picking / deliver / cancel `#N`) and add a routing branch (router.py:86-116) — either a new OPERATIONS/warehouse agent or an extension of the DISPATCH handler (dispatch.py:198-263 is the template: parse → load order → transition → commit → in-chat reply). OUT of first scope: the state transitions are shared, so chat can hook them later without rework.

### 10. Test blast radius
- 14 test files reference the 4 states (~70 lines): test_order_lifecycle (15), test_dispatch (8), test_dispatch_handler (8), test_backoffice (6), test_db_models (6 — locks enum cardinality at lines 86-91 and 178-184), test_approval (5), test_e2e_order (4), test_session_rehydrate_owner (4), test_case_a (3), test_case_c (3), test_pipeline_owner (2), test_sweeper (2), test_finalize (1), test_sourcing_persistence (1). `mark_dispatched` tests (test_order_lifecycle.py:170-181) die with the dead code.
- New tests needed: draft persist/resume/remove-product; single-draft invariant (DB + app); backoffice picking/deliver/cancel actions; late-cancel stock restore + StockAdjustment audit; migration rename round-trip (enum value assertions via db_inspector); needs_requote on Confirmed; Case A/B/C under new states.

## Recommendation

In-place enum migration (Approach 1A) + Draft as an Order row with estado=DRAFT (2A) + partial unique index and app guard (3A) + PO-tab-pattern backoffice actions (4A) + stock restore with StockAdjustment audit on late cancel (5A) + approval as a precondition of start picking (6a) + SourcingState kept as informational axis (7a). The proposal must first resolve Risk 1 — it determines the whole transition map.

## Risks

1. **CRITICAL — Undecided placement of the owner-approval/Sheets/stock-deduction step.** The diagram has no approval transition; today's approval is the semantic core (TTL guard, Sheets quarantine, deduction). Whether it gates `start picking` or moves into `confirm` changes every transition, the dispatch flow, and the spec delta. The proposal must resolve this before design.
2. **CRITICAL — No stock-restore path for late cancel.** Cancel from Ready-for-delivery (and Picking) with CONVERTED reservations leaks stock unless restore + audit are implemented in the same change.
3. **HIGH — customer_id NOT NULL forces customer resolution at draft creation.** Today the customer is resolved at finalize; the chat flow and its tests change.
4. **MEDIUM — PG enum mutation constraints.** `ADD VALUE` cannot run inside a transaction block on PG <12 (verify server version before writing the migration); the new migration must remain downgrade-safe because test_db_models round-trips downgrades to `46bdbdc4a575`.
5. **MEDIUM — Case B cancel interplay.** Canceling a Picking order with OPEN/SENT POs and SourcingNeed rows needs a defined behavior (cancel POs? keep needs?).
6. **LOW-MEDIUM — Conversation TTL vs persisted drafts.** Drafts survive the 30-minute session TTL once persisted; rehydration must restore `draft_items` from DB and keep the "hola bob" reset semantics aligned (reset currently never touches DB rows — router.py:156-163).

## Ready for Proposal

Yes. The orchestrator should tell the user: exploration complete; the proposal must first decide where owner approval + Sheets registration + stock deduction land in the new machine (confirm vs. start-picking gate), since it shapes every transition; late-cancel stock restore is non-negotiable scope.