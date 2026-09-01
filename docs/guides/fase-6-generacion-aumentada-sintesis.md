# Documento Técnico: Generación Aumentada (Prompt Engineering, LLM y Control de Alucinaciones) (Fase 6)

Esta nota técnica establece los principios arquitectónicos, la descripción funcional de componentes, los fundamentos teóricos y las directivas de implementación para la **Fase 6: Generación Aumentada (Prompt Engineering, LLM y Control de Alucinaciones)** dentro del pipeline RAG (*Retrieval-Augmented Generation*).

---

## 1. Introducción a la Fase 6: El Momento de la Verdad en RAG

Para entender el rol crucial de la Fase 6, analicemos el recorrido del sistema hasta este punto:

* **Lo que hicieron las Fases 0 a 5:** 
  * La **Fase 0** extrajo y limpió las tablas y textos del catálogo técnico.
  * La **Fase 1** segmentó el texto en unidades lógicas con sentido completo.
  * Las **Fases 2 y 3** convirtieron esos textos en vectores e indexaron la base de datos (HNSW en pgvector).
  * La **Fase 4** ejecutó una búsqueda híbrida amplia (vectorial + BM25) para capturar los candidatos más prometedores.
  * La **Fase 5** actuó como embudo de precisión: aplicó un reordenador semántico (*Cross-Encoder*), filtró el ruido irrelevante y entregó una selección hiper-enfocada de **3 a 5 fragmentos finales**.
* **El peligro en la Fase 6:** Aquí entra en juego el Generador (LLM). Si el modelo generativo se invoca sin restricciones ni estructura:
  1. **Alucinaciones de catálogo:** Si el usuario consulta por un producto o medida inexistente, el modelo tenderá a inventar códigos alfanuméricos creíbles pero falsos.
  2. **Sesgo de complacencia (*Sycophancy*):** El modelo intentará adivinar o asumir lo que el usuario quiere escuchar en lugar de ceñirse estrictamente a los documentos.
  3. **Pérdida de trazabilidad:** Sin un mecanismo de citación estricto, el usuario no sabrá de qué página o fragmento provino la respuesta técnica.
* **La misión de la Fase 6:** Ensamblar el contexto depurado en un contenedor delimitado de forma inequívoca, aplicar directivas negativas estrictas en el *System Prompt*, configurar hiperparámetros de inferencia determinista (temperatura cero) y obligar al modelo a citar atómicamente cada afirmación, produciendo una respuesta final factual y verificable.

```
                  [Entrada Fase 6: 3 a 5 Chunks de la Fase 5]
                                       │
                                       ▼
                 ┌───────────────────────────────────────────┐
                 │    1. Empaquetador de Contexto en XML     │
                 │   (Delimitación limpia <fragmento id="N">)│
                 └─────────────────────┬─────────────────────┘
                                       │
                                       ▼
                 ┌───────────────────────────────────────────┐
                 │    2. System Prompt y Reglas Negativas    │
                 │   (Directiva de rechazo, códigos y citas) │
                 └─────────────────────┬─────────────────────┘
                                       │
                                       ▼
                 ┌───────────────────────────────────────────┐
                 │    3. Inferencia Determinista (Temp = 0)  │
                 │   (Greedy decoding sin desvíos aleatorios)│
                 └─────────────────────┬─────────────────────┘
                                       │
                                       ▼
                 ┌───────────────────────────────────────────┐
                 │    4. Validación y Formateo de Citas      │
                 │   (Verificación sintáctica [Fragmento N]) │
                 └─────────────────────┬─────────────────────┘
                                       │
                                       ▼
                 [Salida: Respuesta Factual Grounded hacia Fase 7]
```

---

## 2. Descripción de Componentes y sus Funciones Operativas

El procesamiento de la Fase 6 se organiza en cuatro componentes modulares encadenados:

### Componente 1: Empaquetador de Contexto Estructurado (*Context Packaging*)
* **Función:** Recibe los 3 a 5 fragmentos finalistas provenientes de la Fase 5 y los formatea dentro de delimitadores XML explícitos.
* **Por qué es necesario:** Si el contexto se inyecta como texto plano sin fronteras claras, el LLM puede confundir las instrucciones del sistema con el contenido de los documentos, o volverse vulnerable a inyecciones de prompt accidentales.
* **Estructura:** Cada bloque se envuelve con etiquetas claras que contienen sus metadatos de trazabilidad:
  ```xml
  <contexto_conocimiento>
    <fragmento id="1" pagina="2" marca="CARBIZ">
      Categoría: ABRAZADERAS DE ACERO
      Producto: Americana a Cremallera
      | Código | Articulo | Apertura | U/Vta | Paq x |
      | 100001 | 00       | 9/13 mm  | c/u   | 10    |
    </fragmento>
    <fragmento id="2" pagina="2" marca="CARBIZ">
      ...
    </fragmento>
  </contexto_conocimiento>
  ```

### Componente 2: Diseñador del *System Prompt* y Reglas de Control
* **Función:** Define la identidad operativa del asistente y establece directivas rígidas de comportamiento. No se limita a decir "sé servicial", sino que impone restricciones legales y lógicas:
  1. **Directiva de Ausencia / Rechazo Honesto:** Si la respuesta exacta no está en el contexto provisto, el modelo tiene terminantemente prohibido suponer. Debe emitir una respuesta de rechazo explícita y uniforme (ej. *"No cuento con información factual en las fuentes proporcionadas para responder a esta consulta"*).
  2. **Directiva de No-Extrapolación de Códigos de Catálogo:** En un entorno industrial o ferretero, los códigos alfanuméricos (como `100001`) y dimensiones (`9/13 mm`) deben ser literales. Queda prohibido inventar o aproximar códigos.
  3. **Directiva de Citación Atómica:** Cada dato técnico declarado debe culminar con su cita de origen con formato `[Fragmento N]`.

### Componente 3: Motor de Inferencia Determinista
* **Función:** Invoca la API del LLM configurando hiperparámetros estrictos orientados a la máxima precisión:
  * **Temperatura en 0.0:** Cancela la aleatoriedad en el muestreo de tokens (*Greedy Decoding*). El modelo siempre seleccionará el token de mayor probabilidad condicional, garantizando consistencia y respuestas reproducibles.
  * **Control de `max_tokens`:** Delimita el tamaño máximo de respuesta para evitar divagaciones y optimizar la latencia (*Time-to-First-Token*).

### Componente 4: Verificador y Salida Estructurada (*Post-Generation Verification*)
* **Función:** Analiza la respuesta generada antes de entregarla al usuario o a la Fase 7 (Evaluación).
* **Acciones:** 
  * Comprueba mediante expresiones regulares que todas las afirmaciones técnicas incluyan citas válidas (ej. `[Fragmento 1]`, `[Fragmento 2]`).
  * Valida que los identificadores de citas invocados por el LLM correspondan efectivamente a fragmentos presentes en el contexto inyectado (detección de citas inventadas).
  * Permite emitir la respuesta en **Markdown conversacional** o en un esquema **JSON tipado** (*Structured Outputs*).

---

## 3. Fundamentos Teóricos Detrás de Cada Elección

### A. Anatomía de una Alucinación en LLMs: ¿Por qué Ocurre?

Un Modelo de Lenguaje Masivo (LLM) no es una base de datos relacional; es una red neuronal entrenada para predecir el próximo token más probable en función de la distribución estadística del lenguaje.

```
Entrada del Usuario: "¿Cuál es el código del tornillo de titanio M10?"
(Dato NO existente en los fragmentos provistos)

Comportamiento sin Guardrails:
Memoria Paramétrica ──► Busca patrones lingüísticos ──► Genera código inventado: "T-992-M10" (Alucinación)

Comportamiento con Directiva de Ausencia (Fase 6):
Regla del Sistema ──► Detecta ausencia en <contexto_conocimiento> ──► "No cuento con información factual..."
```

1. **La memoria paramétrica vs. el contexto de trabajo:**
   * La **memoria paramétrica** son los conocimientos difusos aprendidos durante el pre-entrenamiento con miles de millones de textos de internet.
   * El **contexto de trabajo** es la información inyectada en la ventana de atención (nuestros fragmentos de la Fase 5).
   * Cuando no existe una directiva negativa estricta, ante una laguna en el contexto de trabajo, el LLM recurre instintivamente a su memoria paramétrica para rellenar el vacío y complacer al usuario (*sycophancy*).
2. **Cómo frenar la alucinación:**
   * La inyección de reglas negativas explícitas cambia la función objetivo percibida por el modelo: premiar la honestidad de decir *"no lo sé"* por encima de la creatividad de intentar adivinar.

---

### B. Mitigación del Fenómeno *"Lost in the Middle"* y Posicionamiento de la Consulta

El estudio de referencia de Liu et al. (Stanford University, 2023), *"Lost in the Middle: How Language Models Use Long Contexts"*, demostró que los LLMs presentan un sesgo posicional en forma de U: prestan máxima atención a los tokens ubicados al principio y al final del prompt, pero pierden capacidad de razonamiento sobre los datos ubicados en el centro.

Además, el trabajo analizó el impacto de la **contextualización consciente de la consulta (*Query-Aware Contextualization*)**:

```
Estructura Tradicional (Débil):
[System Prompt] ──► [Fragmentos 1 al 5] ──► [Consulta del Usuario al final]

Estructura Optimizada Query-Aware (Fase 6):
┌─────────────────────────────────────────────────────────────┐
│ 1. Consulta del Usuario y Meta-Instrucción Inicial          │ ◄── Zona de Primacía (Alta atención)
├─────────────────────────────────────────────────────────────┤
│ 2. <contexto_conocimiento>                                  │
│    Fragmentos ordenados estratégicamente en U por Fase 5    │ ◄── Zona Central
├─────────────────────────────────────────────────────────────┤
│ 3. Recordatorio de Reglas y Consulta del Usuario Reiterada  │ ◄── Zona de Recencia (Máxima atención)
└─────────────────────────────────────────────────────────────┘
```

Al situar la pregunta y la orden de búsqueda tanto al inicio como inmediatamente antes de que el modelo comience a generar la respuesta (zona de recencia inmediata), el modelo enfoca su atención cruzada en los tokens relevantes desde el primer token emitido.

---

### C. La Importancia de las Citaciones Atómicas

En un sistema RAG empresarial, una respuesta sin fuentes verificables destruye la confianza operativa del usuario.
* **Citación Atómica:** Exige que la cita no se coloque como una bibliografía genérica al pie del texto, sino al final de cada afirmación puntual:
  * *Incorrecto:* "Las abrazaderas son de acero con apertura de 9/13 mm y vienen en paquetes de 10. Fuente: Fragmento 1."
  * *Correcto:* "La abrazadera Americana a Cremallera posee fleje de 13 mm de ancho y apertura de 9/13 mm [Fragmento 1]. Se comercializa por unidad en paquetes de 10 unidades bajo el código 100001 [Fragmento 1]."
* **Beneficio para la Fase 7 (Evaluación):** Este formato permite que el evaluador automatizado (*LLM-as-a-Judge*) calcule de forma exacta la métrica de **Fidelidad (*Faithfulness*)**, descomponiendo cada oración y corroborando si el fragmento citado respalda el 100% de la afirmación.

---

## 4. Plantilla de Producción: Ensamblado del Prompt

A continuación se detalla la plantilla canónica utilizada por el pipeline:

```text
=== SYSTEM PROMPT ===
Eres un Asistente Técnico y Especialista en Catálogos Industriales de alta precisión. Tu único objetivo es responder a las preguntas de los usuarios utilizando EXCLUSIVAMENTE los datos provistos en el bloque de contexto.

REGLAS OBLIGATORIAS:
1. DIRECTIVA DE AUSENCIA: Si la respuesta a la consulta no se encuentra de forma explícita y literal en el bloque de contexto, responde ÚNICAMENTE: "No cuento con información factual en las fuentes de conocimiento disponibles para responder a esta consulta." No intentes deducir, extrapolar ni utilizar conocimientos externos.
2. INTEGRIDAD DE CÓDIGOS Y NÚMEROS: No alteres, redondees ni inventes códigos de parte, dimensiones, unidades de venta o empaques. Cópialos exactamente como aparecen.
3. CITACIÓN ATÓMICA OBLIGATORIA: Cada oración o afirmación técnica que formules DEBE finalizar con su correspondiente etiqueta de citación en formato [Fragmento N], indicando el identificador del fragmento exacto donde se encuentra el dato.
4. TONO: Profesional, técnico, directo y conciso. Evita introducciones innecesarias o frases como "Según los documentos...". Comienza directamente con la respuesta.

=== USER PROMPT (Query-Aware Template) ===
CONSULTA A RESPONDER:
{pregunta_usuario}

CONTEXTO DE CONOCIMIENTO AUTORIZADO:
<contexto_conocimiento>
{bloque_xml_fragmentos}
</contexto_conocimiento>

INSTRUCCIONES FINALES:
Responde a la consulta "{pregunta_usuario}" basándote estrictamente en los fragmentos de conocimiento anteriores. Recuerda incluir la cita [Fragmento N] en cada afirmación. Si la información no está presente, rechaza la consulta según la Directiva de Ausencia.
```

---

## 5. Salida Estructurada Alternativa (JSON Schema para APIs)

Cuando el sistema RAG interactúa con sistemas transaccionales (ERP, CRM o carritos de compra), la respuesta debe ser estructurada y tipada:

```json
{
  "tipo": "object",
  "propiedades": {
    "respuesta_narrativa": {
      "type": "string",
      "description": "Explicación en texto natural dirigida al usuario con citas [Fragmento N]"
    },
    "consulta_respondida": {
      "type": "boolean",
      "description": "true si el contexto contenía la información, false si aplicó la directiva de ausencia"
    },
    "productos": {
      "type": "array",
      "items": {
        "type": "object",
        "propiedades": {
          "codigo": { "type": "string" },
          "marca": { "type": "string" },
          "nombre": { "type": "string" },
          "medidas": { "type": "string" },
          "fragmento_id": { "type": "integer" }
        },
        "required": ["codigo", "nombre", "fragmento_id"]
      }
    }
  },
  "required": ["respuesta_narrativa", "consulta_respondida", "productos"]
}
```

---

## 6. Resumen de Parámetros de Inferencia Recomendados

| Parámetro | Valor Óptimo | Justificación Técnica |
|---|---|---|
| **Temperatura** | `0.0` | Inferencia determinista (*Greedy Decoding*). Evita variaciones aleatorias y previene alucinaciones. |
| **Top_P** | `1.0` (o inactivo) | Al fijar temperatura en 0, la selección de tokens queda anclada a la máxima probabilidad. |
| **Max Tokens** | `400 - 800` | Suficiente para respuestas técnicas concisas; evita la generación de texto redundante y ahorra costos. |
| **Presence / Frequency Penalty** | `0.0` | No penalizar la repetición exacta de términos técnicos, nombres de marca o números de catálogo. |

---

## 7. Conexión con la Fase 7 (Evaluación Continua)

La Fase 6 produce la tripleta fundamental del sistema RAG:
$$\text{Tripleta RAG} = (\text{Consulta } Q, \text{Contexto } C, \text{Respuesta } A)$$

Esta tripleta es la entrada directa para la **Fase 7**, donde los evaluadores automatizados (*LLM-as-a-Judge*, mediante herramientas como Ragas o TruLens) medirán:
* **Fidelidad (*Faithfulness*):** ¿Cada afirmación citada de $A$ está 100% contenida en $C$?
* **Relevancia de la Respuesta (*Answer Relevance*):** ¿$A$ responde con precisión a $Q$ sin desviarse?
