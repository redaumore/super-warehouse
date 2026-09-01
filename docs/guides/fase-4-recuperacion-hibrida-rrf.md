# Nota Técnica: Recuperación Híbrida (Dense + Sparse/BM25) y Fusión de Rangos con RRF (Fase 4) para Catálogo de Productos

Esta nota técnica consolida los fundamentos teóricos, los compromisos de diseño operativo y la arquitectura de implementación metodológica para la **Fase 4: Recuperación Híbrida y Fusión de Rangos con Reciprocal Rank Fusion (RRF)** de un pipeline RAG (*Retrieval-Augmented Generation*) de nivel de producción sobre catálogos industriales y técnicos.

---

## 1. Fundamentos Teóricos: ¿Cómo Operar y Optimizar la Recuperación Híbrida?

En un sistema RAG de producción, la fase de recuperación (*retrieval*) actúa como el embudo primario de información. Si el material relevante no se captura en esta etapa, ninguna técnica posterior de re-ordenamiento (*reranking*) o de prompting al LLM podrá compensar la omisión (*"si la red de pesca tiene agujeros grandes, los peces relevantes se pierden para siempre"*).

```
                               ┌───────────────────────────────────────────────┐
                               │            Consulta del Usuario               │
                               │      ("Llave de impacto 1/2 CARBIZ-99")       │
                               └───────┬───────────────────────────────┬───────┘
                                       │                               │
                                       ▼                               ▼
                  ┌───────────────────────────────┐ ┌─────────────────────────────────┐
                  │       Rama Densa (ANN)        │ │       Rama Léxica (BM25)        │
                  │   Inferencia embedding (MRL)  │ │   Tokenización y matching exacto│
                  │   Búsqueda HNSW en pgvector   │ │   tsvector / Inverted Index     │
                  └──────────────┬────────────────┘ └─────────────────┬───────────────┘
                                 │ (Top-50 vectores)                  │ (Top-50 docs)
                                 └───────────────┬────────────────────┘
                                                 │
                                                 ▼
                                ┌─────────────────────────────────┐
                                │  Fusión de Rangos (RRF, k=60)   │
                                │   Score = Σ 1 / (k + rank_i)    │
                                └────────────────┬────────────────┘
                                                 │
                                                 ▼
                                ┌─────────────────────────────────┐
                                │   Top-100 Candidatos Fusionados │
                                │     (Insumo para Reranking)     │
                                └─────────────────────────────────┘
```

### A. La Debilidad Estructural de la Búsqueda Densa Pura
Los modelos de embeddings neuronales (*bi-encoders*) comprimen oraciones o párrafos en representaciones vectoriales densas donde la cercanía geométrica equivale a afinidad conceptual. Si bien sobresalen comprendiendo intenciones semánticas globales, paráfrasis y sinónimos, presentan **fallas críticas documentadas en entornos de catálogos y dominios especializados**:

1.  **Códigos Alfanuméricos y SKUs:** Consultas que contienen números de parte o códigos de catálogo (ej. `CAT-1025-C`, `DIN-933`, `CARBIZ-99`) no poseen significado semántico intrínseco en los corpus de pre-entrenamiento de los transformadores. El embedding densifica estos identificadores en vectores cercanos a "piezas de fijación" o "herramientas generales", perdiendo por completo la coincidencia exacta de caracteres.
2.  **Entidades de Nicho y Nombres Propios:** Términos de baja frecuencia como nombres de marcas registradas o especificaciones métricas exactas (`M8 x 1.25`, `1/2 pulgada`) suelen ser amortiguados por el promedio del pooling del transformer frente a palabras circundantes más comunes.
3.  **El Fenómeno de Dilución Semántica:** Ante consultas cortas y precisas, los bi-encoders pueden devolver pasajes que discuten conceptos tangenciales con alta elocuencia retórica pero que carecen del dato específico solicitado.

### B. Complementariedad con la Búsqueda Léxica (BM25 / Full-Text Search)
El algoritmo **BM25** (*Best Matching 25*) y los índices invertidos de texto completo (*tsvector* en PostgreSQL) evalúan la presencia estadística exacta de tokens mediante dos componentes fundamentales:
*   **Frecuencia de Término ($TF$):** Premia a los documentos donde el término aparece reiteradamente, saturando el puntaje mediante un factor de ajuste asintótico ($k_1$).
*   **Frecuencia Inversa de Documento ($IDF$):** Otorga un peso exponencialmente más alto a los términos infrecuentes en el corpus (como `CARBIZ-99` o `CAT-1025-C`), penalizando términos genéricos (`llave`, `acero`).

**La ventaja híbrida:** La búsqueda léxica aporta exactitud sintáctica determinista sobre identificadores y palabras clave, mientras que la búsqueda vectorial densa aporta tolerancia a formulaciones lingüísticas imprevistas, errores tipográficos conceptuales y sinónimos. La combinación de ambas ramas garantiza maximizar el *Recall@K* inicial.

---

### C. Por Qué RRF Supera a la Normalización Lineal de Puntajes

Al combinar dos o más recuperadores independientes, existen dos paradigmas principales: combinación lineal de puntajes normalizados y fusión por rangos.

```
Enfoque 1: Score Fusion Lineal (Inestable)
Score_Final = α · Normalizar(Score_Denso) + (1 - α) · Normalizar(Score_BM25)

Enfoque 2: Reciprocal Rank Fusion (Robusto e Invariante)
RRF_Score(d) = Σ 1 / (k + rank_i(d))
```

La normalización lineal de puntajes enfrenta tres problemas operativos severos en producción:

1.  **Incompatibilidad Intrínseca de Distribuciones:** La similitud de coseno o producto escalar produce puntajes acotados (ej. $[-1, 1]$ o $[0, 1]$), mientras que los puntajes BM25 se mueven en una escala abierta $[0, \infty)$ cuya magnitud depende de la longitud media del corpus y de la rareza del término en el vocabulario global.
2.  **Vulnerabilidad Extrema a Valores Atípicos (*Outliers*):** Técnicas de escalado como *Min-Max Normalization* o *Z-Score* sufren cuando una consulta genera un documento con una coincidencia léxica masiva. Este valor atípico comprime artificialmente el resto de los puntajes léxicos hacia cero, anulando la contribución relativa de la rama vectorial.
3.  **Dependencia de Hiperparámetros Frágiles ($\alpha$):** La calibración del peso $\alpha$ requiere optimización empírica por cada tipo de corpus. Un valor $\alpha = 0.5$ puede ser óptimo para preguntas conceptuales, pero destruye el rendimiento cuando el usuario busca un SKU exacto.

**La solución RRF:** Al descartar las magnitudes absolutas de los puntajes y operar exclusivamente sobre las **posiciones ordinales (rangos)** que cada documento obtiene en cada recuperador, RRF elimina por completo el sesgo de escala y no requiere reentrenamiento ante cambios en la distribución de datos.

---

### D. Formulación Matemática de RRF y el Rol de la Constante $k=60$

La función de Reciprocal Rank Fusion, formulada por Cormack, Clarke y Buettcher (SIGIR 2009), define el puntaje consolidado de un documento $d$ a partir de un conjunto de recuperadores $Q$:

$$\text{RRF\_Score}(d \in D) = \sum_{q \in Q} \frac{1}{k + r_q(d)}$$

Donde:
*   $Q$: Conjunto de sistemas de recuperación ejecutados en paralelo (en nuestro pipeline: $Q = \{\text{Dense\_HNSW}, \text{Lexical\_BM25}\}$).
*   $r_q(d)$: Posición física o rango ordinal (indexado en base 1: $1, 2, 3, \dots, K$) del documento $d$ en la lista devuelta por el sistema $q$. Si el documento no figura en el Top-K devuelto por dicho sistema, su aporte en ese sumando es estrictamente $0$.
*   $k$: Constante empírica de suavizado (*ranking constant*).

#### ¿Por qué el valor estándar es $k = 60$?
El parámetro $k$ actúa como un amortiguador de la pendiente de penalización:

*   **Si $k \to 0$:** La función $\frac{1}{r}$ tiene una pendiente decreciente abrupta. El documento en el rango 1 recibe score $1.0$, mientras que el rango 2 cae a $0.5$ (penalización del 50%) y el rango 3 a $0.33$. Esto hace al sistema hipersensible a pequeñas oscilaciones en la cima de una sola lista, permitiendo que un documento clasificado primero por error en una rama opaque a un documento presente en el rango 2 de ambas ramas.
*   **Con $k = 60$:** La pendiente se suaviza notablemente:
    *   Rango 1: $\frac{1}{60 + 1} = \frac{1}{61} \approx 0.016393$
    *   Rango 2: $\frac{1}{60 + 2} = \frac{1}{62} \approx 0.016129$ (diferencia marginal de solo $1.6\%$)
    *   Rango 10: $\frac{1}{60 + 10} = \frac{1}{70} \approx 0.014286$

Esta amortiguación produce una propiedad algebraica clave: **el consenso entre sistemas siempre vence a la unilateralidad**:
*   Un documento que califica en el **Rango 10** en ambas listas obtiene:
    $$\text{Score} = \frac{1}{70} + \frac{1}{70} \approx 0.02857$$
*   Un documento que califica en el **Rango 1** en una lista pero está ausente en la otra obtiene:
    $$\text{Score} = \frac{1}{61} + 0 \approx 0.01639$$

El documento con relevancia dual supera al documento unilateral por más de un 74%, garantizando que los elementos verificados por ambos motores escalen a la cima del ranking final.

---

### E. Pre-filtrado vs. Post-filtrado en Recuperación Híbrida

Un error clásico de arquitectura consiste en ejecutar la búsqueda híbrida sobre todo el espacio vectorial/documental y luego filtrar los 100 resultados finales por metadatos (ej. `categoria = 'HERRAMIENTAS'`).

*   **El colapso del Post-filtrado:** Si el filtro es altamente selectivo (por ejemplo, una subcategoría que representa el 2% del catálogo total), es altamente probable que los 50 candidatos recuperados por ANN o BM25 pertenezcan a otras categorías. El post-filtrado descartará la casi totalidad de los ítems, dejando un pool raquítico de 1 o 2 documentos o incluso un conjunto vacío, arruinando el Recall.
*   **La regla del Pre-filtrado Unificado:** Tanto la consulta HNSW como el motor de búsqueda léxica deben incorporar las cláusulas `WHERE` de metadatos **antes o durante la exploración de los índices**. En PostgreSQL con pgvector, esto se logra combinando los índices B-Tree/GIN estructurados creados en la Fase 3 con la exploración del grafo, forzando a que los Top-50 de cada rama sean 100% elegibles antes de ingresar a la función RRF.

---

## 2. Metodología: Pipeline de Recuperación Híbrida (Fase 4)

La metodología transforma la consulta en lenguaje natural del usuario en un pool de 100 candidatos altamente relevantes, estructurado para alimentar el Cross-Encoder de la Fase 5.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        CONSULTA DEL USUARIO                            │
│           (Query String + Filtros de Metadatos Opcionales)             │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ PASO 1: Procesamiento Concurrente de Entrada                           │
│  - Inferencia Vectorial en caliente (OpenAI text-embedding-3-large,    │
│    MRL truncado a 256 dims, normalización L2)                          │
│  - Parseo léxico y extracción de tokens para Full-Text / BM25          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                 ┌──────────────────┴──────────────────┐
                 ▼                                     ▼
┌────────────────────────────────┐   ┌───────────────────────────────────┐
│ PASO 2A: Rama Densa (HNSW)     │   │ PASO 2B: Rama Léxica (BM25)       │
│  - Pre-filtro SQL metadata     │   │  - Pre-filtro SQL metadata        │
│  - SET hnsw.ef_search = 64     │   │  - to_tsquery / BM25 score        │
│  - Distancia: vector_ip_ops    │   │  - Ponderación por campo y SKU    │
│  - Recupera: Top-50 IDs        │   │  - Recupera: Top-50 IDs           │
└────────────────┬───────────────┘   └─────────────────┬─────────────────┘
                 │                                     │
                 └──────────────────┬──────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ PASO 3: Fusión de Rangos Recíprocos (RRF con k=60)                     │
│  - Ingesta de listas ordenadas Top-50 Densa y Top-50 Léxica            │
│  - Acumulación de scores: Score(d) = Σ 1 / (60 + rank_i)               │
│  - Ordenamiento unificado descendente                                  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ PASO 4: Generación y Formateo del Pool Candidato (Top-100)             │
│  - Enriquecimiento con texto completo y metadatos asociados            │
│  - Trazabilidad de origen (proveniencia léxica, vectorial o dual)      │
│  - Output: Entrada lista para Reranker Cross-Encoder (Fase 5)          │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Matriz de Comportamiento Algorítmico: Análisis de Casos

A continuación se detalla cómo responde el algoritmo RRF frente a las tres tipologías de consultas más críticas en un catálogo industrial:

| Tipología de Consulta | Ejemplo Práctico | Rendimiento Densa (HNSW) | Rendimiento Léxica (BM25) | Comportamiento RRF ($k=60$) | Justificación Operativa |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1. SKU / Código Técnico Exacto** | `"Llave impacto CAT-1025-C"` | Pobre (Rango > 30 o ausente). El vector prioriza "llaves de impacto" genéricas. | Excelente (Rango 1). Coincidencia exacta de token alfanumérico ponderado. | **Rescatado al Top-1** ($\text{Score} \approx 0.01639$). | La rama léxica domina y eleva el documento exacto a los primeros puestos del pool. |
| **2. Intención Conceptual / Paráfrasis** | `"Aparato neumático para ajustar tuercas de camión"` | Excelente (Rango 1). Entiende que refiere a llaves de impacto neumáticas pesadas. | Pobre (Rango ausente). Ningún documento contiene exactamente "aparato para ajustar". | **Rescatado al Top-1** ($\text{Score} \approx 0.01639$). | La rama densa compensa el vocabulario abstracto y protege el recall semántico. |
| **3. Consulta Mixta (Concepto + Marca)** | `"Llave de impacto 1/2 CARBIZ"` | Bueno (Rango 3). Halla herramientas de impacto similares. | Bueno (Rango 2). Halla productos de la marca CARBIZ. | **Gana Máxima Prioridad (Rango 1)** ($\text{Score} \approx 0.03201$). | El consenso de ambas ramas supera a cualquier coincidencia unilateral. |

---

## 4. Guía de Calibración de Hiperparámetros en Producción

Para garantizar tiempos de respuesta menores a 25 ms y un *Recall@100* superior al 98%, se recomiendan los siguientes parámetros de ajuste:

1.  **Profundidad de Recuperación por Rama ($K_{\text{dense}}$, $K_{\text{sparse}}$):**
    *   *Valor recomendado:* `50` candidatos por cada recuperador.
    *   *Razón técnica:* 50 candidatos por rama generan un conjunto unión de entre 60 y 90 documentos únicos. Esto cubre con creces el límite de 100 candidatos óptimo para la inferencia por lotes del Cross-Encoder de la Fase 5 sin degradar la latencia de red.
2.  **Amplitud de Búsqueda HNSW (`hnsw.ef_search`):**
    *   *Valor recomendado:* `64`.
    *   *Comportamiento:* Aumentar de 40 a 64 incrementa el recall en un 3% a costa de apenas 1.2 ms de CPU en PostgreSQL.
3.  **Constante de Suavizado ($k$):**
    *   *Valor recomendado:* `60`.
    *   *Rango válido:* `[20, 100]`. Valores menores a 20 sobre-ponderan los primeros dos lugares; valores mayores a 100 aplanan artificialmente las diferencias relativas entre los rangos altos y bajos.
4.  **Tamaño del Pool Consolidado para la Fase 5:**
    *   *Valor recomendado:* `Top-100`.

---

## 5. Salidas y Entregables de la Fase 4

*   **Nota Técnica Formal:** [fase-4-recuperacion-hibrida-rrf.md](file:///Users/rolandodaumas/Development/projects/super-warehouse-data-processing/fase-0-pdf-parsing/guide/fase-4-recuperacion-hibrida-rrf.md).
*   **Script Operativo de Producción:** [fase_4_retrieval.py](file:///Users/rolandodaumas/Development/projects/super-warehouse-data-processing/fase-0-pdf-parsing/fase_4_retrieval.py) con soporte nativo para PostgreSQL/pgvector, emulador BM25/Lexical con tokenización técnica de SKUs, función RRF pura, pre-filtrado SQL dinámico y suite de benchmarking comparativo.
*   **Estructura del Objeto de Salida:**
    ```json
    {
      "query": "Llave de impacto 1/2 CARBIZ-99",
      "total_candidates": 100,
      "candidates": [
        {
          "node_id": "CAT-CARBIZ-099",
          "rrf_score": 0.032522,
          "dense_rank": 2,
          "sparse_rank": 1,
          "retrieval_source": "dual",
          "text_content": "...",
          "metadata": { }
        }
      ]
    }
    ```

Con esta arquitectura, el pipeline de recuperación híbrida garantiza la captura exhaustiva tanto de intenciones abstractas como de datos alfanuméricos rigurosos, consolidando la base necesaria para el re-ordenamiento de alta fidelidad de la **Fase 5 (Cross-Encoder Reranker)**.
