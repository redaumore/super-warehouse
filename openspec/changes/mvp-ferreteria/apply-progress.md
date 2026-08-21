# Apply Progress: mvp-ferreteria — PR3 (Phase 2 remaining)

**Change**: mvp-ferreteria
**PR**: `feat/mvp-ferreteria-pr3` (single local merge, accepted `size:exception` — delivery strategy resolved, no chained PRs)
**Mode**: Standard (strict_tdd: false, pytest runner present)
**Persistence**: hybrid (this file + Engram `sdd/mvp-ferreteria/apply-progress`)
**Batch**: PR3 — tasks 2.2, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11 + Phase 4 test tasks 4.3, 4.4

## Completed Tasks (cumulative)

### PR1 (previous batch — persisted for continuity)
- [x] 1.1–1.7 Phase 1 foundation: pyproject/docker-compose/env/Makefile, ORM models (all entities, `vector(1536)`), session + Alembic, channel ABC + Telegram, config Settings, webhook skeleton + signature + ACK <5s, conftest + RED models tests.

### PR2 (previous batch — persisted for continuity)
- [x] 2.1 pricing engine (pure `compute_base`/`compute_final`, HALF_UP)
- [x] 2.3 customer agent (phone normalize + known/unknown/invalid)
- [x] 2.4 disambiguation agent (pgvector + rapidfuzz hybrid search)
- [x] 2.5 inventory agent (soft-lock, `avail = stock − Σ active unexpired`)

### PR3 (this batch)
- [x] **2.2** `src/agents/perception.py` — mockable `Transcriber`/`VisionAnalyzer` provider interfaces; `transcribe_voice` (clean → usable, noisy → flagged fragments, failure → `TranscriptionError`), `analyze_image` (`VisionError` on failure). Real OpenAI client deferred to 3.2.
- [x] **2.6** `src/agents/sales.py` — `quote_order` (compound discounts via pricing engine), `adjust_line`/`apply_adjustments` (per-line extra discount, `adjustment` = absolute discount amount), `Quote`/`QuoteLine` totals.
- [x] **2.7** `src/agents/dispatch.py` — `Notifier` protocol, `notify_owner` (quote → owner), `parse_decision` (aprobá/rechazá + per-line % adjustments, percent → fraction), `apply_decision` (approve with re-priced order_items / reject releasing reservations; UNKNOWN → `UnknownDecisionError`).
- [x] **2.8** `src/orchestrator/{router,session}.py` — `route_message` (voice/image → Perception; awaiting decision → Dispatch; in-progress → Sales/Disambiguation; fresh → Customer), `ConversationStore` (per-sender context + 30-min TTL), `Orchestrator` (load → route → agent → persist; resumes order after owner wait).
- [x] **2.9** `src/order_lifecycle/state.py` — transitions over `OrderEstado`: `approve_order` (refuses stale via `RequiresRequoteError`, flags `needs_requote`), `reject_order` (releases ACTIVE reservations → RELEASED), `mark_dispatched`, `expire_reservations`, `requires_requote`. Added `Order.customer` relationship so SQLAlchemy orders inserts correctly.
- [x] **2.10** `src/scheduler/sweeper.py` — APScheduler `BackgroundScheduler` job (`reservation-ttl-sweep`); `sweep_expired` marks past-TTL ACTIVE → EXPIRED + sets order `needs_requote`; `_tick` per-tick transaction (commit/rollback). Dep `apscheduler>=3.10,<4` added to pyproject.
- [x] **2.11** RED tests — TTL expiry release (`tests/test_sweeper.py`), reject release (`tests/test_order_lifecycle.py`), expired order cannot be approved (`tests/test_order_lifecycle.py`).

### Phase 4 test tasks delivered in PR3
- [x] **4.3** `test_state_machine`: unit transitions + `needs_requote` — `tests/test_order_lifecycle.py` (fake-session unit tests).
- [x] **4.4** `test_inventory` RED: expiry release, reject release, blocked approval — `tests/test_order_lifecycle.py` + `tests/test_sweeper.py` (Postgres integration, skipif-guarded).

## Files Changed (PR3)

| File | Action | What Was Done |
|------|--------|---------------|
| `src/agents/perception.py` | Created | Mockable STT/vision provider interfaces + failure semantics |
| `src/agents/sales.py` | Created | Quote + per-line adjustments on the pure pricing engine |
| `src/agents/dispatch.py` | Created | Owner notify + approve/reject decision handling |
| `src/agents/__init__.py` | Modified | Docstring updated (agents now delivered) |
| `src/orchestrator/__init__.py` | Created | Package exports |
| `src/orchestrator/session.py` | Created | ConversationState + TTL ConversationStore |
| `src/orchestrator/router.py` | Created | route_message + Orchestrator coordinator |
| `src/order_lifecycle/__init__.py` | Created | Package exports |
| `src/order_lifecycle/state.py` | Created | State transitions, needs_requote, reject release |
| `src/scheduler/__init__.py` | Created | Package exports |
| `src/scheduler/sweeper.py` | Created | APScheduler TTL sweeper |
| `src/db/models.py` | Modified | Added `Order.customer` relationship (FK-ordered inserts) |
| `pyproject.toml` | Modified | Added `apscheduler>=3.10,<4` |
| `tests/test_perception.py` | Created | 9 unit tests (fake providers) |
| `tests/test_sales.py` | Created | 11 unit tests |
| `tests/test_dispatch.py` | Created | 9 unit + 12 integration tests |
| `tests/test_order_lifecycle.py` | Created | 12 unit + 3 integration tests (RED) |
| `tests/test_orchestrator.py` | Created | 17 unit tests |
| `tests/test_sweeper.py` | Created | 3 unit + 3 integration tests (RED) |
| `openspec/changes/mvp-ferreteria/tasks.md` | Modified | Marked 2.2, 2.6–2.11, 4.3, 4.4 `[x]` |
| `openspec/changes/mvp-ferreteria/apply-progress.md` | Created | This record |

## Work Unit Evidence

| Work unit | Focused test command + result | Runtime harness | Rollback boundary |
|---|---|---|---|
| 2.2 perception | `pytest tests/test_perception.py -q` → 9 passed | N/A — provider boundary mocked by design; real OpenAI is 3.2 | Delete `src/agents/perception.py` + `tests/test_perception.py` |
| 2.6 sales | `pytest tests/test_sales.py -q` → 11 passed | N/A — pure computation, no runtime boundary | Delete `src/agents/sales.py` + `tests/test_sales.py` |
| 2.9 state machine | `pytest tests/test_order_lifecycle.py -q` → 15 passed (12 unit + 3 Postgres) | Postgres integration: reject→stock back to 10; approve on expired→`RequiresRequoteError` | Delete `src/order_lifecycle/` + revert `src/db/models.py` relationship |
| 2.7 dispatch | `pytest tests/test_dispatch.py -q` → 21 passed (9 unit + 12 Postgres) | Postgres: approve w/ 5% adjustment → item 100→95.00, adjustment 5.00; reject → reservations RELEASED | Delete `src/agents/dispatch.py` + `tests/test_dispatch.py` |
| 2.8 orchestrator | `pytest tests/test_orchestrator.py -q` → 17 passed | N/A — in-memory store; webhook wiring is Phase 3 (3.4) | Delete `src/orchestrator/` + `tests/test_orchestrator.py` |
| 2.10 sweeper | `pytest tests/test_sweeper.py -q` → 6 passed (3 unit + 3 Postgres) | APScheduler job registered; `_tick` commit/rollback verified with fake factory | Delete `src/scheduler/` + revert `pyproject.toml` dep |
| 2.11 RED tests | Full: `pytest -q` → 139 passed (was 63 at PR3 start; +76) | Postgres integration: TTL expiry → EXPIRED + needs_requote + stock 10; reject release; stale approve refused | Same as 2.9/2.10 boundaries |

Coverage (new modules): `--cov` over perception/sales/dispatch/order_lifecycle/orchestrator/scheduler → **365 stmts, 99%** (only missing line: a `logger.info` count>0 branch in `sweeper._tick`, proven by integration test). perception/sales/dispatch/order_lifecycle/orchestrator at 100%.

## Deviations from Design

1. **`Order.customer` relationship added to `src/db/models.py`** — the design's data model had no relationship; without it SQLAlchemy cannot order inserts (clientes before orders) in one flush, breaking order creation. Schema unchanged; ORM metadata only. Flagged to verify.
2. **`DecisionAction` percent semantics** — dispatch parses "5%" into fraction `0.05` because the pricing engine consumes fractions; the `order_items.adjustment` column stores the absolute amount given.
3. **Router awaiting-decision rule** — every reply while `awaiting_decision` routes to Dispatch (not only parseable decisions), so the owner is asked to clarify instead of the reply falling into disambiguation.
4. **Sweeper TTL check at read time** — confirmed the design's "TTL correctness does NOT depend on the sweeper": availability excludes expired ACTIVE reservations immediately; the sweeper makes expiry durable (EXPIRED + needs_requote). RED test asserts both.

## Issues Found

- `re` word boundaries (`\b`) do not match accented Spanish stems (`aprobá`, `rechazá`) — fixed by boundary-less stem alternation + `no(?!\w)`.
- Backdated reservations in integration tests: `available_stock` already excludes them at read time (design intent), so "still locked" assertions were wrong; tests assert read-time exclusion + durable EXPIRED state separately.
- None blocking.

## Remaining Tasks (PR4)

- Phase 3: 3.1 WhatsApp adapter, 3.2 `src/integrations/openai.py` (real Whisper/Vision/embed), 3.3 Sheets, 3.4 `src/orchestrator/approval.py` (APPROVE→convert→Sheets→stock→confirm), 3.5–3.9 Gradio backoffice, barcode decoder, supplier OCR.
- Phase 4 remaining: 4.5 integration search, 4.6 intake ACK <5s async, 4.7 E2E order + ingestion, 4.8 coverage gate `--cov-fail-under=85` (pricing 100% already).
- Phase 5: README, per-Fase flags, docs, ruff+mypy+pin deps.

## Status

7/7 assigned tasks complete (2.2, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11) + Phase 4 4.3/4.4. **139 tests passing** (up from 63). Ready for next batch (PR4).