# Matriz de escenarios del flujo de pedido de cliente

Mapa de escenarios de prueba del flujo completo de un pedido de cliente (quoting → confirmación → sourcing → PO → recepción → remito). Cada fila indica el comportamiento esperado y el test que lo cubre hoy. Sirve como plan de cobertura: lo que aparece como 🔲 es lo que falta implementar.

## Cómo leer la matriz

El flujo no es una lista plana de casos: es la combinación de **4 dimensiones independientes**. Una "cantidad insuficiente" no es un caso aparte — en el diseño actual la línea queda sin reservar en el quoting y recién en la confirmación se clasifica como Caso B (con candidatos) o Caso C (sin candidatos). Eso reduce la matriz real.

| Dimensión | Valores |
|---|---|
| **D1. Disponibilidad por línea** | A: stock suficiente · Insuficiente (parcial) · B: falta, con candidatos · C: falta, sin candidatos |
| **D2. Composición del pedido** | Solo LOCAL · LOCAL + RAG (mixto) · Solo RAG |
| **D3. Orden de compra al proveedor** | No existe (se crea) · OPEN existente (se agrega ítems) · SENT existente · Compartida con otro pedido |
| **D4. Resultado de ingesta de remito** | Resuelto 1:1 · Ambiguo (>1 hits) · SKU nuevo (adopción RAG) · Sin match en el índice (adopción definitiva, ADR 0003) · Embeddings caídos · Proveedor inactivo · Documento ilegible |

La cobertura completa por parejas (pairwise) entre estas dimensiones se alcanza con ~30 escenarios; el producto cartesiano total sería exponencial y no aporta.

## Leyenda

- ✅ Cubierto por test existente.
- 🟡 Cobertura parcial (test existe pero no ejercita el escenario completo).
- 🔲 Sin cobertura a nivel de escenario — faltante.

## Bloque 1 — Quoting y draft

| ID | Escenario | Comportamiento esperado | Test |
|---|---|---|---|
| Q1 | 1 producto con stock suficiente | Draft persiste, línea reservada (soft-lock), disponibilidad baja | 🟡 `test_persist_draft_order_writes_draft_without_reservations` (draft) + `test_reserve_creates_active_reservation_and_locks` (reserva, unit) |
| Q2 | 1 producto sin stock | Línea queda **sin reservar silenciosamente**; clasificación difiere a confirm | ✅ `test_quoting_leaves_no_stock_line_unreserved_and_defers_classification` |
| Q3 | Mixto: 1 con stock + 1 sin stock | Línea con stock reservada, la otra no; todo-o-nada por línea | ✅ `test_quoting_mixed_order_reserves_only_the_in_stock_line` |
| Q4 | Segundo draft para el mismo cliente | Rechazado, el draft existente se conserva | ✅ `test_second_draft_for_same_customer_is_rejected_and_preserved` |
| Q5 | Race de dos sesiones creando draft | Exactamente uno sobrevive | ✅ `test_two_session_draft_race_exactly_one_survives` |

## Bloque 2 — Confirmación y clasificación A/B/C

| ID | Escenario | Comportamiento esperado | Test |
|---|---|---|---|
| C1 | Caso A: pedido LOCAL todo con stock | Reservas convertidas, stock deducido, Sheets, CONFIRMED | ✅ `test_confirm_and_register_converts_deducts_and_confirms` + `test_e2e_owner_order_confirms_and_deducts_stock` |
| C2 | Caso B: producto faltante con candidatos | SourcingNeed persiste + prompt de selección al dueño | ✅ `test_confirm_discovering_case_b_persists_needs_and_returns_selection_prompt`, `test_guided_order_confirm_lists_missing_items_and_suppliers` |
| C3 | Caso C: faltante sin candidatos | Pedido cancelado, reservas liberadas | ✅ `test_confirm_discovering_case_c_cancels_the_order` |
| C4 | Caso A-insuficiente **con** candidatos (cantidad pedida > stock) | Prompt solo por lo faltante; lo cubierto sigue por A | ✅ `test_confirm_mixed_local_short_with_candidates_prompts_only_local` |
| C5 | Caso A-insuficiente **sin** candidatos (con reserva de quote y stock caído) | Línea insuficiente va a Caso C → cancelación total + reserva liberada | ✅ `test_confirm_insufficient_reserved_line_without_candidates_cancels` |
| C6 | Mixto LOCAL con stock + RAG resuelto | Deducción de lo LOCAL + PO automática para RAG | ✅ `test_confirm_mixed_local_stock_and_rag_completes_with_po` |
| C7 | Solo RAG auto-sourcing exitoso | PO creada sin prompt (proveedor resuelto por código) | ✅ `test_confirm_rag_only_order_autosources_without_prompt` |
| C8 | RAG sin resolver, con candidatos | Fallback a clasificación normal → prompt | ✅ `test_confirm_unresolved_rag_falls_back_to_selection_prompt` |
| C9 | RAG sin resolver, sin candidatos | Fallback → Caso C → cancelación | ✅ `test_confirm_unresolved_rag_without_candidates_cancels` |
| C10 | RAG con nombre ambiguo / código de proveedor inactivo | Línea no resuelta (nunca resuelve por nombre ambiguo) | ✅ `test_confirm_ambiguous_rag_business_name_is_unresolved`, `test_confirm_inactive_supplier_code_match_is_unresolved` |
| C11 | Quote vencida (TTL) al confirmar | Rechazo limpio sin efectos, requiere re-cotización | ✅ `test_confirm_on_expired_reservation_refuses_without_side_effects`, `test_confirm_order_with_stale_reservation_raises_requote` |
| C12 | Confirmar dos veces | Transición inválida | ✅ `test_second_confirm_is_an_invalid_transition` |
| C13 | Orden con conversión pendiente | Confirmación bloqueada | ✅ `test_confirm_pending_conversion_order_is_blocked` |

## Bloque 3 — Selección del caso B: PO nueva vs. existente

| ID | Escenario | Comportamiento esperado | Test |
|---|---|---|---|
| S1 | Selección del dueño, sin PO previa | PO nueva OPEN por proveedor; ítems agregados por SKU | ✅ `test_guided_case_b_owner_selection_accumulates_open_po` |
| S2 | Segunda orden del mismo proveedor con PO OPEN existente | Ítems se **fusionan** en la PO existente (no se crea otra) | ✅ `test_second_order_merges_into_existing_open_po` |
| S3 | Faltantes de varios proveedores | Una PO por proveedor | ✅ `test_multiple_suppliers_produce_multiple_pos`, `test_open_or_create_po_splits_by_supplier` |
| S4 | Re-selección antes de ejecución | Need se desvincula de la PO vieja y pasa a la nueva | ✅ `test_reselection_detaches_from_previous_open_po`, `test_guided_case_b_reselection_before_execution_moves_need_between_pos` |
| S5 | Re-selección tras PO enviada (SENT) | Rechazada (`SelectionExecutedError`) | ✅ `test_reselection_after_execution_is_refused` |
| S6 | PO compartida con otro pedido, caso C | Solo libera los ítems de este pedido; la PO sobrevive | ✅ `test_confirm_case_c_releases_only_this_order_from_a_shared_po` |
| S7 | PO queda vacía tras cancelación | La PO vacía se cancela | ✅ `test_confirm_case_c_after_autosourcing_cancels_the_empty_pos`, `test_cancel_confirmed_order_cancels_the_open_po_it_emptied` |
| S8 | Selección de proveedor inactivo | Rechazada | ✅ `test_open_or_create_po_refuses_inactive_supplier` |
| S9 | Selección inválida en chat (número fuera de rango) | Vuelve a pedir | ✅ `test_guided_case_b_invalid_selection_number_asks_again` |

## Bloque 4 — Ciclo de la PO (envío y recepción del proveedor)

| ID | Escenario | Comportamiento esperado | Test |
|---|---|---|---|
| P1 | Enviar PO OPEN | OPEN → SENT | ✅ `test_send_open_po_moves_to_sent` |
| P2 | Recepción parcial | SENT → PARTIALLY_RECEIVED, inventario sube | ✅ `test_receive_partial_sent_po_moves_to_partially_received` |
| P3 | Recepción completa | → FULLY_RECEIVED, inventario sube | ✅ `test_receive_full_sent_po_moves_to_fully_received`, `test_receive_completes_from_partially_received` |
| P4 | Recepción de más de lo pendiente | Rechazada | ✅ `test_receive_over_remaining_quantity_raises` |
| P5 | Recepción de SKU inexistente en la PO | Rechazada | ✅ `test_receive_unknown_sku_raises` |
| P6 | Cancelación en cada estado | OPEN/SENT cancelan; terminal inválido | ✅ `test_cancel_open_po_moves_to_cancelled`, `test_cancel_sent_po_moves_to_cancelled`, `test_cancel_terminal_po_is_invalid` |

## Bloque 5 — Ingesta de remitos

| ID | Escenario | Comportamiento esperado | Test |
|---|---|---|---|
| R1 | SKU existente, resuelto 1:1 | Stock bump + espejo Inventory + `StockAdjustment(receipt_ingestion)` con provenance | ✅ `test_e2e_receipt_flow_writes_stock_with_node_id_provenance` |
| R2 | Línea sin match en el índice RAG (0 exactos, 0 híbridos) | Se adopta como producto **DEFINITIVO** al confirmar (**ADR 0003**): sin flag provisional ni pantalla de revisión, con `origen={"remito": {...}}` y embedding best-effort (un fallo del embedder la adopta sin vector). Si el SKU calculado ya existe en el catálogo local, se bumpea el stock del existente en lugar de duplicarlo | ✅ `test_e2e_unmatched_line_confirms_and_adopts_definitive_product` |
| R3 | Línea ambigua (>1 hits) | Queda pendiente de asignación manual; luego adopta | ✅ `test_e2e_manual_assignment_resolves_pending_and_adopts`, `test_e2e_manual_search_and_assign_fixes_pending_line` |
| R4 | RAG caído durante la ingesta | Error honesto al usuario, nada escrito | ✅ `test_e2e_rag_down_shows_honest_error_and_writes_nothing` |
| R5 | Embedding falla / dimensión inválida en adopción | Rollback total (`EmbeddingUnavailableError`); el bump ya preparado de otra línea también se descarta | ✅ `test_e2e_embedding_failure_rolls_back_full_ingestion`, `test_e2e_wrong_dimension_embedding_rolls_back_full_ingestion` |
| R6 | Proveedor inactivo al ingestar | Guard bloquea (`ensure_active_supplier`) en parse y confirm: mensaje `Error: supplier N is INACTIVO`, cero escrituras | ✅ `test_e2e_inactive_supplier_blocks_ingestion_and_writes_nothing` |
| R7 | Documento ilegible (sin líneas utilizables) | En el flujo RAG no se levanta `IllegibleDocumentError`: `_ingest_parse` responde con mensaje honesto y cero escrituras (la excepción vive en el límite OCR, cubierta en `test_ocr.py`) | ✅ `test_e2e_document_without_usable_lines_writes_nothing` |
| R8 | Remito llega **sin PO** o con PO en OPEN (no SENT) | Semántica válida por decisión (**ADR 0002**): la ingesta es PO-agnóstica — el remito es evidencia física, la PO es el acuerdo comercial. Bumpea `Inventory` + `StockAdjustment(receipt_ingestion)` siempre, sin tocar la PO ni su máquina de estados | ✅ `test_e2e_receipt_without_po_bumps_stock_with_full_audit`, `test_e2e_receipt_with_open_po_ingests_and_leaves_po_untouched` |

## Bloque 6 — Ciclo de vida, cancelación e invariantes

| ID | Escenario | Comportamiento esperado | Test |
|---|---|---|---|
| L1 | Rechazo del dueño | Cancel + liberación de reservas | ✅ `test_e2e_owner_reject_cancels_order_and_releases_reservation`, `test_reject_releases_auto_sourced_needs_and_cancels_the_empty_po` |
| L2 | Cancelación tardía (post-deducción) | Stock restaurado con auditoría | ✅ `test_late_cancel_restores_deducted_stock_with_audit` |
| L3 | Reservas expiradas marcan la orden | Requiere re-cotización | ✅ `test_expire_reservations_flags_order_when_rows_expired` |
| L4 | Modificación de orden confirmada | Stock restaurado sin doble conteo | ✅ `test_modify_restores_deducted_stock_without_double_count` |
| L5 | Race de reservas: dos dueños reservan el mismo stock | Al menos uno debe fallar limpio | ✅ `test_two_session_reserve_race_at_most_one_succeeds` — **Arreglado**: `reserve_stock` ahora toma `SELECT ... FOR UPDATE` sobre la fila de `Inventory` antes de leer disponibilidad e insertar, serializando las reservas concurrentes por SKU (el TOCTOU que doble-reservaba está cerrado, sin cambio de esquema). El test ya no es `xfail`: T2 o bloquea sobre el lock de T1 (cancelado por `lock_timeout`) o lee la reserva commiteada y es rechazado limpio con `InsufficientStockError`; a lo sumo una reserva activa del lote disputado sobrevive |
| L6 | Invariante: stock nunca negativo (cantidades arbitrarias) | `Inventory.quantity_on_hand − reservas activas ≥ 0` bajo cualquier cantidad | ✅ `test_arbitrary_reservation_sequences_preserve_stock_invariants` (property-based con `hypothesis`, DB real: cada ejemplo aplica una secuencia arbitraria de pedidos y verifica que el stock en mano no cambie, la disponibilidad nunca sea negativa y una reserva rechazada no altere el estado) |
| L7 | Invariante: `catalogo.stock_disponible` vs `Inventory` | Resuelto (**ADR 0002**): `Inventory.quantity_on_hand` es la única fuente de stock en mano; el contador legacy `catalogo.stock_disponible` fue **retirado** (columna eliminada por migración `934d98c5d1b2`, todos los dual-writes ahora Inventory-only, la UI lee `Inventory`). El desync es imposible por construcción | ✅ `test_e2e_owner_order_confirms_and_deducts_stock`, `test_confirm_and_register_converts_deducts_and_confirms` (ambos asertan `not hasattr(Catalogo, "stock_disponible")`) |

## Resumen de faltantes

No quedan faltantes: los 9 escenarios de ingesta (R1–R8) y los 7 de ciclo de vida/invariantes (L1–L7) están implementados y testeados. R8 y L7 se cerraron como decisión semántica en **ADR 0002** (`docs/adr/0002-receipt-ingestion-po-agnostic-inventory-single-source.md`) y sus tests la documentan.

Deuda aceptada registrada en el ADR (no cubierta por la matriz): riesgo de doble-bump si el mismo remito se procesa por ambas vías (ingesta + receive de PO); mitigación procedimental "un remito, un camino".

## Checklist

- [ ] Todo escenario ✅ mantiene su test verde en CI (Postgres de prueba corriendo).
- [ ] Los faltantes nuevos siguen la convención: docstring en español de una línea + `make test-docs` actualiza `escenarios-testeados.md`.
- [x] R8 y L7 se resuelven como decisión (ADR) antes de escribir su test.
- [ ] Si se adopta `hypothesis` y/o `factory_boy`, L6 y los faltantes del bloque 1 se reimplementan sobre la fábrica compartida.

## Próximo paso

La matriz está completa: R1–R8 y L1–L7 cerrados. Q2, Q3, C5 (prioridad alta) se cerraron antes, R6 con su guard, L5 arreglado con `FOR UPDATE` y L6 con property-based testing. El cierre semántico de R8/L7 quedó registrado en **ADR 0002**, con los tests e2e correspondientes y la columna `catalogo.stock_disponible` retirada (migración `934d98c5d1b2`). Siguiente frente natural: los faltantes de prioridad baja del resto de bloques, o atacar la deuda registrada en el ADR (convención de ledger unificada entre las dos vías de recepción).
