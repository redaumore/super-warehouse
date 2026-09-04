# Documento Técnico: Despliegue de Pipelines de Evaluación Continua (Tríada RAG) (Fase 7)

Esta nota técnica establece los fundamentos matemáticos, la arquitectura de control, los algoritmos cuantitativos, las directivas de integración y el diseño del **Orquestador Maestro End-to-End** para la **Fase 7: Despliegue de Pipelines de Evaluación Continua (Tríada RAG)** en pipelines de producción para dominios técnicos y catálogos industriales de ferretería.

---

## 1. Introducción: La Fase 7 como Bucle Cerrado de Control (*Closed-Loop Feedback*)

A lo largo de las Fases 0 a 6, el sistema RAG estructura una cadena secuencial de transformación y generación de información:
1. **Fase 0 (Extracción & Parsing):** Transforma documentos complejos (PDFs de catálogos) en tablas y texto estructurado Markdown.
2. **Fase 1 (Chunking):** Segmenta el corpus en nodos semánticos o productos granulares con solapamiento controlado.
3. **Fases 2 y 3 (Embeddings & HNSW):** Genera representaciones vectoriales densas (con recorte Matryoshka) e indexa el espacio multidimensional en grafos navegables de mundo pequeño.
4. **Fase 4 (Recuperación Híbrida & RRF):** Ejecuta en paralelo búsqueda léxica (BM25) y densa, fusionando rangos mediante *Reciprocal Rank Fusion*.
5. **Fase 5 (Reranking Semántico):** Aplica un Cross-Encoder bidireccional para depurar los 100 candidatos preliminares a 3-5 fragmentos hiper-relevantes.
6. **Fase 6 (Generación Aumentada):** Inyecta el contexto empaquetado en XML y, bajo restricciones negativas severas y temperatura 0.0, sintetiza una respuesta factual con citas exactas.

### El Rol Operativo de la Fase 7
La **Fase 7 no representa un punto de terminación pasivo**, sino el **sistema nervioso central y bucle de control de calidad** del pipeline. En producción, los sistemas RAG enfrentan deriva de datos (*data drift*), actualización de catálogos, cambios de modelos de embeddings o degradación del índice.

La Fase 7 monitorea de forma continua la triple interacción fundamental:
$$\text{Consulta } (Q) \longleftrightarrow \text{Contexto Recuperado } (C) \longleftrightarrow \text{Respuesta Generada } (A)$$

```
                                    ┌──────────────────────────────────────┐
                                    │      Usuario / Aplicación Cliente    │
                                    └──────────────────┬───────────────────┘
                                                       │  Consulta (Q)
                                                       ▼
      ┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
      │                                     PIPELINE RAG RUNTIME                                        │
      │                                                                                                 │
      │   ┌───────────────┐     ┌───────────────┐     ┌───────────────┐     ┌───────────────────────┐   │
      │   │    Fase 1     │────▶│    Fase 3     │────▶│  Fases 4 & 5  │────▶│        Fase 6         │   │
      │   │   Chunking    │     │  Index HNSW   │     │ Hybrid & Rerank│    │ Generador Fact (Temp=0)│  │
      │   └───────▲───────┘     └───────▲───────┘     └───────▲───────┘     └───────────┬───────────┘   │
      │           │                     │                     │                         │               │
      └───────────┼─────────────────────┼─────────────────────┼─────────────────────────┼───────────────┘
                  │                     │                     │                         │
                  │              Señales de Diagnóstico       │    Tripleta (Q, C, A)   │
                  │              y Retroalimentación          │                         ▼
      ┌───────────┴─────────────────────┴─────────────────────┴─────────────────────────────────────────┐
      │                        FASE 7: EVALUADOR CONTINUO (TRÍADA RAG + RECALL)                         │
      │                                                                                                 │
      │   ┌────────────────────────┐  ┌────────────────────────┐  ┌─────────────────────────────────┐   │
      │   │   Context Relevance    │  │      Faithfulness      │  │        Answer Relevance         │   │
      │   │  (S(|S_r|) / S(|S_t|)) │  │ (V(claims) / |claims|) │  │ (cos(emb(Q), emb(Q_gen)))       │   │
      │   └───────────┬────────────┘  └───────────┬────────────┘  └────────────────┬────────────────┘   │
      │               │                           │                                │                    │
      │               ▼                           ▼                                ▼                    │
      │       ¿Baja Relevancia?           ¿Alucinaciones?                  ¿Respuesta Incompleta?       │
      │      Reajustar Chunking         Endurecer Directivas              Refinar Prompt & Top-N        │
      │       (Hacia Fase 1)               (Hacia Fase 6)                     (Hacia Fases 5 & 6)       │
      └─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Fundamentos de la Evaluación sin Ground-Truth ("Sin Oro") y LLM-as-a-Judge

### A. La Insuficiencia de Métricas Clásicas de NLP
Las métricas históricas de procesamiento de lenguaje natural (como **BLEU**, **ROUGE** o **METEOR**) fueron diseñadas para traducción automática o resumen extractivo. Presentan fallas críticas en RAG:
* **Dependencia de respuestas ideales fijas (*Ground Truth*):** Exigen que expertos humanos redacten la respuesta de referencia de cada consulta. En catálogos técnicos industriales con miles de SKUs y consultas infinitas, este enfoque es inviable y costoso.
* **Incapacidad Semántica:** Un cambio de palabras o sinónimos técnicos (ej. *"abrazadera cremallera"* vs. *"grampa sin fin"*) es penalizado severamente por BLEU/ROUGE a pesar de ser semánticamente idéntico y correcto.
* **Invisibilidad del Contexto:** ROUGE no evalúa si la respuesta está respaldada por el contexto provisto o si provino del conocimiento previo del LLM (alucinación paramétrica).

### B. El Paradigma *LLM-as-a-Judge*
El enfoque contemporáneo de evaluación utiliza modelos de lenguaje de frontera (como **GPT-4o** o **Claude 3.5 Sonnet**) configurados con prompts estructurados y rúbricas deterministas sin requerir *ground truth*. Diversos estudios empíricos demuestran que, con directivas de descomposición atómica y restricciones de razonamiento paso a paso (*Chain of Thought*), el acuerdo entre el LLM Juez y evaluadores humanos supera el **90%** en tareas de fidelidad factual y relevancia.

---

## 3. Formulación Matemática de las Métricas de la Tríada RAG

La evaluación de la Tríada RAG analiza las aristas del triángulo formado por la Consulta ($Q$), el Contexto recuperado ($C$) y la Respuesta generada ($A$):

```
                                  Consulta (Q)
                                  /          \
                                 /            \
          Context Relevance     /              \   Answer Relevance
                               /                \
                              ▼                  ▼
                     Contexto (C) ────────────▶ Respuesta (A)
                                  Faithfulness
```

### Métrica 1: Relevancia del Contexto (*Context Relevance*)
* **Propósito:** Evaluar la relación $Q \longrightarrow C$. Mide si el contexto recuperado por las Fases 4 y 5 contiene exclusivamente información indispensable para responder la consulta, penalizando la inyección de fragmentos distractores o texto redundante.
* **Formulación Algorítmica:**
  1. El contexto recuperado $C$ se divide en un conjunto de oraciones individuales $S_t = \{s_1, s_2, \dots, s_n\}$.
  2. El evaluador identifica el subconjunto de oraciones relevantes $S_r \subseteq S_t$ que aportan evidencia directa para responder $Q$.
  3. El puntaje se calcula como la proporción:
  $$\text{Context Relevance} = \frac{|S_r|}{|S_t|}$$
  Donde $|S_r|$ es el cardinal de oraciones relevantes y $|S_t|$ es el número total de oraciones en el contexto inyectado.
* **Impacto en el Pipeline:** Si el contexto inyecta 500 palabras y solo una oración de 20 palabras es útil, el puntaje es bajo ($\approx 0.10$). Esto alerta sobre **fragmentos excesivamente grandes (*oversized chunks*)**, lo que diluye la atención del generador e induce el sesgo *Lost in the Middle*.

### Métrica 2: Fidelidad de la Respuesta (*Faithfulness / Groundedness*)
* **Propósito:** Evaluar la relación $C \longrightarrow A$. Garantiza que la respuesta generada provenga **exclusivamente** de los hechos contenidos en el contexto recuperado, detectando de forma cuantitativa cualquier alucinación o invención de especificaciones técnicas.
* **Formulación Algorítmica:**
  1. La respuesta generada $A$ se descompone en un conjunto de afirmaciones factuales atómicas $U = \{u_1, u_2, \dots, u_m\}$ (*claims* o enunciados indivisibles).
  2. Para cada afirmación $u_i \in U$, se evalúa si puede inferirse directamente del contexto $C$:
  $$V(u_i, C) = \begin{cases} 1 & \text{si } C \models u_i \text{ (afirmación respaldada)} \\ 0 & \text{en caso contrario} \end{cases}$$
  3. El puntaje de fidelidad es la tasa de afirmaciones verificadas:
  $$\text{Faithfulness} = \frac{\sum_{i=1}^{m} V(u_i, C)}{m}$$
* **Impacto en el Pipeline:** En catálogos industriales, **un valor de Faithfulness menor a 1.0 es inadmisible para producción**. Si un modelo afirma que un tornillo es de acero inoxidable pero el contexto solo indica que es de acero zincado, $V(u_i, C) = 0$, disparando una alerta de integridad.

### Métrica 3: Relevancia de la Respuesta (*Answer Relevance*)
* **Propósito:** Evaluar la relación $Q \longrightarrow A$. Mide en qué grado la respuesta responde de manera directa, completa y concisa a lo solicitado por el usuario, castigando divagaciones, evasivas o redundancias.
* **Formulación Algorítmica:**
  1. A partir de la respuesta generada $A$, se instruye a un modelo para que genere sintéticamente $N$ preguntas posibles $q_j$ ($j=1, \dots, N$) que tendrían a $A$ como respuesta ideal.
  2. Se generan los vectores de embedding para la consulta original $\mathbf{e}_Q = \text{emb}(Q)$ y para cada una de las consultas sintéticas $\mathbf{e}_{q_j} = \text{emb}(q_j)$.
  3. Se calcula el promedio de similitud del coseno:
  $$\text{Answer Relevance} = \frac{1}{N} \sum_{j=1}^{N} \frac{\mathbf{e}_Q \cdot \mathbf{e}_{q_j}}{\|\mathbf{e}_Q\| \|\mathbf{e}_{q_j}\|}$$
  Alternativamente, en evaluación determinista local, se utiliza una rúbrica estructurada de alineación semántica calificada por el LLM Juez en escala $[0.0, 1.0]$.
* **Impacto en el Pipeline:** Una respuesta puede tener 100% de fidelidad (todo lo que dice es verdad según el contexto) pero 0% de relevancia (ej. el usuario preguntó por un código de broca y el modelo respondió describiendo la historia de la marca).

### Métrica 4: Recall@K en Búsqueda Vectorial e Híbrida
* **Propósito:** Evaluar la calidad intrínseca del motor de recuperación (Fases 3 y 4) comparando los resultados del índice rápido aproximado (HNSW o Híbrido RRF) contra la búsqueda exacta exhaustiva (*Ground Truth* de fuerza bruta).
* **Formulación Matemática:**
  $$\text{Recall@K} = \frac{|\mathcal{O}_{\text{ANN}} \cap \mathcal{G}_{\text{Exact}}|}{K}$$
  Donde $\mathcal{O}_{\text{ANN}}$ es el conjunto de los $K$ candidatos devueltos por el índice y $\mathcal{G}_{\text{Exact}}$ son los verdaderos $K$ vecinos más cercanos del espacio vectorial.
* **Meta de Producción:** Se exige $\text{Recall@K} \ge 0.95$.

---

## 4. Matriz de Diagnóstico y Bucle de Retroalimentación Operativa

La siguiente matriz define el protocolo de triage técnico cuando las métricas cuantitativas descienden de los umbrales de aceptación:

| Métrica Degradada | Causa Raíz Probable | Fase Afectada | Acción Correctiva de Ingeniería |
| :--- | :--- | :--- | :--- |
| **Context Relevance < 0.70** | Fragmentos excesivamente largos; solapamiento desmedido; o Cross-Encoder permisivo. | **Fase 1 (Chunking) & Fase 5 (Rerank)** | 1. Reducir el tamaño de bloque a nivel de producto granular (~70-150 tokens).<br>2. Incrementar el umbral mínimo del Reranker (ej. de 0.35 a 0.50).<br>3. Reducir el Top-N final de 5 a 3 fragmentos. |
| **Faithfulness < 0.90** | Alucinación del modelo; inferencia de especificaciones no comprobadas; o sesgo de complacencia (*sycophancy*). | **Fase 6 (Prompting & LLM)** | 1. Endurecer la Directiva de Ausencia en el System Prompt.<br>2. Forzar temperatura a 0.0 absoluto.<br>3. Prohibir explícitamente cualquier inferencia alfanumérica de códigos de catálogo. |
| **Answer Relevance < 0.80** | El LLM evade la pregunta; respuesta truncada por `max_tokens`; o contexto insuficiente para la respuesta. | **Fase 5 & Fase 6** | 1. Refinar la formulación del prompt de respuesta.<br>2. Incrementar `max_tokens` para no cortar tablas descriptivas.<br>3. Evaluar si la consulta requiere recuperación multi-query. |
| **Recall@K < 0.95** | Grafo HNSW escaso; desconexión en capas superiores; o esfuerzo de búsqueda insuficiente en caliente. | **Fase 3 (HNSW Index)** | 1. Incrementar `efSearch` en tiempo de consulta (ej. de 40 a 100 o 200).<br>2. Reconstruir el índice con mayor conectividad ($M=32$) y mayor $efConstruction$ (200). |
| **Búsqueda Léxica Fallida** | Términos exactos de catálogo o SKUs no encontrados por coincidencia densa. | **Fase 4 (Hybrid Search)** | 1. Ajustar la constante $k$ de RRF (ej. probar valores entre 20 y 60).<br>2. Asegurar normalización de caracteres alfanuméricos en el índice BM25. |

---

## 5. Integración en CI/CD y Compuertas de Calidad (*Quality Gates*)

Para evitar regresiones al actualizar dependencias, cambiar el modelo de embeddings o incorporar nuevos catálogos de proveedores, el pipeline de evaluación se ejecuta automáticamente en el flujo de integración continua:

```
[Git Commit / PR] ──▶ [Ejecución Suite Test RAG] ──▶ [Cálculo Tríada RAG sobre Golden Dataset]
                                                                  │
                                      ┌───────────────────────────┴───────────────────────────┐
                                      ▼                                                       ▼
                       Todos los Scores ≥ Umbrales                             Algún Score < Umbral
                             (ej. ≥ 0.90)                                            (Regresión)
                                      │                                                       │
                                      ▼                                                       ▼
                            [BUILD EXITOSO: CI PASS]                                [BUILD RECHAZADO: CI FAIL]
                          Habilitado para Producción                                Bloqueo de Despliegue
```

### Umbrales Estándar de Producción (SLA de Calidad)
* **Faithfulness:** $\ge 0.95$ (Tolerancia casi nula a alucinaciones de catálogo).
* **Context Relevance:** $\ge 0.75$ (Contextos limpios sin dilución de información).
* **Answer Relevance:** $\ge 0.85$ (Respuestas completas y directas).
* **Recall@10:** $\ge 0.95$ (El motor vectorial debe capturar al menos el 95% del *ground truth* exacto).

---

## 6. Especificación del Módulo Evaluador (`fase_7_evaluator.py`)

El script evaluador complementario implementa:
1. **Entidades tipadas con `dataclass`:** Representación formal de `EvaluationSample` (consulta, contexto, respuesta generada, fuentes), `EvaluationResult` y `AggregateReport`.
2. **Motor Dual de Evaluación:**
   * **Modo Offline / Heurístico:** Segmentador de afirmaciones (*claim extractor*), detector de citas sintácticas, evaluador de cobertura léxico-semántica y analizador determinista sin costo de API.
   * **Modo LLM-as-a-Judge:** Diseñado para invocar APIs de frontera con prompts en formato JSON estricto, devolviendo razonamiento (*reasoning*) y puntaje normalizado.
3. **Motor de Diagnóstico Automático:** Módulo de reglas que analiza los puntajes individuales y agregados, emitiendo recomendaciones directas dirigidas a las Fases 1, 3, 4, 5 y 6.
4. **Batería de Pruebas del Catálogo:** Incluye 5 casos reales que modelan éxitos factuales, rechazos honestos correctos, alucinaciones de código detectadas y respuestas con contexto diluido.
5. **Exportación Consolidada:** Generación automática de reportes ejecutivos en formato JSON y Markdown.

---

## 7. Implementación Arquitectónica: Orquestador Maestro (*End-to-End Orchestrator*)

### 7.1. Contexto Operativo e Integración con el Sistema Destino
El desarrollo de este sistema se distribuye en dos capas claramente diferenciadas:
1. **El Repositorio de Componentes Modulares:** Alberga los scripts especializados construidos a lo largo de las Fases 0 a 7 (extracción de catálogos en PDF, segmentación semántica tabular, generación de embeddings densos, indexación HNSW en pgvector, búsqueda híbrida BM25 + Vectorial con fusión RRF, reranking semántico Cross-Encoder, generación determinista con control de alucinaciones y el pipeline de evaluación cuantitativa).
2. **El Sistema de Destino (Aplicación Principal):** Constituye el entorno productivo (servicio backend en FastAPI, workers distribuidos en Celery y base de datos relacional/vectorial) en el cual deben operar de manera automatizada tres procesos críticos:
   * **Ingesta Inicial:** Procesamiento masivo del catálogo de ferretería desde sus documentos fuente originales.
   * **Actualización Incremental:** Alta, baja y modificación de precios, marcas y nuevos SKUs sin requerir reindexar todo el espacio vectorial.
   * **Consulta y Servicio en Tiempo Real:** Recepción de preguntas de clientes o asesores de venta, entregando respuestas técnicas exactas en milisegundos con trazabilidad documental.

### 7.2. Motivación de Ingeniería: ¿Por qué Orquestador antes de Pruebas Aisladas?
Frente a la disyuntiva de ejecutar primero pruebas aisladas sobre componentes individuales o construir la capa de orquestación, los principios de arquitectura de software y confiabilidad imponen construir primero el **Orquestador Maestro**:
* **Evitar Pruebas sobre Interfaces Efímeras o "Mockeadas":** Evaluar cada fase por separado obliga a crear entradas y salidas simuladas de forma manual. Esto crea una falsa sensación de seguridad: cada módulo aprueba de forma aislada, pero el sistema falla al conectarse debido a desacoples de formato (por ejemplo, discrepancias entre el formato de tuplas devuelto por RRF y lo esperado por el modelo de entrada del Cross-Encoder).
* **Definición del Contrato de Interfaz Definitivo:** El Orquestador congela la interfaz formal (*API contract*) que consumirá el sistema de destino (`ingest_or_update`, `query`, `evaluate_system`), garantizando que la aplicación principal interactúe con una fachada limpia y cohesiva.
* **Habilitación de Pruebas Reales de Extremo a Extremo (*End-to-End*):** La Tríada RAG (Fase 7) está concebida para evaluar la interacción viva entre Consulta ($Q$), Contexto ($C$) y Respuesta ($A$). Al contar con el Orquestador, las pruebas de la Fase 7 se ejecutan sobre el pipeline real integrado como una verdadera "caja negra" operativa, permitiendo validar la robustez del sistema frente a datos técnicos reales.

### 7.3. En qué Consiste el Desarrollo (`rag_orchestrator.py`)
El Orquestador Maestro se implementa como una clase central (`RAGOrchestrator`) que encapsula la lógica de todas las fases anteriores y expone tres métodos principales:

```
                                  RAGOrchestrator
         ┌──────────────────────────────┼──────────────────────────────┐
         ▼                              ▼                              ▼
 .ingest_or_update(pdf/doc)       .query(user_query)       .evaluate_pipeline(dataset)
 [Flujo Batch / Asíncrono]       [Flujo Online / Síncrono]    [Bucle de Control y Calidad]
 ┌───────────────────────┐       ┌───────────────────────┐    ┌──────────────────────────┐
 │ Fase 0: Extracción    │       │ Fase 4: Búsqueda RRF  │    │ Fase 7: Tríada RAG       │
 │ Fase 1: Chunking      │       │ Fase 5: Reranking     │    │ - Context Relevance      │
 │ Fase 2: Embeddings    │       │ Fase 6: Generador LLM │    │ - Faithfulness (Citas)   │
 │ Fase 3: Upsert pgvect │       │ (Temp=0, Citas XML)   │    │ - Answer Relevance       │
 └───────────────────────┘       └───────────────────────┘    │ - Reporte & Quality Gate │
                                                              └──────────────────────────┘
```

1. **Flujo Batch / Asíncrono (`.ingest_or_update`):**
   * Recibe el archivo de catálogo (ej. PDF industrial).
   * Ejecuta la extracción de texto y detección de tablas (Fase 0).
   * Aplica la estrategia de segmentación por producto granular con metadatos contextuales (Fase 1).
   * Genera las representaciones densas mediante embeddings con normalización (Fase 2).
   * Realiza la carga masiva transaccional (*upsert*) en el índice HNSW y el índice léxico de pgvector (Fase 3).
2. **Flujo Online / Síncrono (`.query`):**
   * Recibe la consulta en lenguaje natural del usuario.
   * Ejecuta la búsqueda híbrida simultánea (léxica BM25 + vectorial) y fusiona los rankings con RRF $k=60$ obteniendo los top candidatos (Fase 4).
   * Pasa los candidatos por el Cross-Encoder reranker, descartando los que no superen el umbral de relevancia semántica (Fase 5).
   * Empaqueta el contexto en etiquetas XML limpias y solicita la generación determinista al LLM con temperatura 0.0 y directiva estricta de citas `[Fragmento N]` (Fase 6).
   * Retorna un objeto tipado `RAGResponse` con el texto final, citas verificadas, fragmentos fuente utilizados y métricas de latencia de cada etapa.
3. **Flujo de Auditoría y Calidad (`.evaluate_pipeline`):**
   * Conecta directamente la suite de evaluación continua de la Fase 7.
   * Ejecuta un lote de consultas de prueba (*Golden Dataset*) contra el método `.query()`, auditando automáticamente *Context Relevance*, *Faithfulness*, *Answer Relevance* y emitiendo un veredicto de *Quality Gate* (PASS/FAIL) con recomendaciones de diagnóstico.

### 7.4. Outcome y Entregables del Pipeline Integrado
1. **Punto Único de Acoplamiento (*Facade Pattern*):** El sistema principal no necesita conocer la complejidad interna de pgvector, RRF o Cross-Encoders; simplemente instancia el orquestador o invoca sus endpoints.
2. **Trazabilidad Total de Consultas:** Cada respuesta generada cuenta con trazabilidad granular, informando exactamente qué filas del catálogo sustentaron cada afirmación y el tiempo insumido en cada fase.
3. **Compuerta de Calidad Automatizada para CI/CD:** Antes de desplegar una nueva versión del catálogo o modificar un hiperparámetro de búsqueda, el pipeline ejecuta su propia auto-evaluación garantizando que la fidelidad factual se mantenga en $\ge 0.95$.
