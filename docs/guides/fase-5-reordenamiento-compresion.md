# Documento Técnico: Re-ordenamiento Semántico (Rerank) y Compresión de Contexto (Fase 5)

Esta nota técnica establece los principios arquitectónicos, la descripción funcional paso a paso, los fundamentos teóricos y las particularidades de implementación para la **Fase 5: Re-ordenamiento Semántico y Compresión de Contexto** dentro del pipeline RAG (*Retrieval-Augmented Generation*).

---

## 1. Introducción a la Fase 5: El Embudo de Precisión

Para comprender el rol fundamental de esta etapa, es útil analizar la transición entre la recuperación preliminar y la generación de respuestas:

* **El papel de la Fase 4 (Recuperación Híbrida):** En la fase previa, el objetivo prioritario fue maximizar el **Recall** (cobertura). Se lanzó una "red de pesca amplia" combinando búsqueda densa (HNSW) y léxica (BM25) fusionadas mediante Reciprocal Rank Fusion (RRF). El resultado es una lista de candidatos preliminares (entre 20 y 100 fragmentos) que garantiza que los datos relevantes hayan sido capturados, aunque acompañados inevitablemente por ruido, coincidencias parciales y fragmentos tangenciales.
* **El peligro de saturar al LLM en Fase 6:** Si esta lista de candidatos se inyectara de forma directa en el prompt del modelo generador, surgirían tres problemas críticos:
  1. **Costos económicos inflados:** Se consumirían miles de tokens de entrada innecesarios en cada interacción.
  2. **Mayor latencia de respuesta:** El tiempo de procesamiento (Time-To-First-Token) del LLM se incrementa de forma directamente proporcional al tamaño de la ventana de contexto.
  3. **Degradación cognitiva y alucinaciones:** Los modelos de lenguaje pierden precisión cuando deben sintetizar información en medio de bloques largos repletos de distractores irrelevantes.
* **La misión de la Fase 5:** Actuar como el **embudo de precisión**. Toma los candidatos recuperados por la Fase 4, los analiza en profundidad mediante un modelo de atención cruzada (*Cross-Encoder*), filtra el material irrelevante mediante umbrales de corte, trunca la lista a los mejores candidatos absolutos y los posiciona estratégicamente para alimentar al LLM en la Fase 6.

```
                    [Salida Fase 4: Pool de Candidatos RRF]
                                       │
                                       ▼
                 ┌───────────────────────────────────────────┐
                 │       1. Evaluador Cross-Encoder          │
                 │   (Scoring profundo Pregunta + Fragmento) │
                 └─────────────────────┬─────────────────────┘
                                       │
                                       ▼
                 ┌───────────────────────────────────────────┐
                 │       2. Filtro de Umbral (Threshold)     │
                 │   (Eliminación de ruido por corte mínimo) │
                 └─────────────────────┬─────────────────────┘
                                       │
                                       ▼
                 ┌───────────────────────────────────────────┐
                 │       3. Truncador de Contexto (Top-N)    │
                 │  (Selección estricta de 3 a 5 finalistas) │
                 └─────────────────────┬─────────────────────┘
                                       │
                                       ▼
                 ┌───────────────────────────────────────────┐
                 │       4. Reordenador Serial (U-Shape)     │
                 │  (Mitigación de sesgo Lost in the Middle) │
                 └─────────────────────┬─────────────────────┘
                                       │
                                       ▼
                 [Entrada Fase 6: Contexto Limpio y Comprimido]
```

---

## 2. Descripción de Componentes y sus Funciones Operativas

El procesamiento de la Fase 5 se estructura en cuatro componentes modulares encadenados:

### Componente 1: Evaluador de Relevancia (*Cross-Encoder / Reranker*)
* **Función:** Es el evaluador de alta fidelidad. Toma la consulta del usuario en texto plano y cada fragmento candidato de manera simultánea. A diferencia de los modelos de vectores que evalúan textos por separado, el Cross-Encoder analiza ambos textos juntos permitiendo que cada palabra de la pregunta interactúe con cada palabra del documento.
* **Entrada:** Tupla `(consulta, fragmento_i)` para cada uno de los $K$ candidatos devueltos por la Fase 4.
* **Salida:** Un puntaje escalar continuo de relevancia semántica (relevance score), ya sea en escala de logits $(-\infty, +\infty)$ o normalizado mediante sigmoide en el intervalo $[0, 1]$.

### Componente 2: Filtro de Umbral de Calidad (*Score Thresholding*)
* **Función:** Actúa como una barrera de admisión estricta para evitar la inyección de "alucinógenos" al LLM. Si los mejores fragmentos recuperados obtienen puntajes inferiores a una cota predefinida (por ejemplo, score normalizado $< 0.35$), el sistema los descarta de inmediato.
* **Utilidad:** Si un usuario formula una pregunta sobre un producto que no existe en el catálogo, este filtro vacía el contexto. De este modo, el LLM recibe una instrucción explícita de "sin coincidencias" y responde de forma determinista comunicando la inexistencia del ítem, en lugar de intentar forzar una respuesta inventada con fragmentos secundarios.

### Componente 3: Truncador de Fragmentos (*Top-N Slicing*)
* **Función:** Limita físicamente el número máximo de fragmentos que ingresarán a la ventana de contexto de la Fase 6. 
* **Lógica:** Aunque 15 fragmentos superen el umbral de calidad, el truncador retiene exclusivamente los $N$ mejores (típicamente $N \in [3, 5]$). Esto comprime el volumen de texto transferido al generador al mínimo indispensable para responder con rigor.

### Componente 4: Reordenador de Posición Serial (*Serial-Position Reorder*)
* **Función:** Reorganiza el orden de aparición de los $N$ fragmentos finalistas dentro del prompt antes de enviarlos al LLM.
* **Lógica:** En lugar de presentar los fragmentos en orden puramente decreciente $(1, 2, 3, \dots, N)$, los distribuye de forma alternada en los extremos del texto: el fragmento de mayor relevancia se ubica al principio, el segundo más relevante al final, y los de menor relevancia relativa en el centro. Esto contrarresta la pérdida de atención central del modelo generador.

---

## 3. Fundamentos Teóricos Detrás de Cada Elección

### A. La Mecánica de Atención: Bi-Encoder vs. Cross-Encoder

La necesidad de emplear dos tipos de modelos en un pipeline RAG obedece al compromiso fundamental entre **escalabilidad computacional** y **capacidad de representación**.

```
Arquitectura Bi-Encoder (Fases 2 y 4):
Consulta    ───► [Transformer A] ───► Vector q ──┐
                                                 ├─► Similitud de Coseno: cos(q, d)
Documento   ───► [Transformer B] ───► Vector d ──┘
* Sin interacción entre tokens de consulta y documento durante la codificación.

Arquitectura Cross-Encoder (Fase 5):
Consulta + Documento ───► [Transformer Unificado: [CLS] q [SEP] d [SEP]] ───► Score de Relevancia
* Mecanismo de atención completa (All-to-All) entre cada token de q y cada token de d.
```

1. **Limitaciones del Bi-Encoder:**
   * En el Bi-Encoder, el documento se procesa en el momento de la ingesta (Fase 2) sin conocer cuál será la consulta futura.
   * La información se colapsa en un único vector estático mediante *mean pooling*. Esto provoca dilución semántica: se pierden modificadores sutiles, negaciones (ej. *"válvula con retorno pero sin manómetro"*) y relaciones sintácticas de dependencia entre términos.
   * La comparación final mediante producto punto o similitud de coseno asume una relación geométrica simplificada en el espacio vectorial.

2. **Ventajas del Cross-Encoder:**
   * Al concatenar la consulta y el fragmento bajo la forma `[CLS] consulta [SEP] documento [SEP]`, las matrices de atención del Transformer calculan pesos de atención cruzada: cada palabra de la consulta evalúa directamente el contexto de cada palabra del documento en todas las capas de la red.
   * Modificadores, preposiciones y especificaciones técnicas son interpretados en su contexto exacto.
   * **Impacto empírico:** El uso de un Cross-Encoder sobre los candidatos preliminares aporta de manera consistente una mejora de **+5 a +15 puntos de NDCG@10** (Normalized Discounted Cumulative Gain) en comparación con la búsqueda vectorial aislada.

3. **¿Por qué no usar el Cross-Encoder para todo el catálogo?**
   * El costo computacional de un Cross-Encoder es $\mathcal{O}(M \cdot L^2)$, donde $M$ es la cantidad de documentos a evaluar y $L$ es la longitud de la secuencia combinada. 
   * Evaluar un catálogo entero de miles de productos por cada consulta en tiempo real implicaría latencias de varios segundos o minutos.
   * Por ello, el diseño óptimo de dos etapas (*two-stage retrieval*) utiliza el Bi-Encoder/BM25 en la Fase 4 para reducir el universo de búsqueda a una lista corta de candidatos, y reserva el Cross-Encoder para dicha lista reducida.

---

### B. La Teoría de "Lost in the Middle" y la Curva de Atención en U

El diseño del Componente 4 se fundamenta en las investigaciones sobre el uso de contextos extensos en modelos de lenguaje, destacándose el trabajo de Liu et al. (Stanford University, 2023): *“Lost in the Middle: How Language Models Use Long Contexts”*.

```
Rendimiento de Recuperación del LLM (%)
▲
│    ████                                        ████
│    ████                                        ████   (Efecto U-Shape)
│    ████                                        ████
│    ████         ░░░░            ░░░░           ████
│    ████         ░░░░            ░░░░           ████
│    ████         ░░░░            ░░░░           ████
└────┴────────────┴───────────────┴──────────────┴────►
   Inicio                    Centro                   Final
 (Primacy)               (Zona Ciega)               (Recency)
                   Posición del Dato en el Prompt
```

* **Sesgo de Primacía (*Primacy Bias*):** Los LLMs retienen y ponderan con alta fidelidad las instrucciones y fragmentos ubicados al inicio del bloque de contexto.
* **Sesgo de Recencia (*Recency Bias*):** Los fragmentos situados inmediatamente antes de la instrucción final de generación gozan de una reactivación de atención en los mecanismos autorregresivos.
* **El Valle Central:** Cuando el pasaje fundamental que contiene la respuesta se ubica en el centro de un conjunto de documentos proporcionados, la probabilidad de que el LLM recupere el dato decae significativamente (hasta un 30-40% en contextos saturados).

**Mecanismo de Reordenamiento Alternado:** Para mitigar esta debilidad, los fragmentos seleccionados se reordenan mediante un algoritmo de distribución simétrica:
* Si se seleccionan 3 fragmentos ordenados por score descendente $[D_1, D_2, D_3]$:
  * Posición 1 (Inicio): $D_1$ (máxima relevancia).
  * Posición 2 (Centro): $D_3$ (relevancia complementaria).
  * Posición 3 (Cierre): $D_2$ (segunda mayor relevancia).

---

### C. Estrategias de Despliegue e Infraestructura para el Evaluador

Para poner en funcionamiento el Componente 1 (Cross-Encoder), existen dos arquitecturas principales:

| Criterio | Opción A: API Administrada (ej. Cohere Rerank 3/3.5) | Opción B: Modelo Local / Open Source (ej. BGE-Reranker-v2-m3) |
| :--- | :--- | :--- |
| **Mecanismo Operativo** | Llamada HTTPS enviando lista de textos; procesamiento en nube de terceros. | Carga del modelo en memoria RAM/VRAM del servidor local vía `sentence-transformers`. |
| **Costo Monetario** | Variable por consulta (pago por millón de tokens o por volumen de búsquedas). | **$0 USD en llamadas externas**; costo absorbido por el servidor existente. |
| **Privacidad de Datos** | La información de las consultas y productos viaja a servidores externos. | **100% On-Premise / Local**; ningún dato abandona la infraestructura del proyecto. |
| **Requisitos de Hardware** | Mínimos en el cliente (solo conexión a internet). | Requiere CPU moderna o GPU modesta según el tamaño del modelo. |
| **Ventana de Contexto** | Amplia (hasta 4.096 - 32.768 tokens por llamada). | Típicamente 512 tokens por par (suficiente para chunks de catálogo). |
| **Latencia de Red** | Suma la latencia del viaje ida y vuelta por internet (100 - 250 ms). | Inferencia local directa en memoria (15 - 80 ms). |

---

## 4. Particularidades y Calibración para Nuestro Proyecto

En función de las características del caso de uso actual (catálogo acotado de productos industriales/técnicos, escala inicial de 179 registros y proyección hasta 10.000 ítems), se aplican las siguientes definiciones arquitectónicas:

```
                                  PARÁMETROS DEL PROYECTO
┌──────────────────────────────┬─────────────────────────────────────────────────────────────┐
│ Dimensión                    │ Calibración Adoptada                                        │
├──────────────────────────────┼─────────────────────────────────────────────────────────────┤
│ Tamaño del Catálogo          │ Acotado (179 registros actuales / hasta 10.000 en régimen) │
│ Insumo desde Fase 4          │ Top-20 Candidatos RRF (en lugar del estándar de 100)        │
│ Entorno de Cómputo           │ Inferencia local 100% en CPU (Sin GPU dedicada, Costo $0)   │
│ Modelo Seleccionado          │ BAAI/bge-reranker-v2-m3 (o cross-encoder/ms-marco-MiniLM)   │
│ Umbral de Calidad (Corte)    │ Score Sigmoide >= 0.35                                      │
│ Profundidad Final (Top-N)    │ Top-3 Fragmentos Estrictos                                  │
│ Distribución Serial          │ Alternada: [Top-1, Top-3, Top-2]                            │
└──────────────────────────────┴─────────────────────────────────────────────────────────────┘
```

### 1. Tamaño del Catálogo y Reducción del Pool de Entrada ($K = 20$)
En arquitecturas masivas de millones de registros, se suelen extraer 100 candidatos en la Fase 4 para evitar falsos negativos. Sin embargo, para un catálogo de hasta 10.000 productos con chunks atómicos y estructurados en YAML:
* El 99% del recall relevante se concentra en los primeros 15 a 20 candidatos devueltos por RRF.
* Reducir el insumo de entrada a **$K = 20$ candidatos** reduce en un 80% la carga de procesamiento del Cross-Encoder, permitiendo una ejecución ultrarrápida.

### 2. Ejecución 100% en CPU y Costo Cero ($0 USD)
* Dado que el reranker evalúa únicamente 20 pares por consulta, no se justifica el costo recurrente de APIs de terceros (Cohere) ni el alquiler de servidores dedicados con placas de video GPU.
* Un procesador de servidor estándar (2 a 4 núcleos virtuales) resuelve la inferencia de 20 pares en menos de 50 milisegundos utilizando `sentence-transformers` en modo CPU.

### 3. Selección del Modelo Local
* **Modelo Primario (Recomendado para Catálogos en Español):** `BAAI/bge-reranker-v2-m3`. 
  * Posee soporte nativo multilingüe de alta precisión en español.
  * Captura terminología técnica y descripciones industriales complejas.
* **Modelo Secundario (Ultra-ligero):** `cross-encoder/ms-marco-MiniLM-L6-v2`.
  * Si la prioridad absoluta es la mínima latencia en CPU (inferencia en ~15 ms), este modelo de 22 millones de parámetros ofrece un rendimiento sobresaliente con huella de memoria prácticamente nula.

### 4. Calibración de Umbral de Descarte (*Thresholding*)
* En búsquedas de catálogo técnico, los usuarios frecuentemente consultan por códigos de repuesto obsoletos o productos no comercializados.
* Mediante la normalización sigmoide de los puntajes del Cross-Encoder, se establece un **umbral de corte en $0.35$**:
  $$\sigma(s) = \frac{1}{1 + e^{-s}} \ge 0.35$$
* Si el candidato con mayor puntuación no alcanza este valor, el pipeline aborta la inclusión de fragmentos y devuelve un estado de `"NO_RELEVANT_DATA_FOUND"`, forzando al sistema a responder con exactitud que el artículo no figura en el catálogo, impidiendo respuestas engañosas.

### 5. Truncado Estricto a Top-3 y Economía de Tokens en Fase 6
* Cada fragmento de producto estructurado en YAML contiene aproximadamente 120 a 150 tokens.
* Truncar estrictamente a los **3 mejores candidatos** limita el contexto inyectado al LLM a menos de 500 tokens en total.
* Esto maximiza la relación señal/ruido, evita la saturación cognitiva del generador y mantiene el consumo de tokens en la Fase 6 en niveles de mínimo costo operativo.

### 6. Posicionamiento Serial en el Prompt
Los 3 fragmentos finales se inyectan en el prompt estructurados bajo la distribución alternada:
* `Contexto 1 (Inicio):` Candidato #1 (Mayor score de confianza).
* `Contexto 2 (Centro):` Candidato #3 (Información de soporte o alternativa).
* `Contexto 3 (Cierre):` Candidato #2 (Segunda mejor coincidencia, inmediatamente contigua a la instrucción de respuesta).

---

## 5. Salidas y Entregables de la Fase 5

* **Nota Técnica Formal:** `fase-5-reordenamiento-compresion.md` (este documento).
* **Script Operativo de Producción:** `fase_5_reranker.py`, implementando la clase `Fase5RerankerCompressor` con soporte para modelos locales vía `sentence-transformers`, normalización de puntajes, filtrado por umbral dinámico, truncado Top-N y reordenamiento de posición serial.
* **Estructura del Objeto de Salida:**
  ```json
  {
    "query": "Llave de impacto neumática CARBIZ 1/2",
    "total_input_candidates": 20,
    "total_passed_threshold": 3,
    "threshold_applied": 0.35,
    "final_context_chunks": [
      {
        "position": 1,
        "original_rank": 1,
        "cross_encoder_score": 0.942,
        "node_id": "CAT-CARBIZ-099",
        "content": "codigo_producto: CARBIZ-099\nmarca: CARBIZ\ntitulo: Llave de impacto neumática 1/2..."
      },
      {
        "position": 2,
        "original_rank": 3,
        "cross_encoder_score": 0.615,
        "node_id": "CAT-CARBIZ-085",
        "content": "codigo_producto: CARBIZ-085\nmarca: CARBIZ\ntitulo: Llave de impacto neumática 3/8..."
      },
      {
        "position": 3,
        "original_rank": 2,
        "cross_encoder_score": 0.887,
        "node_id": "CAT-CARBIZ-101",
        "content": "codigo_producto: CARBIZ-101\nmarca: CARBIZ\ntitulo: Llave de impacto a batería 1/2..."
      }
    ]
  }
  ```
