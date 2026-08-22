# Proposal: mvp-ferreteria — Hardware Store Multi-Agent MVP

## Intent

Automate wholesale core operations (WhatsApp intake, dynamic pricing, owner approval, document ingestion, stock ops) via multi-agent system, cutting owner operational time and manual data entry.

## Scope

### In Scope
- WhatsApp text + voice intake, transcription; Telegram optional until demo ends
- Hybrid catalog search (fuzzy + vector), disambiguation menus
- Phone-based identification → price list + particular discount
- Pricing: Base = cost × (1 + margin%); Final = Base × (1 − list_discount) × (1 − particular_discount)
- WhatsApp approval loop, adjustments, reject path
- Soft-lock, 30-min TTL, auto-rollback
- Approved order → Google Sheets + stock deduction + confirmation
- Remito/invoice (photo/PDF) + price lists (PDF/Excel), OCR confirm-before-entry
- Barcode photo → audited stock query/adjust
- Gradio backoffice: ingestion, catalog/stock, clients & lists, monitor

### Out of Scope
- AFIP, e-invoices/fiscal remitos, ERP, payment gateways
- Client credit / payment conditions
- Illegible handwriting (V1.1); habitual orders, substitutes, route-sheets (Fase 4)

## Capabilities

### New Capabilities
- `whatsapp-order-intake`: text/voice intake, ephemeral ACK
- `catalog-search`: fuzzy+vector search, disambiguation menus
- `clients-and-price-lists`: phone ID, Gremio A/B/Base lists, discounts
- `pricing-engine`: margin + list/particular discounts
- `order-lifecycle`: approval, soft-lock, rejection, Sheets registration
- `supplier-document-ingestion`: remito + price-list OCR
- `barcode-stock-ops`: audited stock query/adjust
- `backoffice`: Gradio modules
- `agent-orchestration`: 6 agents + orchestrator contract

### Modified Capabilities
None.

## Approach

Single change (Approach 1): specs by domain, tasks by Fases 1–3. Architecture: 6 agents + orchestrator, WhatsApp intake, OCR, Sheets registration. Stack deferred to design; tech-doc prescription (Python/FastAPI/PostgreSQL/OpenAI/Gradio/Sheets) directional only.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `openspec/changes/mvp-ferreteria/` | New | SDD artifacts |
| Future `src/` | New | Agents, API, data model |
| Google Sheets | New | Order registration |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| STT/OCR errors | Med | Menus; confirm-before-entry; approval gate |
| Pricing/soft-lock edge cases | Med | Spec scenarios; TTL rollback |
| Fase 4 scope creep | Med | Out-of-scope list |

## Rollback Plan

- Greenfield: stop at any Fase boundary; per-Fase flags.
- Sheets append-only with order IDs; corrupt rows quarantined.
- 30-min TTL auto-releases reservations; OpenSpec + Engram preserve trail.

## Dependencies

- OpenAI APIs, WhatsApp Business API, Google Sheets API.

## Success Criteria

- [ ] Ephemeral ACK < 5 s
- [ ] Identification precision (STT + RAG) ≥ 85%
- [ ] 80% reduction in remito/invoice data-entry time
- [ ] Quote response < 3 min
- [ ] 70% reduction in owner operational time
- [ ] Voice-approval adoption ≥ 90%
- [ ] Unapproved orders auto-released after 30-min TTL
