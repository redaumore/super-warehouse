# Escenarios testeados

Documento generado automáticamente desde los docstrings de los tests. No lo edites a mano: si un escenario cambia, actualizá la primera línea del docstring del test y volvé a correr `make test-docs`.

**Total de escenarios:** 229, agrupados en 26 dominios.

> Cada ítem lista el comportamiento que se valida en lenguaje natural, seguido (entre paréntesis) del nombre técnico del test.

## Índice

- [Motor de precios](#motor-de-precios) — 6
- [Cotización y ventas](#cotización-y-ventas) — 11
- [Stock e inventario](#stock-e-inventario) — 10
- [Despacho y aprobación del dueño](#despacho-y-aprobación-del-dueño) — 13
- [Registro de aprobaciones](#registro-de-aprobaciones) — 7
- [Orquestador y enrutamiento](#orquestador-y-enrutamiento) — 18
- [Pipeline de orquestación (walking skeleton)](#pipeline-de-orquestación-walking-skeleton) — 5
- [Agente Customer (respondedor conversacional)](#agente-customer-respondedor-conversacional) — 13
- [Ciclo de vida del pedido](#ciclo-de-vida-del-pedido) — 15
- [Percepción (voz e imagen)](#percepción-voz-e-imagen) — 9
- [Integración con OpenAI](#integración-con-openai) — 9
- [Búsqueda en catálogo](#búsqueda-en-catálogo) — 9
- [Vencimiento de reservas (scheduler)](#vencimiento-de-reservas-scheduler) — 6
- [Canales de entrada (Telegram/WhatsApp)](#canales-de-entrada-telegram-whatsapp) — 4
- [Canal WhatsApp Cloud API](#canal-whatsapp-cloud-api) — 11
- [Webhook de entrada](#webhook-de-entrada) — 6
- [Intake y trabajo en background](#intake-y-trabajo-en-background) — 3
- [Modelo de datos y migraciones](#modelo-de-datos-y-migraciones) — 7
- [Teléfonos y clientes](#teléfonos-y-clientes) — 5
- [Registro en Google Sheets](#registro-en-google-sheets) — 5
- [Códigos de barras](#códigos-de-barras) — 11
- [OCR de documentos de proveedor](#ocr-de-documentos-de-proveedor) — 11
- [Backoffice (catálogo, clientes, monitor, ingesta)](#backoffice-catálogo-clientes-monitor-ingesta) — 21
- [Feature flags por fase](#feature-flags-por-fase) — 7
- [E2E: pedido completo](#e2e-pedido-completo) — 4
- [E2E: ingesta de documentos](#e2e-ingesta-de-documentos) — 3

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
- Consultar un SKU desconocido lanza error. _(`test_unknown_sku_raises`)_
- Reservar crea una reserva activa con el TTL configurado y bloquea stock. _(`test_reserve_creates_active_reservation_and_locks`)_
- Reservar más de lo disponible se rechaza sin bloquear de más. _(`test_reserve_beyond_available_stock_is_refused`)_
- Reservar una cantidad no positiva se rechaza. _(`test_reserve_rejects_non_positive_quantity`)_

## Despacho y aprobación del dueño

- Se notifica al dueño la cotización a través del notificador. _(`test_notify_owner_sends_quote_via_notifier`)_
- El mensaje de cotización menciona las líneas y el total. _(`test_format_quote_message_mentions_lines_and_total`)_
- El texto del dueño se interpreta como aprobar, rechazar o desconocido. _(`test_parse_decision_actions`)_
  - sí, aprobá
  - aprobá
  - dale
  - ok, dale para adelante
  - no, rechazá
  - rechazá el pedido
  - no
  - hablamos mañana
  - (vacío)
- Aprobar con descuento extra por línea se interpreta como aprobación + ajuste. _(`test_parse_decision_with_adjustment`)_
- Se aceptan porcentajes decimales en el ajuste. _(`test_parse_decision_accepts_decimal_percent`)_
- Un rechazo ignora cualquier mención de ajuste. _(`test_parse_decision_reject_ignores_adjustment_mention`)_
- Aprobar con ajuste reprecifica la línea afectada. _(`test_apply_approve_with_adjustment_reprises_line`)_
- Aprobar sin cambios conserva los precios cotizados. _(`test_apply_plain_approve_keeps_prices`)_
- Rechazar libera las reservas y el stock vuelve a estar disponible. _(`test_apply_reject_releases_reservations`)_
- Aprobar sobre una reserva vencida exige recotizar. _(`test_apply_approve_on_expired_reservation_requires_requote`)_
- Aplicar una decisión desconocida lanza error. _(`test_apply_unknown_decision_raises`)_
- Un ajuste que nombra un producto fuera de la cotización no se puede aplicar. _(`test_apply_adjustment_no_matching_quote_line_raises`)_
- Sin cotización, un SKU desconocido en el ajuste se rechaza. _(`test_apply_adjustment_sku_not_in_order_raises`)_

## Registro de aprobaciones

- El total del pedido suma precio final por cantidad, redondeado a centavos. _(`test_order_total_sums_final_price_times_quantity`)_
- Una línea ajustada aporta su precio final rebajado al total. _(`test_order_total_with_adjusted_line`)_
- El resumen de ítems lista cantidad por SKU separado por punto y coma. _(`test_build_items_summary_lists_each_line`)_
- Aprobar registra: convierte reservas, descuenta stock, agrega a Sheets y confirma. _(`test_approve_and_register_converts_deducts_and_confirms`)_
- Registrar tras un ajuste usa el total reprecificado y confirma igual. _(`test_register_after_adjustment_approve_uses_revised_total`)_
- Aprobar una reserva vencida exige recotizar y no produce efectos laterales. _(`test_approve_on_expired_reservation_refuses_without_side_effects`)_
- La cuarentena de Sheets no bloquea: se confirma y el estado lo reporta. _(`test_sheets_quarantine_never_blocks_approval`)_

## Orquestador y enrutamiento

- Una nota de voz se enruta a Percepción (transcripción). _(`test_voice_note_routes_to_perception_stt`)_
- Una imagen (foto de remito/código) se enruta a Percepción (visión). _(`test_image_routes_to_perception_vision`)_
- Un texto nuevo de cliente se enruta a Customer. _(`test_fresh_text_routes_to_customer`)_
- La aprobación del dueño se enruta a Despacho reanudando el pedido. _(`test_owner_approval_routes_to_dispatch_resuming_order`)_
- El rechazo del dueño se enruta a Despacho. _(`test_owner_rejection_routes_to_dispatch`)_
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

## Pipeline de orquestación (walking skeleton)

- El orquestador de la pipeline enlaza los seis agentes. _(`test_build_orchestrator_registers_all_six_agents`)_
- Un mensaje de Telegram nuevo es respondido por el agente Customer a través del responder LLM. _(`test_handle_inbound_routes_persists_and_replies`)_
- Un segundo mensaje del mismo remitente continúa la conversación con el responder LLM. _(`test_second_message_resumes_context`)_
- Una nota de voz se enruta a Percepción con una respuesta específica. _(`test_voice_routes_to_perception_reply`)_
- Un canal sin adaptador no rompe la pipeline: descarta la respuesta. _(`test_unknown_channel_drops_reply_without_crash`)_

## Agente Customer (respondedor conversacional)

- Un mensaje nuevo le llega al responder con el system prompt y el turno del usuario. _(`test_fresh_text_message_goes_to_responder_with_system_and_user`)_
- Una conversación en curso le pasa el historial completo al responder y agrega el nuevo par. _(`test_continuing_conversation_sends_history_and_appends_new_pair`)_
- Sin clave de API el responder falla al saludo y el historial igual registra el turno. _(`test_unconfigured_responder_falls_back_to_greeting_and_logs_turns`)_
- Un mensaje sin texto saluda sin consultar al responder ni registrar turno de usuario. _(`test_textless_message_greets_without_calling_responder`)_
- El handler respeta el fallback y el system prompt personalizados que recibe. _(`test_build_handler_uses_custom_fallback_and_system_prompt`)_
- Sin OPENAI_API_KEY, el responder OpenAI lanza ResponderNotConfigured. _(`test_openai_responder_raises_not_configured_without_key`)_
- El responder OpenAI mapea roles y contenido y devuelve el texto del modelo. _(`test_openai_responder_maps_messages_and_returns_model_text`)_
- Un modelo que no produce texto dispara ResponderError. _(`test_openai_responder_raises_when_model_returns_empty_reply`)_
- Con catálogo vacío el responder recibe la nota 'sin resultados' y el historial no la guarda. _(`test_product_query_with_empty_catalog_injects_no_stock_note`)_
- Con candidatos, la nota lista nombre oficial y SKU de cada producto. _(`test_catalog_candidates_become_note_listing_names_and_skus`)_
- Un error de base de datos omite la nota y el responder igual contesta. _(`test_searcher_database_error_skips_note_and_keeps_reply`)_
- Sin searcher, la lista de mensajes mantiene la forma del slice 1: system + historial + usuario. _(`test_handler_without_searcher_keeps_slice1_message_shape`)_
- En una conversación en curso, la nota va después del historial y justo antes del último turno del usuario. _(`test_note_lands_after_history_and_before_latest_user_turn`)_

## Ciclo de vida del pedido

- Aprobar un pedido pendiente lo mueve a Aprobado. _(`test_approve_pending_order_moves_to_approved`)_
- El flag needs_requote bloquea la aprobación silenciosa. _(`test_approve_flagged_order_raises_requote`)_
- Un pedido con reserva vencida no se aprueba en silencio: exige recotizar. _(`test_approve_order_with_stale_reservation_raises_requote`)_
- Aprobar un pedido que no está pendiente es una transición inválida. _(`test_approve_non_pending_order_is_invalid`)_
- Rechazar un pedido pendiente lo mueve a Rechazado y libera reservas. _(`test_reject_pending_order_moves_to_rejected_and_releases`)_
- Rechazar un pedido que no está pendiente es inválido. _(`test_reject_non_pending_order_is_invalid`)_
- Despachar solo es válido desde el estado Aprobado. _(`test_mark_dispatched_only_from_approved`)_
- El flag needs_requote hace que requiera recotizar. _(`test_requires_requote_true_when_flagged`)_
- Una reserva vencida hace que requiera recotizar. _(`test_requires_requote_true_when_stale_reservation`)_
- Sin flag ni reservas vencidas, no requiere recotizar. _(`test_requires_requote_false_when_clean`)_
- Expirar reservas vencidas marca el pedido para recotizar. _(`test_expire_reservations_flags_order_when_rows_expired`)_
- Sin reservas vencidas, expirar no hace nada. _(`test_expire_reservations_noop_when_nothing_expired`)_
- Rechazar libera las reservas y restaura el stock disponible. _(`test_reject_releases_reservations_and_restores_stock`)_
- Un pedido con reserva vencida no se puede aprobar: exige recotizar. _(`test_expired_order_cannot_be_approved`)_
- Una reserva vigente no bloquea la aprobación. _(`test_fresh_reservation_can_be_approved`)_

## Percepción (voz e imagen)

- Audio limpio se transcribe a texto utilizable sin fragmentos marcados. _(`test_transcribe_clean_audio_returns_text`)_
- Audio ruidoso se transcribe igual y marca los fragmentos de baja confianza. _(`test_transcribe_noisy_audio_flags_fragments_not_dropped`)_
- Un fallo del proveedor de transcripción lanza TranscriptionError. _(`test_transcribe_provider_error_raises_transcription_error`)_
- Una transcripción vacía es un fallo, no un éxito silencioso. _(`test_transcribe_empty_transcript_raises`)_
- TranscriptionError es un subtipo de PerceptionError. _(`test_transcription_error_is_a_perception_error`)_
- Analizar una imagen devuelve el texto descriptivo con su confianza. _(`test_analyze_image_returns_vision_text`)_
- Un prompt personalizado se reenvía al proveedor de visión. _(`test_analyze_image_custom_prompt_forwarded`)_
- Un fallo del proveedor de visión lanza VisionError. _(`test_analyze_image_provider_error_raises_vision_error`)_
- Una imagen sin descripción lanza VisionError. _(`test_analyze_image_empty_description_raises`)_

## Integración con OpenAI

- El avg_logprob de Whisper se mapea a una confianza en [0, 1]. _(`test_segment_confidence_maps_logprob_to_unit_range`)_
- Un audio limpio transcribe con texto y confianza alta sin fragmentos. _(`test_transcribe_clean_audio_returns_text_and_high_confidence`)_
- Un audio ruidoso marca los fragmentos de baja confianza, nunca los descarta. _(`test_transcribe_noisy_audio_flags_low_confidence_fragments`)_
- Sin segmentos disponibles la confianza es plena (1.0). _(`test_transcribe_without_segments_has_full_confidence`)_
- Un error del proveedor se propaga como TranscriptionError por percepción. _(`test_transcribe_propagates_provider_errors_as_transcription_error`)_
- Una imagen analizada devuelve el texto con confianza plena al finalizar normal. _(`test_analyze_image_returns_text_with_stop_finish`)_
- Un cierre anómalo (length) baja la confianza del análisis. _(`test_analyze_image_suspect_finish_lowers_confidence`)_
- Un fallo del proveedor de visión se propaga como VisionError por percepción. _(`test_analyze_image_raises_vision_error_on_provider_failure`)_
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
- La columna `catalogo.embedding` se declara como pgvector vector(1536). _(`test_catalogo_has_vector_1536_embedding`)_
- El modelo `clientes` no modela límites de crédito ni condiciones de pago. _(`test_cliente_has_no_credit_or_payment_fields`)_
- La máquina de estados del pedido se fija a los cuatro estados de la spec. _(`test_order_estado_enum_values`)_
- La migración crea todas las tablas del diseño. _(`test_migration_creates_all_tables`)_
- La columna migrada `catalogo.embedding` es vector(1536). _(`test_migration_has_vector_1536_column`)_
- La extensión pgvector queda instalada en el esquema migrado. _(`test_migration_enables_pgvector_extension`)_

## Teléfonos y clientes

- Todos los formatos de un mismo número argentino normalizan al mismo E.164 canónico. _(`test_phone_format_variants_normalize_to_same_number`)_
- Un teléfono no interpretable normaliza a None. _(`test_unparseable_phone_normalizes_to_none`)_
  - (vacío)
  - abc
  - 5555
  - 12
- Un número registrado — aun reescrito en otro formato — resuelve como KNOWN. _(`test_known_phone_matches_registered_customer`)_
- Un número válido pero no registrado se marca UNKNOWN, nunca se adivina. _(`test_unknown_phone_is_flagged_for_onboarding`)_
- Un número no interpretable se marca INVALID sin forma normalizada. _(`test_invalid_phone_is_flagged_not_guessed`)_

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
- Un fallo del proveedor de visión se propaga como VisionError. _(`test_extract_document_vision_failure_propagates`)_
- Una lista de precios se parsea en código, descripción y costo. _(`test_parse_price_list_extracts_code_description_cost`)_
- Una imagen local se codifica como data URL con su MIME. _(`test_image_to_data_url_embeds_file_bytes`)_
- La lista de precios mapea SKU existentes y sugiere nuevos. _(`test_ingest_price_list_maps_and_suggests`)_
- Re-ingestar la misma fila actualiza el mapeo sin duplicarlo. _(`test_ingest_price_list_updates_existing_mapping_without_duplicates`)_
- Una descripción normalizada mapea al SKU sin coincidencia de código. _(`test_ingest_price_list_matches_by_normalized_name`)_

## Backoffice (catálogo, clientes, monitor, ingesta)

- Construir la app genera cuatro pestañas con los títulos esperados. _(`test_build_app_creates_four_tabs_with_expected_labels`)_
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
- Una fila sin SKU existente crea un producto nuevo con margen del proveedor. _(`test_confirm_items_creates_new_product_for_unknown_sku`)_
- El monitor lista pedidos con estado y estado de sincronización Sheets. _(`test_monitor_lists_orders_with_state_and_sheets_status`)_
- La grilla del catálogo renderiza los productos sembrados. _(`test_app_catalog_grid_renders_seeded_products`)_
- Registrar un cliente desde la UI devuelve un mensaje de éxito. _(`test_app_register_client_returns_success_message`)_
- Un teléfono inválido desde la UI devuelve el error en pantalla. _(`test_app_register_client_surfaces_error_for_bad_phone`)_
- Confirmar la ingesta desde la UI reporta actualizados y creados. _(`test_app_ingest_confirm_reports_counts`)_
- Confirmar una fila nueva desde la UI la crea en el catálogo. _(`test_app_ingest_confirm_creates_new_product`)_
- La vista previa de ingesta devuelve la grilla y un mensaje de estado. _(`test_app_ingest_preview_returns_grid_and_message`)_

## Feature flags por fase

- Por defecto todas las fases están habilitadas. _(`test_all_fases_enabled_by_default`)_
- El flag de una fase deshabilitada se refleja en fase_enabled. _(`test_fase_enabled_reflects_flag`)_
- Deshabilitar una fase hace que require_fase lance FeatureDisabledError. _(`test_require_fase_raises_when_disabled`)_
- Con la fase habilitada, require_fase no lanza nada. _(`test_require_fase_passes_when_enabled`)_
- Una fase inexistente se rechaza con ValueError. _(`test_unknown_fase_raises_value_error`)_
- El backoffice no se construye cuando la fase 4 está deshabilitada. _(`test_backoffice_build_refuses_when_fase4_disabled`)_
- Con la fase 2 apagada el webhook responde ACK sin despachar trabajo. _(`test_webhook_acks_without_dispatch_when_fase2_disabled`)_

## E2E: pedido completo

- Un pedido de texto llega, cotiza al dueño y al aprobar descuenta stock. _(`test_e2e_text_order_flows_to_owner_approval_and_stock_deduction`)_
- Al rechazar el pedido, la reserva se libera y el stock vuelve a estar libre. _(`test_e2e_owner_reject_releases_reservation`)_
- Una nota de voz de WhatsApp se normaliza marcando media_type voice. _(`test_e2e_whatsapp_voice_payload_flags_media`)_
- Aunque el envío de confirmación falle, el flujo de aprobación no se corta. _(`test_e2e_http_error_on_confirm_still_completes_flow`)_

## E2E: ingesta de documentos

- Un remito subido se previsualiza y al confirmar actualiza el inventario. _(`test_e2e_remito_upload_previews_and_confirms_inventory`)_
- Correcciones del dueño en la grilla reemplazan la extracción cruda. _(`test_e2e_owner_corrections_override_raw_extraction`)_
- Una foto de código de barras decodifica y responde el stock disponible. _(`test_e2e_barcode_stock_query_decodes_and_resolves`)_
