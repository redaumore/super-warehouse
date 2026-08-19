# Exploration: mvp-ferreteria — Hardware Store Multi-Agent System MVP

## Current State

Greenfield project (`super-warehouse`). No source code; stack undecided at SDD-init time. The repository contains only the OpenSpec scaffold (`openspec/`) and two Spanish specification documents under `docs/` that fully define the product:

1. **MVP Product Definition** (`Definición de Producto MVP - Sistema Ferretería.md`) — product vision, value proposition, in/out-of-scope, 4 user journeys, product architecture, Gradio backoffice modules, KPIs, 4-week roadmap.
2. **Technical Specification & Implementation Plan** (`Especificación Técnica y Plan de Implementación.md`) — business diagnosis, n8n-vs-Python decision, 6-agent architecture, agent tools, business rules, data schema, dual interface design, phased plan (Fases 1–3 = MVP, Fase 4 = post-MVP).

The product is an AI multi-agent system for a wholesale hardware-store business (ferretería corretaje/mayorista) that automates order intake via WhatsApp, dynamic pricing, owner approval (human-in-the-loop), supplier purchase-document ingestion (remitos/invoices) via vision OCR, barcode-based stock operations, and order registration to Google Sheets.

## Affected Areas (future — greenfield, nothing to modify today)

- `docs/…` (2 files) — read-only source of requirements (Spanish).
- `openspec/changes/mvp-ferreteria/` — target of SDD artifacts (proposal → specs → design → tasks → verify).
- Future data model (PostgreSQL per tech doc): `proveedores`, `proveedor_sku_mapping`, `clientes`, `catalogo`, `stock_reservations` — plus MISSING entities: `lista_precios` and `orders` (referenced but never defined).
- Future agent system: 6 agents + orchestrator (Perception, Customer & Context, Disambiguation, Inventory & Pricing, Conversational Sales, Dispatch & Owner Assistant).
- Future integrations: WhatsApp webhook (Telegram TBD), OpenAI APIs (Whisper STT, GPT-4o Vision, text-embedding-3-small), Google Sheets API, barcode decode (EAN-13/UPC/QR/internal).

## MVP Scope — What Is IN

| Capability | Source |
|---|---|
| WhatsApp order intake: text + voice notes (.ogg/.mp3), transcription | Both docs |
| Hybrid catalog search: fuzzy + vector (embeddings), numbered disambiguation menus | Both docs |
| Customer identification by phone → price list + particular discount | Both docs |
| Dynamic pricing: cost × (1 + margin), then client list/discount | Both docs (formula images unreadable — see risks) |
| Owner approval loop via WhatsApp (voice/text) with custom adjustments; reject path | Both docs |
| Soft-lock inventory reservation, TTL 30 min, auto-rollback | Tech doc |
| Approved order → Google Sheets row + definitive stock deduction + client confirmation | Both docs |
| Supplier remito/invoice ingestion (photo/PDF) via GPT-4o Vision → item/cost/quantity extraction → confirm-before-entry | Both docs |
| Barcode photo → stock query and stock adjust (audited) from WhatsApp | Both docs |
| Gradio backoffice: remito ingestion grid, catalog/stock editor, clients & price lists, live order monitor | Both docs |
| Ephemeral ACK to client < 5 s; quote SLA < 3 min (KPI) | Tech doc |

## MVP vs. Larger System — Boundaries per the Two Docs

**Agreed (both docs):** MVP = 4-week build, 3 phases, pilot release at end of week 4. Post-MVP (Fase 4 / V1.1): predictive habitual-order suggestions, automatic substitute suggestions, automated daily logistics route-sheet generation, and support for very low-legibility handwritten documents.

**Explicitly out of MVP (both docs):** AFIP fiscal integration, official e-invoices/fiscal remitos, ERP/accounting-system integration, payment gateways/chat checkout, illegible handwriting (V1.1).

**Conflicts / gaps between the two docs (clarification risks):**

1. **Telegram**: product doc says WhatsApp only; tech doc shows "WhatsApp/Telegram" in architecture and webhook deliverables. MVP channel list must be fixed.
2. **Habitual orders**: `get_frequent_orders()` is an MVP agent tool (Agent 2), but the "el pedido habitual" predictive module is a Fase 4 (post-MVP) deliverable. Which slice of that capability is MVP?
3. **Substitutes**: `suggest_substitutes()` is an MVP agent tool (Agent 5), but automatic substitute suggestion is a Fase 4 deliverable. Same boundary question.
4. **Supplier price lists**: `parse_supplier_price_list()` (PDF/Excel) exists in Agent 1's tools, but MVP in-scope only names remitos/facturas ingestion. In or out?
5. **Pricing formula**: product doc says `(Cost × Margin) × Client Discount`; tech-doc formula is embedded in unreadable images; `catalogo` example shows 925.92 × 1.35 = 1250.00 (i.e., price = cost × (1 + margin)). The relationship between price lists (Gremio A / Gremio B / Base) and margin/price is never specified.
6. **Missing `lista_precios` entity**: `clientes.lista_precios_id` references it, but no schema row exists. How lists differ (margins? multipliers?) is undefined.
7. **Missing `orders` entity**: `order_id` appears in `stock_reservations` and flows, and order states are enumerated (Pending Approval / Approved / In Dispatch / Rejected), but no `orders` table schema is given.
8. **Client credit/payment conditions**: mentioned by `get_customer_profile()` but absent from the `clientes` schema.
9. **KPI numeric targets**: identification-precision % and voice-approval-adoption % are embedded images with no extractable text.
10. **"Hoja de Ruta" naming**: MVP backoffice module is "Monitor de Pedidos y Hoja de Ruta" (a live order view), while automated route-sheet GENERATION is Fase 4. Naming overlap risks scope creep; MVP scope is the monitor view only.
11. **Stack**: init says undecided; the tech doc prescribes custom Python + OpenAI SDK + FastAPI + PostgreSQL + pgvector/Qdrant + Gradio + Google Sheets. The proposal phase must confirm this prescription as the chosen stack or treat it as directional only.

Timeline differences (week mapping of phases) are cosmetic — overlapping weeks, same end milestone.

## Approaches (how to structure the SDD change)

1. **Single change, domain-split specs, phase-grouped tasks** — one `mvp-ferreteria` change; specs by domain (catalog, clients, pricing, orders, remito ingestion, barcode ops, backoffice); tasks mirror Fases 1–3.
   - Pros: faithful to docs; one approval gate; cross-domain rules (soft-lock, pricing) stay coherent in one design.
   - Cons: large spec surface for a greenfield MVP; needs discipline to keep tasks small.
   - Effort: Medium.

2. **Three phase-aligned changes** (`mvp-ferreteria-fase1/2/3`) — one SDD cycle per doc phase.
   - Pros: smaller reviews; each phase has its own verify gate.
   - Cons: cross-phase domain rules (pricing, soft-lock) get split across changes; more orchestration; Fase boundaries overlap (weeks 2–3).
   - Effort: High orchestration overhead.

3. **Vertical-slice change** — minimal end-to-end flow first (1 product, 1 client, order → approval → Sheets), then widen.
   - Pros: fastest validation, de-risks AI ambiguity early.
   - Cons: diverges from doc structure; slice definition is arbitrary; risks data-model rework.
   - Effort: Low initially, hidden rework risk.

## Recommendation

**Approach 1** (single `mvp-ferreteria` change, domain-split specs, tasks grouped by Fases 1–3). The docs define one coherent 4-week MVP; splitting into three changes adds orchestration without reducing real risk, and a vertical slice is an implementation strategy better chosen at design time. The 10 boundary questions above must be resolved BEFORE spec-writing: present them to the user (owner/product) as a single clarification round.

## Risks

1. Pricing matrix semantics (price lists vs. margins vs. discounts) are underspecified — the core commercial rule of the product.
2. MVP/post-MVP boundary contradictions (frequent orders, substitutes, supplier price lists, Telegram) may inflate or shrink scope if left ambiguous.
3. Data model gaps (`orders`, `lista_precios`, credit conditions) will surface during spec/design and need decisions.
4. KPI targets are unreadable images in both docs — pilot verification criteria may need re-confirmation.
5. Doc-prescribed stack vs. init "undecided" state — proposal phase must confirm Python/FastAPI/PostgreSQL/OpenAI as the actual stack.
6. No test runner, no CI (repo initialized only recently) — TDD/verification strategy for the apply phase is undefined.

## Ready for Proposal

**No — one clarification round first.** The orchestrator should ask the user to resolve the boundary questions above (especially pricing semantics, channel list, and the MVP/post-MVP overlaps) before `sdd-propose` writes scope. Everything else is ready.
