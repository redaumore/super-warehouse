# Nota Técnica: Generación de Embeddings y Vectorización Optimizada (Fase 2) para Catálogo de Productos

Esta nota técnica consolida la teoría fundamental de toma de decisiones operativas y la aplicación metodológica para la **Fase 2: Generación de Embeddings y Vectorización Optimizada** de un pipeline de RAG (Retrieval-Augmented Generation) de nivel de producción sobre el catálogo de ferretería.

---

## 1. Fundamentos Teóricos: ¿Cómo Operar y Optimizar la Vectorización Semántica?

La vectorización semántica consiste en transformar cadenas de texto estructuradas en representaciones numéricas continuas de alta dimensionalidad (vectores densos), permitiendo que la similitud conceptual y contextual se mida mediante operaciones de álgebra lineal.

```
Texto Enriquecido (YAML) ──► [ Modelo Transformador ] ──► Vector Latente (3072 dims) ──► [ Truncamiento MRL ] ──► Vector Compacto (256 dims) ──► [ Normalización L2 ] ──► Vector Unitario
```

### A. Aprendizaje de Representación Matryoshka (MRL - Matryoshka Representation Learning)
*   **En qué consiste:** Modelos modernos de embeddings (como `text-embedding-3-large` de OpenAI o `Amazon Titan Embeddings V2`) son entrenados bajo una estructura jerárquica anidada (similar a las muñecas rusas Matryoshka). La información semántica más discriminativa y de mayor peso conceptual se comprime y ordena deliberadamente en las primeras dimensiones del vector.
*   **Impacto en Infraestructura y Rendimiento:** Permite truncar o recortar el vector resultante a una dimensión menor (ej. de 3072 a **256 dimensiones**) descartando el resto del array. Esto logra un **ahorro del 91.6% en memoria RAM y almacenamiento físico** para los grafos HNSW en la base de datos vectorial, reteniendo más del 98% del rendimiento semántico (*recall*) original.

### B. Normalización Unitaria ($L_2$) y Equivalencia Matemática de Métricas
*   **En qué consiste:** Un vector $\mathbf{v} = [v_1, v_2, \dots, v_d]$ está normalizado si su norma euclidiana es unitaria:
    $$\|\mathbf{v}\|_2 = \sqrt{\sum_{i=1}^d v_i^2} = 1.0$$
*   **Ventaja Operativa:** Cuando todos los vectores del corpus y los vectores de consulta son unitarios, la **Similitud del Coseno**, el **Producto Escalar (*Dot Product / Inner Product*)** y la **Distancia Euclidiana ($L_2$)** son monótonamente equivalentes:
    $$\text{Cosine}(\mathbf{u}, \mathbf{v}) = \frac{\mathbf{u} \cdot \mathbf{v}}{\|\mathbf{u}\|_2 \|\mathbf{v}\|_2} = \mathbf{u} \cdot \mathbf{v} = 1 - \frac{\|\mathbf{u} - \mathbf{v}\|_2^2}{2}$$
    Esto permite configurar el índice en la Fase 3 utilizando directamente el **producto escalar**, el cual es computacionalmente mucho más rápido y económico en uso de CPU que calcular divisiones y raíces cuadradas por cada nodo del grafo HNSW.

---

## 2. Metodología: Pipeline de Vectorización (Fase 2)

La Fase 2 toma la lista de objetos `Node` generada en la Fase 1 y genera un archivo estructurado con los vectores listos para ingestar en la base de datos vectorial.

```
┌─────────────────────────┐
│ Nodos Fase 1 (JSON)     │
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐     ┌────────────────────────────────────────────────────────┐
│ Batching (100 items)    │────►│ Llamada API Embeddings (con Exponential Backoff)       │
└─────────────────────────┘     └───────────────────────────┬────────────────────────────┘
                                                            │
                                                            ▼
┌─────────────────────────┐     ┌────────────────────────────────────────────────────────┐
│ Normalización L2        │◄────│ Truncamiento Dimensional (Matryoshka MRL a 256 dims)   │
└────────────┬────────────┘     └────────────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────┐
│ Validación & QA Suite   │
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│ Entregable JSON Fase 2  │
└─────────────────────────┘
```

### Pasos Operativos:
1.  **Ingesta y Procesamiento por Lotes (*Batching*):** Agrupar los fragmentos en lotes (ej. 100 nodos por petición) para minimizar la latencia de red y amortizar el costo de conexión HTTP.
2.  **Manejo Resiliente de API (*Exponential Backoff*):** Implementar lógica de reintentos ante códigos de error HTTP 429 (*Rate Limit*) o fluctuaciones temporales de red.
3.  **Compresión Dimensional:** Generar o recortar las representaciones a 256 dimensiones float32.
4.  **Normalización de Seguridad:** Aplicar `vector / np.linalg.norm(vector)` para garantizar matemáticamente la condición de vector unitario.
5.  **Persistencia del Payload:** Asociar a cada vector su identificador unívoco (`node_id`), el texto fuente contextualizado (`text_content`) y su diccionario íntegro de metadatos (`metadata`).

---

## 3. Suite de Validación y Control de Calidad Numérico

Antes de certificar la salida de la Fase 2, se ejecutan las siguientes pruebas de aceptación:

| Validación | Criterio de Aceptación | Propósito |
| :--- | :--- | :--- |
| **Integridad Dimensional** | `len(v) == 256` en el 100% de registros | Evita errores de inserción o desalineación en el esquema de la base vectorial. |
| **Integridad Numérica** | Cero valores `NaN`, `Inf` o vectores nulos | Previene corrupción matemática en las comparaciones de distancia. |
| **Normalización $L_2$** | $\|\mathbf{v}\|_2 = 1.0 \pm 10^{-5}$ | Garantiza equivalencia entre producto escalar y similitud coseno. |
| **Cobertura 1:1** | `Total Vectores == Total Nodos Fase 1 (179)` | Asegura que ningún producto o especificación quedó omitido en la ingesta. |
| **Sanity Check Semántico** | Score Par Similar $> 0.70$ / Score Par Disímil $< 0.40$ | Comprueba que la reducción dimensional conserva la capacidad de discriminación conceptual. |

---

## 4. Ejemplo de Transformación del Entregable (Fase 2)

A continuación se detalla la correspondencia entre la entrada de la Fase 1 y el registro enriquecido final de la Fase 2:

### 1. Entrada desde Fase 1 (`catalogo_nodes.json`):
```json
{
  "node_id": "node_prod_100001",
  "text_to_embed": "proveedor: Ferretera del Norte\ncategoria_padre: Fijaciones y sujeciones\ncategoria: Abrazaderas\nsubcategoria: Abrazaderas de acero a cremallera\nmarca: CARBIZ\nnombre: Abrazadera de acero Americana a Cremallera artículo 00, apertura 9/13 mm\ndescripcion: Abrazadera de acero CARBIZ Americana a Cremallera, con fleje de 13 mm de ancho, artículo 00 y rango de apertura de 9/13 mm. Se comercializa por unidad con paquete de 10 unidades.\ntipo: Americana a Cremallera\nfleje_ancho: 13 mm\narticulo: 00\napertura: 9/13 mm\nunidad_venta: c/u\npaquete_cantidad: 10",
  "text_length_char": 544,
  "text_length_tokens": 124,
  "metadata": {
    "pagina": 4,
    "codigo": "100001",
    "proveedor": "Ferretera del Norte",
    "marca": "CARBIZ",
    "categoria": "Abrazaderas",
    "subcategoria": "Abrazaderas de acero a cremallera",
    "articulo": "00",
    "apertura": "9/13 mm"
  }
}
```

### 2. Salida Generada en Fase 2 (`catalogo_embeddings.json`):
```json
{
  "metadata": {
    "source_nodes_file": "catalogo_nodes.json",
    "embedding_model": "text-embedding-3-large",
    "dimensions": 256,
    "total_vectors": 179,
    "created_at": "2026-08-28T12:30:34.540734+00:00"
  },
  "records": [
    {
      "node_id": "node_prod_100001",
      "embedding": [
        -0.03420588746666908,
        0.01582910493016243,
        0.08912401201948215,
        "...",
        -0.07366013526916504
      ],
      "dimension": 256,
      "norm_l2": 1.0,
      "text_content": "proveedor: Ferretera del Norte\ncategoria_padre: Fijaciones y sujeciones\ncategoria: Abrazaderas\nsubcategoria: Abrazaderas de acero a cremallera\nmarca: CARBIZ\nnombre: Abrazadera de acero Americana a Cremallera artículo 00, apertura 9/13 mm\ndescripcion: Abrazadera de acero CARBIZ Americana a Cremallera, con fleje de 13 mm de ancho, artículo 00 y rango de apertura de 9/13 mm. Se comercializa por unidad con paquete de 10 unidades.\ntipo: Americana a Cremallera\nfleje_ancho: 13 mm\narticulo: 00\napertura: 9/13 mm\nunidad_venta: c/u\npaquete_cantidad: 10",
      "metadata": {
        "pagina": 4,
        "codigo": "100001",
        "proveedor": "Ferretera del Norte",
        "marca": "CARBIZ",
        "categoria": "Abrazaderas",
        "subcategoria": "Abrazaderas de acero a cremallera",
        "tipo": "Americana a Cremallera",
        "fleje_ancho": "13 mm",
        "articulo": "00",
        "apertura": "9/13 mm"
      }
    }
  ]
}
```

---

## 5. Próximos Pasos: Conexión con la Fase 3

Con los vectores validados y normalizados a 256 dimensiones, el sistema queda listo para la **Fase 3: Diseño de Infraestructura e Indexación Vectorial (ANN Indexing)**:
1.  **Definición del Esquema Físico:** Configurar la colección en la base de datos vectorial estableciendo la métrica de distancia en `DotProduct / Cosine` y la dimensión estricta en `256`.
2.  **Calibración del Grafo HNSW:** Configurar los hiperparámetros de construcción (`M=16` a `32`, `efConstruction=128` a `256`) y de consulta (`efSearch=64`).
3.  **Indexación de Metadatos:** Crear índices estructurados (B-Tree o Invertidos) sobre los campos `codigo`, `marca` y `categoria` para habilitar el pre-filtrado determinista de alta velocidad.
