# **Documento de Definición de Producto: MVP (Minimum Viable Product)**

## **Sistema Multi-Agente de Inteligencia Artificial para Corretaje / Mayorista de Ferretería**

## **1\. Visión y Propósito del MVP**

### **1.1. Problema Central a Resolver**

El dueño del negocio invierte entre 4 y 6 horas diarias en tareas operativas repetitivas: escuchar audios de WhatsApp, descifrar qué producto busca el cliente, verificar precios/listas manualmente, consultar si hay stock antes de confirmar un pedido y **cargar manualmente las facturas/remitos de compra que le entregan sus proveedores para actualizar stock y costos**. Todo esto mientras maneja o hace repartos.

### **1.2. Propuesta de Valor del MVP**

Demostrar que un **sistema agéntico con IA** puede:

1. Recibir un pedido en WhatsApp (audio o texto), extraer e identificar los productos del catálogo con alta precisión.  
2. Calcular automáticamente el precio final considerando la condición, lista de precios y descuentos del cliente.  
3. Enviar una notificación resumida al WhatsApp del dueño para que este la **apruebe por voz o un clic** desde la calle.  
4. Impactar el pedido confirmado automáticamente en una planilla de Google Sheets para su despacho.  
5. **Automatizar la ingesta de facturas y remitos de compra** (imágenes/PDFs) mediante visión por computadora para actualizar inventario y costos sin tipeo manual.

## **2\. Alcance del MVP (In-Scope vs. Out-of-Scope)**

Para garantizar un lanzamiento rápido y controlado, el alcance se divide estrictamente en lo esencial frente a lo que se postergará para versiones posteriores:

┌────────────────────────────────────────────────────────────────────────────────────────┐  
│                                DENTRO DEL ALCANCE (IN-SCOPE)                           │  
├────────────────────────────────────────────────────────────────────────────────────────┤  
│ • Ingesta de mensajes de texto y notas de voz (.ogg/.mp3 de WhatsApp) de clientes.     │  
│ • Búsqueda Híbrida de productos (Fuzzy \+ Vectorial) sobre catálogo base.               │  
│ • Gestión de clientes básica: teléfono, lista de precios asignada y descuento general. │  
│ • Cálculo dinámico de precio: (Costo \* Margen) \* Descuento Cliente.                    │  
│ • Ingesta Inteligente de Remitos / Facturas de Proveedores (Fotos / PDF) vía Web o Chat│  
│   con extracción automática de ítems, cantidades, costos y actualización de stock.      │  
│ • Interacción con el Dueño vía WhatsApp: Alerta de pedido \+ aprobación por voz/texto. │  
│ • Registro automático de la orden aprobada en Google Sheets (Hoja de Pedidos).         │  
│ • Panel Web liviano (Gradio) para administración de catálogo, clientes, stock y remitos.│  
│ • Consulta y actualización rápida de stock por foto de código de barras desde WhatsApp.│  
└────────────────────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────────────────────┐  
│                              FUERA DEL ALCANCE (OUT-OF-SCOPE)                          │  
├────────────────────────────────────────────────────────────────────────────────────────┤  
│ • Interpretación de fotos manuscritas extremadamente ilegibles (se deja para V1.1).    │  
│ • Integración directa y bidireccional con ERPs / Sistemas contables legados AFIP.      │  
│ • Generación de facturas electrónicas oficiales o remitos fiscales afip automáticos.   │  
│ • Pasarelas de pago automatizadas / Cobro integrado por chat.                          │  
└────────────────────────────────────────────────────────────────────────────────────────┘

## **3\. Historias de Usuario Principales (User Journeys)**

### **Historia 1: El Cliente realiza un pedido no estructurado por WhatsApp**

* **Como** cliente mayorista/ferretero,  
* **Quiero** enviar una nota de voz diciendo *"Mándame 10 cajas de clavos de 2 pulgadas y 5 martillos"*,  
* **Para** no perder tiempo redactando un pedido formal ni buscando códigos en una web.  
* **Criterio de Aceptación:** El sistema transcribe el audio, asocia cada ítem al catálogo real, calcula los precios según la lista asignada al cliente y genera la cotización preliminar.

### **Historia 2: El Dueño aprueba el pedido desde la calle (Human-in-the-Loop)**

* **Como** dueño del negocio mientras reparto mercadería,  
* **Quiero** recibir un mensaje en mi WhatsApp personal que me diga *"Ferretería El Cóndor pidió: 10 cajas Clavos 2' ($10.600 c/u) y 5 Martillos ($15.000 c/u). Total: $181.000. Hay stock. ¿Aprobar ORD-102?"*,  
* **Para** responder con un audio corto *"Sí, aprobá"* o *"Cambiale el descuento a 10%"* sin tener que estacionar ni usar una laptop.  
* **Criterio de Aceptación:** El sistema procesa la respuesta del dueño, aplica cualquier ajuste solicitado, confirma el pedido al cliente y lo asienta en Google Sheets.

### **Historia 3: El Dueño procesa un remito/factura de compra de proveedor (Ingesta Automática)**

* **Como** dueño del negocio,  
* **Quiero** sacar una foto o subir el archivo PDF del remito/factura enviado por el proveedor (vía WhatsApp o Panel Web),  
* **Para** que la IA extraiga los productos, cantidades recibidas y precios de costo, actualizando el stock e incrementando la base de datos sin cargarlos renglón por renglón.  
* **Criterio de Aceptación:** El *Perception Agent (Vision)* procesa el documento, mapea los ítems a los productos existentes (o sugiere crear nuevos SKUs), calcula los nuevos costos y actualiza el inventario previa confirmación simple del usuario.

### **Historia 4: El Dueño consulta/actualiza stock por código de barras en depósito**

* **Como** dueño en el depósito,  
* **Quiero** tomar una foto a la caja del producto con la cámara de WhatsApp y preguntar *"¿Cuánto stock me queda de esto?"* o enviar *"Sumá 50 cajas que llegaron"*,  
* **Para** obtener respuesta o actualización inmediata sin buscar manualmente en fichas de papel.  
* **Criterio de Aceptación:** El sistema decodifica el código de barras, efectúa la consulta o el ajuste en la base de datos y responde por chat.

## **4\. Arquitectura de Producto del MVP**

\[ Cliente \] ────► (WhatsApp) ────► \[ Perception Agent \] ──► Transcripción STT  
                                          │  
                                          ▼  
                                \[ Disambiguation Agent \] ──► Identificación SKU (Fuzzy \+ Embedding)  
                                          │  
                                          ▼  
                                \[ Inventory & Price \] ──► Cotización según Cliente \+ Stock  
                                          │  
                                          ▼  
                                \[ Assistant Dueño \] ──► Notificación Push WhatsApp al Dueño  
                                          │  
                          ┌───────────────┴───────────────┐  
                          ▼                               ▼  
                 \[ Dueño Aprueba \]                \[ Dueño Ajusta / Rechaza \]  
                          │                               │  
                          ▼                               ▼  
                \[ Google Sheets API \] ◄─────── \[ Conversational Agent \]  
             (Registro de Pedido)            (Confirmación al Cliente)

\[ Proveedor \] ──► (Remito / Factura PDF/Foto)  
                        │  
                        ▼  
            \[ Vision OCR Agent \] ──► Extracción de Ítems / Costos / Cantidades  
                        │  
                        ▼  
            \[ Actualización Inventario / Catálogo \]

## **5\. Módulo Administrador del MVP (Panel Web Backoffice)**

El MVP contará con una interfaz web simple e intuitiva en **Gradio**, organizada en 4 secciones funcionales:

1. **Ingesta Inteligente de Remitos y Facturas (Nuevo):**  
   * Módulo para arrastrar imágenes o archivos PDF de facturas de compra de proveedores.  
   * Grilla interactiva de pre-visualización extraída por IA (Código, Descripción, Cantidad, Precio Costo Proveedor) con botón *"Confirmar e Ingresar a Inventario"*.  
2. **Gestión de Catálogo e Inventario:**  
   * Lista de productos con: SKU, Código de Barras, Descripción, Costo Proveedor, Margen de Ganancia, Precio de Lista y Stock Actual.  
   * Buscador de productos y editor rápido de stock/precios.  
3. **Gestión de Clientes y Listas de Precios:**  
   * Tabla de clientes con Teléfono WhatsApp, Nombre Comercial, Lista de Precios Asignada (Gremio A, Gremio B, Base) y Descuento Particular (%).  
4. **Monitor de Pedidos y Hoja de Ruta:**  
   * Vista en vivo de pedidos entrantes, su estado (Pendiente Aprobación, Aprobado, En Despacho, Rechazado) y sincronización con Google Sheets.

## **6\. Muestreo de Métricas de Éxito del MVP (KPIs)**

Para evaluar la viabilidad comercial y operativa del MVP tras 30 días de prueba, se medirán los siguientes indicadores:

| **Métrica / KPI** | **Meta esperada en MVP** |

| **Precisión de Identificación de Productos (STT \+ RAG):** | ![][image1] de coincidencia correcta sobre audios/textos de clientes. |

| **Tiempo Ahorrado en Carga de Remitos/Facturas:** | Reducción del **80%** del tiempo de carga manual de compras de proveedores. |

| **Tiempo de Respuesta al Cliente:** | Reducción a menos de **3 minutos** para recibir cotización. |

| **Ahorro de Tiempo Operativo General:** | Reducción del **70%** del tiempo operativo diario del dueño. |

| **Tasa de Adopción de Aprobación por Voz:** | El dueño aprueba ![][image2] de los pedidos directamente por respuesta de audio en WhatsApp. |

## **7\. Hoja de Ruta de Definición de Producto (Línea de Tiempo)**

\[ Semana 1: Modelo de Datos, Catálogo & OCR Remitos \]  
   └── Carga de Catálogo Base, Clientes piloto e Ingesta con GPT-4o Vision para Facturas/Remitos.

\[ Semana 2: Agentes Core & Búsqueda Híbrida \]  
   └── Configuración de Prompts, Búsqueda Híbrida de productos y motor de precios.

\[ Semana 3: Integración WhatsApp & Flujo Dueño \]  
   └── Webhooks de WhatsApp, recepción de audios, alertas al dueño y escaneo de código de barras.

\[ Semana 4: Pruebas Piloto & Lanzamiento MVP \]  
   └── Pruebas reales de ciclo completo (Ingreso Remito ──► Pedido Cliente ──► Aprobación Dueño ──► Google Sheets).

[image1]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADsAAAAZCAYAAACPQVaOAAADR0lEQVR4Xu2Wy0tVQRzHj1oUvbDH7ZqPe64PkAoiMsqCih6LIMrKoLJ3UFZQi6SIWhnVol2lRJg9DDJcFSTUpoUIgq2saOV/UFtd9MA+X8/MdRy84aOE8n7hw5zfY35n5szMOScIMsooo8lQWVnZPJFMJiv9mKswDJcK3z+ZyoYs32mkmEjrSyQSy+GF4QiT6SoqKtoVRDUH6/IQZuJvoD0mbN8RReIhQeJKPzYRMbhb1G2Bu9S+IwJnIvh7BP5uk9cFrcLJuUd8vzD2O7jpcB2a4bXt81uVl5fPFXS4RNGntJv9nLGISZ4W1Dnp+C4LfHXWx70+CHwfib01sRzDoPB90njsmGgvsrKrbdz4mgoLCwtcX1pNqcm6Skb7/1xothOD2BmkP3MjSg/MPLSr1scANwj8bU7eK2HtkUS8gzo7hGzGc18vKyeuLX481WGcmiaS0aFv42Y1gffU08k8LPFd5zYej89WDVFcXLzR5hF7KbjMod1WUlKScMrYnAvUuS0qKiqmm/ygtLR0sdBD9ftMVNncrBo6BQNe4ye40qBEGL10BqCPQV0Rbh52t6EedpPXrlXyVwp/ndCqwjLj00up2W5f+uSxc/KF23esyqJQFYVbac8IttEMP8kVuUsMnZog7Rf4aah28lJn0dir4IcoKChYaP2+qHkATgjZ5O+FBnhoWO/3Ga3++8nm0KFGJKKP+eEgOsOjEoPoEPTfI5uHE+O63fA1iF542Ukj2y8/P39RGG37Ae551PpdcYTixFtcH3aveS/kCexGNz6itGIk15L8nLZKBGN/E+dqQmZSKWkiZjL9WjUmc8pODLvQ9NVAB31cn3X7WxF75H5msIuhz82h9mPXHqZwaBWf0G7x42MVNXqF++ZlgAsE/h7Z3KsiHPpbsr9927H7hbviVrgOJr3fQb2Ryf9mTNXJwn7m5gxTLBabI3z/eMWAKkUY/RDcYGLn4Y1wfwiINQly680n5jPtPuHW09YV6VaMfu+psQnWCuxrfk5KU2qyf0vJ6G9snbYz53WW8HMknUEmshXm+zGJOrWGPD8msZWLiD3gXo2C61w/JyX7NLUKo4HBr/Br/DOy21ifiNGgvyO/RkYZZZTRn9YvCAEsALcI28kAAAAASUVORK5CYII=>

[image2]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADsAAAAZCAYAAACPQVaOAAADTElEQVR4Xu2XSWgUQRSGO45LcEccJ5klPTMJBkdQcUTFiwvexAUX1GgwXoyIKES8mJMbikoOmiAS404inhT0IIoHCXgIgmJQBG+KHjzkpIgg8f+7X828FEmcGTWg6R8+qt+r92qqul9V9zhOoECBRkI1NTVTSTKZXGr3abmuO4fY/pFUKJvNjrOdgyhkO6iqqqq54LZQj8U8SyQS69BVJji4CeXwt6LdRQaOYAmBOwgCF9h9pYiLI5jcQYx5HdzB+CeIjkulUvMJ/I/ABdCGnCwxMfQjfysR+wk4qTgGOsD9/MjDqLa2dgpBwmFODu1KO6YYIf+c8ED5HhOM30A7k8mMh/1R8EovnU7PxvUHwrKVvF7Ox8wJbROe7CIzrvja4/F4TPuG1KharJbU/z7QRVBSax3ZGwVqDPK+CseNE+OeJhyTNsbdguvPJJ/qTfwTMWWL9insNUTyLpobIf0s8QZjl6qxJOlveu65Osc/RAY9SIyi0ehMxPYLR4wf183CG9qY9Clcvyb5bC/uldAicQdwfYbIOXCX/urq6lmElajz/4T4tDaBboJDZbEdoIWYPiF3IOG6VfiBGzIRbTt4Sazc50QvAvYhwqcKMuLjodRhyhfxFSjvKDF5pagMA63HwF1o9xKU0QQ7SAuxTUIPzBD24jRcvxX65Xy4MdxiQaf2a2EO28BuQhuxG13/Rl4Wltk5heq/X2wICXVEXuY7HX8PFyXk7eGiMKmjaM8KfexD2+L6J22vzoH9Qjiv/UbYQhGOqX2w30UikUksZQK7TfcPKj4xBDciuJNPkzjFncSesI9mEIxTaXxY+FUC3y3arBBXTt58pjfx98J+7TeC/4p+zcBOgS86hr+j7QFy80/xGtpVdn+xcv0SJd20ZeHmnbpQYirBN4KbHFZx3msrFovF9ZgUbtD2pPU5yBMZ8d/F9D4hYd/UMQMUDocnE9tfqkxVgHvcV/jxXn7PyjdtTri59cT1PwF5sx8ifgPRcSxdMtQTQ24PcpaDJQR2sx2T06ha7N8SyxM/vgKU231aLF8sZDUXZPdRyG8UKuw+CqWcQN8lLLKN4Hq6HZMTfmgzceWfya/ApObZY/wzMmXMJ1EIBf4/DRQoUKDf0k+zVTqbEFmu0gAAAABJRU5ErkJggg==>