# **Especificación Técnica y Plan de Implementación**

## **Sistema Multi-Agente de Inteligencia Artificial para Corretaje / Mayorista de Ferretería**

### **1\. Contexto y Diagnóstico del Negocio**

#### **1.1. Situación Actual**

* **Modo Operativo:** El dueño del negocio gestiona pedidos principalmente mientras está en la calle realizando repartos o trabajando dentro del depósito.  
* **Canal Dominante:** WhatsApp, utilizando notas de voz, fotos de remitos/hojas manuscritas, fotos de códigos de barras y mensajes breves.  
* **Cuellos de Botella:**  
  * Alta saturación en tareas operativas y repetitivas (transcripción de pedidos, revisión manual contra facturas de compra/stock, conteo físico e ingresos manuales de mercadería).  
  * Imposibilidad de escalar las ventas por falta de tiempo para atender nuevos clientes o realizar venta proactiva.  
  * Ambigüedad semántica severa en los pedidos (nombres informales de calle, marcas, medidas en pulgadas/milímetros, faltas de ortografía).  
* **Complejidad Comercial, Proveedores y Márgenes:** Gestión de costos provenientes de listas de proveedores con márgenes aplicados, combinada con listas de precios heterogéneas por cliente (gremio, mayoristas, descuentos particulares y patrones de compra repetitivos).  
* **Decisión e Inventario en Tránsito / Depósito (Hands-free & Barcode Mobile):** El dueño necesita enterarse de las ventas entrantes en su WhatsApp personal, consultar stock/precios por voz o escaneando un código de barras en el depósito, y aprobar/rechazar ventas sin depender de sentarse frente a una computadora.

#### **1.2. Decisión de Arquitectura: *Custom Python \+ OpenAI SDK* vs *n8n***

Tras evaluar y probar soluciones no-code/low-code en n8n, se identificaron limitaciones estructurales:

* **Gestión de Estado Asíncrono:** Manejar flujos conversacionales ramificados con intervención humana (*human-in-the-loop*) en n8n resulta frágil e ineficiente.  
* **Búsqueda Vectorial e Híbrida (RAG):** La resolución de ambigüedades requiere combinar algoritmos de distancia de cadenas (*Fuzzy*) con búsqueda semántica por *embeddings*, algo que requiere código desacoplado y directo.  
* **Reglas Comerciales Dinámicas:** Calcular precios finales combinando costos de proveedores \+ márgenes \+ listas gremio \+ descuentos por cliente y stock en tiempo real excede la capacidad lógica limpia de flujos visuales.  
* **Manejador de Eventos / Colas Backend (FastAPI Background Tasks):** Para garantizar un SLA de recepción en WhatsApp ![][image1], el procesamiento pesado (STT Whisper, Visión GPT-4o, RAG) se desacopla mediante FastAPI Background Tasks, evitando el bloqueo de webhooks y timeouts de reintento.

### **2\. Visión General de la Arquitectura del Sistema**

El sistema se estructura como un backend modular de **Agentes Especializados** coordinados mediante un orquestador, consumiendo datos estructurados (PostgreSQL / Google Sheets) y no estructurados (Base de Datos Vectorial de Catálogo y Perfiles de Clientes/Proveedores).

┌──────────────────────────────────────┐         ┌──────────────────────────────────────┐  
│         WhatsApp / Telegram          │         │           Interfaz Web               │  
│ (Audios/Fotos/Código Barras Cliente) │         │     (Gradio / Backoffice \+ Mobile)   │  
└──────────────────┬───────────────────┘         └──────────────────┬───────────────────┘  
                   │                                                │  
                   ▼                                                ▼  
┌───────────────────────────────────────────────────────────────────────────────────────┐  
│                            API Gateway / Webhook Handler                              │  
│                      (FastAPI \+ Background Tasks Asíncronos)                          │  
└──────────────────────────────────────────┬────────────────────────────────────────────┘  
                                           │  
                                           ▼  
┌───────────────────────────────────────────────────────────────────────────────────────┐  
│                         SISTEMA MULTI-AGENTE (OpenAI SDK)                             │  
│                                                                                       │  
│  1\. Perception Agent (STT Whisper / GPT-4o Vision / Barcode OCR Reader)              │  
│  2\. Customer & Context Agent (Identificación y Reglas de Cliente)                     │  
│  3\. Disambiguation Agent (Extractor \+ RAG Híbrido)                                    │  
│  4\. Inventory & Pricing Agent (Stock \+ Soft-Lock \+ Barcode Scanner \+ Costos)          │  
│  5\. Conversational Sales Agent (ACK Efímero \+ Cierre y Cotizaciones)                  │  
│  6\. Dispatch & Owner Assistant Agent (Control WhatsApp \+ Gestión de Depósito)         │  
└──────────────────────────────┬────────────────────────────────┬───────────────────────┘  
                               │                                │  
                               ▼                                ▼  
┌──────────────────────────────────────────────┐    ┌──────────────────────────────────┐  
│ Base de Datos (PostgreSQL)                   │    │  Google Sheets API /             │  
│ Vector DB (Qdrant / pgvector)                │    │  Sistema de Gestión              │  
└──────────────────────────────────────────────┘    └──────────────────────────────────┘  
                               ▲  
                               │ (Alertas, Consultas, Veto y Conteo por Código de Barras)  
                               ▼  
┌──────────────────────────────────────────────┐  
│             WhatsApp del Dueño               │  
│ (Alertas, Comandos Voz, Fotos de Barcode)    │  
└──────────────────────────────────────────────┘

### **3\. Especificación Detallada de Agentes de IA**

#### **Agente 1: Ingesta y Percepción Multimodal (*Perception Agent*)**

* **Objetivo:** Convertir cualquier entrada no estructurada (audios/fotos de clientes, remitos de papel, fotos de códigos de barras, listas PDF de proveedores) en datos estructurados normalizados.  
* **Herramientas (Tools):**  
  * transcribe\_audio(): Utiliza Whisper API de OpenAI para procesar audios .ogg/.mp3 de WhatsApp.  
  * extract\_text\_from\_image(): Utiliza GPT-4o Vision para extraer ítems y cantidades de fotos de papel manuscrito o remitos de compra.  
  * decode\_barcode\_image(): Reconoce y decodifica imágenes con códigos de barras (EAN-13, UPC, QR o códigos internos) enviadas como fotos desde WhatsApp o capturadas en la app web.  
  * parse\_supplier\_price\_list(): Procesa archivos PDF o planillas Excel de proveedores extrayendo código, descripción, precio de costo e IVA, guardando mapeos nuevos en proveedor\_sku\_mapping.  
* **Salida:** raw\_text, barcode\_value o structured\_json normalizado e identificador de origen.

#### **Agente 2: Contexto y Gestión de Clientes (*Customer & Context Agent*)**

* **Objetivo:** Identificar al cliente, recuperar su condición comercial, lista de precios asignada y detectar patrones de compra repetitiva.  
* **Herramientas (Tools):**  
  * get\_customer\_profile(phone\_number): Consulta en la base de datos la ficha del cliente, su lista de precios asignada, porcentaje de descuento base y condiciones de crédito/pago.  
  * get\_frequent\_orders(customer\_id): Recupera la plantilla de pedido habitual o histórico de compras recientes para facilitar reordenamientos automáticos ("el pedido de siempre").

#### **Agente 3: Extractor y Desambiguador de Pedidos (*Disambiguation Agent*)**

* **Objetivo:** Convertir el texto libre o entrada multimodal en una lista estructurada de productos identificados en el catálogo real.  
* **Herramientas (Tools):**  
  * parse\_order\_items(): Extrae entidades mediante *Structured Outputs* de OpenAI (Pydantic schema: {item\_mencionado, cantidad, unidad, atributos}).  
  * hybrid\_catalog\_search(): Búsqueda combinada (*Fuzzy* \+ *Embeddings* text-embedding-3-small).  
* **Lógica de Decisión y Micro-Copy Structured:**  
  * **Confianza ![][image2]:** Mapeo automático al SKU/ID oficial.  
  * **Confianza ![][image3] o Múltiples Coincidencias:** Generación de menú interactivo numerado enviado por WhatsApp (ej. *"1. Clavo Paris 2' / 2\. Clavo Espiralado 2'"*) permitiendo al usuario responder únicamente con el dígito 1 o 2\.

#### **Agente 4: Verificador de Inventario, Costos y Precios (*Inventory & Pricing Agent*)**

* **Objetivo:** Validar disponibilidad física, gestionar reservas temporales (*Soft-Lock*), calcular cotizaciones y ejecutar actualizaciones/consultas rápidas por código de barras.  
* **Herramientas (Tools):**  
  * check\_stock\_availability(sku, cantidad): Consulta el inventario disponible real considerando reservas activas (stock\_disponible \- sum(stock\_reservations)).  
  * reserve\_stock\_soft(sku, cantidad, customer\_id): Registra un bloqueo temporal en stock\_reservations con un TTL de 30 minutos mientras se aguarda la aprobación del dueño.  
  * get\_stock\_by\_barcode(barcode): Obtiene la ficha del producto, disponibilidad actual y ubicación en depósito mediante la lectura del código de barras.  
  * update\_stock\_by\_barcode(barcode, quantity\_change, reason): Incrementa o descuenta inventario registrando auditoría.  
  * calculate\_item\_price(sku, customer\_id): Aplica la matriz de precios:  
    ![][image4]![][image5]

#### **Agente 5: Comunicación y Ventas (*Conversational Sales Agent*)**

* **Objetivo:** Mantener el diálogo fluido con los clientes, cotizar con precisión, gestionar expectativas de tiempo y cerrar ventas.  
* **Herramientas (Tools):**  
  * send\_ephemeral\_ack(): Responde en ![][image6] recibida la entrada (*"Recibí tu pedido, ya te lo estoy cotizando..."*) para eliminar la ansiedad del cliente mientras corren los agentes pesados.  
  * send\_quotation\_summary(): Envía cotización clara con el desglose de precios aplicados según su lista/descuento.  
  * suggest\_substitutes(): Presenta alternativas de sustitutos para productos sin existencias suficientes.

#### **Agente 6: Despacho y Asistente Interactivo del Dueño (*Dispatch & Owner Assistant Agent*)**

* **Objetivo:** Gestionar la logística diaria y permitir al dueño controlar el negocio 100% desde su WhatsApp mientras maneja, reparte o trabaja en el depósito.  
* **Herramientas (Tools) del Asistente:**  
  * notify\_owner\_summary(order\_id): Envía ficha resumida con contexto completo al WhatsApp privado del dueño.  
  * query\_stock\_by\_voice\_text(query): Consulta disponibilidad e historial rápido para el dueño.  
  * approve\_and\_dispatch\_order(order\_id, custom\_adjustments): Cambia el estado a Aprobado, convierte la reserva *Soft-Lock* en descuento definitivo de stock, genera el renglón en Google Sheets y confirma al cliente.  
  * reject\_and\_release\_order(order\_id): Cancela la cotización y libera de inmediato las reservas temporales de inventario.

### **4\. Flujos de Trabajo Destacados & Reglas de Negocio**

#### **4.1. Flujo de Aprobación de Pedidos por WhatsApp (SLA & Feedback Efímero)**

\[ Cliente \]                        \[ Sistema Agentes \]                    \[ WhatsApp Dueño \]  
     │                                      │                                     │  
     ├─ Audios/Texto: "Necesito 10 ────►│                                     │  
     │  cajas de clavos 2' y 5 martillos"   │                                     │  
     │                                      ├─ ACK Efímero (\<5s) ─────────────────┤  
     │◄─ "Recibí tu pedido, procesando..." ─┤  "Procesando pedido..."             │  
     │                                      │                                     │  
     │                                      ├─ Transcribe, RAG, calcula precios, │  
     │                                      │  asigna Soft-Lock (30 min)          │  
     │                                      │                                     │  
     │                                      ├─ Notificación push al dueño ───────►│  
     │                                      │  "El Cóndor pidió 10 cajas clavos   │  
     │                                      │   y 5 martillos ($181.000).         │  
     │                                      │   ¿Aprobar ORD-302?"                │  
     │                                      │                                     │  
     │                                      │◄─ Audio del dueño: ─────────────────┤  
     │                                      │  "Dale, aprobá pero hacele un 5%    │  
     │                                      │   de descuento extra en clavos"     │  
     │                                      │                                     │  
     │◄─ Cotización ajustada y confirmación ├─ Actualiza Google Sheets / Stock    │  
     │   de despacho enviada al cliente.    │  y confirma acción al dueño ───────►│

#### **4.2. Flujo de Operación en Depósito por Código de Barras**

\[ Dueño en Depósito \]                     \[ Perception / Inventory Agent \]               \[ Base de Datos \]  
        │                                                │                                       │  
        ├─ Envía Foto de Barcode \+ Audio: ──────────────►│                                       │  
        │  "¿Cuántas cajas quedan de esto?"              ├─ Decode Barcode Image                 │  
        │                                                ├─ Query Stock by Barcode ─────────────►│  
        │                                                │◄─ Retorna SKU, Nombre, Cantidad ──────┤  
        │◄─ Responde WhatsApp/Voz: "Hay 150 cajas ───────┤                                       │  
        │   disponibles de Clavos Paris 2'".             │                                       │

#### **4.3. Política de Reserva Temporal de Inventario (*Soft-Lock*)**

* **Regla de Bloqueo:** Al generar una cotización enviada al cliente o puesta en espera de aprobación del dueño, el sistema ejecuta un *Soft-Lock* registrando los ítems en stock\_reservations con un Tiempo de Vida (![][image7]).  
* **Cálculo de Stock Disponible:**  
  ![][image8]  
* **Expiración y Rollback:** Si el pedido es rechazado o el dueño no aprueba dentro de la ventana de 30 minutos, la reserva expira automáticamente quedando el stock disponible para otros clientes.

### **5\. Esquema de Datos Completo**

#### **5.1. Tabla proveedores**

{  
  "proveedor\_id": "PROV-101",  
  "razon\_social": "Distribuidora Vulcano S.A.",  
  "contacto\_nombre": "Miguel Ángel",  
  "telefono": "+5491144443322",  
  "margen\_ganancia\_predeterminado\_pct": 35.0,  
  "condiciones\_compra": "Contado / 30 días"  
}

#### **5.2. Tabla proveedor\_sku\_mapping (Equivalencias de OCR/Visión)**

{  
  "mapping\_id": "MAP-901",  
  "proveedor\_id": "PROV-101",  
  "codigo\_proveedor": "VUL-CL-50MM",  
  "descripcion\_proveedor\_raw": "CLAVO PAR 2 PULG ZINC",  
  "sku\_interno\_mapeado": "SKU-1024",  
  "confianza\_mapeo": 0.98  
}

#### **5.3. Tabla clientes**

{  
  "customer\_id": "CLI-502",  
  "nombre\_comercial": "Ferretería El Cóndor",  
  "contacto\_nombre": "Roberto Gómez",  
  "telefono": "+5491155550199",  
  "direccion\_entrega": "Av. Rivadavia 4520, CABA",  
  "lista\_precios\_id": "LISTA\_GREMIO\_B",  
  "descuento\_general\_pct": 5.0  
}

#### **5.4. Tabla catalogo**

{  
  "id": "SKU-1024",  
  "codigo\_interno": "FERR-CL2",  
  "codigo\_barras": "7791234567890",  
  "proveedor\_id": "PROV-101",  
  "nombre\_oficial": "Clavos Paris 2 Pulgadas (50mm)",  
  "costo\_proveedor": 925.92,  
  "margen\_aplicado\_pct": 35.0,  
  "precio\_lista\_base": 1250.00,  
  "stock\_disponible": 150,  
  "sinonimos": \["clavo de dos pulgadas", "clavos 2pulg", "clavo paris 2"\]  
}

#### **5.5. Tabla stock\_reservations (*Soft-Lock*)**

{  
  "reservation\_id": "RES-882",  
  "sku": "SKU-1024",  
  "customer\_id": "CLI-502",  
  "order\_id": "ORD-302",  
  "cantidad\_reservada": 10,  
  "timestamp\_reserva": "2026-08-18T14:30:00Z",  
  "ttl\_minutes": 30,  
  "estado": "ACTIVA"  
}

### **6\. Diseño de la Interfaz de Control Dual (Gradio Web \+ WhatsApp Bot)**

* **Interfaz Web Gradio (Backoffice & Depósito Mobile):**  
  1. **Ingesta y Mapeo Asistido de Remitos:** Módulo para subir imágenes/PDFs de facturas de proveedores. Grilla interactiva que resalta en verde SKUs con mapeo automático (![][image2]) y en amarillo SKUs ambiguos con un menú desplegable para confirmación visual rápida antes del ingreso a stock.  
  2. **Catálogo, Márgenes y Depósito Mobile:** Gestión de precios, costos y escáner activable desde la cámara del teléfono celular.  
  3. **Clientes y Listas de Precios:** ABM de condiciones comerciales y descuentos.  
  4. **Monitor de Pedidos:** Tabla de control en tiempo real con estados de reserva *Soft-Lock* y sincronización a Google Sheets.  
* **Asistente de WhatsApp del Dueño:**  
  * Alertas push de nuevos pedidos con botones/comandos de voz directos.  
  * Búsqueda por voz e incremento/descuento de inventario en depósito escaneando código de barras.

### **7\. Plan de Implementación por Fases (MVP vs. Post-MVP)**

┌────────────────────────────────────────────────────────────────────────────────────────┐  
│                        BLOQUE I: CONSTRUCCIÓN Y LANZAMIENTO MVP                       │  
│ ┌──────────────────────┐      ┌──────────────────────┐      ┌──────────────────────┐   │  
│ │ Fase 1: Core Catálogo│ ───► │ Fase 2: Agentes Core │ ───► │ Fase 3: Integración  │   │  
│ │   & Ingesta Remitos  │      │  & Asistente Dueño   │      │    WhatsApp & E2E    │   │  
│ └──────────────────────┘      └──────────────────────┘      └──────────────────────┘   │  
│                                           │                                            │  
│                                           ▼                                            │  
│                       \[ HITO PRINCIPAL: RELEASE MVP PILOTO \]                           │  
└───────────────────────────────────────────┬────────────────────────────────────────────┘  
                                            │  
                                            ▼  
┌────────────────────────────────────────────────────────────────────────────────────────┐  
│                        BLOQUE II: ESCALAMIENTO Y EVOLUTIVAS                            │  
│ ┌────────────────────────────────────────────────────────────────────────────────────┐ │  
│ │ Fase 4 (Post-MVP): Autonomía, Pedidos Recurrentes y Hojas de Ruta                  │ │  
│ └────────────────────────────────────────────────────────────────────────────────────┘ │  
└────────────────────────────────────────────────────────────────────────────────────────┘

#### **7.1. Fase 1: Motor Core de Catálogo, Proveedores, Clientes y Búsqueda Híbrida (MVP \- Semanas 1 y 2\)**

* **Objetivo:** Establecer la persistencia relacional/vectorial, el esquema de dominios y el pipeline de resolución semántica e ingesta de compras.  
* **Entregables:**  
  * Base de datos en PostgreSQL con tablas estructuradas: proveedores, clientes, catalogo, stock\_reservations y proveedor\_sku\_mapping.  
  * Configuración de índice vectorial en pgvector/Qdrant con embeddings para sinónimos y terminología de ferretería.  
  * Módulo de Búsqueda Híbrida (*Fuzzy Matching* \+ *Vector Similarity*) con umbrales calibrados (![][image2] mapeo directo, ![][image3] desambiguación estructurada numerada).  
  * Ingesta asistida de remitos/facturas de proveedores con GPT-4o Vision y vista de mapeo/confirmación en Gradio.  
  * Funciones de consulta y ajuste de inventario por código de barras (decode\_barcode\_image, get\_stock\_by\_barcode, update\_stock\_by\_barcode).

#### **7.2. Fase 2: Agentes Core, Cotización Dinámica y Asistente Móvil del Dueño (MVP \- Semanas 2 y 3\)**

* **Objetivo:** Desarrollar la lógica de negocio comercial, control de concurrencia e interacción *Human-in-the-Loop*.  
* **Entregables:**  
  * Implementación de los agentes: *Perception*, *Customer & Context*, *Disambiguation*, *Inventory & Pricing* y *Dispatch & Owner Assistant*.  
  * Motor de cotización dinámica aplicando la fórmula por cliente:  
    ![][image9]![][image5]  
  * Mecanismo de reserva temporal de inventario (*Soft-Lock* con TTL de 30 minutos).  
  * Asistente de WhatsApp del Dueño para recepción de alertas de pedidos, consulta de existencias por voz/texto y aprobación/modificación en tránsito.  
  * Panel Web Backoffice en Gradio (pestañas de remitos, catálogo/stock, clientes y monitor de pedidos).

#### **7.3. Fase 3: Integración de Canales, Webhooks y Validación Piloto MVP (MVP \- Semanas 3 y 4\)**

* **Objetivo:** Conectar los puntos de contacto externos, automatizar el flujo extremo a extremo y validar los KPIs del negocio con clientes y órdenes reales.  
* **Entregables:**  
  * API Gateway en FastAPI con manejo asíncrono (*FastAPI Background Tasks*) y SLA conversacional con ACK efímero (![][image6]).  
  * Webhook bidireccional de WhatsApp/Telegram para recepción y envío de notas de voz (Whisper STT), imágenes y texto.  
  * Integración con Google Sheets API para el volcado automático de pedidos aprobados.  
  * **Hito de Salida (Milestone MVP \- Fin de Semana 4):** Despliegue del piloto operativo y evaluación contra las métricas de éxito (precisión semántica ![][image2], reducción del ![][image10] en carga de remitos y ahorro del ![][image11] del tiempo operativo del dueño).

#### **7.4. Fase 4: Autonomía, Pedidos Recurrentes y Hojas de Ruta (Post-MVP / Escalamiento)**

* **Objetivo:** Desarrollar automatizaciones operativas avanzadas sobre la base validada del MVP.  
* **Entregables:**  
  * Módulo predictivo de reconocimiento y sugerencia de compras periódicas ("el pedido habitual") en base al historial del cliente.  
  * Algoritmo de sugerencia automática de productos sustitutos directos ante quiebres de inventario.  
  * Generador automatizado de hojas de ruta de logística y reparto diario para cuando el dueño sale a la calle.  
  * Soporte avanzado para remitos y notas manuscritas complejas de baja legibilidad (V1.1).

[image1]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAC0AAAAZCAYAAACl8achAAABq0lEQVR4Xu2WvUvDQBiHWxVU8GPQLk2T9Eu6uDk4iIgoguIf4OAoKE4OKqKTi4qTq5uUCk6Kg+CgODmJ4NfsX+Dg4qzPWy4Q31aaVCkW8sBD0rtfcm9zd01jsYiIiHpo1Q3/jnw+3yO6rjuTTqcPON7qTCOJi4lEokt3+JFiRcdx1jme4oPONJLmKZqpbmfgBSyKFDOkMz9BdrdhRRcKhW6RAVexxOCTOhOEMEXLDLIHFo17sh+4fkfnKrAsq4/wNuEjkfNhnQlDmKLJbZKfF+VzLpezabvTuTKpVMoSCexT5KFt24M6Uy+m6EfdXg2yJ2SvxUwmM0UtHdQyqnNlmrJoOpdEwhdhNlkQpGju/aTbq8GDG6CGe+MnfuC4zn1DXggMsEGwyHFM1JmwmCf9rNurQXbCO89msw7XreFbMpnsF/3ZCmRaCC8bj7nZrM4EJUzR5G5kafqXJ20vzECn6M/Wok12MxeX+DJzYqzGfwnXvFzwDF/xHa+MWzrvwTiX3P9cNMtKfvZWdC4ITVm0R5wBp0VuOKI7/wLZU945a7qXQ4uvOyIiIuKXfAHCgX5UrfZnFgAAAABJRU5ErkJggg==>

[image2]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADsAAAAZCAYAAACPQVaOAAADR0lEQVR4Xu2Wy0tVQRzHj1oUvbDH7ZqPe64PkAoiMsqCih6LIMrKoLJ3UFZQi6SIWhnVol2lRJg9DDJcFSTUpoUIgq2saOV/UFtd9MA+X8/MdRy84aOE8n7hw5zfY35n5szMOScIMsooo8lQWVnZPJFMJiv9mKswDJcK3z+ZyoYs32mkmEjrSyQSy+GF4QiT6SoqKtoVRDUH6/IQZuJvoD0mbN8RReIhQeJKPzYRMbhb1G2Bu9S+IwJnIvh7BP5uk9cFrcLJuUd8vzD2O7jpcB2a4bXt81uVl5fPFXS4RNGntJv9nLGISZ4W1Dnp+C4LfHXWx70+CHwfib01sRzDoPB90njsmGgvsrKrbdz4mgoLCwtcX1pNqcm6Skb7/1xothOD2BmkP3MjSg/MPLSr1scANwj8bU7eK2HtkUS8gzo7hGzGc18vKyeuLX481WGcmiaS0aFv42Y1gffU08k8LPFd5zYej89WDVFcXLzR5hF7KbjMod1WUlKScMrYnAvUuS0qKiqmm/ygtLR0sdBD9ftMVNncrBo6BQNe4ye40qBEGL10BqCPQV0Rbh52t6EedpPXrlXyVwp/ndCqwjLj00up2W5f+uSxc/KF23esyqJQFYVbac8IttEMP8kVuUsMnZog7Rf4aah28lJn0dir4IcoKChYaP2+qHkATgjZ5O+FBnhoWO/3Ga3++8nm0KFGJKKP+eEgOsOjEoPoEPTfI5uHE+O63fA1iF542Ukj2y8/P39RGG37Ae551PpdcYTixFtcH3aveS/kCexGNz6itGIk15L8nLZKBGN/E+dqQmZSKWkiZjL9WjUmc8pODLvQ9NVAB31cn3X7WxF75H5msIuhz82h9mPXHqZwaBWf0G7x42MVNXqF++ZlgAsE/h7Z3KsiHPpbsr9927H7hbviVrgOJr3fQb2Ryf9mTNXJwn7m5gxTLBabI3z/eMWAKkUY/RDcYGLn4Y1wfwiINQly680n5jPtPuHW09YV6VaMfu+psQnWCuxrfk5KU2qyf0vJ6G9snbYz53WW8HMknUEmshXm+zGJOrWGPD8msZWLiD3gXo2C61w/JyX7NLUKo4HBr/Br/DOy21ifiNGgvyO/RkYZZZTRn9YvCAEsALcI28kAAAAASUVORK5CYII=>

[image3]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADsAAAAZCAYAAACPQVaOAAADI0lEQVR4Xu2XyUtVURzHr0rQAGXjszc/dVNBi16DBRVRCyGKJqgWlQUlBrVIaFwZ1aJWlVJhGmpQtCpQqE0LCQRbWdGq/6C2tmjAPt/3znmdjtNL5BF5v/Dh3d9wfme45xyvQRAqVKhSqLa2dr5Ip9N1fsxVKpVaIXx/KVUOZb7TSDExri+ZTK6Cp4YjTGYgkUjsDvI1c3VZhNn4W/k9JmzbkorB3WAQ3XCHQdwWgTMR/EMC/6DJG4Anwsm5S/ygMPZruO5wFTqg17aZLuVWMxKJzPMDrpjkKcEATji+8wJfs/UxgXcC33tir0yswpATvg+wTRj7HG92rY0bX3s8Ho+5vunQ/z9ZnQloTOW3Wncmk1nt57git0uQe9n6GOBmgf+Zk/dCWHssEe+nzk4hm0W5p8vKiWuLNxQaTEX2BqT4BTrqsStbjMg9bfiuc6udoEkKFmqLzSP2XPBYwe+O6urqpFPG5pylzk2RzWZnmfygpqZmmdCi+m2KVjQaXZLKH/pOwdtY5+dMJg1KmJ0wAsMM6qJw87AHDS2wh7w+vSX/TeFvFnqrsNL4dCl12O1LmyrGGhVu21FSA0HjWxS7bwtOVdRZbnijCfL7GX4a9jt5hbNo7DXwQ8RiscXW74uah+C4kE3+PmiFh4ZNfpuCZtRkadQkSOpVh378b0WtfkGtvbI5+0t57jN8CfK3ennayLYzR2hEsOBHrd8VZz5CvNv1YX8y90KVwG5z42OKC2IByZegy96efs5kom2lJmQmVZAmYibzVW+NyZy0E8OOm7YaaM7Hc5Pb3opYp/tnBjsDw24OtR+59oSi2BwanBEUekzH9X7ORNJKC/fmpeYigX9INrWzqd9fS/azr16LIdw3boXrcNr7HNSNTP43Y+a+A7B73JyipVuVDho0aQZ4QASjv2f/EPl1IpX/ILhmFu6lcD8IiLULcluS+T8xH50+CtLWFeO9Mdq9pcZW2CCwr/g5RWlGTdZRGZ3tEnS83g+OpXT+v5GN2s6c17nCz5F0Bqm7HRb6MYk6jYYqPyaxlRPEHtBXm+C50s8JFSpUqFD/in4B1kMbXPUHhf0AAAAASUVORK5CYII=>

[image4]: <data:image/png;base64,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>

[image5]: <data:image/png;base64,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>

[image6]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAC0AAAAZCAYAAACl8achAAACC0lEQVR4Xu2WTUhVQRiGjxmUUSnkXXh/z/2JuxEJXRhEiBRG4DZKaSkkrVwUSDs3Km0KBMFFKWJQFIFBEERI0C4CF9GuncugVWt9XpuB8bPrPS0sL5wXHs6c73tn5htmztwbRalSpUqiY44/qVH8/yqO45uiVCpt8XzD8xV8E7wPWf9hq01kMpnTNhGK4sYd32GzWCwuU+wFYb3/Qq1TdK1WO8HkE7AqKGLAekJR3C3HmM0dqur1+hlBkfdgjUKvWk8j+TOtoqvVaoHntcjtkvWG0g7iveOYh8fMO2t9+5TL5c5hnsG8ImgPWk8z0e+G4yPcZowpFv5W5PP5nPV7kX8gv9C7Fkzss/XtSgMJDA+ZYKlQKPRaz99IkwnGrPkYY792LIbeUBT7nPwHUS6XR6jlJLVctr5dtWTRJCeFtq/ZR5ZEjNMjstnsKR9j3EeC+FboDcUiz5P/4tiGXzBsfXvELXGW4qcxrvIcEtaTRCrM8TSILTh+ht5QLOqKb1cqlSLe+/CDxXeL0LtP2hbMdx3PGGzUeg4S/V8I+vb7GO1PjiehNxS5DR3N8HgS+8oOdIjQ20zH9TXTeS129y+xdmsKheeioM9LHTvdQrTfC967rN8L3zvy64L2XPz72puyviRqyaK92pj0umDASzbZQO36+rkJ+qIEPy76pnybM90ZHdV/hKlSpWpR7QAo/pm6HFdNTAAAAABJRU5ErkJggg==>

[image7]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAIAAAAAaCAYAAAB/w1TuAAAFFUlEQVR4Xu2Ya4hWRRjHz25Wds9w29jL+5y91OZGZi0VRVBYYZJiliJdMLXCygrpAtG36IOVXyq7aFkfMhAxTYpM0rKVqOhC90LRLpgFFhpGRJDa///O87jPjmf1fbFeLecPP845/3lmzpw5M2dmTpYlJSUl1VJ1PT09h0ZePRgUeUn/U6UOcKBJRBYqm8Aq8ArYoqwHL5VKpRU4/qhszfN8nXqrlW3gQ43tJTjfDEZG9zpLwr22W3koaxGY4uNqrDqCOk9AfZ4FT+B8LPFBnZ2dxxKkzwZzUOfp5vm4f1LaXitJR0dHa5y+z8JDDkHhb5OmpqahzuML2o6HnGmxOJ9HkL6mu7v7MPXGEcTuxHGExVKIW9be3n6K96jm5uYWxiP9JhKn11qoy5XK87isw/E88BfxnQDX7ylX8Vqf/X2S/UtfMJR9LviMoC1Lcfo+CwVPxUNeSpx3BV8QaWtrG+78Ncoo5z1CEPtLFkbSLmmDHuI99a9l2bkqTq+18OxzCeq0JQvPUI/zP5QFjEE1L8L5r8Tn5XMT3yb/KaHy4zP9BDrvMQmf782R/wDp6uo6xrxceyf8xeaZ0KgTY49C7DPgm9jfX+KahNgXEHU7U3QA2BdAn30t8Xlx/RVB3CzvmzCAzkH73AwmEXzGT8Tx+jwMvCEkC9PPxfQxnTQQzV4PfzL8maS1tbWDJs7Hwb+d4N7ng2HgVlxfQrKCQTegJHWAg7sDFAmFfZmHhdmiOM2LD4PYHQSxt8TpAwnxG8D82K9EyDce3Fgh07IwN1c0P7Phkecu8B0a8jpiaaUwRXxKfB5cf6Q8532ThPXEarTPOoLz2SjrNJaX65SK86fwcs/We39CsjDwBiH9XtE2BqNZJuIn4nyj8g64k2stHD8g1byL3YTMJ0mYn6eTON0L6ZMYS9DTu+L0IiFONM81cVolamlpOVnCyjiGo7YM6jUC9zkDsafH+fck5BuMfMMlvIjlxEYdzhdI38vZJQk7H7LQ+15Iu0fCLmkbXtRx6l0u2nZWT9c29Dpd/t+UcgdQ71WCztDrPO5O2MFWmFe10AhXswLsUUUreC/EzpOwddwUpw0k5JnK8tGwTQVpF8Yjb38p19U96vqWXnOh+znxcbj+WHnc+14SOlS/vCjvMtGdlnm2OyJIP9V80cWn9O8A3KqTOeYhz4ME3irzqhYyzwc/xH6RJPwjeIHEaQNJ4/vNoyb4SxsaGo4mcZoJneMOxD1cCWiMh/BZP5zE5Thx27dYmWEm7rOMwNvBNQ+Ot0lBZ8f19yR32+VYEjpAv+kD8aNEt5rmYdQ3i3YAMMx8nG9VdusAKOdR81DfWQT+G+ZVLUkdoKyDuQN8K7r33ZPcfHUDidMLVN5pIPYnVHJulMbFDj9de70v4gY3NjYeVQlY1R8Z54/FGNz3T+VJ80VX/KJzvn6ey3F4USfQQ12OF/1fwPawvLEkrAGqmgLQRt3mS/EUUF4DSPEU8KZ5exVu1IMMLyr8DcyH3GAjAExwsZMJ0pdImPcYa4ugJb7SXki7W/p+Ge8EX4j+XiYSfgnzrxu3MDUXn1Hp5UjWOn1NuOi0OKRNIfBX5mEBvNzy+vK8EDNDwpeyPIqR72XEj9U2+J3Aex3eGAkr+rIH3s3DV+I16VsEMp3bvqdFt+lgI+Lux/E+nis/S/gJl1SN8LKPwIu4wK/Ai8QvABp4JLfBcVpSUlJSUlJSUlJSUlJSUlJSUtIBqb8Bi8s9hp8fIegAAAAASUVORK5CYII=>

[image8]: <data:image/png;base64,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>

[image9]: <data:image/png;base64,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>

[image10]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACYAAAAZCAYAAABdEVzWAAACrElEQVR4Xu2VS4iNYRjHv4nkVkJnTp3rdy4lZ0VkQUKxsUCITC5lNi6pmdhIirIYjdsoNCK5JrFQysKCTGFBSbEQUWRhIWspfv857zPzzHecrKbO4vzr1/c9l/d5n/e87/eeKGqrRZTL5arFYrGU9Juy2exsUa1WU8nYuGo8GpuQdDTRP/PiON4iaOoUz+M8bxUKhZpwOTH+J6JUKqX9+DEql8szhIowZpDnZQptFz6P2A5B/CHPM9DHLzNLWA6xd4KxM1WTnPfYxwTvR+EI78+JbxO+foPUUGgqDq4O7EfCVspzAfYHgTlRPtJ7sW8K2ZVKpZP33yLUUe3hmElbR60r3tdUrdzYV8F+L3K+/kCXbIpdsAVYTj6fX4j9S6RSqek0Otkaq9Vqk6L6Aq9ZvoR9ka3Pel9TkXwv8J3imzWQ9yGRTqenhZwXxAaEjdMvjP+PoMmlwXdfsJCV+BfzftDNswG6zf6vKJoRFHkTJvoJ84Xl8P4RTgg/zhpj7Dr53Id0EvozmcxUuxqwr7t6c4V+ZfM1iOAyQeLjMLm245ugYE45vH/RRMLG+cb4hTaOVhwrag8Kjkoxqm/vWdgfuBuFM9uglmxM9w3BT4LBsXz6CLA/By7Jx/M1uaeFjWXLCtYYrDC/FyXXwi4hu1g/Ig9cvIeaS0ZHBJG0nsBt4f1xuMHhabDvwFXhcuZZY/bLeoVF67roCGi+A/66wC7BIbNHFFb9Q9gXKGF3CSbvk02xTdivhOXg24r9TJjPC/85Tex91NuN74azl2Pv9DkjIrA38JLJ9sX1v47hz943q5UK8s5DNwxxqeaFrycRWw17kn6OyRz8b/W1CuYYCGevUS3bmEl3DROv0tmJ3LlIiq+xoi3gIp6SjJmY/HDUfPyaOHyp0JOMt9VWW62ov+7YBSBcBw/6AAAAAElFTkSuQmCC>

[image11]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACYAAAAZCAYAAABdEVzWAAACjklEQVR4Xu2WzYtNcRjHzzCESEOX3Ldz3+qau6LLhhQlCwsUKUXKbJjFNF42kqLkJa+jEBthTJosKGVDeSmklA1JrOz9ASI+35nzmMfPmZGi7uJ+69P5Pd/n+f1+z7nnd043itpqEeXz+Vocx+XQN+VyubmiVqtlwtx/1T9prFAoVEXo/0GTQ0MqlUpbBE2d4XqC61CxWGwIV1PCfyTK5fJ8P/8XUfdSUPgumfAQHji6k7rtgvg+13NwnF9mjrC1yL0VNNJVqVRmU/Oe+IhgfBgOMX5OfpsY6yJFFH4QFN5h4jDXW9YU48eUTOLatDriTs2jdjfxTaG4Wq3OY/xVuLVHciY9Ota66r1x1ZKNaTEKjwrvs+CgIF9QTP4S8ZCwGs7lEuIvIpPJzKTRadZYo9GYSkkH4+s/F41G1r3Co895L1XNZnNKvV6fJcxjci+NbBLOe8HGA8I8xjrE3wVNrki8u4K5q/GXMd7v1tgIPRb/lZi4gMWepPgf4ZQwj2ay1hhzNsjTgRd4p+FkNpudYZ8G4htuvW6hX9m8CcWdHmPCntDH+6SNhHm+Mf/rhmLzy4JPQxyNPt7zsDfhdpSc2QnVqo11UviZBdaECfzXbH5WmMcjK1pjsMrXm2hoPewUiqlbDPdcvp81l4/NSFHylum8LAxzeMNwTThvkTXGGcr7eokNu+LRz0VHghrb5z8XxGU4YHGq2GiXNrFPhBeLbSb3SjhvK/Ez4WtN+Be0sfeSPQZdvJJ4h6/5TWzUR9E33WmYk3SngpqL0ANPdRNpN0JuLfSGPsekjv9Gb6ugsYHk7I2vlm1MhZyzpaEfSv9E9Aj4gk8PcyY2Pxgl5yoU89eVkjcV+sN8W2211Yr6AfAO6LS3jnOEAAAAAElFTkSuQmCC>