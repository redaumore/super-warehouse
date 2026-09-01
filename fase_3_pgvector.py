#!/usr/bin/env python3
"""
fase_3_pgvector.py
==================
Fase 3: Infraestructura e Indexación Vectorial (ANN / HNSW) con PostgreSQL + pgvector.

Este script implementa:
1. Conexión segura a PostgreSQL leyendo variables de entorno (.env o DATABASE_URL).
2. Aprovisionamiento de la extensión 'vector' y del esquema DDL con tipado relacional y vectorial.
3. Ingesta masiva transaccional por lotes (Batch DML Upsert) de nodos vectorizados.
4. Construcción y optimización de índices HNSW (vector_ip_ops) e índices relacionales (B-Tree y GIN).
5. Suite de validación, auditoría con EXPLAIN ANALYZE y calibración en caliente (ef_search).
"""

import os
import sys
import json
import time
import logging
import argparse
from typing import List, Dict, Any, Optional, Tuple
import psycopg
from psycopg import sql
from pgvector.psycopg import register_vector
from dotenv import load_dotenv

# Cargar variables de entorno desde .env
load_dotenv()

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("RAG_Fase_3_pgvector")


def build_db_url() -> str:
    """Construye la URL de conexión a PostgreSQL desde las variables de entorno."""
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        return db_url

    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "postgres")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db_name = os.getenv("POSTGRES_DB", "postgres")

    return f"postgresql://{user}:{password}@{host}:{port}/{db_name}"


class PgVectorManager:
    """
    Administrador de infraestructura, ingesta e indexación vectorial sobre PostgreSQL + pgvector.
    """

    def __init__(self, db_url: str, table_name: str, dimension: int = 256):
        self.db_url = db_url
        self.table_name = table_name
        self.dimension = dimension
        # Sanitizar nombre de tabla para identificadores de índices
        self.clean_tbl = "".join(c if c.isalnum() else "_" for c in table_name)

    def _get_connection(self) -> psycopg.Connection:
        """Establece conexión a la base de datos con adaptador pgvector."""
        conn = psycopg.connect(self.db_url, autocommit=True)
        try:
            register_vector(conn)
        except Exception as e:
            logger.debug(f"Aviso al registrar vector con psycopg3: {e}")
        return conn

    @staticmethod
    def format_vector_literal(vec: List[float]) -> str:
        """Formatea un array de floats a representación literal vector '[x, y, ...]'."""
        return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"

    # -------------------------------------------------------------------------
    # 1. Aprovisionamiento de Esquema DDL
    # -------------------------------------------------------------------------
    def init_schema(self, recreate: bool = False) -> None:
        """
        Crea la extensión 'vector' y la tabla relacional con tipado vectorial estricto.
        """
        logger.info(f"Aprovisionando esquema DDL para tabla '{self.table_name}' (vector {self.dimension} dims)...")
        
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                # 1. Habilitar extensión vector
                cur.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS vector;"))
                
                # 2. Recrear tabla si fue solicitado
                if recreate:
                    logger.warning(f"Flag --recreate-table activo: Eliminando tabla '{self.table_name}' existente...")
                    cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE;").format(sql.Identifier(self.table_name)))

                # 3. Crear tabla con todos los campos normalizados
                ddl_table = sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    node_id VARCHAR(64) PRIMARY KEY,
                    codigo_producto VARCHAR(64),
                    codigo_orig VARCHAR(64),
                    nombre_proveedor VARCHAR(128),
                    codigo_proveedor VARCHAR(32),
                    marca VARCHAR(128),
                    categoria_padre VARCHAR(128),
                    categoria VARCHAR(128),
                    subcategoria VARCHAR(128),
                    precio NUMERIC(12, 2),
                    moneda VARCHAR(16),
                    pagina_origen INT,
                    es_tabla BOOLEAN DEFAULT FALSE,
                    text_content TEXT NOT NULL,
                    metadata JSONB NOT NULL,
                    embedding vector({}) NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """).format(sql.Identifier(self.table_name), sql.Literal(self.dimension))
                cur.execute(ddl_table)

        logger.info(f"[✓] Esquema DDL aprovisionado exitosamente en tabla '{self.table_name}'.")

    # -------------------------------------------------------------------------
    # 2. Ingesta Masiva Transaccional (Batch DML Upsert)
    # -------------------------------------------------------------------------
    def ingest_records(self, records: List[Dict[str, Any]], batch_size: int = 100) -> int:
        """
        Inserta de forma idempotente (upsert) lotes de nodos vectorizados.
        """
        if not records:
            logger.warning("No hay registros para ingestar.")
            return 0

        logger.info(f"Iniciando ingesta masiva de {len(records)} nodos (Batch size: {batch_size})...")
        t_start = time.time()

        insert_sql = sql.SQL("""
        INSERT INTO {} (
            node_id, codigo_producto, codigo_orig, nombre_proveedor, codigo_proveedor,
            marca, categoria_padre, categoria, subcategoria, precio, moneda,
            pagina_origen, es_tabla, text_content, metadata, embedding
        ) VALUES (
            %(node_id)s, %(codigo_producto)s, %(codigo_orig)s, %(nombre_proveedor)s, %(codigo_proveedor)s,
            %(marca)s, %(categoria_padre)s, %(categoria)s, %(subcategoria)s, %(precio)s, %(moneda)s,
            %(pagina_origen)s, %(es_tabla)s, %(text_content)s, %(metadata)s, %(embedding)s
        )
        ON CONFLICT (node_id) DO UPDATE SET
            codigo_producto = EXCLUDED.codigo_producto,
            codigo_orig = EXCLUDED.codigo_orig,
            nombre_proveedor = EXCLUDED.nombre_proveedor,
            codigo_proveedor = EXCLUDED.codigo_proveedor,
            marca = EXCLUDED.marca,
            categoria_padre = EXCLUDED.categoria_padre,
            categoria = EXCLUDED.categoria,
            subcategoria = EXCLUDED.subcategoria,
            precio = EXCLUDED.precio,
            moneda = EXCLUDED.moneda,
            pagina_origen = EXCLUDED.pagina_origen,
            es_tabla = EXCLUDED.es_tabla,
            text_content = EXCLUDED.text_content,
            metadata = EXCLUDED.metadata,
            embedding = EXCLUDED.embedding;
        """).format(sql.Identifier(self.table_name))

        total_inserted = 0
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                batch = []
                for i, rec in enumerate(records):
                    emb = rec.get("embedding", [])
                    if len(emb) != self.dimension:
                        raise ValueError(
                            f"Error de dimensión en nodo {rec.get('node_id')}: "
                            f"esperada {self.dimension}, recibida {len(emb)}."
                        )

                    meta = rec.get("metadata", {})
                    
                    # Parsear precio de manera segura
                    raw_precio = meta.get("precio")
                    precio_val = None
                    if raw_precio is not None:
                        try:
                            precio_val = float(raw_precio)
                        except (ValueError, TypeError):
                            precio_val = None

                    # Parsear página de manera segura
                    raw_pagina = meta.get("pagina") or meta.get("pagina_origen")
                    pagina_val = None
                    if raw_pagina is not None:
                        try:
                            pagina_val = int(raw_pagina)
                        except (ValueError, TypeError):
                            pagina_val = None

                    vec_literal = self.format_vector_literal(emb)
                    meta_json = json.dumps(meta, ensure_ascii=False)

                    item = {
                        "node_id": rec["node_id"],
                        "codigo_producto": meta.get("codigo") or meta.get("codigo_producto"),
                        "codigo_orig": meta.get("codigo_orig"),
                        "nombre_proveedor": meta.get("nombre_proveedor") or meta.get("proveedor"),
                        "codigo_proveedor": meta.get("codigo_proveedor"),
                        "marca": meta.get("marca"),
                        "categoria_padre": meta.get("categoria_padre"),
                        "categoria": meta.get("categoria"),
                        "subcategoria": meta.get("subcategoria"),
                        "precio": precio_val,
                        "moneda": meta.get("moneda"),
                        "pagina_origen": pagina_val,
                        "es_tabla": bool(meta.get("es_tabla", False)),
                        "text_content": rec.get("text_content") or rec.get("text_to_embed", ""),
                        "metadata": meta_json,
                        "embedding": vec_literal
                    }
                    batch.append(item)

                    if len(batch) >= batch_size or (i == len(records) - 1):
                        cur.executemany(insert_sql, batch)
                        total_inserted += len(batch)
                        logger.info(f"   Progreso: {total_inserted}/{len(records)} registros procesados...")
                        batch = []

        elapsed = time.time() - t_start
        throughput = total_inserted / max(elapsed, 0.001)
        logger.info(f"[✓] Ingesta completada: {total_inserted} registros en {elapsed:.2f}s ({throughput:.1f} ops/s).")
        return total_inserted

    # -------------------------------------------------------------------------
    # 3. Compilación de Índices HNSW y Relacionales
    # -------------------------------------------------------------------------
    def create_indexes(self, m: int = 32, ef_construction: int = 200) -> None:
        """
        Compila el grafo de proximidad HNSW (vector_ip_ops) y las estructuras de pre-filtrado.
        """
        logger.info(f"Construyendo índice HNSW (m={m}, ef_construction={ef_construction}) e índices relacionales...")
        t_start = time.time()

        index_queries = [
            (
                "Índice HNSW Vectorial (vector_ip_ops)",
                sql.SQL("""
                CREATE INDEX IF NOT EXISTS {}
                ON {}
                USING hnsw (embedding vector_ip_ops)
                WITH (m = {}, ef_construction = {});
                """).format(
                    sql.Identifier(f"idx_{self.clean_tbl}_embedding_hnsw"),
                    sql.Identifier(self.table_name),
                    sql.Literal(m),
                    sql.Literal(ef_construction)
                )
            ),
            (
                "Índice B-Tree (codigo_producto)",
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}(codigo_producto);").format(
                    sql.Identifier(f"idx_{self.clean_tbl}_codigo"),
                    sql.Identifier(self.table_name)
                )
            ),
            (
                "Índice B-Tree (codigo_orig)",
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}(codigo_orig);").format(
                    sql.Identifier(f"idx_{self.clean_tbl}_codigo_orig"),
                    sql.Identifier(self.table_name)
                )
            ),
            (
                "Índice B-Tree (marca)",
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}(marca);").format(
                    sql.Identifier(f"idx_{self.clean_tbl}_marca"),
                    sql.Identifier(self.table_name)
                )
            ),
            (
                "Índice B-Tree (categoria)",
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}(categoria);").format(
                    sql.Identifier(f"idx_{self.clean_tbl}_categoria"),
                    sql.Identifier(self.table_name)
                )
            ),
            (
                "Índice B-Tree (categoria_padre)",
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}(categoria_padre);").format(
                    sql.Identifier(f"idx_{self.clean_tbl}_cat_padre"),
                    sql.Identifier(self.table_name)
                )
            ),
            (
                "Índice B-Tree (subcategoria)",
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}(subcategoria);").format(
                    sql.Identifier(f"idx_{self.clean_tbl}_subcat"),
                    sql.Identifier(self.table_name)
                )
            ),
            (
                "Índice B-Tree (nombre_proveedor)",
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}(nombre_proveedor);").format(
                    sql.Identifier(f"idx_{self.clean_tbl}_nom_prov"),
                    sql.Identifier(self.table_name)
                )
            ),
            (
                "Índice B-Tree (codigo_proveedor)",
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}(codigo_proveedor);").format(
                    sql.Identifier(f"idx_{self.clean_tbl}_cod_prov"),
                    sql.Identifier(self.table_name)
                )
            ),
            (
                "Índice B-Tree (pagina_origen)",
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}(pagina_origen);").format(
                    sql.Identifier(f"idx_{self.clean_tbl}_pagina"),
                    sql.Identifier(self.table_name)
                )
            ),
            (
                "Índice GIN (metadata JSONB)",
                sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} USING gin (metadata);").format(
                    sql.Identifier(f"idx_{self.clean_tbl}_metadata_gin"),
                    sql.Identifier(self.table_name)
                )
            )
        ]

        with self._get_connection() as conn:
            with conn.cursor() as cur:
                for label, stmt in index_queries:
                    logger.info(f"   Creando: {label}...")
                    cur.execute(stmt)

        elapsed = time.time() - t_start
        logger.info(f"[✓] Todos los índices fueron compilados exitosamente en {elapsed:.2f}s.")

    # -------------------------------------------------------------------------
    # 4. Búsqueda ANN y Calibración Runtime (ef_search)
    # -------------------------------------------------------------------------
    def search_ann(
        self,
        query_vector: List[float],
        top_k: int = 5,
        marca: Optional[str] = None,
        categoria: Optional[str] = None,
        ef_search: int = 128
    ) -> List[Dict[str, Any]]:
        """
        Ejecuta una búsqueda de vecinos más cercanos con pre-filtrado y calibración ef_search.
        """
        vec_literal = self.format_vector_literal(query_vector)

        where_clauses = []
        params: Dict[str, Any] = {}

        if marca:
            where_clauses.append(sql.SQL("marca = %(marca)s"))
            params["marca"] = marca
        if categoria:
            where_clauses.append(sql.SQL("categoria = %(categoria)s"))
            params["categoria"] = categoria

        where_sql = sql.SQL("WHERE {}").format(sql.SQL(" AND ").join(where_clauses)) if where_clauses else sql.SQL("")

        # Consulta con producto escalar invertido (-1) para obtener similitud de coseno (0.0 a 1.0)
        query_sql = sql.SQL("""
        SELECT 
            node_id,
            codigo_producto,
            codigo_orig,
            marca,
            categoria,
            nombre_proveedor,
            precio,
            moneda,
            pagina_origen,
            es_tabla,
            text_content,
            (embedding <#> {vec}::vector) * -1 AS similarity_score
        FROM {tbl}
        {where}
        ORDER BY embedding <#> {vec}::vector ASC
        LIMIT {limit};
        """).format(
            vec=sql.Literal(vec_literal),
            tbl=sql.Identifier(self.table_name),
            where=where_sql,
            limit=sql.Literal(int(top_k))
        )

        results = []
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SET hnsw.ef_search = {};").format(sql.Literal(int(ef_search))))
                cur.execute(query_sql, params)
                rows = cur.fetchall()
                for row in rows:
                    results.append({
                        "node_id": row[0],
                        "codigo_producto": row[1],
                        "codigo_orig": row[2],
                        "marca": row[3],
                        "categoria": row[4],
                        "nombre_proveedor": row[5],
                        "precio": float(row[6]) if row[6] is not None else None,
                        "moneda": row[7],
                        "pagina_origen": row[8],
                        "es_tabla": row[9],
                        "text_content": row[10],
                        "similarity_score": float(row[11])
                    })

        return results

    # -------------------------------------------------------------------------
    # 5. Auditoría con EXPLAIN ANALYZE
    # -------------------------------------------------------------------------
    def explain_ann(
        self,
        query_vector: List[float],
        top_k: int = 5,
        marca: Optional[str] = None,
        ef_search: int = 128
    ) -> str:
        """
        Ejecuta EXPLAIN (ANALYZE, BUFFERS) para certificar que el optimizador usa el índice HNSW.
        """
        vec_literal = self.format_vector_literal(query_vector)
        where_sql = sql.SQL("WHERE marca = {}").format(sql.Literal(marca)) if marca else sql.SQL("")

        sql_stmt = sql.SQL("""
        EXPLAIN (ANALYZE, BUFFERS)
        SELECT node_id, codigo_producto, marca,
               (embedding <#> {vec}::vector) * -1 AS score
        FROM {tbl}
        {where}
        ORDER BY embedding <#> {vec}::vector ASC
        LIMIT {limit};
        """).format(
            vec=sql.Literal(vec_literal),
            tbl=sql.Identifier(self.table_name),
            where=where_sql,
            limit=sql.Literal(int(top_k))
        )

        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SET hnsw.ef_search = {};").format(sql.Literal(int(ef_search))))
                cur.execute(sql_stmt)
                plan_lines = [r[0] for r in cur.fetchall()]
                return "\n".join(plan_lines)

    # -------------------------------------------------------------------------
    # 6. Suite de QA & Verificación Operativa
    # -------------------------------------------------------------------------
    def run_qa_suite(
        self,
        expected_count: int,
        sample_vector: List[float],
        sample_marca: Optional[str] = None,
        codigo_proveedor: Optional[str] = None
    ) -> None:
        """Ejecuta la batería completa de validación de infraestructura."""
        logger.info("==================================================")
        logger.info("INICIANDO SUITE DE CONTROL DE CALIDAD (QA FASE 3)")
        logger.info("==================================================")

        with self._get_connection() as conn:
            with conn.cursor() as cur:
                # 1. Paridad de Registros
                if codigo_proveedor:
                    cur.execute(
                        sql.SQL("SELECT COUNT(*) FROM {} WHERE codigo_proveedor = %s;").format(sql.Identifier(self.table_name)),
                        (codigo_proveedor,)
                    )
                    row_count = cur.fetchone()
                    actual_count = row_count[0] if row_count else 0
                    if actual_count != expected_count:
                        raise AssertionError(f"Fallo de paridad para proveedor '{codigo_proveedor}': esperados {expected_count} filas, encontradas {actual_count}.")
                    logger.info(f"✓ [1/4] Paridad de registros para proveedor '{codigo_proveedor}': {actual_count} filas == {expected_count} esperadas.")
                else:
                    cur.execute(sql.SQL("SELECT COUNT(*) FROM {};").format(sql.Identifier(self.table_name)))
                    row_count = cur.fetchone()
                    actual_count = row_count[0] if row_count else 0
                    if actual_count < expected_count:
                        raise AssertionError(f"Fallo de paridad: esperadas al menos {expected_count} filas, encontradas {actual_count}.")
                    logger.info(f"✓ [1/4] Paridad de registros: {actual_count} filas totales en tabla (batch actual: {expected_count}).")

                # 2. Verificación de No Nulos en Campos Críticos
                cur.execute(sql.SQL("""
                    SELECT 
                        COUNT(*) FILTER (WHERE embedding IS NULL) AS null_embeddings,
                        COUNT(*) FILTER (WHERE text_content IS NULL OR text_content = '') AS empty_texts,
                        COUNT(*) FILTER (WHERE es_tabla IS TRUE) AS total_tablas,
                        COUNT(*) FILTER (WHERE es_tabla IS FALSE) AS total_no_tablas
                    FROM {};
                """).format(sql.Identifier(self.table_name)))
                row = cur.fetchone()
                if row is None:
                    raise AssertionError("No se pudo obtener métricas de integridad de datos.")
                null_emb, empty_txt, n_tab, n_notab = row[0], row[1], row[2], row[3]
                if null_emb > 0 or empty_txt > 0:
                    raise AssertionError(f"Corrupción de datos: {null_emb} embeddings nulos, {empty_txt} textos vacíos.")
                logger.info(f"✓ [2/4] Integridad de datos: 0 nulos. Desglose es_tabla: {n_tab} estructurados en tabla / {n_notab} conceptuales.")

                # 3. Verificación de Auditoría EXPLAIN ANALYZE
                _ = self.explain_ann(sample_vector, top_k=5, marca=sample_marca, ef_search=64)
                logger.info("✓ [3/4] Auditoría de Plan de Consulta EXPLAIN ANALYZE ejecutada.")

                # 4. Búsqueda ANN de Prueba
                results = self.search_ann(sample_vector, top_k=3, marca=sample_marca, ef_search=64)
                logger.info(f"✓ [4/4] Búsqueda ANN de prueba exitosa (Top-{len(results)} recuperados).")
                for rank, res in enumerate(results, 1):
                    logger.info(
                        f"   [{rank}] Similitud: {res['similarity_score']:.4f} | "
                        f"Código: {res['codigo_producto']} ({res['codigo_orig']}) | "
                        f"Marca: {res['marca']} | es_tabla: {res['es_tabla']} | "
                        f"Proveedor: {res['nombre_proveedor']}"
                    )

        logger.info("==================================================")
        logger.info("SUITE DE QA FASE 3 SUPERADA CON ÉXITO")
        logger.info("==================================================")


# =============================================================================
# Helper de carga de JSON
# =============================================================================
def load_embeddings_json(filepath: str) -> Tuple[List[Dict[str, Any]], int]:
    """
    Carga el JSON de embeddings generado en la Fase 2 y extrae los registros y su dimensión.
    Soporta formato entregable ({metadata, records}), formato lista directa o {nodos}.
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"No se encontró el archivo de embeddings: '{filepath}'")

    logger.info(f"Cargando archivo de embeddings desde '{filepath}'...")
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    records: List[Dict[str, Any]] = []
    dim = 256

    if isinstance(data, dict):
        if "metadata" in data and isinstance(data["metadata"], dict):
            dim = data["metadata"].get("dimensions", 256)
        if "records" in data and isinstance(data["records"], list):
            records = data["records"]
        elif "nodos" in data and isinstance(data["nodos"], list):
            records = data["nodos"]
        elif "nodes" in data and isinstance(data["nodes"], list):
            records = data["nodes"]
    elif isinstance(data, list):
        records = data

    if not records:
        raise ValueError(f"El archivo '{filepath}' no contiene registros de embeddings válidos.")

    # Validar dimensión del primer registro si está disponible
    if "embedding" in records[0]:
        dim = len(records[0]["embedding"])

    logger.info(f"Total de registros cargados: {len(records)} (Dimensión detectada: {dim}).")
    return records, dim


# =============================================================================
# Entrada Principal / CLI
# =============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Fase 3: Infraestructura e Indexación Vectorial (pgvector) para Catálogo RAG",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    # Parámetros principales (posicionales o nombrados)
    parser.add_argument(
        "input_json_pos",
        nargs="?",
        default=None,
        help="Ruta al archivo JSON de embeddings resultante de la Fase 2 (parámetro posicional)."
    )
    parser.add_argument(
        "table_name_pos",
        nargs="?",
        default=None,
        help="Nombre de la tabla destino en PostgreSQL (parámetro posicional)."
    )
    parser.add_argument(
        "--input-json", "-i",
        type=str,
        default=None,
        help="Ruta al archivo JSON de embeddings (opción nombrada)."
    )
    parser.add_argument(
        "--table-name", "-t",
        type=str,
        default=None,
        help="Nombre de la tabla destino en PostgreSQL (opción nombrada)."
    )
    parser.add_argument(
        "--db-url",
        type=str,
        default=None,
        help="Cadena de conexión a PostgreSQL (default: construida desde variables en .env)."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Tamaño de lote para la inserción masiva."
    )
    parser.add_argument(
        "--m",
        type=int,
        default=32,
        help="Número máximo de conexiones bidireccionales por nodo en HNSW (default: 32)."
    )
    parser.add_argument(
        "--ef-construction",
        type=int,
        default=200,
        help="Profundidad de exploración durante la construcción del índice HNSW (default: 200)."
    )
    parser.add_argument(
        "--ef-search",
        type=int,
        default=128,
        help="Profundidad de exploración durante las consultas ANN ef_search (default: 128)."
    )
    parser.add_argument(
        "--recreate-table",
        action="store_true",
        help="Si se especifica, elimina la tabla anterior si existe antes de crearla."
    )
    parser.add_argument(
        "--skip-qa",
        action="store_true",
        help="Omite la ejecución de la suite de validación final."
    )

    args = parser.parse_args()

    # Resolver archivo JSON de entrada
    input_json = args.input_json or args.input_json_pos
    if not input_json:
        # Fallback a archivos comunes si no se especificó
        for default_candidate in ["AMX_embeddings.json", "catalogo_embeddings.json"]:
            if os.path.exists(default_candidate):
                input_json = default_candidate
                break
        if not input_json:
            logger.error("Debe especificar el archivo JSON de embeddings como primer parámetro.")
            parser.print_help()
            sys.exit(1)

    # Resolver nombre de tabla destino
    table_name = args.table_name or args.table_name_pos or "catalogo_productos_rag"

    # Resolver URL de base de datos
    db_url = args.db_url or build_db_url()

    logger.info("=" * 60)
    logger.info("INICIANDO FASE 3: PIPELINE DE INFRAESTRUCTURA E INDEXACIÓN")
    logger.info(f"Archivo de Entrada: {input_json}")
    logger.info(f"Tabla Destino:      {table_name}")
    logger.info("=" * 60)

    try:
        # 1. Cargar embeddings
        records, dimension = load_embeddings_json(input_json)

        # 2. Inicializar Manager
        manager = PgVectorManager(db_url=db_url, table_name=table_name, dimension=dimension)

        # 3. Aprovisionar esquema DDL
        manager.init_schema(recreate=args.recreate_table)

        # 4. Ingestar registros
        manager.ingest_records(records, batch_size=args.batch_size)

        # 5. Compilar índices HNSW y Relacionales
        manager.create_indexes(m=args.m, ef_construction=args.ef_construction)

        # 6. Suite de QA y verificación
        if not args.skip_qa and records:
            # Tomar el vector del primer registro para el test
            sample_vec = records[0].get("embedding", [])
            sample_marca = records[0].get("metadata", {}).get("marca")
            manager.run_qa_suite(
                expected_count=len(records),
                sample_vector=sample_vec,
                sample_marca=sample_marca
            )

        logger.info("=" * 60)
        logger.info(f"¡FASE 3 COMPLETADA CON ÉXITO! Tabla '{table_name}' lista para RAG.")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"Fallo crítico durante la ejecución de la Fase 3: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
