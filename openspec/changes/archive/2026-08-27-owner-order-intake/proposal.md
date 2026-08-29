# Proposal: Owner Order Intake

## Intent

The chatbot's only user becomes the owner: intake, quotes, cancellations, supplier selection, and approvals all happen in the owner's chat, and every order names its customer, resolved by name (not sender phone).

## Scope

### In Scope
- Owner sender gate on both channels (explicit allowlist; polite rejection otherwise).
- Customer-by-name resolution: exact `nombre_comercial` → folded containment; 1 auto-picks, ≥2 ask, 0 offers creation; `telefono_norm` stays DB key. In-chat creation: `nuevo cliente <nombre> <teléfono>` (reuses `backoffice.clients.create_client`).
- Owner-keyed rehydration: latest open order; `pedido #N` in replies, `#N` override in decisions; quotes/cancellations become in-chat replies, separate push to `owner_phone` removed.
- DISPATCH wired: `parse_decision`/`apply_decision` + `approve_and_register` (SheetsWriter).
- Test pivot; new gate/ambiguity/approval/rehydration tests.

### Out of Scope
Multi-owner, customer-facing WhatsApp, owner-console refactor, backoffice beyond `create_client` reuse.

## Capabilities

### New Capabilities
None.

### Modified Capabilities
- `whatsapp-order-intake`: owner-only sender; name-resolved customers; non-owner rejection; chat client creation.
- `agent-orchestration`: DISPATCH wired to real approval; owner-keyed rehydration; parse-step additions.
- `order-sourcing`: quotes/cancellations/approvals in owner chat; Case B answered by owner. Spec: unarchived `order-sourcing-workflow` delta.

## Approach

Exploration approach 1 (minimal owner pivot): reuse parse → classify → case A/B/C → PO accumulation; concentrate on gate, name resolution, DISPATCH wiring, rehydration.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `src/config.py` | Modified | New owner-sender keys; `owner_phone` deprecated |
| `src/pipeline.py` | Modified | Owner gate; wire DISPATCH; drop notifier pushes |
| `src/agents/customer.py` | Modified | Owner persona; name resolution |
| `src/orchestrator/{session,router}.py` | Modified | Owner-keyed rehydration; gate; client command |
| `tests/`, `README.md`, `docs/` | Modified | Owner fixtures + docs |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| DISPATCH never ran in production | Med | Await in webhook task; Sheets failure → reply, order pending |
| Non-owner passes the gate | Low | Explicit config; normalized compare; test matrix |
| Name ambiguity over/under-match | Med | Exact→folded; ≥2 always asks; parametrized tests |
| Latest-order-wins misroutes approval | Med | `pedido #N` + override; documented MVP limit |
| Rehydration rework breaks Case B | Med | DB source of truth; extend rehydrate tests |

## Rollback Plan

No migrations. Revert + redeploy; `owner_phone` stays parseable.

## Dependencies

- Landed `order-sourcing-workflow` (parse/classify/cases/PO machinery).
- Google Sheets creds for `approve_and_register`.

## Success Criteria

- [ ] Owner senders pass the gate; others politely rejected; customer phone-lookup gone.
- [ ] Name resolution: 1 → auto-pick, ≥2 → ask, 0 → create; `nuevo cliente` creates a Cliente.
- [ ] Quote in chat; `aprobá`/`rechazá` run `apply_decision` + `approve_and_register`; Case B owner selection accumulates POs; post-TTL rehydration recovers latest open order; pytest green.
