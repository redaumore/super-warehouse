# Nota Técnica: Infraestructura e Indexación Vectorial con pgvector (Fase 3) para Catálogo de Productos

Esta nota técnica consolida la teoría fundamental de toma de decisiones operativas y la aplicación metodológica para la **Fase 3: Infraestructura e Indexación Vectorial (ANN / HNSW)** de un pipeline de RAG (Retrieval-Augmented Generation) de nivel de producción sobre el catálogo de ferretería, utilizando **PostgreSQL con la extensión `pgvector`**.

---

## 1. Fundamentos Teóricos: ¿Cómo Operar y Optimizar la Indexación Vectorial?

La indexación vectorial resuelve el problema del cuello de botella computacional en la búsqueda semántica. Mientras que una búsqueda exacta por fuerza bruta (*k-Nearest Neighbors* o kNN) requiere calcular la distancia contra todos los vectores del corpus con complejidad $\mathcal{O}(N \cdot d)$, los algoritmos de **Búsqueda Aproximada de Vecinos Más Cercanos (ANN - Approximate Nearest Neighbors)** permiten recuperar los elementos más similares en tiempo logarítmico $\mathcal{O}(\log N)$ manteniendo un *Recall* $\ge 0.95$.

```
Consulta (Vector 256 dims) ──► [ Punto de Entrada ] ──► [ Ruteo por Hubs / Capas Superiores ] ──► [ Beam Search Local (efSearch) ] ──► Top-K Candidatos
```

### A. Algoritmo HNSW (*Hierarchical Navigable Small World*) y la Hipótesis de la Autopista de Hubs
*   **En qué consiste HNSW:** HNSW organiza los vectores en un grafo de múltiples capas inspirado en las listas por salto (*skip-lists*). Las capas superiores contienen pocos nodos con aristas largas (para realizar saltos rápidos a través del espacio vectorial), mientras que la capa base (Capa 0) contiene todos los nodos con conexiones densas para la exploración fina.
*   **La Hipótesis de la Autopista de Hubs (*Hub Highway Hypothesis*):** En espacios de alta dimensionalidad ($d \ge 32$, como nuestros embeddings de 256 dimensiones generados en la Fase 2), el fenómeno geométrico de *Hubness* hace que ciertos nodos acumulen conexiones de forma natural en la Capa 0, formando una "autopista de conexión rápida". Esto permite que la navegación alcance la vecindad óptima de forma casi instantánea incluso sin depender críticamente de las capas superiores, justificando el uso de configuraciones de grafos con menor conectividad ($M=16$) para ahorrar memoria RAM sin degradar el *Recall*.

### B. Calibración de Hiperparámetros en Grafos HNSW
El rendimiento del índice se rige por tres variables operativas críticas:

1.  **$M$ (Número máximo de conexiones bidireccionales por nodo):**
    *   *Rango recomendado:* `16` a `32` (para catálogos técnicos con 256 dims, $M=16$ es el punto dulce).
    *   *Impacto:* Determina el uso de memoria RAM del servidor ($\approx \mathcal{O}(M \cdot N)$) y la robustez del grafo.
2.  **$efConstruction$ (Amplitud de exploración durante la construcción):**
    *   *Rango recomendado:* `128` a `256`.
    *   *Impacto:* Cantidad de vecinos evaluados al insertar cada vector. Valores altos eliminan "callejones sin salida" en el grafo. Solo afecta el tiempo de creación inicial del índice; **no incrementa el uso de RAM ni ralentiza las consultas**.
3.  **$efSearch$ o $ef$ (Amplitud de exploración en tiempo de consulta):**
    *   *Rango recomendado:* `40` a `100` (Default: `64`).
    *   *Impacto:* Es el único parámetro modificable en caliente en tiempo de ejecución (`SET hnsw.ef_search = 64;`). Controla la profundidad del *beam search* durante la consulta: a mayor valor, mayor *Recall*, con un incremento marginal de latencia (1–3 ms).

### C. Métrica de Distancia y Eficiencia de CPU (`vector_ip_ops`)
Dado que los vectores fueron normalizados unitariamente bajo la norma $L_2$ en la Fase 2 ($\|\mathbf{v}\|_2 = 1.0$), la **Similitud Coseno** es matemáticamente idéntica al **Producto Escalar (*Inner Product*)**:
$$\text{Cosine}(\mathbf{u}, \mathbf{v}) = \mathbf{u} \cdot \mathbf{v}$$
En `pgvector`, se indexa utilizando la clase de operadores de producto escalar (`vector_ip_ops` / operador `<#>`), evitando el cálculo repetitivo de raíces cuadradas y divisiones, lo que reduce el consumo de ciclos de CPU por cada salto en el grafo.

### D. Pre-filtrado vs. Post-filtrado de Metadatos
*   **El Problema del Post-filtrado:** Si se recuperan primero los 10 vectores más cercanos globalmente y luego se aplica un filtro (ej. `marca = 'CARBIZ'`), es probable que los 10 resultados pertenezcan a otras marcas y la respuesta quede vacía, destruyendo el recall.
*   **Pre-filtrado Eficiente en PostgreSQL:** Al utilizar `pgvector` dentro de PostgreSQL, el planificador de consultas combina índices relacionales tradicionales (**B-Tree** para campos unívocos e índices **GIN** para el `JSONB` de metadatos) con el escaneo HNSW, garantizando que la exploración de vecinos se restrinja únicamente a las filas válidas antes de rankear.

---

## 2. Metodología: Pipeline de Infraestructura e Indexación (Fase 3)

La Fase 3 toma la salida de la Fase 2 (`nodos_vectorizados.json`) y construye la base de datos relacional y vectorial lista para el motor de recuperación.

```
┌────────────────────────────────────────────────────────┐
│ INPUT: Nodos Vectorizados Fase 2 (JSON)                │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│ PASO 1: Aprovisionamiento DDL en PostgreSQL            │
│  - Extensión vector                                    │
│  - Tabla catalogo_productos_rag                        │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│ PASO 2: Ingesta Masiva por Lotes (Batch DML)           │
│  - Inserción transaccional por lotes (psycopg3)        │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│ PASO 3: Construcción de Índices HNSW y B-Tree / GIN    │
│  - HNSW (vector_ip_ops, M=16, ef_construction=128)     │
│  - Índices B-Tree en marca, categoria, codigo_producto │
│  - Índice GIN en metadata JSONB                        │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│ PASO 4: Calibración Runtime (ef_search) & QA Suite     │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│ OUTPUT: pgvector DB Productiva y Certificada           │
└────────────────────────────────────────────────────────┘
```

---

## 3. Aplicación Práctica: Implementación sobre el Catálogo de Ferretería

### A. Esquema Relacional DDL (`schema.sql`)

```sql
-- 1. Habilitar extensión pgvector
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. Crear tabla optimizada para RAG y catálogo transaccional
CREATE TABLE IF NOT EXISTS catalogo_productos_rag (
    node_id VARCHAR(64) PRIMARY KEY,
    codigo_producto VARCHAR(64),
    marca VARCHAR(128),
    categoria_padre VARCHAR(128),
    categoria VARCHAR(128),
    subcategoria VARCHAR(128),
    pagina_origen INT,
    es_tabla BOOLEAN DEFAULT FALSE,
    text_content TEXT NOT NULL,
    metadata JSONB NOT NULL,
    embedding vector(256) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
```

### B. Creación de Índices (`indexes.sql`)

> **Regla de Producción:** Crear los índices HNSW **después** de la carga masiva de datos inicial para maximizar la velocidad de construcción y optimizar la topología del grafo.

```sql
-- Índice vectorial HNSW con producto escalar
CREATE INDEX IF NOT EXISTS idx_catalogo_hnsw_embedding 
ON catalogo_productos_rag 
USING hnsw (embedding vector_ip_ops)
WITH (
    m = 16,
    ef_construction = 128
);

-- Índices B-Tree para pre-filtrado relacional rápido
CREATE INDEX IF NOT EXISTS idx_catalogo_codigo ON catalogo_productos_rag(codigo_producto);
CREATE INDEX IF NOT EXISTS idx_catalogo_marca ON catalogo_productos_rag(marca);
CREATE INDEX IF NOT EXISTS idx_catalogo_categoria ON catalogo_productos_rag(categoria);

-- Índice GIN para consultas flexibles sobre metadatos JSON
CREATE INDEX IF NOT EXISTS idx_catalogo_metadata_gin ON catalogo_productos_rag USING gin (metadata);
```

### C. Pipeline Automatizado de Ingesta e Indexación (`fase_3_pgvector.py`)

```python
import json
import psycopg
from psycopg.rows import dict_row
from pgvector.psycopg import register_vector
import numpy as np

DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/rag_catalog_db"

def ejecutar_fase_3(json_fase_2_path: str):
    print("=== Iniciando Fase 3: Ingesta e Indexación en pgvector ===")
    
    # 1. Cargar datos de la Fase 2
    with open(json_fase_2_path, 'r', encoding='utf-8') as f:
        datos = json.load(f)
    nodos = datos.get("nodos", [])
    print(f"[*] Total de nodos leídos desde Fase 2: {len(nodos)}")
    
    # 2. Conectar a PostgreSQL
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        register_vector(conn)
        
        with conn.cursor() as cur:
            # 3. Crear Esquema DDL
            cur.execute("""
                CREATE TABLE IF NOT EXISTS catalogo_productos_rag (
                    node_id VARCHAR(64) PRIMARY KEY,
                    codigo_producto VARCHAR(64),
                    marca VARCHAR(128),
                    categoria_padre VARCHAR(128),
                    categoria VARCHAR(128),
                    subcategoria VARCHAR(128),
                    pagina_origen INT,
                    es_tabla BOOLEAN DEFAULT FALSE,
                    text_content TEXT NOT NULL,
                    metadata JSONB NOT NULL,
                    embedding vector(256) NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            print("[✓] Esquema DDL aprovisionado.")
            
            # 4. Ingesta por Lotes (Batch DML)
            insert_query = """
                INSERT INTO catalogo_productos_rag (
                    node_id, codigo_producto, marca, categoria_padre,
                    categoria, subcategoria, pagina_origen, es_tabla,
                    text_content, metadata, embedding
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (node_id) DO UPDATE SET
                    text_content = EXCLUDED.text_content,
                    metadata = EXCLUDED.metadata,
                    embedding = EXCLUDED.embedding;
            """
            
            lote = []
            for n in nodos:
                meta = n.get("metadata", {})
                vec = np.array(n["embedding"], dtype=np.float32)
                lote.append((
                    n["node_id"],
                    meta.get("codigo_producto"),
                    meta.get("marca"),
                    meta.get("categoria_padre"),
                    meta.get("categoria"),
                    meta.get("subcategoria"),
                    meta.get("pagina_origen"),
                    meta.get("es_tabla", False),
                    n["text_content"],
                    json.dumps(meta),
                    vec
                ))
            
            cur.executemany(insert_query, lote)
            print(f"[✓] {len(lote)} registros insertados con éxito.")
            
            # 5. Construcción de Índices HNSW y B-Tree
            print("[*] Construyendo índice HNSW y estructuras de pre-filtrado...")
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_catalogo_hnsw_embedding 
                ON catalogo_productos_rag 
                USING hnsw (embedding vector_ip_ops)
                WITH (m = 16, ef_construction = 128);
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_catalogo_codigo ON catalogo_productos_rag(codigo_producto);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_catalogo_marca ON catalogo_productos_rag(marca);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_catalogo_categoria ON catalogo_productos_rag(categoria);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_catalogo_metadata_gin ON catalogo_productos_rag USING gin (metadata);")
            print("[✓] Todos los índices fueron compilados en PostgreSQL.")

if __name__ == "__main__":
    ejecutar_fase_3("nodos_vectorizados.json")
```

### D. Ejemplo de Consulta en Tiempo de Ejecución con Pre-filtrado

```sql
-- Calibrar la amplitud de exploración en consulta (ef_search)
SET hnsw.ef_search = 64;

-- Búsqueda de alta precisión: Top-5 productos de 'CARBIZ' más cercanos al vector de consulta
SELECT 
    node_id,
    codigo_producto,
    marca,
    categoria,
    text_content,
    (embedding <#> '[0.052, -0.018, ...]'::vector) * -1 AS similarity_score
FROM catalogo_productos_rag
WHERE marca = 'CARBIZ'
ORDER BY embedding <#> '[0.052, -0.018, ...]'::vector ASC
LIMIT 5;
```

---

## 4. Suite de Validación y Control de Calidad Numérico y Operativo

Antes de dar por finalizada la Fase 3, se deben ejecutar y certificar las siguientes pruebas:

| Validación | Criterio de Aceptación | Propósito Operativo |
| :--- | :--- | :--- |
| **Paridad de Registros** | `COUNT(*) == 179` | Verifica que no existió pérdida de información durante la inserción en lote. |
| **Uso de Índice HNSW** | `EXPLAIN ANALYZE` muestra `Index Scan using idx_catalogo_hnsw_embedding` | Garantiza que PostgreSQL no recurra a un escaneo secuencial (*Seq Scan*). |
| **Pre-filtrado Eficiente** | Búsqueda con filtro `WHERE marca = '...'` se resuelve en $< 5\text{ ms}$ | Comprueba que el índice B-Tree/GIN reduce el espacio de búsqueda antes del cálculo vectorial. |
| **Fidelidad de Recall** | Coincidencia de vecinos Top-5 $\ge 98\%$ frente a búsqueda exacta (fuerza bruta sin índice) | Certifica que la aproximación HNSW con $M=16$ y $ef=64$ no genera falsos negativos. |
| **Latencia P95** | Latencia total de consulta $\le 10\text{ ms}$ | Valida que el sistema cumple con los requisitos de tiempo real para el usuario final. |
| **Consumo de Memoria** | Tamaño total del índice HNSW en RAM $< 5\text{ MB}$ para el catálogo actual | Asegura la viabilidad del despliegue en instancias de bajo costo computacional. |

---
