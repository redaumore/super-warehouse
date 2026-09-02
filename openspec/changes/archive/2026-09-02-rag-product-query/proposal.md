# Proposal: RAG-backed product queries (local-first → RAG-fallback)

## Intent

Step 1 of customer order generation: Telegram product queries MUST be answered through the supplier-catalog RAG as if there were no inventory, replacing today's empty-catalog "no stock" reply.

## Scope

### In Scope
- Local-first → RAG-fallback chain behind the product-query seam (fixed; no bypass for exact codes).
- `RagProductClient` (sync httpx, injectable transport, `structured_json=true`) with result `LOCAL|RAG|NONE|ERROR`.
- Source-aware note: product, provider, price, specs, source page/PDF; results numbered, cheapest first ("el 2" disambiguates); dual-source entries labeled, local first.
- `is_refusal` → suggest synonyms/reformulation; RAG down/slow → structured unavailability notice (no retry, no stock claim).
- Same for order-building queries; product addable via natural phrases ("agregalo", "sumá 5 de eso"); no open order → offer one.

### Out of Scope
- Supplier seam (Case B) upgrade; `catalogo.embedding`; three-layer `products` model; spec changes to `order-sourcing`/`supplier-catalog-search`; `resolve_item`; rehydration.

## Capabilities

### New Capabilities
- `rag-product-query`: RAG client contract, precedence chain, source-discriminated results, refusal/unavailability handling.

### Modified Capabilities
- `catalog-search`: note behavior — RAG fallback on empty local search; source-aware rendering replaces "no stock" note.

## Approach

Approach 2 (exploration): `CatalogSearcher` stays the local hop; add `RagProductClient` (mirrors `integrations/openai.py`) plus `PrecedenceProductSearcher`. `build_handler` renders per source; wiring stays injectable.

## Affected Areas

| Area | Impact |
|------|--------|
| `src/integrations/rag.py` | New — RagProductClient |
| `src/agents/product_search.py` | New — chain + DTO |
| `src/agents/customer.py` | Modified — note rendering |
| `src/pipeline.py` | Modified — wire composite |
| `src/config.py`, `.env.example` | Modified — RAG settings (port 8001) |
| `tests/` (customer, pipeline, rag) | Modified — precedence/failure/refusal cases |

## Decision Gaps

- Wiring "add to order via natural phrases" without touching `order-sourcing`: CUSTOMER-agent prompt vs. later sourcing delta — fix in spec/design.
- Note content: `productos[]` fields vs. `respuesta_narrativa` (recommend the former).

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| RAG latency ~5s (15s cold) | High | ACK-first; 8–10s timeout |
| RAG-down read as "no stock" | Med | ERROR → unavailability note |
| SKU hygiene (double prefix) | Med | Never display raw `codigo` |
| Port drift (docs 8000, real 8001) | Med | Default 8001; flag docs |

## Rollback Plan

Revert `pipeline.py` wiring to `DbCatalogSearcher`, delete `integrations/rag.py`. DB untouched.

## Dependencies

- RAG reachable at `http://localhost:8001` (sibling repo `fase-0-pdf-parsing`).

## Success Criteria

- [ ] Empty local catalog returns RAG products (provider, price, specs, source).
- [ ] Results numbered, cheapest first; "el 2" disambiguates.
- [ ] Dual-source shows both entries, source-labeled, local first.
- [ ] Refusal → reformulation suggestion; RAG down → unavailability notice.
- [ ] Local hits never reach RAG; supplier/sourcing specs untouched.
