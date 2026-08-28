# Exploration: owner-order-intake

## Current State

The MVP runs a conversational intake pipeline (Python 3.12 + SQLAlchemy 2.0 + Alembic +
Postgres/pgvector, pytest, ruff, mypy) whose implied actor is the END CUSTOMER: inbound
messages from WhatsApp/Telegram are treated as customer messages, the customer is
identified by the sender's phone, and the owner only appears as a notification target.
The requested pivot inverts this: the ONLY chatbot user is the business owner, and every
order the owner creates must state which customer it is for (customer resolved by name).

**Verification of the prior routing map (all 8 points confirmed, with updates from the
landed order-sourcing-workflow change):**

1. **Telegram wiring** — `src/channels/telegram.py:41` sets `sender_id = str(chat.get("id", ""))`
   (Telegram chat id, NOT a phone). `src/channels/whatsapp.py:87` sets `sender_id` from the
   Cloud API `from` phone. `src/api/webhook.py` verifies (HMAC for WhatsApp, secret token for
   Telegram) and dispatches to `pipeline.handle_inbound` as a background task.
   `scripts/telegram_loop.py` is the dev loop (ngrok + setWebhook + uvicorn).
2. **Routing is state-based only** — `src/orchestrator/router.py:85-113`: media → Perception;
   `awaiting_decision` → Dispatch; `sourcing_selection_pending` → Sourcing; in-progress order
   → Sales/Disambiguation; fallback → CUSTOMER. There is NO actor check ("is this the owner?")
   anywhere. New since the map: the parse step (`router.py:149-158`) runs `SimpleOrderParser`
   before a fresh Customer turn.
3. **Identity: customer by sender phone** — `src/agents/customer.py:273` (`_run_sourcing_turn`
   calls `lookup_phone(session, message.sender_id)`) and
   `src/orchestrator/session.py:162` (`rehydrate_conversation` requires
   `Cliente.telefono_norm == sender_id`). `ParsedOrder.customer_name`
   (`src/agents/intake.py:31`, `_extract_customer_name` at 160-166, matches "soy/para/de
   parte de <name>") IS parsed but is NEVER used to resolve the customer — a grep confirms
   `customer_name` from `ParsedOrder` is never read anywhere in `src/`.
4. **Customer persona** — `SYSTEM_PROMPT` (`customer.py:58-63`) is end-customer-facing
   (rioplatense store assistant addressing "el cliente"), and `_run_sourcing_turn` replies
   as such ("Todavía no estás registrado como cliente; hablá con el dueño para darte de
   alta", `customer.py:274-279`).
5. **Cliente model** — keyed by unique `telefono_norm` (`src/db/models.py:93-109`);
   `Order.customer_id` is NOT NULL FK (`models.py:224`). Clients are created only in the
   backoffice (`src/backoffice/clients.py:54-79 create_client`, Gradio Clients tab in
   `src/backoffice/app.py`). No owner entity exists in the DB.
6. **Owner notifications** — `src/pipeline.py:105` builds the notifier as
   `_ChannelNotifier(CHANNELS["telegram"])` targeting `settings.owner_phone` (config,
   `src/config.py:57`); Case A calls `notify_owner` (`case_a.py:92`), Case C calls
   `cancel_for_no_supplier` (`case_c.py:52`). `parse_decision`/`apply_decision`
   (`src/agents/dispatch.py`) and `approve_and_register` (`src/orchestrator/approval.py`)
   carry REAL approval logic — but the DISPATCH agent is a walking-skeleton STUB in the
   pipeline: `pipeline.build_orchestrator` only wires CUSTOMER and SOURCING; every other
   agent (including DISPATCH) is `_stub_agent`. The owner's "aprobá/rechazá" reply routes
   to Dispatch today and nothing happens.
7. **Tests asserting customer-facing behavior** — `tests/test_customer.py` (LLM chat as
   customer), `test_phone.py`, `test_e2e_order.py` (WhatsApp message `from == customer
   phone`), `test_session_rehydrate.py:136` (phone-keyed rehydration), `test_case_a/b/c.py`
   (`sender_id=CUSTOMER_PHONE`), `test_dispatch.py` (owner notified at a separate phone),
   `test_intake_parser.py:98` (best-effort customer name).
8. **Docs** — `README.md` (WhatsApp-first customer intake), `docs/architecture.md` (phone
   identity), `docs/sourcing.md` (owner notifications over Telegram).

**End-to-end order flow today** (with `OWNER_PHONE` set): inbound message → webhook →
`pipeline.handle_inbound` → `Orchestrator.handle_inbound`: `store.get(sender_id)` (in-memory
TTL, falls back to DB rehydration keyed by customer phone) → route → parse step (fresh text
turn) → CUSTOMER handler → `_run_sourcing_turn`: `lookup_phone(sender_id)` → UNKNOWN ⇒
"register with the owner" reply; KNOWN ⇒ resolve items → `classify_case` → Case A: persist
order + quote + reservations, notify owner on Telegram, reply quote summary to the customer
sender; Case B: persist order + `SourcingNeed` rows, reply supplier-selection question;
Case C: persist + reject + cancel, notify owner, reply unavailability to the customer.
Case B selection replies route to SOURCING and accumulate OPEN POs. Approval replies route
to DISPATCH (a stub today).

**Three non-obvious findings (new since the prior map):**

- **Case B supplier selection is currently answered by the CUSTOMER, not the owner.**
  `format_case_b_reply` is the agent reply sent to `message.sender_id` (the customer), and
  `sourcing_selection_pending` is stored on the CUSTOMER's sender state. This contradicts
  `docs/sourcing.md` ("The owner selects") and the `case_b.py` docstring ("the owner's
  numbered reply"). Under the pivot this inconsistency disappears naturally: the owner IS
  the sender, so the selection question goes to the owner in chat.
- **The approval loop is not wired.** The owner receives the Case A quote as a Telegram
  notification, but the "aprobá/rechazá" reply is swallowed by the DISPATCH stub. Any
  in-chat approval for the pivot requires wiring DISPATCH to `parse_decision` +
  `apply_decision` (+ `approve_and_register` with the `SheetsWriter`).
- **The owner has no sender identity anywhere.** `owner_phone` is only a notification
  TARGET on Telegram. Telegram senders are chat ids (not phones), and there is no
  owner-Telegram-chat-id config. Recognizing "the owner is talking to the bot" needs new
  configuration (e.g. `owner_whatsapp_phone` and/or `owner_telegram_chat_id`).

## Affected Areas

- `src/config.py` — new owner-sender settings (owner WhatsApp phone and/or owner Telegram
  chat id) distinct from the existing notification-only `owner_phone`.
- `src/pipeline.py` — owner gate before/inside routing; wire DISPATCH to the real approval
  flow (`parse_decision`/`apply_decision` + `approve_and_register` with `SheetsWriter`);
  decide how quotes/notifications are delivered when the owner is the sender (chat reply
  vs. separate Telegram push); `_sourcing_deps` currently disables the whole flow when
  `owner_phone` is empty — that gate becomes "owner sender configured".
- `src/agents/customer.py` — replace the customer-facing persona with an owner-facing
  assistant prompt; `_run_sourcing_turn` must resolve the customer from
  `ParsedOrder.customer_name` (new name lookup against `Cliente.nombre_comercial` with
  exact/folded matching and ambiguity handling) instead of `lookup_phone(sender)`; replies
  addressed to the owner ("Pedido #N para <cliente>..."); optional in-chat client creation
  reusing `src/backoffice/clients.create_client`.
- `src/agents/dispatch.py` / `src/orchestrator/approval.py` — logic exists; the pivot must
  wire it into the pipeline (DISPATCH handler) and adjust quote/cancellation copy to the
  owner-in-chat context.
- `src/orchestrator/session.py` — `rehydrate_conversation` keyed by `telefono_norm ==
  sender_id` must be reworked for the owner sender: load the latest non-rejected order for
  ANY customer (or the explicitly referenced order). One owner sender can have several
  pending orders for different customers — a single `ConversationState` per sender needs an
  explicit active-order semantics ("latest order wins" vs. "pedido #N" reference).
- `src/orchestrator/router.py` — likely minimal change; the parse step and routing already
  work per sender. An owner gate (reject/redirect non-owner senders, or keep them
  customer-facing per the WhatsApp decision) is the only new routing concern.
- `src/agents/intake.py` — `_NAME_RE`/`_extract_customer_name` may need tuning for owner
  phrasing ("pedido para Ferretería Don Juan"), multi-word commercial names, and
  client-creation commands.
- `src/backoffice/clients.py` — `create_client`/`update_client` reusable for in-chat
  client registration (likely no change; possibly a default-price-list helper).
- Tests — `tests/test_case_a.py`, `test_case_b.py`, `test_case_c.py`,
  `tests/test_e2e_order.py`, `tests/test_session_rehydrate.py`, `tests/test_customer.py`,
  `tests/test_router_sourcing.py` all encode sender-is-customer; they must be reworked to
  owner-sender + named-customer fixtures, plus NEW tests for owner gate, name ambiguity,
  in-chat approval, and owner rehydration.
- Docs/specs — `README.md`, `docs/architecture.md`, `docs/sourcing.md`; delta specs for
  `openspec/specs/whatsapp-order-intake/`, `agent-orchestration/`, `order-sourcing/`.

## Approaches

1. **Minimal owner pivot (persona swap + owner gate + name-based customer + dispatch wiring)**
   Add owner-sender config; gate inbound traffic so the owner is the chat actor; swap the
   Customer agent persona to an owner-assistant; resolve the customer from
   `ParsedOrder.customer_name` with exact/folded `nombre_comercial` matching, ambiguity
   prompting, and "client not found → offer to register from chat"; rework rehydration to
   "latest open order for any customer" for the owner sender; wire DISPATCH to the existing
   `parse_decision`/`apply_decision`/`approve_and_register` so approve/reject works in chat.
   - Pros: reuses ALL existing sourcing machinery (parse → classify → case A/B/C persist →
     PO accumulation) and the real dispatch/approval modules that are already tested; small
     surface; the Case B owner-selection inconsistency is fixed for free; demo-ready fast.
   - Cons: owner persona is still bolted onto the Customer agent name; single
     `ConversationState` per sender means multiple concurrent orders need "latest order
     wins" semantics (acceptable for MVP demo); client creation from chat is a new flow.
   - Effort: Medium

2. **Full owner-console refactor**
   Introduce an OWNER agent (or console router) with its own command vocabulary ("nuevo
   pedido para X", "aprobar #N", "clientes", "crear cliente ..."), an order registry view in
   chat, multi-order state keyed by order id (not just sender), and full client CRUD from
   chat; keep the legacy customer path untouched behind a flag.
   - Pros: clean long-term architecture; no conflation of actor and customer; scales to the
     post-MVP vision of the owner managing everything from chat.
   - Cons: large refactor of routing/session/persona; high regression surface on the
     working sourcing flow; far more than the MVP demo needs.
   - Effort: High

3. **Dual-mode intake (owner and customer senders both supported)**
   Keep customer senders working as today (phone identity) AND add the owner path (name
   identity) selected by the sender gate.
   - Pros: preserves the original customer-chat value prop; owner can demo either side.
   - Cons: two personas, two identity models, and two rehydration keys in one pipeline;
     the state ambiguity (sender == customer vs. sender == owner) compounds; largest test
     surface; the pivot's clarity ("ONLY chatbot user is the owner") is diluted.
   - Effort: Medium-High

## Recommendation

Approach 1 (minimal owner pivot). The pivot's product intent is a single-actor demo: the
owner drives intake, approval, and supplier selection from one chat. Every hard piece
already exists — the parser, case classification, persistence, PO accumulation, and the
real (but unwired) dispatch/approval modules. The work concentrates in four seams: (a)
owner-sender identity + gate, (b) customer-by-name resolution with ambiguity handling, (c)
DISPATCH wiring for in-chat approval, and (d) rehydration keyed to the owner sender instead
of the customer phone. The pivot also happens to fix the current Case B contradiction
(customer picking suppliers vs. docs saying owner picks).

Open questions the proposal must resolve: (1) WhatsApp channel posture — document BOTH
options and let the user decide: owner-only WhatsApp (single persona everywhere; simplest;
owner demos on the channel they already use; needs an owner WhatsApp sender gate) vs.
WhatsApp stays customer-facing (customers keep ordering by phone; the pivot demo runs only
on Telegram; two identity models coexist). Tradeoffs: owner-only = one persona, one
rehydration model, but foregoes the customer channel until later; customer-facing WhatsApp
= preserves the original roadmap, but doubles personas, tests, and ambiguity risk. (2)
Owner sender identity config — `owner_telegram_chat_id` + `owner_whatsapp_phone` (or a
generic list), and what happens to non-owner senders under each WhatsApp posture. (3)
Customer-name matching semantics — exact first, then accent/case-folded containment; how
many candidates triggers the disambiguation question vs. auto-pick; whether
`lookup_phone`/`telefono_norm` remains the customer key at rest (yes — it stays the DB
unique key; only the chat resolution changes). (4) In-chat client creation — single-message
parse ("nuevo cliente <nombre> <teléfono>") vs. short multi-turn; reuse
`backoffice.clients.create_client`. (5) Multiple pending orders in one owner chat — "latest
order wins" + explicit `pedido #N` reference in messages, or an active-order pointer in
state. (6) Notification posture — with the owner in chat, quotes/cancellations become chat
replies; does the separate Telegram push to `owner_phone` stay (dual delivery) or go?

## Risks

- **`lookup_phone`/`telefono_norm` is load-bearing for rehydration** — changing
  `rehydrate_conversation` to owner-sender semantics touches the multi-turn Case B
  survival guarantees (SourcingNeed rows) and `tests/test_session_rehydrate.py`; a
  mistake silently breaks the 30-min-TTL recovery path.
- **DISPATCH has never run in production** — `parse_decision`/`apply_decision`/
  `approve_and_register` are tested in isolation, but wiring them into the live pipeline
  exposes the Sheets writer, notifier, and event-loop bridging (`_ChannelNotifier`
  background-task pattern) for the first time; approval confirmation must not be lost when
  the reply happens inside the webhook background task.
- **Owner-sender misidentification** — the owner gate is the new security boundary of the
  whole demo: a non-owner sender that passes the gate would create orders/approve quotes
  as the owner. The gate must be explicit config, not "any sender with `owner_phone`
  behavior".
- **Name-matching ambiguity** — `nombre_comercial` is free text (e.g. "Don Juan" vs
  "Ferretería Don Juan"); folded substring matching can over-match (multiple clients) or
  under-match (typos); ambiguity handling must be a tested, deterministic flow, and the
  regex-based `_NAME_RE` extraction is brittle for multi-word commercial names.
- **Conversation state is per-sender, not per-order** — with ONE owner sender handling
  many customers, "latest open order" semantics can route an approval to the wrong order
  when several are pending; the proposal must define order addressing (explicit
  `pedido #N` references) or accept the MVP limitation explicitly.
- **Test churn is wide but mechanical** — case A/B/C, e2e, rehydration, and router tests
  all encode sender-is-customer fixtures; they must move to owner-sender + named-customer
  fixtures without weakening coverage of the sourcing flows themselves.
- **Spec/docs drift** — `whatsapp-order-intake`, `agent-orchestration`, and
  `order-sourcing` specs plus README/architecture/sourcing docs all describe the
  customer-first world; they need MODIFIED deltas in the same change or the documentation
  contradicts the product.

## Ready for Proposal

Yes. The orchestrator should tell the user: the map is confirmed and up to date; the
pivot's four seams are (a) owner identity + gate, (b) customer-by-name resolution, (c)
DISPATCH wiring, (d) owner-keyed rehydration. Everything else (parse → classify → case
A/B/C → POs) is reusable. Two findings surfaced: supplier selection is currently answered
by the customer (contradicting the docs, fixed for free by the pivot) and the approval
loop is a stub that has never been wired. The proposal must lock the six open questions
above — most importantly the WhatsApp channel posture, which the user must decide, and the
owner-sender config shape — before spec writing.
