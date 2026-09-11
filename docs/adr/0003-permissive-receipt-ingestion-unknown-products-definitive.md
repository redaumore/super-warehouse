# ADR 0003 — Permissive Receipt Ingestion: Unknown Products Enter as Definitive

- **Status:** Accepted
- **Date:** 2026-09-10
- **Deciders:** Project owner (Rolando Daumas), architecture review with AI pair
- **Related commits:** _this change_

## Context

Since the RAG-backed receipt ingestion rewrite
(`src/backoffice/ingestion.py`), a receipt line that matched no product in the
`catalogo_productos_rag` index (0 exact hits, 0 hybrid candidates) failed
closed: `ingest_receipt_lines` raised `UnresolvedLineError` before any write
and the whole confirmation was blocked until the owner manually assigned the
line. The premise was that "the index is authoritative: if it is not there,
the product must not be created".

That premise does not hold in operation. The RAG index is a snapshot rebuilt
from supplier price lists, and those lists are re-ingested over time (full or
incremental replacement). A product can legitimately be absent from the
current index while still being a real, stocked item — for example when the
supplier's new price list has already been ingested and dropped or renamed a
line, or when the index is simply behind the store's physical reality.
Fail-closing on index absence turned a normal situation into a hard blocker,
forcing the owner through a manual assignment screen for products that have no
RAG row to assign.

## Decision

1. **A `NO_CANDIDATES` line enters directly as a DEFINITIVE product at confirm
   time.** No provisional flag, no review screen, no pending state that gates
   confirmation. The owner's decision (2026-09-10): a missing index row is not
   evidence that the product is unknown, so ingestion proceeds.

2. **Two kinds of pending lines are now distinguished** (`PendingReason` in
   `ingestion.py`):

   - **`NO_CANDIDATES`** — the index returned nothing (0 exact, 0 hybrid).
     Adopted automatically at confirm time as a new definitive product
     (`_adopt_from_document`): deterministic SKU
     `build_sku(supplier.code, codigo_orig)` falling back to the normalized
     description when the line carries no code, `nombre_oficial` from the
     parsed description, supplier cost from the document (0.00 when absent),
     and `origen={"remito": {...}}` provenance (document filename, ingest
     timestamp, supplier, page and the parsed line snapshot). **No `node_id`
     is ever fabricated** — document-text provenance is now an acceptable
     `origen` shape alongside `{"rag": {...}}`.
   - **`AMBIGUOUS`** — the index returned 2+ candidates (duplicate exact hits
     or multiple hybrid results). The product EXISTS in the index; OCR cannot
     disambiguate, so auto-creating would duplicate an existing product. These
     lines still require manual assignment in the UI, and `UnresolvedLineError`
     remains fail-closed **for this case only**, with a message that states
     the ambiguity explicitly.

3. **Local-catalog safety check before creating.** The computed SKU is looked
   up in the local `Catalogo` first; if it already exists locally (RAG missed
   it, local catalog did not), the line is treated as a match: stock is bumped
   on the existing product via the same `_bump_stock` path — never a duplicate
   row, never an `origen` overwrite (write-once holds).

4. **Embedding is best-effort on this path.** The description is embedded with
   the existing `Embedder` protocol; on any failure (or a wrong-dimension
   vector) the product is created WITHOUT a vector — `Catalogo.embedding` is
   nullable precisely for this. An embedder outage must never block receipt
   ingestion; `EmbeddingUnavailableError` is not raised from this path (the
   RAG-resolved adoption path keeps its fail-closed embedding).

5. **Confirm-before-write stays.** Nothing is written at parse/resolve time;
   adoption happens inside `ingest_receipt_lines` during confirm, in the same
   single transaction (session-in / caller-commits).

### Owner override (2026-09-11)

The owner can reclassify an `AMBIGUOUS` line as new from the UI ("➕ Marcar
como nuevo" in the receipt-ingestion tab). Real case: the line `SM 0048-84`
(ducha flexible) resolved as `AMBIGUOUS` against 3 semantically related but
wrong candidates (arrancador, grasa, monocomando) because the product's source
page was never ingested into the RAG index — none of the retrieved candidates
was the actual product, yet only `NO_CANDIDATES` lines could proceed.

The override replaces the line with a `NO_CANDIDATES` `ResolvedLine`
(`product=None`, cached candidates dropped) purely in UI state — no domain
change. The confirm gate blocks only `AMBIGUOUS`, so the reclassified line
stops blocking and the existing adopt path creates the definitive product with
`origen={"remito": ...}` at confirm. Guard rails: resolved lines are rejected,
invalid indexes leave state untouched, already-`NO_CANDIDATES` lines are
idempotent, and zero/negative-quantity lines are refused (they are never
ingested). The reclassification is recorded by the grid label
`NUEVO (por confirmar)` and the remito provenance written at confirm.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Keep fail-closed on every unresolved line (status quo) | Blocks legitimate confirmations whenever the index lags reality; the manual-assignment screen has no candidate to assign in the 0-candidate case, so the flow dead-ends. |
| Adopt as PROVISIONAL with a review screen | Adds a new lifecycle state, UI surface and queries for a case the owner resolved by policy: the receipt itself is sufficient evidence the product is real and priced. |
| Create the product only when the line carries a supplier code (skip description-only lines) | Arbitrary: a legible description is as good an identity seed for a deterministic SKU as the code, and the supplier-scoped SKU namespace keeps collisions visible. |
| Queue unknown lines for later batch review | Defers the stock bump and breaks the "one remito, one confirmation" audit unit established by ADR 0002. |

## Consequences

**Positive**

- Receipt confirmations no longer dead-end on index lag; initial inventory
  loading (ADR 0002's primary use) works even for products the index dropped.
- No new lifecycle state: adopted products are immediately first-class
  (searchable, priceable, orderable).
- Ambiguity protection is preserved where it matters: duplicates are never
  auto-created.

**Negative / accepted debt**

- A misparsed `codigo_orig` can silently create a wrong-SKU product instead of
  being caught by the manual gate. Mitigation: the deterministic SKU makes the
  mistake visible and correctable in the catalog grid; `origen={"remito": ...}`
  records exactly which document line produced it.
- Description-derived SKUs can collide across suppliers' similarly named
  products; the supplier prefix bounds this to per-supplier namespaces, and
  the local-catalog check converts any collision into a stock bump rather than
  a duplicate row (which may mis-bump a genuinely different product — accepted
  risk, visible in the audit trail).
- Products adopted from document text carry no RAG vector until re-embedded;
  hybrid search may not surface them until then. Accepted: availability and
  orders read `Inventory`/`Catalogo`, not the index.

## Verification

- Unit: no-candidate line adopts a definitive product with
  `origen={"remito": ...}`; inventory bumped; `StockAdjustment` written; local
  SKU collision bumps the existing product; description-only line builds its
  SKU from the description; embedder failure adopts without a vector;
  ambiguous line still blocks with the new message.
- E2E: receipt with an unmatched line confirms, creates the definitive product
  and bumps stock with exactly one adjustment; receipt with an ambiguous line
  stays blocked with zero writes.
- `docs/matriz-escenarios-pedido.md` row R2 and dimension D4 updated;
  `docs/escenarios-testeados.md` regenerated from test docstrings.
