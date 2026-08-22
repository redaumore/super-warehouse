# Escenarios testeados

Documento generado automáticamente desde los docstrings de los tests. No lo edites a mano: si un escenario cambia, actualizá la primera línea del docstring del test y volvé a correr `make test-docs`.

**Total de escenarios:** 117, agrupados en 13 dominios.

> Cada ítem lista el comportamiento que se valida en lenguaje natural, seguido (entre paréntesis) del nombre técnico del test.

## Índice

- [Motor de precios](#motor-de-precios) — 6
- [Cotización y ventas](#cotización-y-ventas) — 11
- [Stock e inventario](#stock-e-inventario) — 10
- [Despacho y aprobación del dueño](#despacho-y-aprobación-del-dueño) — 13
- [Orquestador y enrutamiento](#orquestador-y-enrutamiento) — 17
- [Ciclo de vida del pedido](#ciclo-de-vida-del-pedido) — 15
- [Percepción (voz e imagen)](#percepción-voz-e-imagen) — 9
- [Búsqueda en catálogo](#búsqueda-en-catálogo) — 9
- [Vencimiento de reservas (scheduler)](#vencimiento-de-reservas-scheduler) — 6
- [Canales de entrada (Telegram/WhatsApp)](#canales-de-entrada-telegram-whatsapp) — 4
- [Webhook de entrada](#webhook-de-entrada) — 5
- [Modelo de datos y migraciones](#modelo-de-datos-y-migraciones) — 7
- [Teléfonos y clientes](#teléfonos-y-clientes) — 5

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

## Webhook de entrada

- El endpoint de salud responde 200. _(`test_healthz`)_
- Un canal desconocido devuelve 404. _(`test_unknown_channel_returns_404`)_
- El webhook confirma (ACK) muy por debajo del SLA de 5 segundos. _(`test_ack_returns_quickly`)_
- Un payload sin firma válida se rechaza con 401. _(`test_unauthenticated_payload_rejected`)_
- Un payload con firma incorrecta se rechaza con 401. _(`test_bad_signature_rejected`)_

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
