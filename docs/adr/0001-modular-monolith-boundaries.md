# ADR 0001 — Formalize the Modular Monolith with Enforced Import Boundaries

- **Status:** Accepted
- **Date:** 2026-09-07
- **Deciders:** Project owner (Rolando Daumas), architecture review with AI pair
- **Related commits:** `800450b` (phase 0 tooling), `377cdc9`, `3c83f1f`, `664839c`, `de40cd9` (phase 1 fixes)

## Context

`src/` was already organized into domain-oriented packages (`agents`, `orchestrator`,
`sourcing`, `purchasing`, `supplier`, `pricing`, `order_lifecycle`, `channels`,
`backoffice`, `integrations`, `db`, `observability`), but the boundaries existed
only as folder names. Nothing enforced them.

A full cross-module import audit (278 `src.*` import statements over 72 files)
found **6 direct import cycles**, with the core domain tangled in a mutually
dependent cluster: `{agents, orchestrator, sourcing, supplier} + integrations`.
Concrete examples:

- `sourcing/case_b.py` imported from 6 different top-level modules.
- `backoffice` pulled shared utilities (`normalize_text`, `normalize_phone`)
  from the `agents` domain package, dragging the interface layer into the
  conversational domain.
- `agents/customer.py` lazily imported client-creation **business logic** from
  `backoffice/clients.py` (domain → interface, the worst direction).
- `orchestrator/approval.py` and `agents/dispatch.py` imported the Sheets
  adapter directly, and four modules imported `integrations.rag` directly.

No boundary tooling existed: ruff and mypy (strict) were configured, but no
import-linter, no layering contract, and no CI workflows in the repository.

Two adjacent decisions were evaluated around the same time:

- **Frontend:** the Gradio backoffice stays. A React migration was rejected
  because the motivation was purely aesthetic and the backoffice is
  internal-only; a second runtime stack would add complexity with no
  functional driver.
- **Extraction precedent:** `services/rag-api/` is already a fully
  self-contained service coupling to `src/` only at the HTTP boundary, proving
  that module extraction to a service is viable when justified.

## Decision

1. **Keep the monolith.** One deployable Python application. No microservice
   split beyond the already-extracted `rag-api`.
2. **Formalize the module structure as a 4-layer architecture** with imports
   allowed only downward:

   ```
   ┌─────────────────────────────────────────────────────────────────┐
   │  L3 INTERFACE — composition & delivery                          │
   │  pipeline · api · backoffice · scheduler · barcode              │
   └───────────────────────────────┬─────────────────────────────────┘
                                   │ may import ↓
   ┌───────────────────────────────▼─────────────────────────────────┐
   │  L2 ADAPTERS — infrastructure implementations                   │
   │  integrations (openai · sheets · rag · gspread)                 │
   │  implement domain protocols; re-export domain vocabulary        │
   └───────────────────────────────┬─────────────────────────────────┘
                                   │ may import ↓
   ┌───────────────────────────────▼─────────────────────────────────┐
   │  L1 DOMAIN — business logic                                     │
   │  agents · orchestrator · sourcing · purchasing · supplier       │
   │  pricing · order_lifecycle · channels                           │
   │  same-layer imports: unrestricted today (see Consequences)      │
   └───────────────────────────────┬─────────────────────────────────┘
                                   │ may import ↓
   ┌───────────────────────────────▼─────────────────────────────────┐
   │  L0 FOUNDATION — framework-free support                         │
   │  config · tz · db · observability · features                    │
   └─────────────────────────────────────────────────────────────────┘
   ```

3. **Enforce the contract mechanically, not by convention**, using
   `import-linter` (layers contract in `pyproject.toml`, `make
   lint-boundaries`). Every violation is a CI-blocking failure.
4. **Resolve dependency direction with two idioms**, now the house patterns:
   - **Ports where consumed:** the domain module defines the narrow Protocol
     (`ClientRegistrar`, `SheetsPort`, `RagCatalogPort`); the adapter
     implements it structurally; the composition root (`pipeline.py`) injects.
   - **Domain-owned vocabulary:** data types and enums the domain speaks
     (`SheetsWriteStatus`, `RagProduct`, `normalize_rag_sku`) live in domain
     modules (`supplier/rag_catalog.py`); `integrations/` re-exports them
     preserving object identity, so `is` comparisons and test doubles keep
     working.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Microservices split of the domain | Operationally premature: single team, single deployment, ~72 files. Would multiply deploy/test/infra cost for no current scaling need. The `rag-api` extraction remains available per module if a real driver appears. |
| Do nothing (folders as documentation) | The 6 cycles were actively growing; in ~6 months the structure would degrade into a big ball of mud with tidy folder names. Cheap to fix now (1 week incremental) vs. expensive later. |
| Event-driven decoupling (in-process bus) | Solves temporal coupling, not import coupling; adds indirection and harder debugging for a codebase this size. Overkill. |
| React rewrite of the backoffice (adjacent) | Aesthetic motivation only; internal tool; Gradio shares the Python domain layer directly. Rejected — see Context. |

## Consequences

**Positive**

- Layer boundaries are machine-checked; a forbidden import breaks the build
  immediately instead of being discovered in review or production.
- Every domain module is testable below L3 without adapters or network.
- Module → service extraction is now mechanical (proven pattern: `rag-api`).
- The port + injection idiom gives a uniform way to add/remove infrastructure
  providers (OpenAI, Sheets, RAG) without touching domain logic.

**Negative / accepted debt**

- **Same-layer (L1) imports are still unrestricted.** The cycles among
  `agents ↔ orchestrator ↔ sourcing` remain *legal* under the current
  contract. Per-module neighbor rules are a planned follow-up (phase 2).
- **Legacy runtime coupling remains:** `integrations/openai.py` still imports
  `agents` and `orchestrator` types (legal L2→L1), so the adapter is not yet
  fully inverted.
- **14 pre-existing mypy strict errors** in `backoffice/app.py` and
  `agents/dispatch.py` (introduced by the 2026-09-06 RAG ingestion change)
  are unrelated debt, tracked separately.
- **No CI yet:** the repo has no `.github/workflows/`; `make
  lint-boundaries` must be wired into CI when workflows are introduced.

## Verification

- `lint-imports`: 1 contract kept, **0 ignored imports** (started at 7).
- Phase 1 was executed as 4 work-unit commits: 7 → 6 → 5 → 4 → 0 ignored
  imports, full suite green (882 tests) after every step, zero behavior
  changes (error messages and owner replies byte-identical).
