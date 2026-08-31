#!/usr/bin/env python3
"""
query_rag.py
============
Script de consulta y recuperación semántica (RAG Retrieval) sobre PostgreSQL + pgvector.

Flujo:
1. Recibe la consulta en lenguaje natural (string literal).
2. Genera el embedding contextual utilizando OpenAI (text-embedding-3-large con MRL a 256 dims y normalización L2).
3. Ejecuta la búsqueda de vecinos más cercanos (ANN con HNSW y vector_ip_ops) sobre la tabla especificada.
4. Soporta pre-filtrado relacional (marca, categoría, proveedor, etc.) y calibración ef_search.
5. Retorna los resultados formateados tanto para lectura humana en terminal como en JSON para integración con agentes.

EJEMPLOS DE USO:
    # 1. Consulta directa en terminal:
    python query_rag.py "clavadora neumática para clavos brad de 50mm" --table catalogo_amx_rag --top-k 3

    # 2. Consulta con salida JSON (para integración con agentes):
    python query_rag.py "llave de impacto de 1/2 pulgada" --table catalogo_amx_rag --top-k 1 --json

    # 3. Consulta con pre-filtrado relacional (marca, solo tablas, etc.):
    python query_rag.py "clavos" --table catalogo_amx_rag --marca "AMX" --solo-tablas --top-k 5
"""

import os
import sys
import json
import time
import logging
import argparse
from typing import List, Dict, Any, Optional
import numpy as np
import psycopg
from psycopg import sql
from pgvector.psycopg import register_vector
from dotenv import load_dotenv
from openai import OpenAI

# Cargar variables de entorno desde .env
load_dotenv()

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("RAG_Query_Engine")


def build_db_url() -> str:
    """Construye la URL de conexión a PostgreSQL desde variables de entorno."""
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        return db_url

    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "postgres")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db_name = os.getenv("POSTGRES_DB", "postgres")

    return f"postgresql://{user}:{password}@{host}:{port}/{db_name}"


def get_embedding(
    text: str,
    client: OpenAI,
    model: str = "text-embedding-3-large",
    dimensions: int = 256
) -> List[float]:
    """
    Genera el vector de embedding con compresión MRL y normalización unitaria L2.
    """
    sanitized = text.strip() if (text and text.strip()) else "N/A"
    
    if "text-embedding-3" in model:
        response = client.embeddings.create(
            input=sanitized,
            model=model,
            dimensions=dimensions
        )
    else:
        response = client.embeddings.create(
            input=sanitized,
            model=model
        )

    raw_vector = np.array(response.data[0].embedding, dtype=np.float32)
    if "text-embedding-3" not in model and len(raw_vector) > dimensions:
        raw_vector = raw_vector[:dimensions]

    # Normalización L2 unitaria
    norm = float(np.linalg.norm(raw_vector))
    if norm > 0:
        unit_vector = (raw_vector / norm).tolist()
    else:
        unit_vector = raw_vector.tolist()

    return unit_vector


def query_catalog(
    query_text: str,
    table_name: str = "catalogo_amx_rag",
    top_k: int = 5,
    marca: Optional[str] = None,
    categoria: Optional[str] = None,
    nombre_proveedor: Optional[str] = None,
    solo_tablas: Optional[bool] = None,
    ef_search: int = 64,
    min_score: Optional[float] = None,
    embedding_model: str = "text-embedding-3-large",
    dimensions: int = 256,
    db_url: Optional[str] = None,
    api_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Orquesta la consulta RAG:
    1. Vectorización de la consulta.
    2. Ejecución de la búsqueda ANN con pre-filtrado en pgvector.
    3. Conformación del payload de respuesta.
    """
    # 1. OpenAI Client
    openai_key = api_key or os.getenv("OPENAI_API_KEY")
    if not openai_key:
        raise ValueError("OPENAI_API_KEY no configurada en el entorno ni en el archivo .env")
    client = OpenAI(api_key=openai_key)

    # 2. Generar Embedding de consulta
    t0_embed = time.time()
    query_vector = get_embedding(query_text, client, model=embedding_model, dimensions=dimensions)
    embed_duration_ms = round((time.time() - t0_embed) * 1000, 2)

    # 3. Preparar consulta SQL con psycopg.sql
    vec_literal = "[" + ",".join(f"{float(x):.6f}" for x in query_vector) + "]"
    conn_url = db_url or build_db_url()

    where_clauses = []
    params: Dict[str, Any] = {}

    if marca:
        where_clauses.append(sql.SQL("marca ILIKE %(marca)s"))
        params["marca"] = f"%{marca}%"
    if categoria:
        where_clauses.append(sql.SQL("categoria ILIKE %(categoria)s"))
        params["categoria"] = f"%{categoria}%"
    if nombre_proveedor:
        where_clauses.append(sql.SQL("nombre_proveedor ILIKE %(nombre_proveedor)s"))
        params["nombre_proveedor"] = f"%{nombre_proveedor}%"
    if solo_tablas is not None:
        where_clauses.append(sql.SQL("es_tabla = %(es_tabla)s"))
        params["es_tabla"] = solo_tablas

    where_sql = sql.SQL("WHERE {}").format(sql.SQL(" AND ").join(where_clauses)) if where_clauses else sql.SQL("")

    select_sql = sql.SQL("""
    SELECT 
        node_id,
        codigo_producto,
        codigo_orig,
        marca,
        categoria_padre,
        categoria,
        subcategoria,
        nombre_proveedor,
        codigo_proveedor,
        precio,
        moneda,
        pagina_origen,
        es_tabla,
        text_content,
        metadata,
        (embedding <#> {vec}::vector) * -1 AS similarity_score
    FROM {tbl}
    {where}
    ORDER BY embedding <#> {vec}::vector ASC
    LIMIT {limit};
    """).format(
        vec=sql.Literal(vec_literal),
        tbl=sql.Identifier(table_name),
        where=where_sql,
        limit=sql.Literal(int(top_k))
    )

    # 4. Conectar y ejecutar en PostgreSQL
    t0_db = time.time()
    results = []

    with psycopg.connect(conn_url, autocommit=True) as conn:
        try:
            register_vector(conn)
        except Exception:
            pass

        with conn.cursor() as cur:
            cur.execute(sql.SQL("SET hnsw.ef_search = {};").format(sql.Literal(int(ef_search))))
            cur.execute(select_sql, params)
            rows = cur.fetchall()

            for row in rows:
                score = float(row[15])
                if min_score is not None and score < min_score:
                    continue

                raw_meta = row[14]
                meta_dict = raw_meta if isinstance(raw_meta, dict) else json.loads(str(raw_meta))

                item = {
                    "node_id": row[0],
                    "codigo_producto": row[1],
                    "codigo_orig": row[2],
                    "marca": row[3],
                    "categoria_padre": row[4],
                    "categoria": row[5],
                    "subcategoria": row[6],
                    "nombre_proveedor": row[7],
                    "codigo_proveedor": row[8],
                    "precio": float(row[9]) if row[9] is not None else None,
                    "moneda": row[10],
                    "pagina_origen": row[11],
                    "es_tabla": row[12],
                    "text_content": row[13],
                    "metadata": meta_dict,
                    "similarity_score": round(score, 4)
                }
                results.append(item)

    db_duration_ms = round((time.time() - t0_db) * 1000, 2)
    total_duration_ms = round(embed_duration_ms + db_duration_ms, 2)

    return {
        "query": query_text,
        "table": table_name,
        "total_results": len(results),
        "latencies_ms": {
            "embedding": embed_duration_ms,
            "database_retrieval": db_duration_ms,
            "total": total_duration_ms
        },
        "filters_applied": {
            "marca": marca,
            "categoria": categoria,
            "nombre_proveedor": nombre_proveedor,
            "solo_tablas": solo_tablas,
            "ef_search": ef_search,
            "min_score": min_score
        },
        "results": results
    }


def main():
    parser = argparse.ArgumentParser(
        description="Query Engine RAG: Consulta semántica y recuperación vectorial con pgvector",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "query_pos",
        nargs="?",
        default=None,
        help="Texto literal de la consulta (parámetro posicional)."
    )
    parser.add_argument(
        "--query", "-q",
        type=str,
        default=None,
        help="Texto literal de la consulta."
    )
    parser.add_argument(
        "--table", "-t",
        type=str,
        default="catalogo_amx_rag",
        help="Tabla destino en PostgreSQL a consultar."
    )
    parser.add_argument(
        "--top-k", "-k",
        type=int,
        default=5,
        help="Cantidad de candidatos más cercanos a retornar."
    )
    parser.add_argument(
        "--marca", "-m",
        type=str,
        default=None,
        help="Pre-filtro relacional por marca (ej: 'XMAX', 'CARBIZ')."
    )
    parser.add_argument(
        "--categoria", "-c",
        type=str,
        default=None,
        help="Pre-filtro relacional por categoría (ej: 'Herramientas Neumáticas')."
    )
    parser.add_argument(
        "--proveedor", "-p",
        type=str,
        default=None,
        help="Pre-filtro relacional por nombre del proveedor."
    )
    parser.add_argument(
        "--solo-tablas",
        action="store_true",
        default=None,
        help="Filtrar únicamente nodos provenientes de tablas (es_tabla = true)."
    )
    parser.add_argument(
        "--ef-search",
        type=int,
        default=64,
        help="Profundidad de exploración HNSW en tiempo de consulta."
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="Umbral mínimo de similitud de coseno para retornar el resultado (0.0 a 1.0)."
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Imprime la salida en formato JSON puro (ideal para agentes o integración API)."
    )

    args = parser.parse_args()

    query_text = args.query or args.query_pos
    if not query_text:
        logger.error("Debe proporcionar un texto de consulta (ej: python query_rag.py 'clavadora neumatica 50mm')")
        parser.print_help()
        sys.exit(1)

    try:
        response = query_catalog(
            query_text=query_text,
            table_name=args.table,
            top_k=args.top_k,
            marca=args.marca,
            categoria=args.categoria,
            nombre_proveedor=args.proveedor,
            solo_tablas=True if args.solo_tablas else None,
            ef_search=args.ef_search,
            min_score=args.min_score
        )

        if args.json:
            print(json.dumps(response, ensure_ascii=False, indent=2))
        else:
            print("\n" + "=" * 80)
            print("RESULTADOS DE BÚSQUEDA SEMÁNTICA RAG")
            print("=" * 80)
            print(f"Consulta: '{response['query']}'")
            print(f"Tabla:    {response['table']} | Resultados: {response['total_results']}")
            print(f"Latencia: Embedding={response['latencies_ms']['embedding']}ms | "
                  f"pgvector={response['latencies_ms']['database_retrieval']}ms | "
                  f"Total={response['latencies_ms']['total']}ms")
            print("-" * 80)

            if not response["results"]:
                print("No se encontraron resultados que coincidan con los filtros y umbrales aplicados.")
            else:
                for idx, item in enumerate(response["results"], 1):
                    precio_str = f"{item['moneda']} {item['precio']:.2f}" if item['precio'] is not None else "N/D"
                    print(f"[{idx}] Score: {item['similarity_score']:.4f} | Código: {item['codigo_producto']} ({item['codigo_orig']}) | Marca: {item['marca']}")
                    print(f"    Categoría: {item['categoria_padre']} > {item['categoria']} > {item['subcategoria']}")
                    print(f"    Proveedor: {item['nombre_proveedor']} | Precio: {precio_str} | Pág: {item['pagina_origen']} | Tabla: {item['es_tabla']}")
                    print(f"    Fragmento contextual:\n    {item['text_content'].replace(chr(10), chr(10) + '    ')}")
                    print("-" * 80)

    except Exception as e:
        logger.error(f"Error al ejecutar la consulta RAG: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
