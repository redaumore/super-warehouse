# Apply Progress: customer-order-persistence

**Mode**: Standard (`strict_tdd: false`)
**Delivery**: `single-pr`, owner-approved 2000-line budget
**Status**: WU1–WU4 and Phase 5 complete; all final runtime objectives settled successfully
**Commits**:

- `90881fe feat(pricing): add customer order pricing foundation`
- `dde0188 feat(order): add RAG price lookup and draft persistence`
- `eacbd00 feat(customer): finalize draft orders`
- `2eed076 feat(backoffice): add customer orders maintenance`
- `404a1b2 test(customer-orders): approval Sheets boundary and migration round-trip`

## Completed Tasks

### WU1 — DB + pricing foundation

- [x] 1.1 Add the reversible additive customer-order migration with order totals,
  line snapshots, exchange rates, app settings, and ARS/default-margin seeds.
- [x] 1.2 Add `Order`/`OrderItem` persistence fields plus `ExchangeRate` and
  `AppSetting` ORM models.
- [x] 1.3 Add pure source-aware `compute_order` pricing with `PricedLine`,
  `PricedOrder`, and `MissingRateError`.
- [x] 1.4 Add unit coverage for local cost pricing, RAG supplier/default margins,
  missing rates, conversion, subtotals, and final totals.
- [x] 1.5 Include `exchange_rates` and `app_settings` in test cleanup.

The first focused attempt exposed a syntax error in the new pricing module; it was
settled as failed, corrected, and the replacement attempt passed. The migration
smoke initially exposed a typed seed INSERT error; the numeric literal correction
was settled as passed.

### WU2 — RAG + persistence

- [x] 2.1 Add the RAG product price lookup contract and preserve
  `codigo_proveedor` on product results.
- [x] 2.2 Persist source-aware draft orders with frozen line snapshots and
  local-only stock reservations.
- [x] 2.3 Add HTTP boundary coverage for successful, missing, transport, and
  server-error RAG price lookups.
- [x] 2.4 Add persistence coverage for local reservations, RAG catalog
  independence, stored totals, and pending conversion.

### WU3 — Finalize + routing

- [x] 3.1 Add finalize parsing and supplier-code propagation.
- [x] 3.2 Add customer finalization, pricing, persistence, and draft lifecycle.
- [x] 3.3 Route draft-carrying state back to `CUSTOMER`.
- [x] 3.4 Add finalize, ambiguity, creation, routing, and add-intent tests.

The authorized fixture sequence correction was retained unchanged. The finalize
integration suite passed all four scenarios after the correction.

### WU4 — Backoffice Customer Orders

- [x] 4.1 Add customer-order, exchange-rate, default-margin, and recompute operations.
- [x] 4.2 Add the seventh backoffice tab and maintenance controls.
- [x] 4.3 Add seven-tab and backoffice maintenance tests.
- [x] 4.4 Block pending-conversion orders at approval.

The Customer Orders tab lists ARS totals and frozen lines, makes ARS read-only,
recomputes pending orders after non-ARS rate saves, and persists the seeded
default margin setting. `PendingConversionError` is raised before registration
side effects.

## Work Unit Evidence

| Unit | Focused test command and exact result | Runtime harness command and exact result | Rollback boundary |
|------|----------------------------------------|------------------------------------------|-------------------|
| WU1 DB + pricing | `.venv/bin/python -m pytest tests/test_order_pricing.py tests/test_pricing.py` — **19 passed** in 0.03s | `.venv/bin/python -m pytest tests/test_db_models.py` — **18 passed**, 2 warnings in 0.57s; migration upgrade and ORM schema checks completed | Revert `90881fe`; migration `downgrade()` removes the added columns/tables, and the pricing module/test cleanup can be removed without changing Case A behavior |
| WU2 RAG + persistence | `.venv/bin/python -m pytest tests/test_rag.py tests/test_draft_order.py` — **22 passed**, 2 warnings in 0.81s | The same command exercised the `httpx.MockTransport` boundary and disposable Postgres persistence fixture; both completed with no orphaned process | Revert `dde0188`; remove `src/sourcing/draft_order.py` and the WU2 RAG/persistence additions while leaving WU1 pricing and Case A untouched |
| WU3 Finalize + routing | `.venv/bin/python -m pytest tests/test_finalize.py` — **4 passed**, 2 warnings in 3.30s; `.venv/bin/python -m pytest tests/test_customer.py tests/test_product_search.py tests/test_router_sourcing.py` — **56 passed** in 1.01s | The finalize integration command exercised the Postgres-backed customer, order, snapshot, reservation, ambiguity, and minimal-create paths | Revert `eacbd00`; restore the `order_id is None` add-intent gate and remove the finalize parser/routing changes and `tests/test_finalize.py` |
| WU4 Backoffice Customer Orders | `.venv/bin/python -m pytest tests/test_backoffice.py` — **28 passed**, 2 warnings in 7.03s | The same command exercised the live Gradio Blocks construction plus disposable Postgres order, rate, margin, and approval-guard paths | Revert `2eed076`; remove `src/backoffice/customer_orders.py`, the seventh tab, rate recomputation, and `PendingConversionError` wiring |

The repository-mandated documentation hook was satisfied with `make test-docs`,
which generated `docs/escenarios-testeados.md` with **291 scenarios**. The refreshed
generated documentation was included with the verification-test commit because the
repository hook rejects a stale scenario inventory.

### Phase 5 work-unit evidence

| Unit | Focused test command and exact result | Runtime harness command and exact result | Rollback boundary |
|------|----------------------------------------|------------------------------------------|-------------------|
| Phase 5 focused 5.3 + 5.4 | `.venv/bin/python -m pytest tests/test_approval.py tests/test_db_models.py` — **27 passed** in 2.24s, 5 Alembic deprecation warnings | The same command exercised the approval call-count boundary and Alembic downgrade/upgrade plus legacy Case A persistence; exited 0 with no leftover process | Revert `404a1b2`; remove the verification tests and generated scenario-doc update without changing production code |
| Phase 5.2 chat flow | N/A — runtime-only scripted scenario; its assertions are recorded in the harness result | Ephemeral `.venv/bin/python - <<'PY'` harness — **PASS**: local product search → add intent → unknown-customer finalize kept the draft → new customer creation persisted order → `list_customer_orders`/`order_detail` showed it. `localhost:8001` accepted TCP but the RAG query timed out, so the RAG-specific line was skipped with evidence and not claimed as passed | Remove the ephemeral harness evidence only; the production rollback boundary is revert `eacbd00` plus `2eed076` for finalize/backoffice behavior |
| Phase 5.2 pending conversion | N/A — runtime-only scripted scenario; its assertions are recorded in the harness result | Ephemeral `.venv/bin/python - <<'PY'` harness — **PASS**: persisted RAG/USD snapshot had `conversion_pending=True` and NULL totals; saving USD `1000.0000` and recomputing updated 1 order, cleared the flag, and filled subtotal/total as `24000.00` ARS | Revert `2eed076` and remove the customer-order rate/recompute operations; no unrelated WU1–WU3 behavior is required to remove the backoffice conversion path |
| Phase 5 final suite | `.venv/bin/python -m pytest tests/` — **569 passed** in 18.42s, 5 Alembic deprecation warnings | The same complete suite exercised the final committed tree; exited 0 and pytest teardown removed the disposable test schema | Revert `404a1b2` for the final verification artifacts; production rollback remains the WU1–WU4 boundaries above |

## Phase 5 Evidence

- [x] 5.1 `.venv/bin/python -m pytest tests/` — **569 passed** in 18.42s, 5 Alembic deprecation warnings after the final verification commit.
- [x] 5.2 The bounded chat harness passed the local path: product search, add intent, new-customer finalize, persisted order, and Customer Orders visibility. RAG status was **TCP reachable but query timed out**; the RAG-specific line was skipped with that evidence. The separate pending-conversion harness passed with a persisted RAG/USD snapshot, USD rate save, recompute, cleared flag, and `24000.00` ARS totals.
- [x] 5.3 `test_sheets_append_belongs_to_approval_not_draft_persistence` passed: draft persistence made zero `SheetsWriter.append_order_row` calls, while `register_approved_order` called it once and returned `APPENDED`.
- [x] 5.4 `test_customer_order_migration_round_trips_and_keeps_case_a_persistable` passed: downgrade to `46bdbdc4a575`, schema absence checks, upgrade to `7d2f4a1e8b90`, and legacy Case A persistence all succeeded.

## Runtime Authority Notes

The earlier maintainer-authorized reset released the WU3 objective. The historical
auxiliary pending-conversion dispatch attempt failed only because a test read
`order.order_id` after an injected session had closed; that correction was reverted
and was not part of this final batch. For this batch, the focused, chat, pending-
conversion, and final-suite objectives were each acquired and settled as passed.
One documentation-hook settlement attempt supplied a malformed evidence revision
and was rejected before settlement; it was immediately retried with a valid
64-hex revision and settled as passed. No runtime gate was bypassed.

## Pending Tasks

### Phase 5 — Verification

- [x] 5.1 Run the complete test suite.
- [x] 5.2 Verify the end-to-end chat and backoffice conversion flow.
- [x] 5.3 Verify Sheets synchronization occurs only at approval.
- [x] 5.4 Verify migration downgrade round-trip and legacy Case A persistence.

## Deviations and Risks

- The native dispatcher reports the artifact store as `openspec`; this progress
  file and `tasks.md` are the authoritative apply-phase file locators.
- `default_margin_pct` is seeded as the design-specified string value `20`; the
  pricing layer accepts percentage points and fraction inputs and normalizes them
  before applying the margin.
- `git diff --shortstat e261524..HEAD` reports 2,054 insertions and 51 deletions
  (2,105 total changed lines including generated docs). Excluding the generated
  `docs/escenarios-testeados.md` delta leaves 2,083 authored changed lines, which
  is 83 lines above the owner-approved 2,000-line budget; no code-golf reduction
  was attempted.
- Fresh measurement of the current working tree (all authored fix files
  included, generated `docs/escenarios-testeados.md` excluded) reports
  **2,321 insertions / 48 deletions = 2,369 authored changed lines**, above the
  historical exception recorded below; no code-golf reduction or unrelated
  trimming was attempted per apply policy.
- `localhost:8001` accepted the RAG TCP connection during the chat harness, but
  `/api/v1/query` timed out; the local chat path passed and no RAG success was
  claimed. The pending-conversion evidence used a persisted RAG snapshot and did
  not require a live RAG query.
- The repository's scenario-documentation hook initially rejected the staged test
  commit because `docs/escenarios-testeados.md` was stale; `make test-docs` refreshed
  it to 291 scenarios and the same conventional commit then passed all hooks.
- The owner-approved `size:exception` remains recorded for the historical
  **2,083 authored implementation lines**; the current working-tree measurement
  (2,369 authored lines, generated docs excluded) exceeds it, and no code-golf
  reduction or unrelated trimming was done.
