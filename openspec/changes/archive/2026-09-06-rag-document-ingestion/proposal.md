# Proposal: RAG-backed Document Ingestion

## Intent
The current flow uses GPT-4o Vision/regex and `confirm_items` creates unknown products. Make `catalogo_productos_rag` authoritative: parse with Luna, require review, and fail closed on unresolved lines.

## Scope
### In Scope
- Put an active-supplier dropdown first; display `business_name`, retain the ID.
- Add a RAG multipart endpoint using `DirectLunaCatalogProcessor`/`gpt-5.6-luna` Structured Outputs with quantity/cost.
- Add client parse and indexed exact lookup by supplier code/`codigo_orig`; hybrid search only for misses.
- Add identified-product grid, per-line code search, and confirmation gating.
- Persist stock only from matched RAG rows carrying `node_id` provenance.

### Out of Scope
- RAG reindexing or catalog authoring.
- Cross-service imports or general RAG retrieval redesign.
- Confirming unresolved lines or creating products from model output alone.

## Capabilities
### New Capabilities
- `rag-document-ingestion`: parse, resolve, review, and persist catalog-backed receipt lines.
- `manual-rag-product-resolution`: assign pending lines by RAG product-code search.
### Modified Capabilities
- None identified; sdd-spec maps any existing ingestion contract.

## Approach
`app.py` puts supplier selection first and passes metadata to preview/confirm. The RAG endpoint adapts uploads to the processor's PDF/page contract, returns structured lines/source metadata, and errors without writes. An exact route queries indexed `codigo_orig` with supplier scope and returns row/`node_id`; misses use hybrid. The grid shows matches, supports pending resolution, and gates confirmation until all positive lines match. Rework `confirm_items` to update/adopt matched rows; remove the unknown branch.

## Affected Areas
| Area | Change |
|---|---|
| `src/backoffice/app.py` | Supplier-first UI and review wiring. |
| `src/backoffice/ingestion.py` | Two-pass resolution and fail-closed writes. |
| `src/integrations/rag.py` | Parse/exact-lookup client. |
| `services/rag-api/app/...`, `tests/...` | Endpoint/schema and regression coverage. |

## Risks
| Risk | Mitigation |
|---|---|
| RAG/Luna unavailable or slow | Timeout, honest error, no writes. |
| Missing quantity or duplicate codes | Schema, supplier scope, manual assignment. |
| Partial inventory transaction | Single transaction and rollback tests. |

## Rollback Plan
Revert UI/client/endpoint code without deleting RAG data. If needed in production, disable ingestion rather than restore legacy creation.

## Dependencies
Reachable RAG API, OpenAI credentials, indexed table, active suppliers, and provenance services.

## Open Design Questions
- Convert image uploads, or constrain this flow to PDFs?
- Extend `ExtractedProduct`, or add an ingestion-only output schema?
- For matched-but-local-missing rows, reuse `adopt_product` or create a receipt use case?
- Define duplicate-code, case-normalization, and manual-candidate policy.

## Success Criteria
- [ ] Active `business_name` selection precedes upload; no free numeric ID.
- [ ] Luna parses; exact lookup precedes hybrid fallback.
- [ ] Confirmation is blocked while any line is unresolved.
- [ ] Tests prove every write has RAG provenance and no unmatched line creates `Catalogo`.
