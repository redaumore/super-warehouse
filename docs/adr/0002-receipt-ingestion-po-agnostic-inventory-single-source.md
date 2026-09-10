# ADR 0002 — Receipt Ingestion Is PO-Agnostic; Inventory Is the Single Stock Source

- **Status:** Accepted
- **Date:** 2026-09-10
- **Deciders:** Project owner (Rolando Daumas), architecture review with AI pair
- **Related commits:** _this change_

## Context

The customer-order scenario matrix (`docs/matriz-escenarios-pedido.md`) closed all
high-priority gaps but left two rows open as pending *semantic decisions*: R8 and
L7. Both concern what "stock" means in the system.

### R8 — receipt ingestion vs. purchase-order lifecycle

Two parallel receiving paths exist and never reconcile:

1. **Receipt ingestion** (`src/backoffice/ingestion.py:167-213`): validates only
   supplier activity (`ensure_active_supplier`) and resolvability of lines
   (fail-closed). It holds **zero references to `SupplierPurchaseOrder`** — no
   lookup, no status check, no requirement that a PO exists. On success it
   bumps `Catalogo.stock_disponible` and `Inventory.quantity_on_hand` and writes
   an audited `StockAdjustment(reason="receipt_ingestion")` with provenance
   (`ingestion.py:216-238`, `_adopt_new` at `:241-304`).
2. **PO receiving state machine** (`src/purchasing/state.py:71-113`): enforces
   the lifecycle (`receive_po` is legal only from `SENT`/`PARTIALLY_RECEIVED`,
   rejects over-receipt), but bumps **only** `Inventory.quantity_on_hand` and
   writes no `StockAdjustment`.

Consequence today: a remito arriving with **no linked PO** or with its PO in
`OPEN` (not `SENT`) gets stock bumped silently — but with a complete audit
trail. The question was whether this is valid semantics or debt to close with a
guard.

### L7 — `catalogo.stock_disponible` vs. `Inventory`

Order deduction subtracts **only** from `Inventory.quantity_on_hand`
(`src/orchestrator/approval.py:196-212`); the docstring already declares the
intent: *"Inventory.quantity_on_hand is the single on-hand source; the legacy
catalogo.stock_disponible counter is deliberately left untouched."* Late-cancel
restoration is Inventory-only too (`src/order_lifecycle/state.py:201`), and every
availability read for decisions (`src/agents/inventory.py:32-54`,
`src/sourcing/product_search.py:190`) reads only `Inventory`.

Meanwhile, every other write path dual-writes both fields: receipt ingestion
(`ingestion.py:222`), adoption (`backoffice/adoption.py:185`), barcode adjust
(`barcode/decoder.py:131-142`), manual catalog edit (`backoffice/catalog.py:73-84`).

Result: after the first confirmed order, `catalogo.stock_disponible` freezes at
its pre-order value forever. Nothing syncs it back. No decision consumes it, but
the backoffice grid still displays it (`backoffice/app.py:130`,
`backoffice/catalog.py:56`), so the owner sees stock that no longer exists. The
sourcing workflow verify report of 2026-08-27
(`openspec/changes/archive/2026-08-27-order-sourcing-workflow/verify-report.md:152-169`)
already flagged dual-write drift as warnings and recommended retiring the legacy
counter.

## Decision

1. **R8: receipt ingestion is PO-agnostic — valid semantics, not debt.**
   The remito is physical evidence that goods arrived; the PO is a commercial
   agreement. Ingestion never requires a PO, and never inspects its state.
   Business rationale: at system startup, POs barely exist (they are generated
   downstream by customer orders), so PO-less ingestion is the **primary
   mechanism** for building initial real inventory. A `SENT`-only guard would
   make it impossible to load stock before the first customer order.

2. **L7: `Inventory` is the single on-hand stock source; the legacy
   `catalogo.stock_disponible` counter is retired.**
   - All remaining dual-writes (ingestion, adoption, barcode adjust, manual
     catalog edit) become Inventory-only.
   - The backoffice UI reads availability from `Inventory`.
   - The column is dropped via migration (schema and model).
   - The known desync ends by construction: the field that could drift no
     longer exists.

3. **Documented receiving discipline (both paths):** a remito is ingested **once**;
   a PO is received via **one** path. Processing the same goods through both
   paths double-bumps stock, and nothing reconciles
   `SupplierPurchaseOrderItem.received_quantity` against
   `StockAdjustment(receipt_ingestion)`. This remains accepted, unguarded debt
   (see Consequences).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| R8: guard ingestion behind a PO in `SENT`/`PARTIALLY_RECEIVED` with SKU matching | Contradicts the ingestion module's design (document-driven, RAG-backed, supports ad-hoc purchases and new-product adoption). There is no direct remito→PO link; only SKU. Breaks initial inventory loading entirely. |
| L7: keep the counter as display-only and reconcile it on deduct/restore | Perpetual dual-write code for a purely decorative value — reintroduces the two-sources-of-truth problem the deduction docstring explicitly avoided. |
| L7: test-only documentation of the desync as accepted debt (no code change) | Cheapest, but leaves the user-visible symptom: the backoffice shows phantom stock after every confirmed order. |
| Either: event/in-process-bus reconciliation between PO-receive and ingestion | Overkill for a single-operator monolith (see ADR 0001); solves a risk that the documented one-path discipline already bounds. |

## Consequences

**Positive**

- One stock source of truth. UI, decisions, and audits all read the same
  number; the drift class of bug disappears by construction.
- Initial inventory loading stays simple: ingest remitos, done — no PO
  scaffolding required.
- The dual-write discipline (write both fields in ~5 places) is deleted, not
  just ignored.

**Negative / accepted debt**

- **Double-bump risk between the two receiving paths is unguarded.** If the
  same receipt is ingested *and* received via the PO button, stock counts
  twice. Mitigation is procedural (one path per remito), not mechanical.
- **PO-receive and ingestion keep different ledger conventions** (PO-receive
  writes no `StockAdjustment`). Unifying them is a possible follow-up, not
  required by this decision.
- Schema migration drops a column: deployments must run it before the new
  code reads the model.

## Verification

- E2E test: remito with no PO, and remito with PO in `OPEN`, bump `Inventory`
  and write `StockAdjustment(receipt_ingestion)`; nothing blocks.
- Updated existing tests that asserted on `stock_disponible` now assert the
  retired behavior (field gone; `Inventory` carries the number).
- Full suite green; `make test-docs` regenerated; matrix rows R8/L7 marked
  resolved with references to this ADR.
