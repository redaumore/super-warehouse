# Proposal: Log por Sesión de Telegram

## Intent

Implementar trazabilidad completa y estructurada por sesión de usuario en Telegram, iniciada cuando el usuario envía el comando canónico "Hola Bob", permitiendo auditar e inspeccionar en una traza unificada cada interacción con los servicios del sistema (RAG de catálogo de proveedores, ciclo de vida de órdenes, orquestador/enrutamiento y respuestas de agentes).

## Scope

### In Scope
- Identificador único de sesión (`session_id`) en `ConversationState`, generado de manera determinista al recibir el comando canónico "hola bob" (vía `is_session_reset`).
- Propagación de contexto de sesión mediante `contextvars` para asociar ejecuciones sincrónicas y asincrónicas a lo largo del pipeline.
- Módulo de registro de eventos de sesión (`src/observability/session_logger.py` o similar) que capture:
  - Mensajes entrantes y salientes de Telegram.
  - Decisiones de ruteo del `Orchestrator` y agente seleccionado.
  - Consultas y respuestas del cliente RAG (`RagProductClient`: query, tiempo de respuesta, cantidad de productos o refusal/error).
  - Operaciones sobre órdenes (`Order`: creación/edición de draft, líneas agregadas/removidas, transiciones de estado y reservas de stock).
- Almacenamiento de trazas por sesión en archivos dedicados bajo `logs/sessions/{session_id}.log` (formato estructurado / legible) y correlación en el logger global.
- Visor o endpoint de consulta en el Backoffice (Gradio) o interfaz de inspección para explorar sesiones pasadas y sus trazas.

### Out of Scope
- Modificación de la lógica de negocio de órdenes o transiciones de estado de `OrderEstado`.
- Modificación del algoritmo o del backend del RAG (`services/rag-api`).
- Reemplazo de la biblioteca de logging estándar de Python; se extiende mediante handlers/formatters/filtros.

## Capabilities

### New Capabilities
- `session-trace-logging`: Ciclo de vida del `session_id`, captura de trazas multi-servicio y persistencia en archivos de sesión individuales.

### Modified Capabilities
- `agent-orchestration`: Inyección y preservación de `session_id` al procesar "hola bob" y durante el ruteo de mensajes.
- `rag-product-query`: Emisión de eventos de traza estructurados en `query` y `price_lookup` asociados al `session_id` activo.
- `customer-order-persistence`: Emisión de eventos de traza en creación, mutación y transición de órdenes asociadas a la sesión.

## Approach

1. **Ciclo de vida y Estado**:
   - En `ConversationState`, agregar el campo `session_id: str | None = None`.
   - En `Orchestrator.handle_inbound`, cuando se detecta `is_session_reset` ("hola bob"), generar un nuevo `session_id` con formato `ses_{timestamp}_{short_id}` y asociarlo al nuevo estado.
2. **Propagación**:
   - Definir un `ContextVar` para `current_session_id`. En `handle_inbound` (en `src/pipeline.py`), activar el token de contexto de la sesión.
3. **Session Event Logger**:
   - Crear un helper centralizado para registrar eventos estructurados: `log_session_event(service: str, action: str, details: dict)`.
   - Escribir en un archivo dedicado por sesión: `logs/sessions/{session_id}.log` con timestamps ISO, nivel, servicio, acción y payload.
4. **Instrumentación de Servicios**:
   - `src/channels/telegram.py` / `src/pipeline.py`: registrar recepción y envío de mensajes.
   - `src/orchestrator/router.py`: registrar decisión de ruteo.
   - `src/integrations/rag.py`: registrar llamadas a RAG (queries, latencia, resultados).
   - `src/sourcing/draft_order.py` / `src/order_lifecycle/state.py`: registrar creación y actualización de órdenes.
5. **Visualización**:
   - Opcional pero recomendado: incorporar un visualizador de logs de sesión en el Backoffice Gradio para inspección rápida sin necesidad de acceder a la consola del servidor.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `src/orchestrator/session.py` | Modified | Agregar `session_id` a `ConversationState`. |
| `src/orchestrator/router.py` | Modified | Generar `session_id` al procesar `is_session_reset`. |
| `src/pipeline.py` | Modified | Activar contexto de sesión y registrar entrada/salida de mensajes. |
| `src/observability/` (nuevo módulo) | New | Gestor de logs por sesión, handlers y helpers. |
| `src/integrations/rag.py` | Modified | Registrar eventos de sesión en queries y lookups. |
| `src/sourcing/` / `src/order_lifecycle/` | Modified | Registrar eventos de sesión en mutaciones de órdenes. |
| `src/backoffice/` | Modified | Pestaña de visualización de trazas de sesión en Gradio. |
| `tests/` | New / Modified | Tests unitarios para generación de sesión, contextvar y registro de eventos. |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Concurrencia de múltiples sesiones de usuarios | Medium | ContextVar a nivel de tarea asincrónica y archivos de log nombrados por `session_id` único. |
| Overhead de I/O por escritura síncrona de logs | Low | Escrituras en append sobre archivos locales de sesión sin locking pesado. |
| Sesión previa a "hola bob" | Low | Si llega un mensaje sin sesión iniciada por "hola bob", se maneja con sesión anónima o se inicia trazabilidad básica hasta el reset. |

## Rollback Plan

Revertir los cambios en `src/pipeline.py`, `router.py` y `session.py`, y eliminar el módulo `src/observability/`. Las órdenes en base de datos y la funcionalidad de Telegram no se ven afectadas.

## Success Criteria

- [ ] Al recibir "Hola Bob" por Telegram, se inicia una nueva sesión con un `session_id` único y nuevo archivo de log.
- [ ] La traza de la sesión registra el mensaje recibido, la respuesta enviada y la decisión del orquestador.
- [ ] Las consultas al RAG se registran en la traza de la sesión correspondiente con sus parámetros, tiempo de respuesta y resultado.
- [ ] Las modificaciones sobre órdenes (creación de draft, agregado de items, confirmación) quedan registradas en la traza de la sesión.
- [ ] La suite de tests pasa sin regresiones (`make test`, `make lint`).
