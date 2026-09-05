# Escenarios testeados

Documento generado automáticamente desde los docstrings de los tests. No lo edites a mano: si un escenario cambia, actualizá la primera línea del docstring del test y volvé a correr `make test-docs`.

**Total de escenarios:** 355, agrupados en 30 dominios.

> Cada ítem lista el comportamiento que se valida en lenguaje natural, seguido (entre paréntesis) del nombre técnico del test.

## Índice

- [Motor de precios](#motor-de-precios) — 6
- [Cotización y ventas](#cotización-y-ventas) — 11
- [Stock e inventario](#stock-e-inventario) — 13
- [Despacho y aprobación del dueño](#despacho-y-aprobación-del-dueño) — 12
- [Registro de aprobaciones](#registro-de-aprobaciones) — 24
- [Orquestador y enrutamiento](#orquestador-y-enrutamiento) — 26
- [Pipeline de orquestación (walking skeleton)](#pipeline-de-orquestación-walking-skeleton) — 6
- [Agente Customer (respondedor conversacional)](#agente-customer-respondedor-conversacional) — 32
- [Ciclo de vida del pedido](#ciclo-de-vida-del-pedido) — 28
- [Integración con RAG de catálogo de proveedores](#integración-con-rag-de-catálogo-de-proveedores) — 16
- [Búsqueda de producto (precedencia local → RAG)](#búsqueda-de-producto-precedencia-local-rag) — 12
- [Percepción (voz e imagen)](#percepción-voz-e-imagen) — 9
- [Integración con OpenAI](#integración-con-openai) — 9
- [Búsqueda en catálogo](#búsqueda-en-catálogo) — 9
- [Vencimiento de reservas (scheduler)](#vencimiento-de-reservas-scheduler) — 6
- [Canales de entrada (Telegram/WhatsApp)](#canales-de-entrada-telegram-whatsapp) — 4
- [Canal WhatsApp Cloud API](#canal-whatsapp-cloud-api) — 11
- [Webhook de entrada](#webhook-de-entrada) — 6
- [Intake y trabajo en background](#intake-y-trabajo-en-background) — 3
- [Modelo de datos y migraciones](#modelo-de-datos-y-migraciones) — 23
- [Teléfonos y clientes](#teléfonos-y-clientes) — 3
- [Registro en Google Sheets](#registro-en-google-sheets) — 5
- [Códigos de barras](#códigos-de-barras) — 11
- [OCR de documentos de proveedor](#ocr-de-documentos-de-proveedor) — 11
- [Backoffice (catálogo, clientes, monitor, ingesta)](#backoffice-catálogo-clientes-monitor-ingesta) — 36
- [Feature flags por fase](#feature-flags-por-fase) — 7
- [E2E: pedido completo](#e2e-pedido-completo) — 4
- [E2E: ingesta de documentos](#e2e-ingesta-de-documentos) — 4
- [Observabilidad y logs por sesión](#observabilidad-y-logs-por-sesión) — 7
- [Trazabilidad de sesión en el pipeline](#trazabilidad-de-sesión-en-el-pipeline) — 1

## Motor de precios

- El precio base = costo × (1 + margen), redondeado HALF_UP. _(`test_compute_base`)_
- El precio final = base × (1 − descuento lista) × (1 − descuento particular). _(`test_compute_final`)_
- Los descuentos componen multiplicativamente, nunca se suman. _(`test_discounts_compound_multiplicatively_not_additively`)_
- El precio final sigue la fórmula de la spec: lista y luego particular. _(`test_final_matches_spec_formula_list_then_particular`)_
- El precio base redondea HALF_UP al centavo. _(`test_base_price_rounds_half_up_to_cent`)_
- El precio final redondea HALF_UP al centavo. _(`test_final_price_rounds_half_up_to_cent`)_

## Cotización y ventas

- La cotización aplica descuentos compuestos (nunca sumados). _(`test_quote_applies_compound_discounts`)_
- Sin descuentos, la cotización usa el precio base. _(`test_quote_without_discounts_prices_at_base`)_
- El total acumula los subtotales por línea (cantidad × precio). _(`test_quote_total_accumulates_line_totals`)_
- La cotización redondea HALF_UP al centavo. _(`test_quote_rounds_half_up_to_cents`)_
- Un ajuste aplica descuento extra solo a la línea indicada. _(`test_adjust_line_applies_extra_discount_to_one_line_only`)_
- Ajustar un SKU desconocido lanza error. _(`test_adjust_line_unknown_sku_raises`)_
- Ajustar no muta la cotización original (es inmutable). _(`test_adjust_line_keeps_original_quote_immutable`)_
- Se pueden aplicar varios ajustes por línea a la vez. _(`test_apply_adjustments_multi_line`)_
- Un ajuste a un destino desconocido lanza error. _(`test_apply_adjustments_unknown_target_raises`)_
- Pedir la línea de un SKU desconocido lanza error. _(`test_quote_line_for_unknown_sku_raises`)_
- Pedir la línea de un SKU conocido la devuelve. _(`test_quote_line_for_known_sku_returns_line`)_

## Stock e inventario

- Sin reservas, el stock disponible es todo el stock en mano. _(`test_available_equals_stock_without_reservations`)_
- Una reserva activa reduce la disponibilidad. _(`test_active_reservation_reduces_availability`)_
- Varias reservas activas se acumulan al descontar disponibilidad. _(`test_multiple_active_reservations_accumulate`)_
- Las reservas no activas (convertidas, liberadas, expiradas) no bloquean stock. _(`test_non_active_reservations_do_not_lock_stock`)_
- Una reserva ACTIVE vencida por TTL se excluye al leer la disponibilidad. _(`test_expired_ttl_reservation_does_not_lock_stock`)_
- Una reserva vigente todavía bloquea stock. _(`test_unexpired_reservation_still_locks_stock`)_
- Consultar un SKU desconocido devuelve 0 (nunca KeyError). _(`test_unknown_sku_returns_zero`)_
- El seed copia stock_disponible del catálogo a Inventory.quantity_on_hand. _(`test_seed_inventory_backfills_from_catalogo`)_
- Volver a sembrar no duplica filas ni pisa valores existentes. _(`test_seed_inventory_is_idempotent`)_
- Un SKU sin fila en Inventory se trata como no disponible. _(`test_missing_inventory_row_means_zero_on_hand`)_
- Reservar crea una reserva activa con el TTL configurado y bloquea stock. _(`test_reserve_creates_active_reservation_and_locks`)_
- Reservar más de lo disponible se rechaza sin bloquear de más. _(`test_reserve_beyond_available_stock_is_refused`)_
- Reservar una cantidad no positiva se rechaza. _(`test_reserve_rejects_non_positive_quantity`)_

## Despacho y aprobación del dueño

- El mensaje de cotización menciona las líneas y el total. _(`test_format_quote_message_mentions_lines_and_total`)_
- La referencia 'pedido #N' se extrae como número de pedido. _(`test_parse_order_reference`)_
  - aprobá el pedido #3
  - aprobá el pedido#3
  - aprobá # 42
  - aprobá
  - rechazá el pedido
  - (vacío)
  - aprobá el pedido #3 y el #7
- El texto del dueño se interpreta como confirmar, cancelar o desconocido. _(`test_parse_decision_actions`)_
  - sí, aprobá
  - aprobá
  - dale
  - ok, dale para adelante
  - no, rechazá
  - rechazá el pedido
  - no
  - hablamos mañana
  - (vacío)
- Aprobar con descuento extra por línea se interpreta como confirm + ajuste. _(`test_parse_decision_with_adjustment`)_
- Se aceptan porcentajes decimales en el ajuste. _(`test_parse_decision_accepts_decimal_percent`)_
- Un rechazo ignora cualquier mención de ajuste. _(`test_parse_decision_reject_ignores_adjustment_mention`)_
- Confirmar con ajuste reprecifica la línea afectada. _(`test_apply_approve_with_adjustment_reprises_line`)_
- Confirmar sin cambios conserva los precios cotizados. _(`test_apply_plain_approve_keeps_prices`)_
- Rechazar cancela el pedido: reservas liberadas y stock disponible. _(`test_apply_reject_cancels_order_and_releases_reservations`)_
- Aplicar una decisión desconocida lanza error. _(`test_apply_unknown_decision_raises`)_
- Un ajuste que nombra un producto fuera de la cotización no se puede aplicar. _(`test_apply_adjustment_no_matching_quote_line_raises`)_
- Sin cotización, un SKU desconocido en el ajuste se rechaza. _(`test_apply_adjustment_sku_not_in_order_raises`)_

## Registro de aprobaciones

- El total del pedido suma precio final por cantidad, redondeado a centavos. _(`test_order_total_sums_final_price_times_quantity`)_
- Una línea ajustada aporta su precio final rebajado al total. _(`test_order_total_with_adjusted_line`)_
- El resumen de ítems lista cantidad por SKU separado por punto y coma. _(`test_build_items_summary_lists_each_line`)_
- Sheets append is skipped at draft save and called once during confirm. _(`test_sheets_append_belongs_to_confirm_not_draft_persistence`)_
- Confirmar registra: convierte reservas, descuenta stock, agrega a Sheets y confirma. _(`test_confirm_and_register_converts_deducts_and_confirms`)_
- Confirmar una reserva vencida exige recotizar y no produce efectos laterales. _(`test_confirm_on_expired_reservation_refuses_without_side_effects`)_
- Confirmar dos veces es una transición inválida (idempotencia del ceremonia). _(`test_second_confirm_is_an_invalid_transition`)_
- La cuarentena de Sheets NO revierte: el pedido queda Confirmado y se informa. _(`test_sheets_quarantine_is_tolerated_and_order_stays_confirmed`)_
- Confirmar un pedido con precios pendientes de conversión se bloquea. _(`test_confirm_pending_conversion_order_is_blocked`)_
- Classify at confirm: stock que cayó sin supplier cancela el pedido (Case C). _(`test_confirm_discovering_case_c_cancels_the_order`)_
- Case C con códigos de proveedor sin mapear avisa al dueño y queda logueado. _(`test_confirm_case_c_with_unmapped_codes_notifies_the_owner`)_
- Sin códigos sin mapear, la respuesta Case C queda sin la nota adicional. _(`test_confirm_case_c_without_unmapped_codes_keeps_plain_reply`)_
- Classify at confirm: stock que cayó con suppliers devuelve la selección (Case B). _(`test_confirm_discovering_case_b_persists_needs_and_returns_selection_prompt`)_
- RAG-only: sin prompt de selección; needs con supplier y una OC por supplier. _(`test_confirm_rag_only_order_autosources_without_prompt`)_
- Mixto: LOCAL en stock + RAG → Case A completo con descuento, Sheets y OC. _(`test_confirm_mixed_local_stock_and_rag_completes_with_po`)_
- Mixto Case B: el prompt lista SOLO el LOCAL faltante; el RAG ya está auto-sourced. _(`test_confirm_mixed_local_short_with_candidates_prompts_only_local`)_
- RAG sin supplier resoluble + candidates → entra al prompt como los LOCAL. _(`test_confirm_unresolved_rag_falls_back_to_selection_prompt`)_
- RAG sin supplier ni candidates → Case C: cancelación + nota de ingesta. _(`test_confirm_unresolved_rag_without_candidates_cancels`)_
- Dos suppliers ACTIVO con el mismo business_name → la línea queda sin resolver. _(`test_confirm_ambiguous_rag_business_name_is_unresolved`)_
- Un código de proveedor INACTIVO no resuelve la línea RAG (nunca auto-source). _(`test_confirm_inactive_supplier_code_match_is_unresolved`)_
- Sin código, un business_name único ACTIVO resuelve la línea RAG. _(`test_confirm_rag_line_resolves_by_unique_business_name`)_
- Case C tras auto-sourcing: los OCs vacíos se cancelan y el pedido lo informa. _(`test_confirm_case_c_after_autosourcing_cancels_the_empty_pos`)_
- OC compartida: la cancelación de un pedido NO toca lo del otro pedido. _(`test_confirm_case_c_releases_only_this_order_from_a_shared_po`)_
- Necesidad ligada a una OC SENT: release no toca link ni cantidades. _(`test_release_order_needs_leaves_an_executed_po_untouched`)_

## Orquestador y enrutamiento

- Una nota de voz se enruta a Percepción (transcripción). _(`test_voice_note_routes_to_perception_stt`)_
- Una imagen (foto de remito/código) se enruta a Percepción (visión). _(`test_image_routes_to_perception_vision`)_
- Un texto nuevo de cliente se enruta a Customer. _(`test_fresh_text_routes_to_customer`)_
- La aprobación del dueño se enruta a Despacho reanudando el pedido. _(`test_owner_approval_routes_to_dispatch_resuming_order`)_
- El rechazo del dueño se enruta a Despacho. _(`test_owner_rejection_routes_to_dispatch`)_
- Un paso del flujo guiado se enruta al agente GUIDED. _(`test_guided_flow_step_routes_to_guided_agent`)_
- El comando 'sacá X' se enruta a Customer aunque haya un pedido en curso. _(`test_remove_product_command_routes_to_customer_with_order_context`)_
- Una respuesta ambigua mientras se espera sigue en la conversación del dueño. _(`test_non_decision_reply_while_awaiting_goes_to_dispatch_menu`)_
- Un pedido en curso con ítems se enruta a Ventas. _(`test_in_progress_order_with_items_routes_to_sales`)_
- Un pedido en curso sin ítems se enruta a Desambiguación. _(`test_in_progress_order_without_items_routes_to_disambiguation`)_
- Un mensaje sin texto ni media se enruta a Customer. _(`test_textless_medialess_message_routes_to_customer`)_
- El almacén de conversación conserva el contexto entre pasos. _(`test_store_preserves_context_between_steps`)_
- El almacén descarta el contexto vencido por TTL. _(`test_store_drops_expired_context`)_
- El almacén conserva el contexto reciente. _(`test_store_keeps_fresh_context`)_
- Eliminar el contexto lo borra del almacén. _(`test_store_drop_removes_context`)_
- Actualizar devuelve un estado nuevo y refresca el reloj. _(`test_with_updates_returns_new_state_and_touches_clock`)_
- El orquestador enruta y persiste el contexto. _(`test_orchestrator_routes_and_persists_context`)_
- Tras la espera del dueño, su respuesta reanuda el mismo pedido. _(`test_orchestrator_resumes_order_after_owner_wait`)_
- Registrar un agente enlaza su handler. _(`test_orchestrator_register_binds_handler`)_
- La respuesta que produce un agente viaja en el resultado del turno. _(`test_orchestrator_surfaces_agent_reply`)_
- El gatillo "hola bob" descarta el estado previo y arranca el flujo guiado. _(`test_session_reset_drops_previous_state_and_starts_guided_flow`)_
- El reset borra awaiting_decision y draft: el próximo texto va al flujo guiado. _(`test_session_reset_clears_pending_decision_and_draft_for_next_turn`)_
- Mayúsculas y puntuación final no impiden el reset. _(`test_session_reset_variants_match`)_
  - Hola Bob!
- Una oración que contiene las palabras no resetea: sigue el flujo normal. _(`test_sentence_containing_trigger_words_does_not_reset`)_
  - decile hola a Bob
  - hola bob, cómo va todo
- Una nota de voz nunca dispara el reset, aunque su texto sea el gatillo. _(`test_voice_message_does_not_trigger_session_reset`)_
- El matcher solo acepta el mensaje completo y exacto del gatillo. _(`test_is_session_reset_anchored_whole_message`)_
  - hola bob
  - Hola Bob!
  - (vacío)
  - decile hola a Bob
  - hola bob, cómo va todo

## Pipeline de orquestación (walking skeleton)

- El orquestador de la pipeline enlaza los seis agentes. _(`test_build_orchestrator_registers_all_six_agents`)_
- Un mensaje de Telegram nuevo es respondido por el agente Customer a través del responder LLM. _(`test_handle_inbound_routes_persists_and_replies`)_
- Un segundo mensaje del mismo remitente continúa la conversación con el responder LLM. _(`test_second_message_resumes_context`)_
- Una nota de voz se enruta a Percepción con una respuesta específica. _(`test_voice_routes_to_perception_reply`)_
- Un canal sin adaptador no rompe la pipeline: descarta la respuesta. _(`test_unknown_channel_drops_reply_without_crash`)_
- Un resultado RAG del searcher llega al responder como nota de catálogo de proveedores. _(`test_rag_fallback_result_reaches_responder_as_source_note`)_

## Agente Customer (respondedor conversacional)

- Un mensaje nuevo le llega al responder con el system prompt y el turno del usuario. _(`test_fresh_text_message_goes_to_responder_with_system_and_user`)_
- Una conversación en curso le pasa el historial completo al responder y agrega el nuevo par. _(`test_continuing_conversation_sends_history_and_appends_new_pair`)_
- Sin clave de API el responder falla al saludo y el historial igual registra el turno. _(`test_unconfigured_responder_falls_back_to_greeting_and_logs_turns`)_
- Un mensaje sin texto saluda sin consultar al responder ni registrar turno de usuario. _(`test_textless_message_greets_without_calling_responder`)_
- El handler respeta el fallback y el system prompt personalizados que recibe. _(`test_build_handler_uses_custom_fallback_and_system_prompt`)_
- Sin OPENAI_API_KEY, el responder OpenAI lanza ResponderNotConfigured. _(`test_openai_responder_raises_not_configured_without_key`)_
- El responder OpenAI mapea roles y contenido y devuelve el texto del modelo. _(`test_openai_responder_maps_messages_and_returns_model_text`)_
- Un modelo que no produce texto dispara ResponderError. _(`test_openai_responder_raises_when_model_returns_empty_reply`)_
- Un resultado NONE inyecta la nota de no encontrado y el historial no la guarda. _(`test_product_query_with_none_result_injects_not_found_note`)_
- Un resultado LOCAL lista nombre oficial y SKU de cada producto bajo own stock. _(`test_local_candidates_become_note_listing_names_and_skus`)_
- Un resultado RAG se numera, ordena por precio ascendente e incluye campos y footer. _(`test_rag_results_note_numbered_cheapest_first_with_fields_and_footer`)_
- La nota RAG nunca muestra el codigo crudo con doble prefijo (higiene de SKU). _(`test_rag_note_never_leaks_raw_double_prefix_codigo`)_
- El intent de alta conserva el SKU normalizado del producto RAG en el draft. _(`test_add_intent_preserves_normalized_rag_sku_in_draft`)_
- La nota NONE sugiere sinónimos/reformulación y no afirma estado de stock. _(`test_refusal_none_note_suggests_reformulation_without_stock_claim`)_
- La nota ERROR dice que los catálogos no pudieron consultarse, sin stock claim. _(`test_error_note_states_catalogs_unavailable_without_stock_claim`)_
- Un draft mixto (local + RAG) renderiza local primero, numeración global y etiquetado. _(`test_dual_source_note_lists_local_first_labeled`)_
- Un error de base de datos omite la nota y el responder igual contesta. _(`test_searcher_database_error_skips_note_and_keeps_reply`)_
- Sin searcher, la lista de mensajes mantiene la forma del slice 1: system + historial + usuario. _(`test_handler_without_searcher_keeps_slice1_message_shape`)_
- En una conversación en curso, la nota va después del historial y justo antes del último turno del usuario. _(`test_note_lands_after_history_and_before_latest_user_turn`)_
- 'agregalo' con pedido abierto agrega el producto al draft sin llamar al LLM. _(`test_add_intent_with_open_order_appends_draft_and_clears_options`)_
- 'sumá 5 de eso' agrega 5 unidades del último producto mostrado al draft. _(`test_add_intent_with_quantity_appends_draft_with_qty`)_
- 'el 2' selecciona el segundo resultado mostrado. _(`test_add_intent_numbered_reference_picks_displayed_result`)_
- An add intent starts the draft even when no persisted order exists yet. _(`test_add_intent_without_open_order_starts_draft`)_
- 'quiero 2' after a displayed product adds 2 of it with the finalize hint, no LLM. _(`test_bare_quantity_after_displayed_product_adds_qty_with_finalize_hint`)_
- Un alta de un producto RAG con precio muestra el precio en la respuesta. _(`test_add_intent_reply_shows_rag_price_currency_and_unit`)_
- Un alta de un producto local sin precio no muestra ningún precio. _(`test_add_intent_reply_omits_price_for_local_entry_without_price`)_
- 'sacá X' quita la línea del draft en memoria sin llamar al LLM. _(`test_remove_command_removes_in_memory_draft_line`)_
- Un objetivo que no está en el draft informa y no modifica nada. _(`test_remove_command_unknown_target_reports_without_touching_draft`)_
- Sin opciones mostradas, la frase de alta no es intent y sigue el camino LLM. _(`test_add_phrase_without_options_goes_to_llm`)_
- Un query con resultados deja product_options listas para la referencia del próximo turno. _(`test_query_updates_product_options_for_next_turn`)_
- 'agregale 2' after a displayed product adds 2 of it without calling the LLM. _(`test_verb_quantity_add_after_displayed_product_adds_qty`)_
- A turn whose search shows nothing keeps product_options, so a later bare '2' still adds. _(`test_turn_without_results_keeps_last_displayed_product_anchor`)_

## Ciclo de vida del pedido

- Confirmar un pedido Draft lo mueve a Confirmado. _(`test_confirm_draft_moves_to_confirmed`)_
- El flag needs_requote bloquea la confirmación silenciosa. _(`test_confirm_flagged_order_raises_requote`)_
- Un pedido con reserva vencida no se confirma en silencio: exige recotizar. _(`test_confirm_order_with_stale_reservation_raises_requote`)_
- Confirmar un pedido que no está en Draft es una transición inválida. _(`test_confirm_non_draft_order_is_invalid`)_
- Start picking solo es válido desde Confirmado. _(`test_start_picking_only_from_confirmed`)_
- Complete picking solo es válido desde Picking. _(`test_complete_picking_only_from_picking`)_
- Deliver solo es válido desde Ready for delivery. _(`test_deliver_only_from_ready_for_delivery`)_
- Cancelar un Draft libera las reservas activas de inmediato. _(`test_cancel_releases_active_reservations_from_draft`)_
- Cancelar un Confirmado libera las reservas activas de inmediato. _(`test_cancel_releases_active_reservations_from_confirmed`)_
- Cancelar desde Picking libera las reservas convertidas (restore: integration). _(`test_cancel_from_picking_releases_converted_reservations`)_
- Cancelar un pedido cerrado o ya cancelado es inválido. _(`test_cancel_from_closed_or_canceled_is_invalid`)_
- Modify solo es válido desde Confirmado y libera las reservas convertidas. _(`test_modify_only_from_confirmed_and_releases_converted`)_
- add_draft_item crea la línea y acumula cantidad si el SKU ya existe. _(`test_add_draft_item_upserts_and_accumulates`)_
- Una cantidad no positiva no se puede agregar a un Draft. _(`test_add_draft_item_refuses_non_positive_quantity`)_
- Solo los pedidos Draft aceptan edición de líneas. _(`test_add_remove_draft_item_only_on_draft`)_
- Quitar un SKU que no está en el Draft no hace nada. _(`test_remove_draft_item_unknown_sku_is_a_noop`)_
- El flag needs_requote hace que requiera recotizar. _(`test_requires_requote_true_when_flagged`)_
- Una reserva vencida hace que requiera recotizar. _(`test_requires_requote_true_when_stale_reservation`)_
- Sin flag ni reservas vencidas, no requiere recotizar. _(`test_requires_requote_false_when_clean`)_
- Expirar reservas vencidas marca el pedido para recotizar. _(`test_expire_reservations_flags_order_when_rows_expired`)_
- Sin reservas vencidas, expirar no hace nada. _(`test_expire_reservations_noop_when_nothing_expired`)_
- Draft → Confirmed → Picking → Ready for delivery → Closed con fecha. _(`test_happy_path_draft_to_closed_sets_delivery_date`)_
- Un pedido con reserva vencida no se confirma: exige recotizar. _(`test_stale_quote_refused_with_requote_requirement`)_
- Una reserva vigente no bloquea la confirmación. _(`test_fresh_reservation_can_be_confirmed`)_
- Cancelar un Draft libera las reservas: el stock vuelve a estar disponible. _(`test_cancel_draft_releases_reservations_and_stock_is_available`)_
- Cancelar desde Picking restaura el stock descontado y audita el ajuste. _(`test_late_cancel_restores_deducted_stock_with_audit`)_
- Modify restaura el stock descontado y libera las reservas convertidas. _(`test_modify_restores_deducted_stock_without_double_count`)_
- add/remove mutan OrderItem rows; el Draft vacío sigue Draft. _(`test_add_remove_draft_item_on_persisted_draft`)_

## Integración con RAG de catálogo de proveedores

- El SKU RAG con prefijo duplicado se normaliza a una sola forma. _(`test_normalize_rag_sku`)_
  - AMX-AMX-AT-5044 / AMX / AMX-AT-5044
  - AMX-AT-5044 / AMX / AMX-AT-5044
  - AMX-AMX-AMX-AT-5044 / AMX / AMX-AT-5044
  - AT-5044 / AMX / AT-5044
  - AMX-AMX-AT-5044 / (vacío) / AMX-AMX-AT-5044
- Un query exitoso mapea productos tipados y pide structured_json=true. _(`test_rag_client_query_maps_products_and_sends_structured_json`)_
- Un is_refusal=true se traduce a lista vacía (no encontrado en catálogos). _(`test_rag_client_refusal_returns_empty_tuple`)_
- Un SUCCESS sin productos devuelve lista vacía, no un error. _(`test_rag_client_empty_products_returns_empty_tuple`)_
- Los productos sin nombre se omiten del resultado tipado. _(`test_rag_client_skips_products_without_name`)_
- El codigo_orig gana; el codigo normalizado es solo el fallback. _(`test_rag_client_prefers_codigo_orig_over_normalized_codigo`)_
- Sin codigo_orig, el codigo con doble prefijo se normaliza al mostrarlo. _(`test_rag_client_normalizes_double_prefix_codigo`)_
- Un error de conexión se convierte en RagProductError, nunca transport crudo. _(`test_rag_client_connect_error_raises_domain_error`)_
- Un timeout de lectura se convierte en RagProductError. _(`test_rag_client_read_timeout_raises_domain_error`)_
- Un HTTP 500 del servicio se convierte en RagProductError. _(`test_rag_client_http_500_raises_domain_error`)_
- Un payload 200 no-JSON se convierte en RagProductError. _(`test_rag_client_malformed_json_raises_domain_error`)_
- Sin RAG_BASE_URL el cliente lanza RagProductNotConfigured al usarse. _(`test_rag_client_without_base_url_raises_not_configured`)_
- Un httpx.Client inyectado se usa tal cual, sin construir otro desde settings. _(`test_rag_client_injected_client_is_used_directly`)_
- A successful price lookup returns the offer and forwards the supplier code. _(`test_price_lookup_200_maps_price_and_supplier_query_parameter`)_
- A missing supplier product is a normal lookup miss. _(`test_price_lookup_404_returns_none`)_
- Transport and server failures never leak raw HTTP exceptions. _(`test_price_lookup_transport_and_server_errors_raise_domain_error`)_

## Búsqueda de producto (precedencia local → RAG)

- Add phrases resolve to (index, quantity); bare quantity answers map to the last product. _(`test_parse_product_add`)_
  - agregalo
  - agregala
  - sumá 5 de eso
  - sumale 3 de eso
  - agregá 2 de esos
  - agregale 2
  - sumale 3
  - AGREGÁ 1
  - agregale 2 unidades
  - agregale 2 de eso
  - el 2
  - quiero el 3
  - agregalo
  - el 5
  - el 2
  - pasame el precio
  - (vacío)
  - quiero 2
  - dame 3
  - anotame 2
  - llevo 2 unidades
  - necesito 2
  - quiero llevar 2
  - 2 unidades
  - llevo 2 u.
  - dos
  - un
  - diez
  - veinte
  - 2
  - Serían 2
  - si, está bien
  - dale
  - nada más
  - ok
  - sí
  - no
  - todo bien
  - quiero 2 recolectores
  - agregale 2 recolectores de aceite
  - agregale
  - agregale 2
  - quiero 2
- Un hit local (>= floor) resuelve LOCAL y nunca llama al RAG. _(`test_local_hit_skips_rag`)_
- Un candidato local bajo el floor no es hit: el RAG se consulta. _(`test_local_below_floor_falls_back_to_rag`)_
- Un local vacío cae al RAG y los campos del producto viajan al entry. _(`test_empty_local_falls_back_to_rag_and_maps_fields`)_
- Un RAG que rechaza (sin productos) resuelve NONE, no un error. _(`test_empty_local_with_refusal_is_none`)_
- Un error del RAG resuelve ERROR sin propagar la excepción. _(`test_empty_local_with_rag_error_is_error`)_
- Un SQLAlchemyError del hop local no propaga: el RAG igual se consulta. _(`test_local_sqlalchemy_error_still_calls_rag`)_
- Hop local caído + RAG caído resuelve ERROR (la cadena nunca lanza). _(`test_local_error_and_rag_error_is_error`)_
- La cadena con un RagProductClient real normaliza el doble prefijo del codigo. _(`test_chain_normalizes_sku_from_real_client`)_
- A finalize command returns its customer name only when a draft exists. _(`test_parse_finalize_extracts_customer_name_from_non_empty_draft`)_
- The handler can ask for a customer when a draft is being finalized anonymously. _(`test_is_finalize_recognizes_command_without_customer_name`)_
- El comando 'sacá X' extrae el producto (artículo incluido); frases largas no. _(`test_parse_product_remove`)_
  - sacá los clavos / clavos
  - quitá la pintura / pintura
  - eliminá el recolector de aceite / recolector de aceite
  - borrá tornillos / tornillos
  - Sacá el 2 / 2
  - sacá clavos. / clavos
  - no sacó nada
  - me gustaría comprar clavos
  - (vacío)

## Percepción (voz e imagen)

- Audio limpio se transcribe a texto utilizable sin fragmentos marcados. _(`test_transcribe_clean_audio_returns_text`)_
- Audio ruidoso se transcribe igual y marca los fragmentos de baja confianza. _(`test_transcribe_noisy_audio_flags_fragments_not_dropped`)_
- A transcription provider failure raises TranscriptionError. _(`test_transcribe_provider_error_raises_transcription_error`)_
- Una transcripción vacía es un fallo, no un éxito silencioso. _(`test_transcribe_empty_transcript_raises`)_
- TranscriptionError es un subtipo de PerceptionError. _(`test_transcription_error_is_a_perception_error`)_
- Analizar una imagen devuelve el texto descriptivo con su confianza. _(`test_analyze_image_returns_vision_text`)_
- A custom prompt is forwarded to the vision provider. _(`test_analyze_image_custom_prompt_forwarded`)_
- A vision provider failure raises VisionError. _(`test_analyze_image_provider_error_raises_vision_error`)_
- Una imagen sin descripción lanza VisionError. _(`test_analyze_image_empty_description_raises`)_

## Integración con OpenAI

- El avg_logprob de Whisper se mapea a una confianza en [0, 1]. _(`test_segment_confidence_maps_logprob_to_unit_range`)_
- Un audio limpio transcribe con texto y confianza alta sin fragmentos. _(`test_transcribe_clean_audio_returns_text_and_high_confidence`)_
- Un audio ruidoso marca los fragmentos de baja confianza, nunca los descarta. _(`test_transcribe_noisy_audio_flags_low_confidence_fragments`)_
- Sin segmentos disponibles la confianza es plena (1.0). _(`test_transcribe_without_segments_has_full_confidence`)_
- A provider error propagates as TranscriptionError through perception. _(`test_transcribe_propagates_provider_errors_as_transcription_error`)_
- Una imagen analizada devuelve el texto con confianza plena al finalizar normal. _(`test_analyze_image_returns_text_with_stop_finish`)_
- Un cierre anómalo (length) baja la confianza del análisis. _(`test_analyze_image_suspect_finish_lowers_confidence`)_
- A vision provider failure propagates as VisionError through perception. _(`test_analyze_image_raises_vision_error_on_provider_failure`)_
- El embedder conserva el orden de entrada y pasa modelo y dimensiones. _(`test_embed_preserves_input_order_and_passes_model_dimensions`)_

## Búsqueda en catálogo

- Un nombre informal se mapea automáticamente al producto correcto. _(`test_informal_name_auto_maps_to_right_product`)_
- Un nombre con errores de tipeo igual recupera el producto. _(`test_misspelling_resolves_to_right_product`)_
- Un sinónimo exacto mapea sin ambigüedad al SKU oficial. _(`test_exact_synonym_auto_maps_unambiguously`)_
- Mayúsculas, puntuación y espacios extra no rompen la resolución. _(`test_unnormalized_input_still_resolves`)_
- La búsqueda híbrida rankea primero el producto objetivo. _(`test_search_ranks_right_product_first`)_
- Un único candidato bajo el umbral no se adivina: presenta menú. _(`test_low_confidence_single_candidate_presents_menu`)_
- Una consulta sin coincidencia se reporta como NO_ENCONTRADO. _(`test_no_match_is_reported`)_
- La similitud vectorial rankea correcto cuando el fuzzy es débil. _(`test_vector_auto_maps_when_fuzzy_is_weak`)_
- Embeedings equidistantes presentan un menú de dos candidatos. _(`test_vector_ambiguity_presents_two_candidate_menu`)_

## Vencimiento de reservas (scheduler)

- El tick del sweeper hace commit al terminar con éxito. _(`test_sweep_tick_commits_on_success`)_
- Ante un fallo, el tick hace rollback y el scheduler sigue vivo. _(`test_sweep_tick_rolls_back_and_keeps_scheduler_alive_on_failure`)_
- El scheduler registra el job de intervalo del sweeper. _(`test_build_sweeper_registers_interval_job`)_
- El sweeper expira reservas vencidas por TTL y marca el pedido. _(`test_sweep_expires_past_ttl_and_flags_order`)_
- El sweeper deja activas las reservas vigentes. _(`test_sweep_leaves_fresh_reservations_active`)_
- Entre reservas mixtas, el sweeper expira solo las vencidas. _(`test_sweep_expires_only_past_ttl_among_mixed`)_

## Canales de entrada (Telegram/WhatsApp)

- Un canal implementa el contrato completo de la abstracción Channel. _(`test_channel_abc_contract_is_implemented`)_
- El hook de verificación de firma devuelve un booleano. _(`test_channel_verify_request`)_
- El adaptador de Telegram normaliza un update crudo en un InboundMessage. _(`test_telegram_parse_inbound`)_
- El canal demo de Telegram acepta webhooks autenticados por bot token. _(`test_telegram_verify_request_accepts_demo_payload`)_

## Canal WhatsApp Cloud API

- Un mensaje de texto de WhatsApp se normaliza con remitente y cuerpo. _(`test_parse_text_message_normalizes_sender_and_body`)_
- Una nota de voz se marca como media_type voice con su media_id. _(`test_parse_voice_message_flags_media_kind`)_
- Una foto se marca como media_type image con su media_id. _(`test_parse_image_message_flags_media_kind`)_
- Un payload sin mensajes normaliza un InboundMessage vacío. _(`test_parse_empty_payload_yields_empty_sender`)_
- El token de suscripción del webhook autentica la verificación hub. _(`test_verify_request_checks_subscription_verify_token`)_
- Un payload de mensaje confía en la firma HMAC ya validada por el endpoint. _(`test_verify_request_message_payload_defers_to_endpoint_hmac`)_
- Enviar texto postea al endpoint de mensajes con el bearer token. _(`test_send_text_posts_to_graph_messages_endpoint`)_
- Sin token ni phone id configurados, enviar texto es un no-op. _(`test_send_text_without_config_is_noop`)_
- Un error HTTP al enviar se traduce en WhatsAppError. _(`test_send_text_http_error_raises_whatsapp_error`)_
- Descargar media resuelve el id a URL y devuelve los bytes. _(`test_fetch_media_resolves_id_to_bytes`)_
- Sin token configurado, descargar media falla sin tocar la red. _(`test_fetch_media_without_token_raises_before_network`)_

## Webhook de entrada

- El endpoint de salud responde 200. _(`test_healthz`)_
- Un canal desconocido devuelve 404. _(`test_unknown_channel_returns_404`)_
- El webhook confirma (ACK) muy por debajo del SLA de 5 segundos. _(`test_ack_returns_quickly`)_
- Un payload de WhatsApp sin firma válida se rechaza con 401. _(`test_unauthenticated_payload_rejected`)_
- Un payload de WhatsApp con firma incorrecta se rechaza con 401. _(`test_bad_signature_rejected`)_
- Con token secreto configurado, Telegram exige el header de autenticación. _(`test_telegram_webhook_requires_secret_token_when_configured`)_

## Intake y trabajo en background

- El ACK responde en menos de 5 segundos aunque el trabajo pesado duerma. _(`test_ack_returns_under_five_seconds_with_slow_handler`)_
- El trabajo pesado se ejecuta en background, después del ACK. _(`test_heavy_work_runs_after_ack_is_sent`)_
- El handler de fondo recibe el mensaje entrante ya normalizado. _(`test_handler_receives_normalized_inbound_message`)_

## Modelo de datos y migraciones

- Cada entidad del diseño tiene su modelo ORM correspondiente. _(`test_all_design_entities_are_modeled`)_
- Las tablas del eje de sourcing existen en el modelo ORM. _(`test_sourcing_entities_are_modeled`)_
- El pedido tiene sourcing_state y delivery_date, sin tocar order_estado. _(`test_order_has_sourcing_axis_and_delivery_date`)_
- El enum SourcingState tiene exactamente los tres estados del eje. _(`test_sourcing_state_enum_values`)_
- El enum del PO tiene exactamente los cinco estados de su máquina. _(`test_po_state_enum_values`)_
- El enum SupplierStatus tiene exactamente ACTIVO e INACTIVO. _(`test_supplier_status_enum_values`)_
- El enum IvaCondition tiene exactamente los cinco valores confirmados. _(`test_iva_condition_enum_values`)_
- El modelo suppliers expone las columnas de datos maestros. _(`test_supplier_model_has_master_data_columns`)_
- code tiene índice único; cuit único parcial cuando no es NULL. _(`test_supplier_code_and_cuit_indexes`)_
- La columna `catalogo.embedding` se declara como pgvector vector(1536). _(`test_catalogo_has_vector_1536_embedding`)_
- El modelo `clientes` no modela límites de crédito ni condiciones de pago. _(`test_cliente_has_no_credit_or_payment_fields`)_
- La máquina de estados del pedido se fija a los seis estados de la spec. _(`test_order_estado_enum_values`)_
- El modelo orders declara el índice único parcial de un draft por cliente. _(`test_order_has_one_draft_per_customer_partial_index`)_
- La migración crea todas las tablas del diseño. _(`test_migration_creates_all_tables`)_
- RED: la migración agrega sourcing_state y delivery_date a orders. _(`test_migration_creates_sourcing_columns`)_
- RED: los enums del eje de sourcing existen tras la migración. _(`test_migration_creates_sourcing_enums`)_
- La migración deja el enum order_estado con los seis estados de la spec. _(`test_migration_order_estado_has_six_values`)_
- La migración crea el índice único parcial de un draft por cliente. _(`test_migration_creates_one_draft_per_customer_index`)_
- RED: sourcing_needs queda indexado por order_id y supplier_id. _(`test_migration_indexes_sourcing_needs`)_
- La columna migrada `catalogo.embedding` es vector(1536). _(`test_migration_has_vector_1536_column`)_
- La extensión pgvector queda instalada en el esquema migrado. _(`test_migration_enables_pgvector_extension`)_
- The order-state-machine migration downgrades safely and re-upgrades. _(`test_order_state_machine_migration_downgrade_safety`)_
- A freshly migrated DB seeds default_margin_pct=20 and pricing consumes it. _(`test_migration_seeded_default_margin_is_read_by_pricing`)_

## Teléfonos y clientes

- Todos los formatos de un mismo número argentino normalizan al mismo E.164 canónico. _(`test_phone_format_variants_normalize_to_same_number`)_
- Un teléfono no interpretable normaliza a None. _(`test_unparseable_phone_normalizes_to_none`)_
  - (vacío)
  - abc
  - 5555
  - 12
- Una línea fija válida normaliza a su forma E.164 sin prefijo 9. _(`test_non_mobile_landline_still_normalizes`)_

## Registro en Google Sheets

- Una fila válida se agrega a la hoja y el pedido queda sincronizado. _(`test_append_success_registers_row_and_marks_synced`)_
- Si la hoja falla, la fila se aísla en cuarentena sin lanzar excepción. _(`test_append_failure_quarantines_row_and_never_raises`)_
- Si la cuarentena también falla, la fila queda registrada en memoria. _(`test_append_failure_with_unreachable_quarantine_keeps_memory_log`)_
- Sin credenciales configuradas, la fila se cuarentena y no se lanza nada. _(`test_missing_credentials_quarantines_instead_of_raising`)_
- El registro sincronizado refleja solo los appends exitosos. _(`test_sheets_synced_reflects_only_successful_appends`)_

## Códigos de barras

- Una imagen con códigos devuelve datos y simbología de cada código. _(`test_decode_image_returns_values_and_symbologies`)_
- Una imagen sin códigos decodifica a una lista vacía. _(`test_decode_image_without_codes_returns_empty_list`)_
- Una imagen ilegible falla con un error claro de decodificación. _(`test_decode_failure_raises_clear_error`)_
- Un código único mapea a un solo SKU del catálogo. _(`test_single_barcode_maps_to_one_sku`)_
- Un código compartido por dos SKU se marca DUPLICATE sin elegir por nadie. _(`test_duplicate_barcode_flags_candidates_for_owner`)_
- Un código sin match se reporta como UNKNOWN para resolución manual. _(`test_unknown_barcode_is_reported`)_
- Un ajuste positivo por código de barras aumenta el stock y registra el motivo y el actor. _(`test_adjust_stock_increase_records_audit_trail`)_
- Un ajuste negativo por código de barras reduce el stock y registra el motivo y el actor. _(`test_adjust_stock_decrease_records_audit_trail`)_
- Un código de barras duplicado no ajusta stock y exige desambiguar al dueño. _(`test_adjust_stock_duplicate_barcode_raises`)_
- Un código de barras desconocido no ajusta stock y reporta el motivo. _(`test_adjust_stock_unknown_barcode_raises`)_
- Un ajuste que deja el stock negativo falla y conserva el stock sin cambios. _(`test_adjust_stock_below_zero_raises_and_keeps_stock`)_

## OCR de documentos de proveedor

- El texto de un remito se parsea en filas con cantidad y costo. _(`test_parse_line_items_extracts_quantity_and_cost_rows`)_
- Las líneas no interpretables quedan señaladas, nunca se descartan. _(`test_parse_line_items_keeps_unparsed_lines_flagged`)_
- Un texto vacío no produce filas ni líneas pendientes. _(`test_parse_line_items_empty_text_has_no_items`)_
- Extraer un documento legible devuelve las filas parseadas. _(`test_extract_document_returns_parsed_items`)_
- Un documento ilegible se rechaza con un error claro, sin escribir nada. _(`test_extract_document_rejects_illegible_with_clear_error`)_
- A vision provider failure propagates as VisionError. _(`test_extract_document_vision_failure_propagates`)_
- Una lista de precios se parsea en código, descripción y costo. _(`test_parse_price_list_extracts_code_description_cost`)_
- Una imagen local se codifica como data URL con su MIME. _(`test_image_to_data_url_embeds_file_bytes`)_
- La lista de precios mapea SKU existentes y sugiere nuevos. _(`test_ingest_price_list_maps_and_suggests`)_
- Re-ingestar la misma fila actualiza el mapeo sin duplicarlo. _(`test_ingest_price_list_updates_existing_mapping_without_duplicates`)_
- Una descripción normalizada mapea al SKU sin coincidencia de código. _(`test_ingest_price_list_matches_by_normalized_name`)_

## Backoffice (catálogo, clientes, monitor, ingesta)

- Building the app creates tabs with the expected labels. _(`test_build_app_creates_tabs_with_expected_labels`)_
- La pestaña Ingestion expone la vista previa editable y el botón de confirmar. _(`test_build_app_ingestion_tab_has_preview_and_confirm`)_
- La pestaña Catalog expone la grilla de productos y el botón de guardado. _(`test_build_app_catalog_tab_has_product_grid`)_
- Las filas extraídas se renderizan como grilla editable. _(`test_to_grid_rows_renders_editable_preview`)_
- La extracción delega en el analizador de visión y parsea las filas. _(`test_extract_document_items_uses_vision_analyzer`)_
- Un documento ilegible se rechaza con un error claro. _(`test_extract_document_items_rejects_illegible`)_
- La grilla de catálogo devuelve todos los campos por producto. _(`test_catalog_list_products_returns_expected_fields`)_
- Editar stock y precio se refleja en la grilla. _(`test_catalog_update_stock_and_price`)_
- Cambiar el margen recalcula el precio de lista con el motor de precios. _(`test_catalog_update_margin_recomputes_base_price`)_
- Registrar un cliente normaliza el teléfono al formato canónico. _(`test_clients_create_normalizes_phone`)_
- Un teléfono inválido impide registrar el cliente. _(`test_clients_create_rejects_invalid_phone`)_
- Editar un cliente cambia su descuento particular. _(`test_clients_update_changes_discount`)_
- Confirmar filas con SKU existente aumenta el stock y el costo. _(`test_confirm_items_updates_existing_product_stock`)_
- Una fila sin SKU existente crea un producto nuevo con margen del supplier. _(`test_confirm_items_creates_new_product_for_unknown_sku`)_
- El monitor lista pedidos con estado y estado de sincronización Sheets. _(`test_monitor_lists_orders_with_state_and_sheets_status`)_
- Customer Orders returns persisted order totals and frozen line fields. _(`test_customer_orders_list_and_detail_include_ars_totals_and_snapshots`)_
- ARS cannot be edited while a USD rate is stored with a timestamp. _(`test_exchange_rate_rejects_ars_and_persists_usd`)_
- Loading a rate recomputes a pending RAG order and clears its flag. _(`test_recompute_pending_conversion_clears_flag_and_fills_totals`)_
- The default RAG margin setting can be read and updated. _(`test_default_margin_round_trips`)_
- Approval registration refuses an order until its prices are converted. _(`test_pending_conversion_order_is_blocked_at_approval`)_
- La grilla del catálogo renderiza los productos sembrados. _(`test_app_catalog_grid_renders_seeded_products`)_
- Registrar un cliente desde la UI devuelve un mensaje de éxito. _(`test_app_register_client_returns_success_message`)_
- Editar stock desde la UI persiste el cambio en el catálogo. _(`test_app_catalog_edit_persists_stock_change`)_
- Un teléfono inválido desde la UI devuelve el error en pantalla. _(`test_app_register_client_surfaces_error_for_bad_phone`)_
- Confirmar la ingesta desde la UI reporta actualizados y creados. _(`test_app_ingest_confirm_reports_counts`)_
- Confirmar una fila nueva desde la UI la crea en el catálogo. _(`test_app_ingest_confirm_creates_new_product`)_
- La grilla con headers llega como DataFrame y se confirma igual. _(`test_app_ingest_confirm_accepts_dataframe_with_headers`)_
- La vista previa de ingesta devuelve la grilla y un mensaje de estado. _(`test_app_ingest_preview_returns_grid_and_message`)_
- The app-level rate save bumps updated_at and recomputes pending orders. _(`test_app_rate_save_updates_timestamp_and_recomputes_pending_order`)_
- Solo las acciones legales del estado se ofrecen en el tab (backoffice spec). _(`test_legal_actions_per_state`)_
  - DRAFT
  - CONFIRMED
  - PICKING
  - READY_FOR_DELIVERY
  - CANCELED
  - CLOSED
- La acción start picking transiciona y hace commit (patrón po.py). _(`test_start_picking_action_commits_transition`)_
- Confirmado → Picking → Ready → Closed; deliver guarda la fecha de entrega. _(`test_fulfillment_chain_commits_to_closed_with_delivery_date`)_
- Cancelar desde Confirmado libera reservas; el actor del ajuste es backoffice. _(`test_cancel_action_releases_reservations_with_backoffice_actor`)_
- Cancelar desde Picking restaura stock y audita con actor backoffice. _(`test_cancel_action_restores_deducted_stock_with_audit`)_
- El monitor muestra los seis estados del pedido. _(`test_monitor_shows_all_six_states`)_
- El tab Customer Orders expone las cuatro acciones de cumplimiento. _(`test_app_customer_orders_tab_has_fulfillment_buttons`)_

## Feature flags por fase

- Por defecto todas las fases están habilitadas. _(`test_all_fases_enabled_by_default`)_
- El flag de una fase deshabilitada se refleja en fase_enabled. _(`test_fase_enabled_reflects_flag`)_
- Deshabilitar una fase hace que require_fase lance FeatureDisabledError. _(`test_require_fase_raises_when_disabled`)_
- Con la fase habilitada, require_fase no lanza nada. _(`test_require_fase_passes_when_enabled`)_
- Una fase inexistente se rechaza con ValueError. _(`test_unknown_fase_raises_value_error`)_
- El backoffice no se construye cuando la fase 4 está deshabilitada. _(`test_backoffice_build_refuses_when_fase4_disabled`)_
- Con la fase 2 apagada el webhook responde ACK sin despachar trabajo. _(`test_webhook_acks_without_dispatch_when_fase2_disabled`)_

## E2E: pedido completo

- El pedido del dueño se confirma: reserva convertida, Sheets y stock descontado. _(`test_e2e_owner_order_confirms_and_deducts_stock`)_
- Si Sheets falla, el pedido IGUAL queda Confirmado (cuarentena tolerada). _(`test_e2e_sheets_failure_keeps_order_confirmed`)_
- Al rechazar el pedido, se cancela y la reserva se libera. _(`test_e2e_owner_reject_cancels_order_and_releases_reservation`)_
- Una nota de voz de WhatsApp se normaliza marcando media_type voice. _(`test_e2e_whatsapp_voice_payload_flags_media`)_

## E2E: ingesta de documentos

- Un remito subido se previsualiza y al confirmar actualiza el inventario. _(`test_e2e_remito_upload_previews_and_confirms_inventory`)_
- Correcciones del dueño en la grilla reemplazan la extracción cruda. _(`test_e2e_owner_corrections_override_raw_extraction`)_
- Una foto de código de barras decodifica y responde el stock disponible. _(`test_e2e_barcode_stock_query_decodes_and_resolves`)_
- confirm_items rechaza un supplier INACTIVO sin escribir inventario. _(`test_confirm_items_refuses_inactive_supplier_and_writes_nothing`)_

## Observabilidad y logs por sesión

- Genera identificadores de sesión únicos y formateados. _(`test_generate_session_id`)_
- Propaga el session_id a través de contextvars en el contexto síncrono. _(`test_contextvar_propagation`)_
- Aísla el session_id entre diferentes corutinas asincrónicas concurrentes. _(`test_contextvar_async_isolation`)_
- Registra eventos estructurados en el archivo de log individual de la sesión. _(`test_log_session_event_and_read`)_
- El backoffice puede listar archivos de sesión y renderizar eventos tabulares. _(`test_backoffice_sessions_helpers`)_
- Verifica que el archivo de log contenga cabecera y bloques legibles para debugging. _(`test_human_readable_log_format`)_
- Verifica que los eventos de dispatch y approval se formateen con diagnóstico legible. _(`test_human_readable_dispatch_and_approval_logs`)_

## Trazabilidad de sesión en el pipeline

- El pipeline inicia sesión con 'Hola Bob', preserva la sesión y registra eventos de RAG. _(`test_session_trace_lifecycle_and_rag_events`)_
