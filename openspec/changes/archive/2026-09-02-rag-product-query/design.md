# Design: RAG-backed product queries (local-first → RAG-fallback)

## Technical Approach

Approach 2 (explore): keep `CatalogSearcher` as the local hop; add `RagProductClient` (mirrors `integrations/openai.py`) plus `PrecedenceProductSearcher` returning a source-discriminated `ProductSearchResult`. `build_handler` renders a source-aware note; wiring stays injectable via `build_orchestrator(searcher=...)`.

## Architecture Decisions

| Decision | Alternatives | Rationale |
|---|---|---|
| Dual-source note = multi-turn order-building accumulation | (b) call RAG even on local hit in order-building | (b) breaks local-first + doubles latency. A chain call is single-source by construction; mixed lists arise when the owner's draft accumulates local + RAG items across queries. Renderer sorts local-first regardless. |
| SKU hygiene: normalize once at the client | trust raw `codigo`; regex at render | `normalize_rag_sku` collapses a duplicated `{provider}-` prefix (`AMX-AMX-AT-5044`→`AMX-AT-5044`); prefer `codigo_orig`, fall back to normalized `codigo`. |
| `RagProductClient` in `integrations/rag.py`; sync `httpx`, injectable transport | async client; `requests` | mirrors `OpenAIResponder` (sync, lazy holder, domain errors). Tests pass `httpx.MockTransport`. `RagProductError`→ERROR. |
| Chain = `PrecedenceProductSearcher` → `ProductSearchResult(source, entries)` | extend `SearchCandidate` | source enum `LOCAL\|RAG\|NONE\|ERROR`; `SearchCandidate` untouched. "Hit" = ≥1 local candidate at/above `search_ambiguity_floor`; else RAG. |
| Note: English source-aware templates, numbered, cheapest-first, local-first | Spanish rioplatense templates | explicit "English UI copy" directive + internal system note (LLM persona renders rioplatense). Never claims "no stock". |
| Order-building: pure `parse_product_add` + two `ConversationState` fields | touch `resolve_item`/rehydration | out of scope. Draft accumulation only; offer-to-create prompts the full-order message through the existing sourcing path. |
| Observability: logs at client + chain | silent failures | log latency/product count, refusal status, and a warning on error for later tuning. |

## Data Flow

```
query → build_handler → PrecedenceProductSearcher.search(text)
                          local = DbCatalogSearcher.search(text)
              ┌───────── floor-qualified hit? ─────────┐
              │ yes                                   no │
         source=LOCAL                        RagProductClient.query
         (RAG never called)                  success→RAG | refusal/[]→NONE | error→ERROR
                          product_context_note(result) → system note → LLM
```

Local `SQLAlchemyError` still propagates to `build_handler`, which skips the note — RAG is not a DB-outage fallback.

## File Changes

| File | Action | Description |
|---|---|---|
| `src/integrations/rag.py` | Create | `RagProduct`, errors, `normalize_rag_sku`, `RagProductClient` |
| `src/agents/product_search.py` | Create | `ProductSource`, `ProductEntry`, `ProductSearchResult`, `ProductSearcher`/`LocalSearcher`, `PrecedenceProductSearcher`, `parse_product_add` |
| `src/agents/customer.py` | Modify | `catalog_context_note`→`product_context_note`; seam → `ProductSearcher`; add-intent short-circuit |
| `src/orchestrator/session.py` | Modify | add `product_options`, `draft_items` |
| `src/pipeline.py` | Modify | wire `PrecedenceProductSearcher(DbCatalogSearcher(), RagProductClient())` |
| `src/config.py`, `.env.example` | Modify | `RAG_BASE_URL`(8001), `RAG_TIMEOUT_SECONDS`(10), `RAG_TOP_N`(3), `RAG_THRESHOLD`(0.45), `RAG_TABLE_NAME`, `RAG_MODEL` |
| `tests/test_rag.py`, `tests/test_product_search.py` | Create | client + chain + `parse_product_add` |
| `tests/test_customer.py`, `tests/test_pipeline.py` | Modify | fake `ProductSearcher`; precedence/refusal/error shapes |

## Interfaces / Contracts

```python
def normalize_rag_sku(codigo: str, provider: str) -> str   # collapse "P-P-" prefix
class ProductSource(str, enum.Enum): LOCAL, RAG, NONE, ERROR
@dataclass(frozen=True)
class ProductEntry:
    sku: str; name: str; source: ProductSource
    provider=None; brand=None; price=None; currency=None; unit=None
    specs=None; source_file=None; page=None
@dataclass(frozen=True)
class ProductSearchResult:
    source: ProductSource; entries: tuple[ProductEntry, ...] = ()
def parse_product_add(text, options) -> tuple[int, int] | None  # (index, qty)
# ConversationState: product_options: tuple[ProductEntry, ...]; draft_items: tuple[tuple[ProductEntry, int], ...]
```

`PrecedenceProductSearcher` depends on a `LocalSearcher` protocol (structurally satisfied by `DbCatalogSearcher` and test fakes) — no import cycle with `customer.py`/`session.py`.

## Note Rendering

RAG: `Catalog results for «{query}» — supplier catalog:` then `{i}. {name} — {brand} — {provider} — {price} {currency}/{unit} — {specs} — {source_file} p.{page}`, cheapest first, closing `These are supplier-catalog items, not own stock.`

LOCAL: `…— own stock:` then `{i}. {nombre_oficial} ({sku})`. Dual: local block first (global numbering), then RAG cheapest-first, each labeled. NONE: `no match in current catalogs. Ask the owner for a synonym or reformulation. Do not claim the item is out of stock.` ERROR: `supplier catalogs could not be consulted. Tell the owner they are unavailable and offer to retry later. Do not claim the item is out of stock.`

## Testing Strategy

| Layer | What | Approach |
|---|---|---|
| Unit | `normalize_rag_sku`, `parse_product_add` (agregalo/sumá N/el 2) | parametrized pure tests |
| Unit | `RagProductClient` success/refusal/timeout/500/sku-normalize | `httpx.MockTransport`; assert domain errors + args |
| Unit | `PrecedenceProductSearcher` hit-skips-RAG / fallback / none / error | fake `LocalSearcher` + fake client |
| Unit | note shapes per source | fake `ProductSearcher` → assert exact note text |

## Threat Matrix

N/A — no shell, subprocess, VCS/PR automation, executable-file classification, or process-integration boundary. The outbound HTTP call targets a configurable base URL (default 8001): a deployment-config concern, not an adversarial-routing boundary.

## Migration / Rollout

No migrations. Flag the 8000↔8001 doc drift. Rollback = revert `pipeline.py` wiring to `DbCatalogSearcher`, delete `integrations/rag.py`.

## Resolved Open Questions

- [x] Dual-source trigger (ADR 1) · [x] SKU hygiene (ADR 2) · [x] client/transport/timeout/errors (ADR 3) · [x] chain + floor reuse (ADR 4) · [x] note rendering + English copy (ADR 5) · [x] order-building state (ADR 6) · [x] observability (ADR 7)
