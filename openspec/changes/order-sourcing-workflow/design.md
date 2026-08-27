# Design: Order Sourcing Workflow

## Technical Approach

Additive. Add a `SourcingState` axis + `delivery_date` to `Order` (the four-state `OrderEstado` is untouched); introduce `Inventory` (canonical on-hand), `SupplierPurchaseOrder`/`Item`, and a `SourcingNeed` link table. NL parsing becomes a dedicated router step before the Customer agent; Case A/B/C classification is a pure function; Case B multi-turn selection persists to the DB and accumulates into one OPEN PO per supplier; PO lifecycle lives in `src/purchasing/` mirroring `src/order_lifecycle/state.py`.

## Architecture Decisions

| Option | Tradeoff | Decision |
|--------|----------|----------|
| New `SourcingState` column vs extending `OrderEstado` | Separate axis vs mixing approval + fulfillment in one machine | Separate column; `OrderEstado` untouched (honors "fixed by spec") |
| `Inventory` canonical vs keep `Catalogo.stock_disponible` | One truth vs drift between two stock counters | `Inventory.quantity_on_hand` becomes the single on-hand source; repoint `available_stock`; backfill from `Catalogo.stock_disponible` |
| `SourcingNeed` table vs JSONB on `Order` | Normalized (matches `OrderItem` pattern), traceable vs less tables | `SourcingNeed` child table keyed by `order_id` — this *is* the "persist on the order" source of truth (survives TTL) |
| PO items aggregate by `(po_id, sku)` + `received_quantity` vs per-order PO items | Enables accumulation + partial receipt vs duplicate rows per customer order | Aggregate by SKU; `SourcingNeed` preserves the per-order link |
| `SupplierCatalogSearcher` Protocol + fake vs direct RAG call | Decouples unavailable RAG vs coupled | Protocol seam; `FakeSupplierCatalogSearcher` in tests |
| Case A unchanged quotation/approval; B/C bypass approval | Minimal regression vs uniform flow | A → quote/approve; B → selection→PO; C → notify (no approval) |

## Data Flow

```
        inbound ──► parse (extract items/date) ──► resolve SKUs ──► availability (Inventory)
                                                                      │
                                          ┌───────────────────────────┴───────────────┐
                                    Case A (all ok)            Case B (partial)        Case C (no supplier)
                                    Order PENDING_ASSEMBLY    Order IN_PREPARATION    Order CANCELLED
                                    → reserve → quote         → list missing+suppliers → notify
                                    → approve (unchanged)     → owner selects → accumulate PO
```

Case B multi-turn: `SourcingNeed.supplier_id` is written/updated on selection; when `ConversationStore.get` returns `None` (TTL), the orchestrator rehydrates `ConversationState` from the most recent open `Order` + its `SourcingNeed` rows.

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `src/db/models.py` | Modify | `SourcingState`, `SupplierPurchaseOrderState` enums; `Order.sourcing_state` + `delivery_date`; `Inventory`, `SupplierPurchaseOrder`, `SupplierPurchaseOrderItem`, `SourcingNeed` |
| `alembic/versions/*_sourcing_axis_inventory.py` | Create | Add enum types, `Order` columns, `Inventory`, backfill |
| `alembic/versions/*_supplier_purchase_orders.py` | Create | PO header/item + `SourcingNeed` tables, indexes |
| `src/purchasing/state.py` | Create | PO transitions: `send_po`, `cancel_po`, `receive_po` |
| `src/purchasing/accumulate.py` | Create | `open_or_create_po`, `accumulate_need` |
| `src/supplier/searcher.py` | Create | `SupplierCatalogSearcher` Protocol + `FakeSupplierCatalogSearcher` |
| `src/sourcing/classify.py` | Create | `classify_case` → Case A/B/C |
| `src/agents/intake.py` | Create | `OrderParser` Protocol, `ParsedOrder`, fuzzy date resolution |
| `src/agents/inventory.py` | Modify | Repoint `available_stock` to `Inventory`; add `seed_inventory` |
| `src/orchestrator/router.py` | Modify | Parse step + Case B supplier-selection routing |
| `src/orchestrator/session.py` | Modify | DB-row rehydration of `ConversationState` |
| `src/agents/customer.py` | Modify | Per-case confirmation replies |
| `src/orchestrator/approval.py` | Modify | `_deduct_stock` writes `Inventory` (+`updated_at`) |
| `src/backoffice/monitor.py`, `app.py` | Modify | PO list + send/receive/cancel execution tab |

## Interfaces / Contracts

```python
@dataclass(frozen=True)
class SupplierCandidate:
    supplier_id: int; business_name: str; sku: str; description: str
    available_quantity: int | None

class SupplierCatalogSearcher(Protocol):
    def search(self, *, sku: str | None = None, description: str | None = None) -> tuple[SupplierCandidate, ...]: ...

def available_stock(session, sku, *, now=None) -> int   # Inventory.quantity_on_hand − Σ(ACTIVE unexpired); 0 if absent
def seed_inventory(session) -> int                       # INSERT...SELECT from catalogo.stock_disponible
def accumulate_need(session, need: SourcingNeed, supplier_id: int) -> SupplierPurchaseOrder
def receive_po(session, po, received: Mapping[str, int]) -> SupplierPurchaseOrder  # bumps received_quantity + Inventory
```

`SourcingNeed` columns: `need_id`, `order_id` FK, `sku`, `missing_quantity`, `supplier_id` (nullable — persisted selection), `po_item_id` (nullable). PO item: `po_item_id`, `po_id` FK, `sku`, `quantity` (aggregated), `received_quantity`.

## Testing Strategy

| Layer | What | Approach |
|-------|------|----------|
| Unit | PO transitions, classification, date parse, searcher fake | Pure functions + `_FakeSession` (as `test_order_lifecycle.py`) |
| Integration | `Inventory` availability, accumulation, partial→full receipt, migration RED | Postgres fixture; extend TRUNCATE list with new tables |
| E2E | Case A/B/C via orchestrator turn | Fake parser/searcher/notifier; assert Order/PO state |

Existing `available_stock`/inventory tests are updated to seed `Inventory`; unknown-SKU now returns `0` (per local-inventory spec) instead of `KeyError`. Coverage: new modules are small pure units; gate met by unit + integration coverage of each new file.

## Threat Matrix

N/A — no routing (network), shell, subprocess, VCS/PR automation, executable-file classification, or process-integration boundary. Router changes are in-process agent dispatch only.

## Migration / Rollout

Two additive migrations. New PG enum types (`sourcing_state`, `supplier_purchase_order_state`) via `sa.Enum(name=...)`; new columns nullable/with default to stay additive; `Inventory` backfilled by `INSERT INTO inventory (sku_id, quantity_on_hand, updated_at) SELECT codigo_interno, stock_disponible, now() FROM catalogo`. Downgrade drops tables/columns/types in reverse. Rollback = `alembic downgrade` + feature flag disabling the parse step (intake keeps legacy routing). No four-state transition modified.

## Resolved Open Questions

- [x] Case B sourcing state during pre-selection: reuse `IN_PREPARATION` for the whole Case B span (no distinct "awaiting supplier" state). Confirmed by owner.
- [x] Case C `OrderEstado`: set `REJECTED` (reuses the existing rejection flow and releases reservations) together with `sourcing=CANCELLED`. Confirmed by owner.
