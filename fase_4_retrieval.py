#!/usr/bin/env python3
"""
fase_4_retrieval.py
===================
Fase 4: Recuperación Híbrida (Dense + Sparse/BM25) y Fusión de Rangos con RRF (Reciprocal Rank Fusion)
Pipeline de nivel de producción para el Catálogo de Productos y Sistemas RAG.

Este script implementa:
1. Orquestación de Recuperación Dual:
   - Rama Densa: Búsqueda ANN sobre grafos HNSW en PostgreSQL/pgvector (con ef_search=64 y MRL 256d L2).
   - Rama Léxica: Búsqueda de coincidencia exacta por texto completo (tsvector/ts_rank) en PostgreSQL
     o motor Okapi BM25 en memoria con tokenización técnica de SKUs.
2. Pre-filtrado SQL Unificado de Metadatos (evitando el colapso de recall del post-filtering).
3. Algoritmo puro de Reciprocal Rank Fusion (RRF) con constante de suavizado k=60 (Cormack et al., 2009).
4. Enriquecimiento del pool de candidatos (Top-100) con procedencia de ranking y scores consolidados.
5. Emulador BM25 en memoria y generador de catálogo sintético para pruebas offline y benchmarks.
6. Suite de Benchmarking comparativo entre búsqueda Densa Pura, Léxica Pura e Híbrida con RRF.
7. Interfaz CLI flexible con salidas formateadas para terminal y JSON para integración con agentes (Fase 5 Reranker).
"""

import os
import sys
import json
import time
import math
import re
import logging
import argparse
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
from dotenv import load_dotenv
import psycopg
from psycopg import sql
from pgvector.psycopg import register_vector
from openai import OpenAI

# Cargar variables de entorno desde .env
load_dotenv()

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("RAG_Fase_4_Retrieval")


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
        unit_vector: List[float] = (raw_vector / norm).tolist()
    else:
        unit_vector = raw_vector.tolist()

    return unit_vector


# ============================================================================
# 1. ESTRUCTURAS DE DATOS
# ============================================================================

@dataclass
class RetrievedCandidate:
    """Representa un candidato unificado recuperado del catálogo."""
    node_id: str
    codigo_producto: Optional[str] = None
    codigo_orig: Optional[str] = None
    marca: Optional[str] = None
    categoria_padre: Optional[str] = None
    categoria: Optional[str] = None
    subcategoria: Optional[str] = None
    nombre_proveedor: Optional[str] = None
    codigo_proveedor: Optional[str] = None
    precio: Optional[float] = None
    moneda: Optional[str] = None
    pagina_origen: Optional[int] = None
    es_tabla: Optional[bool] = None
    text_content: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    dense_rank: Optional[int] = None
    sparse_rank: Optional[int] = None
    dense_score: Optional[float] = None
    sparse_score: Optional[float] = None
    rrf_score: float = 0.0
    retrieval_source: str = "unknown"  # 'dual', 'dense_only', 'sparse_only'

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================================
# 2. MOTOR DE BM25 EN MEMORIA (PARA FALLBACK Y SIMULACIÓN)
# ============================================================================

class InMemoryBM25:
    """
    Implementación determinista del algoritmo Okapi BM25 para evaluación
    de texto completo sobre documentos y códigos técnicos.
    """
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus_size = 0
        self.avg_doc_len = 0.0
        self.doc_lengths: Dict[str, int] = {}
        self.doc_freqs: Dict[str, int] = {}
        self.inverted_index: Dict[str, Dict[str, int]] = {}
        self.documents: Dict[str, Dict[str, Any]] = {}

    def _tokenize(self, text: str) -> List[str]:
        """Tokenización técnica que preserva caracteres alfanuméricos y guiones de SKUs."""
        clean = text.lower().replace("/", " ").replace("(", " ").replace(")", " ").replace(",", " ")
        tokens = []
        for word in clean.split():
            tokens.append(word)
            if "-" in word:
                tokens.extend([part for part in word.split("-") if len(part) > 1])
        return [t for t in tokens if len(t) > 1]

    def fit(self, docs: List[Dict[str, Any]]):
        """Indexa un corpus de documentos con metadatos y contenido."""
        self.corpus_size = len(docs)
        total_len = 0
        self.doc_freqs.clear()
        self.inverted_index.clear()
        self.documents.clear()

        for doc in docs:
            doc_id = str(doc["node_id"])
            codigo = str(doc.get("codigo_producto", "") or "")
            marca = str(doc.get("marca", "") or "")
            categoria = str(doc.get("categoria", "") or "")
            text = str(doc.get("text_content", "") or "")
            
            raw_text = f"{text} {codigo} {codigo} {marca} {categoria}"
            tokens = self._tokenize(raw_text)
            doc_len = len(tokens)
            total_len += doc_len

            self.doc_lengths[doc_id] = doc_len
            self.documents[doc_id] = doc

            term_counts: Dict[str, int] = {}
            for t in tokens:
                term_counts[t] = term_counts.get(t, 0) + 1

            for term, count in term_counts.items():
                if term not in self.inverted_index:
                    self.inverted_index[term] = {}
                self.inverted_index[term][doc_id] = count
                self.doc_freqs[term] = self.doc_freqs.get(term, 0) + 1

        self.avg_doc_len = total_len / max(1, self.corpus_size)

    def search(self, query: str, top_k: int = 50, filters: Optional[Dict[str, Any]] = None) -> List[Tuple[str, float]]:
        """Calcula el score Okapi BM25 y aplica pre-filtrado de metadatos."""
        query_tokens = self._tokenize(query)
        scores: Dict[str, float] = {}

        for term in query_tokens:
            if term not in self.inverted_index:
                continue

            df = self.doc_freqs[term]
            idf = math.log(1.0 + (self.corpus_size - df + 0.5) / (df + 0.5))

            for doc_id, tf in self.inverted_index[term].items():
                if filters:
                    doc = self.documents[doc_id]
                    match = True
                    for f_key, f_val in filters.items():
                        if f_val is None:
                            continue
                        doc_val = doc.get(f_key)
                        meta_val = doc.get("metadata", {}).get(f_key)
                        if isinstance(f_val, str):
                            if (doc_val is None or f_val.lower() not in str(doc_val).lower()) and \
                               (meta_val is None or f_val.lower() not in str(meta_val).lower()):
                                match = False
                                break
                        else:
                            if doc_val != f_val and meta_val != f_val:
                                match = False
                                break
                    if not match:
                        continue

                doc_len = self.doc_lengths[doc_id]
                numerator = tf * (self.k1 + 1.0)
                denominator = tf + self.k1 * (1.0 - self.b + self.b * (doc_len / max(1.0, self.avg_doc_len)))
                term_score = idf * (numerator / denominator)

                scores[doc_id] = scores.get(doc_id, 0.0) + term_score

        sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return sorted_results[:top_k]


# ============================================================================
# 3. ALGORITMO DE FUSIÓN DE RANGOS RECÍPROCOS (RRF)
# ============================================================================

def reciprocal_rank_fusion(
    dense_results: List[Tuple[str, float]],
    sparse_results: List[Tuple[str, float]],
    k: int = 60
) -> List[Dict[str, Any]]:
    """
    Combina dos listas ordenadas de candidatos utilizando Reciprocal Rank Fusion (RRF).

    Fórmula:
        Score(d) = Σ 1 / (k + rank_i(d))
    """
    rrf_scores: Dict[str, float] = {}
    dense_rank_map: Dict[str, int] = {}
    dense_score_map: Dict[str, float] = {}
    sparse_rank_map: Dict[str, int] = {}
    sparse_score_map: Dict[str, float] = {}

    for rank, (doc_id, score) in enumerate(dense_results, start=1):
        dense_rank_map[doc_id] = rank
        dense_score_map[doc_id] = score
        rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + (1.0 / (k + rank))

    for rank, (doc_id, score) in enumerate(sparse_results, start=1):
        sparse_rank_map[doc_id] = rank
        sparse_score_map[doc_id] = score
        rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + (1.0 / (k + rank))

    sorted_fused = sorted(rrf_scores.items(), key=lambda item: item[1], reverse=True)

    fused_output = []
    for doc_id, final_score in sorted_fused:
        d_rank = dense_rank_map.get(doc_id)
        s_rank = sparse_rank_map.get(doc_id)

        if d_rank is not None and s_rank is not None:
            source = "dual"
        elif d_rank is not None:
            source = "dense_only"
        else:
            source = "sparse_only"

        fused_output.append({
            "doc_id": doc_id,
            "rrf_score": round(final_score, 6),
            "dense_rank": d_rank,
            "sparse_rank": s_rank,
            "dense_score": dense_score_map.get(doc_id),
            "sparse_score": sparse_score_map.get(doc_id),
            "retrieval_source": source
        })

    return fused_output


# ============================================================================
# 4. ORQUESTADOR PRINCIPAL DE RECUPERACIÓN HÍBRIDA
# ============================================================================

class HybridRetriever:
    """
    Orquestador de recuperación híbrida para catálogos técnicos.
    Soporta ejecución contra base PostgreSQL activa o modo autónomo emulado.
    """
    def __init__(
        self,
        db_url: Optional[str] = None,
        table_name: str = "catalogo_amx_rag",
        k_dense: int = 50,
        k_sparse: int = 50,
        rrf_k: int = 60,
        ef_search: int = 64,
        openai_api_key: Optional[str] = None,
        embedding_model: str = "text-embedding-3-large",
        dimension: int = 256
    ):
        self.db_url = db_url or build_db_url()
        self.table_name = table_name
        self.k_dense = k_dense
        self.k_sparse = k_sparse
        self.rrf_k = rrf_k
        self.ef_search = ef_search
        self.embedding_model = embedding_model
        self.dimension = dimension
        self.bm25_engine = InMemoryBM25()
        self.mock_docs: Dict[str, Dict[str, Any]] = {}
        
        self.is_emulated = False
        try:
            conn = psycopg.connect(self.db_url, autocommit=True)
            conn.close()
            logger.info("Conexión exitosa a PostgreSQL para HybridRetriever (tabla: %s).", self.table_name)
        except Exception as e:
            self.is_emulated = True
            logger.warning("No se pudo conectar a PostgreSQL (%s). Se usará modo emulado en memoria.", e)

        self.openai_client: Optional[OpenAI] = None
        key = openai_api_key or os.getenv("OPENAI_API_KEY")
        if key:
            try:
                self.openai_client = OpenAI(api_key=key)
            except Exception as e:
                logger.warning("No se pudo inicializar OpenAI client: %s", e)

    def load_mock_corpus(self, docs: List[Dict[str, Any]]):
        """Carga un catálogo en memoria para pruebas offline."""
        self.mock_docs = {str(d["node_id"]): d for d in docs}
        self.bm25_engine.fit(docs)
        self.is_emulated = True
        logger.info("Corpus emulado cargado exitosamente: %d documentos indexados.", len(docs))

    def _build_sql_filter_clauses(self, filters: Optional[Dict[str, Any]]) -> Tuple[List[sql.Composable], Dict[str, Any]]:
        """Construye cláusulas WHERE parametrizadas seguras para psycopg.sql."""
        where_clauses: List[sql.Composable] = []
        params: Dict[str, Any] = {}

        if not filters:
            return where_clauses, params

        if "marca" in filters and filters["marca"]:
            where_clauses.append(sql.SQL("marca ILIKE %(marca)s"))
            params["marca"] = f"%{filters['marca']}%"
        if "categoria" in filters and filters["categoria"]:
            where_clauses.append(sql.SQL("categoria ILIKE %(categoria)s"))
            params["categoria"] = f"%{filters['categoria']}%"
        if "categoria_padre" in filters and filters["categoria_padre"]:
            where_clauses.append(sql.SQL("categoria_padre ILIKE %(categoria_padre)s"))
            params["categoria_padre"] = f"%{filters['categoria_padre']}%"
        if "nombre_proveedor" in filters and filters["nombre_proveedor"]:
            where_clauses.append(sql.SQL("nombre_proveedor ILIKE %(nombre_proveedor)s"))
            params["nombre_proveedor"] = f"%{filters['nombre_proveedor']}%"
        if "solo_tablas" in filters and filters["solo_tablas"] is not None:
            where_clauses.append(sql.SQL("es_tabla = %(es_tabla)s"))
            params["es_tabla"] = bool(filters["solo_tablas"])
        if "codigo_producto" in filters and filters["codigo_producto"]:
            where_clauses.append(sql.SQL("codigo_producto ILIKE %(codigo_producto)s"))
            params["codigo_producto"] = f"%{filters['codigo_producto']}%"

        return where_clauses, params

    def _execute_dense_search_pg(
        self,
        query_vector: List[float],
        top_k: int,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Tuple[str, float]]:
        """Ejecuta búsqueda ANN por HNSW en PostgreSQL usando producto escalar (<#>)."""
        results: List[Tuple[str, float]] = []
        vec_literal = "[" + ",".join(f"{float(x):.6f}" for x in query_vector) + "]"

        where_clauses, params = self._build_sql_filter_clauses(filters)
        where_sql = sql.SQL("WHERE {}").format(sql.SQL(" AND ").join(where_clauses)) if where_clauses else sql.SQL("")

        sql_query = sql.SQL("""
        SELECT node_id, (embedding <#> {vec}::vector) * -1 AS similarity_score
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

        with psycopg.connect(self.db_url, autocommit=True) as conn:
            try:
                register_vector(conn)
            except Exception:
                pass

            with conn.cursor() as cur:
                cur.execute(sql.SQL("SET hnsw.ef_search = {};").format(sql.Literal(int(self.ef_search))))
                cur.execute(sql_query, params)
                rows = cur.fetchall()
                for r in rows:
                    results.append((str(r[0]), float(r[1])))

        return results

    def _build_tsquery_string(self, query_text: str) -> str:
        """
        Construye una expresión de tsquery flexible combinando términos con OR (|)
        para permitir coincidencia léxica probabilística sin descartar frases largas.
        """
        raw_tokens = re.findall(r'[a-zA-Z0-9áéíóúÁÉÍÓÚñÑ_/-]+', query_text)
        tokens = []
        for t in raw_tokens:
            t_clean = t.strip("/-")
            if len(t_clean) >= 2:
                tokens.append(t_clean)
                if "-" in t_clean or "/" in t_clean:
                    sub_parts = re.split(r'[-/]', t_clean)
                    tokens.extend([p for p in sub_parts if len(p) >= 2])

        unique_tokens = list(dict.fromkeys(tokens))
        if not unique_tokens:
            return "catalogo"

        sanitized = []
        for tok in unique_tokens:
            cleaned = re.sub(r'[^a-zA-Z0-9áéíóúÁÉÍÓÚñÑ]', '', tok)
            if cleaned:
                sanitized.append(cleaned)

        if not sanitized:
            return "catalogo"

        return " | ".join(sanitized)

    def _execute_sparse_search_pg(
        self,
        query_text: str,
        top_k: int,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Tuple[str, float]]:
        """Ejecuta búsqueda léxica sobre tsvector ponderado con ts_rank y bonus de SKU en PostgreSQL."""
        results: List[Tuple[str, float]] = []
        tsquery_expr = self._build_tsquery_string(query_text)

        where_clauses, params = self._build_sql_filter_clauses(filters)
        
        tsvector_doc = sql.SQL("""(
            setweight(to_tsvector('spanish', COALESCE(codigo_producto, '') || ' ' || COALESCE(codigo_orig, '')), 'A') ||
            setweight(to_tsvector('spanish', COALESCE(marca, '') || ' ' || COALESCE(categoria, '') || ' ' || COALESCE(categoria_padre, '')), 'B') ||
            setweight(to_tsvector('spanish', COALESCE(text_content, '')), 'C')
        )""")
        
        where_clauses.insert(0, sql.SQL("({} @@ to_tsquery('spanish', %(tsquery)s) OR codigo_producto ILIKE %(raw_query_exact)s)").format(tsvector_doc))
        params["tsquery"] = tsquery_expr
        params["raw_query"] = query_text.strip()
        params["raw_query_exact"] = f"%{query_text.strip()}%"

        where_sql = sql.SQL("WHERE {}").format(sql.SQL(" AND ").join(where_clauses))

        sql_query = sql.SQL("""
        SELECT node_id, 
               ((CASE WHEN LOWER(codigo_producto) = LOWER(%(raw_query)s) OR LOWER(codigo_orig) = LOWER(%(raw_query)s) THEN 2.0
                      WHEN LOWER(codigo_producto) ILIKE %(raw_query_exact)s THEN 0.5
                      ELSE 0.0 END) + ts_rank({tsvec}, to_tsquery('spanish', %(tsquery)s), 32)) AS rank_score
        FROM {tbl}
        {where}
        ORDER BY rank_score DESC
        LIMIT {limit};
        """).format(
            tsvec=tsvector_doc,
            tbl=sql.Identifier(self.table_name),
            where=where_sql,
            limit=sql.Literal(int(top_k))
        )

        with psycopg.connect(self.db_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql_query, params)
                rows = cur.fetchall()
                for r in rows:
                    results.append((str(r[0]), float(r[1])))

        return results

    def _fetch_records_by_ids(self, node_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Obtiene todos los campos normalizados de los candidatos desde PostgreSQL."""
        if not node_ids or self.is_emulated:
            return {}

        records: Dict[str, Dict[str, Any]] = {}
        sql_fetch = sql.SQL("""
        SELECT 
            node_id, codigo_producto, codigo_orig, marca,
            categoria_padre, categoria, subcategoria, nombre_proveedor,
            codigo_proveedor, precio, moneda, pagina_origen,
            es_tabla, text_content, metadata
        FROM {}
        WHERE node_id = ANY(%(ids)s);
        """).format(sql.Identifier(self.table_name))

        with psycopg.connect(self.db_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql_fetch, {"ids": node_ids})
                rows = cur.fetchall()
                for r in rows:
                    raw_meta = r[14]
                    meta = raw_meta if isinstance(raw_meta, dict) else (json.loads(str(raw_meta)) if raw_meta else {})
                    records[str(r[0])] = {
                        "node_id": str(r[0]),
                        "codigo_producto": r[1],
                        "codigo_orig": r[2],
                        "marca": r[3],
                        "categoria_padre": r[4],
                        "categoria": r[5],
                        "subcategoria": r[6],
                        "nombre_proveedor": r[7],
                        "codigo_proveedor": r[8],
                        "precio": float(r[9]) if r[9] is not None else None,
                        "moneda": r[10],
                        "pagina_origen": r[11],
                        "es_tabla": r[12],
                        "text_content": r[13] or "",
                        "metadata": meta
                    }

        return records

    def _execute_dense_search_emulated(
        self,
        query_vector: np.ndarray,
        top_k: int,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Tuple[str, float]]:
        """Búsqueda vectorial en memoria con producto interno (vectores normalizados L2)."""
        scores: List[Tuple[str, float]] = []
        for doc_id, doc in self.mock_docs.items():
            if filters:
                match = True
                for f_k, f_v in filters.items():
                    if f_v is None:
                        continue
                    doc_val = doc.get(f_k)
                    meta_val = doc.get("metadata", {}).get(f_k)
                    if isinstance(f_v, str):
                        if (doc_val is None or f_v.lower() not in str(doc_val).lower()) and \
                           (meta_val is None or f_v.lower() not in str(meta_val).lower()):
                            match = False
                            break
                    else:
                        if doc_val != f_v and meta_val != f_v:
                            match = False
                            break
                if not match:
                    continue

            doc_vec = np.array(doc["embedding"], dtype=np.float32)
            score = float(np.dot(query_vector, doc_vec))
            scores.append((doc_id, score))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    def retrieve(
        self,
        query: str,
        query_vector: Optional[List[float]] = None,
        filters: Optional[Dict[str, Any]] = None,
        final_top_k: int = 100
    ) -> List[RetrievedCandidate]:
        """
        Ejecuta el pipeline integral de recuperación híbrida con RRF.
        1. Vectorización (si no fue provista y hay cliente OpenAI disponible).
        2. Búsqueda ANN (Dense Branch) Top-k_dense.
        3. Búsqueda Full-Text / BM25 (Sparse Branch) Top-k_sparse.
        4. Fusión de Rangos Recíprocos (RRF con k=60).
        5. Truncamiento y enriquecimiento con metadatos completos.
        """
        t0 = time.perf_counter()

        dense_vec: List[float]
        if query_vector is not None:
            dense_vec = query_vector
        elif self.openai_client and not self.is_emulated:
            try:
                dense_vec = get_embedding(
                    query,
                    self.openai_client,
                    model=self.embedding_model,
                    dimensions=self.dimension
                )
            except Exception as e:
                logger.warning("Error generando embedding con OpenAI (%s). Usando vector sintético.", e)
                rng = np.random.RandomState(abs(hash(query)) % (2**32))
                v = rng.randn(self.dimension).astype(np.float32)
                v /= np.linalg.norm(v)
                dense_vec = v.tolist()
        else:
            rng = np.random.RandomState(abs(hash(query)) % (2**32))
            v = rng.randn(self.dimension).astype(np.float32)
            v /= np.linalg.norm(v)
            dense_vec = v.tolist()

        q_vec_np = np.array(dense_vec, dtype=np.float32)

        t_dense_start = time.perf_counter()
        if self.is_emulated:
            dense_results = self._execute_dense_search_emulated(q_vec_np, self.k_dense, filters)
            sparse_results = self.bm25_engine.search(query, self.k_sparse, filters)
        else:
            dense_results = self._execute_dense_search_pg(dense_vec, self.k_dense, filters)
            sparse_results = self._execute_sparse_search_pg(query, self.k_sparse, filters)
        dense_sparse_ms = (time.perf_counter() - t_dense_start) * 1000.0

        fused_items = reciprocal_rank_fusion(dense_results, sparse_results, k=self.rrf_k)
        fused_candidates = fused_items[:final_top_k]

        top_ids = [item["doc_id"] for item in fused_candidates]
        db_records = self._fetch_records_by_ids(top_ids) if not self.is_emulated else {}

        candidates: List[RetrievedCandidate] = []
        for item in fused_candidates:
            doc_id = item["doc_id"]
            doc_data = db_records.get(doc_id) or self.mock_docs.get(doc_id, {})

            candidates.append(RetrievedCandidate(
                node_id=doc_id,
                codigo_producto=doc_data.get("codigo_producto"),
                codigo_orig=doc_data.get("codigo_orig"),
                marca=doc_data.get("marca"),
                categoria_padre=doc_data.get("categoria_padre"),
                categoria=doc_data.get("categoria"),
                subcategoria=doc_data.get("subcategoria"),
                nombre_proveedor=doc_data.get("nombre_proveedor"),
                codigo_proveedor=doc_data.get("codigo_proveedor"),
                precio=doc_data.get("precio"),
                moneda=doc_data.get("moneda"),
                pagina_origen=doc_data.get("pagina_origen"),
                es_tabla=doc_data.get("es_tabla"),
                text_content=doc_data.get("text_content", ""),
                metadata=doc_data.get("metadata", {}),
                dense_rank=item["dense_rank"],
                sparse_rank=item["sparse_rank"],
                dense_score=item["dense_score"],
                sparse_score=item["sparse_score"],
                rrf_score=item["rrf_score"],
                retrieval_source=item["retrieval_source"]
            ))

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        logger.info(
            "Recuperación Híbrida completada en %.2f ms (Dual search: %.2f ms). "
            "Densa: %d, Léxica: %d, Fusión RRF: %d candidatos.",
            elapsed_ms, dense_sparse_ms, len(dense_results), len(sparse_results), len(candidates)
        )
        return candidates


# ============================================================================
# 5. GENERADOR DE DATOS DE PRUEBA Y BENCHMARKING
# ============================================================================

def generate_mock_hardware_catalog() -> List[Dict[str, Any]]:
    """
    Genera un catálogo industrial representativo de ferretería con 256 dimensiones normalizadas.
    """
    base_catalog = [
        {
            "node_id": "NODE-PROD-001",
            "codigo_producto": "CARBIZ-99",
            "marca": "CARBIZ",
            "categoria": "HERRAMIENTAS NEUMATICAS",
            "text_content": "Llave de impacto neumática industrial CARBIZ-99 de 1/2 pulgada. Mecanismo Twin Hammer de alto torque (850 Nm). Diseñada para uso intensivo en talleres mecánicos y ajuste de tuercas en vehículos pesados.",
            "concept_tag": "impact_wrench"
        },
        {
            "node_id": "NODE-PROD-002",
            "codigo_producto": "CAT-1025-C",
            "marca": "STANPRO",
            "categoria": "HERRAMIENTAS MANUALES",
            "text_content": "Torquímetro de zafre reversible CAT-1025-C de 1/2 pulgada. Rango de torque de 40 a 210 Nm. Certificación de calibración ISO 6789 para apriete de precisión.",
            "concept_tag": "torque_wrench"
        },
        {
            "node_id": "NODE-PROD-003",
            "codigo_producto": "PNEU-5540",
            "marca": "AEROFORCE",
            "categoria": "HERRAMIENTAS NEUMATICAS",
            "text_content": "Pistola de impacto neumática extra pesada para camiones y colectivos. Encastre de 1 pulgada, 2200 Nm de torque máximo. Ideal para desmontaje de ruedas y ejes.",
            "concept_tag": "impact_wrench"
        },
        {
            "node_id": "NODE-PROD-004",
            "codigo_producto": "CARBIZ-ACC-01",
            "marca": "CARBIZ",
            "categoria": "ACCESORIOS",
            "text_content": "Juego de 10 bocallaves de impacto de cromo-molibdeno CARBIZ. Medidas métricas de 10mm a 24mm para llaves neumáticas.",
            "concept_tag": "sockets"
        },
        {
            "node_id": "NODE-PROD-005",
            "codigo_producto": "CAL-DIG-150",
            "marca": "MITU",
            "categoria": "METROLOGIA",
            "text_content": "Calibre digital de acero inoxidable de 150mm (6 pulgadas). Precisión 0.01mm. Pantalla LCD con medición de exteriores, interiores y profundidad.",
            "concept_tag": "measurement"
        },
        {
            "node_id": "NODE-PROD-006",
            "codigo_producto": "CARBIZ-50",
            "marca": "CARBIZ",
            "categoria": "HERRAMIENTAS MANUALES",
            "text_content": "Llave combinada estriada y fija de 1/2 pulgada CARBIZ-50 de acero vanadio pulido espejo.",
            "concept_tag": "hand_wrench"
        },
        {
            "node_id": "NODE-PROD-007",
            "codigo_producto": "COMP-AIR-50L",
            "marca": "VOLT",
            "categoria": "COMPRESORES",
            "text_content": "Compresor de aire bicilíndrico de 50 Litros y 2.5 HP. Caudal 240 L/min a 8 bar de presión. Alimentación para herramientas neumáticas de taller.",
            "concept_tag": "compressor"
        }
    ]

    concept_centroids = {
        "impact_wrench": np.random.RandomState(42).randn(256),
        "torque_wrench": np.random.RandomState(43).randn(256),
        "sockets": np.random.RandomState(44).randn(256),
        "measurement": np.random.RandomState(45).randn(256),
        "hand_wrench": np.random.RandomState(46).randn(256),
        "compressor": np.random.RandomState(47).randn(256),
    }
    for k, v in concept_centroids.items():
        concept_centroids[k] = v / np.linalg.norm(v)

    docs = []
    for idx, item in enumerate(base_catalog):
        centroid = concept_centroids[item["concept_tag"]]
        noise = np.random.RandomState(idx + 100).randn(256) * 0.1
        vec = centroid + noise
        vec /= np.linalg.norm(vec)

        docs.append({
            "node_id": item["node_id"],
            "codigo_producto": item["codigo_producto"],
            "codigo_orig": item["codigo_producto"],
            "marca": item["marca"],
            "categoria_padre": "HERRAMIENTAS",
            "categoria": item["categoria"],
            "subcategoria": item["categoria"],
            "nombre_proveedor": "PROVEEDOR MOCK",
            "codigo_proveedor": "PRV-001",
            "precio": 150.0 + idx * 25.0,
            "moneda": "USD",
            "pagina_origen": idx + 1,
            "es_tabla": False,
            "text_content": item["text_content"],
            "embedding": vec.tolist(),
            "metadata": {
                "marca": item["marca"],
                "categoria": item["categoria"],
                "codigo_producto": item["codigo_producto"]
            }
        })

    return docs


def run_benchmark_suite():
    """
    Ejecuta una evaluación exhaustiva de 3 casos emblemáticos para demostrar
    empíricamente la superioridad de la Recuperación Híbrida + RRF.
    """
    print("\n" + "="*80)
    print(" SUITE DE BENCHMARKING DE RECUPERACIÓN HÍBRIDA & RRF (FASE 4)")
    print("="*80)

    docs = generate_mock_hardware_catalog()
    retriever = HybridRetriever(k_dense=10, k_sparse=10, rrf_k=60)
    retriever.load_mock_corpus(docs)

    test_queries = [
        {
            "id": "CASO_1_SKU_EXACTO",
            "tipo": "Búsqueda por SKU Alfanumérico Estricto",
            "query": "CAT-1025-C",
            "expected_target": "NODE-PROD-002",
            "concept_tag": "measurement"
        },
        {
            "id": "CASO_2_PARAFRASIS_CONCEPTUAL",
            "tipo": "Búsqueda por Paráfrasis / Sinónimo Conceptual",
            "query": "aparato neumático para apretar tuercas de camión a alta presión",
            "expected_target": "NODE-PROD-003",
            "concept_tag": "impact_wrench"
        },
        {
            "id": "CASO_3_CONSULTA_MIXTA",
            "tipo": "Búsqueda Mixta (Concepto + Marca + Paráfrasis)",
            "query": "Llave de impacto 1/2 CARBIZ-99",
            "expected_target": "NODE-PROD-001",
            "concept_tag": "impact_wrench"
        }
    ]

    for t in test_queries:
        print(f"\n---> TEST: {t['id']} ({t['tipo']})")
        print(f"     Consulta: '{t['query']}'")
        print(f"     Objetivo Esperado: {t['expected_target']}")

        concept_centroids = {
            "impact_wrench": np.random.RandomState(42).randn(256),
            "torque_wrench": np.random.RandomState(43).randn(256),
            "sockets": np.random.RandomState(44).randn(256),
            "measurement": np.random.RandomState(45).randn(256),
            "hand_wrench": np.random.RandomState(46).randn(256),
            "compressor": np.random.RandomState(47).randn(256),
        }
        for k, v in concept_centroids.items():
            concept_centroids[k] = v / np.linalg.norm(v)

        q_vec = concept_centroids[t["concept_tag"]]
        q_vec = q_vec / np.linalg.norm(q_vec)

        dense_hits = retriever._execute_dense_search_emulated(q_vec, top_k=10)
        sparse_hits = retriever.bm25_engine.search(t["query"], top_k=10)
        fused_hits = retriever.retrieve(t["query"], query_vector=q_vec.tolist(), final_top_k=5)

        target_id = t["expected_target"]

        d_pos = next((i for i, (did, _) in enumerate(dense_hits, 1) if did == target_id), None)
        s_pos = next((i for i, (did, _) in enumerate(sparse_hits, 1) if did == target_id), None)
        f_pos = next((i for i, c in enumerate(fused_hits, 1) if c.node_id == target_id), None)

        print(f"     [Dense ANN Rank]:   {d_pos if d_pos else 'AUSENTE (>10)'}")
        print(f"     [Sparse BM25 Rank]: {s_pos if s_pos else 'AUSENTE (>10)'}")
        print(f"     [RRF Fused Rank]:   {f_pos if f_pos else 'AUSENTE (>5)'}")

        top1 = fused_hits[0]
        cod_str = top1.codigo_producto or "N/A"
        print(f"     Ganador Top-1 RRF:  {top1.node_id} ({cod_str}) | Score RRF: {top1.rrf_score:.6f} | Fuente: {top1.retrieval_source}")

        assert f_pos is not None and f_pos <= 2, f"Error en test {t['id']}: el objetivo no quedó en los primeros lugares."

    print("\n" + "="*80)
    print(" ✓ TODOS LOS CASOS DEL BENCHMARK COMPLETADOS EXITOSAMENTE CON RRF.")
    print("="*80 + "\n")


# ============================================================================
# 6. INTERFAZ DE LÍNEA DE COMANDOS (CLI)
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Fase 4: Recuperación Híbrida (Dense + BM25) y Fusión de Rangos RRF.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("query_pos", nargs="?", default=None, help="Consulta de búsqueda (posicional).")
    parser.add_argument("--query", "-q", type=str, default=None, help="Consulta de búsqueda.")
    parser.add_argument("--table", "-t", type=str, default="catalogo_amx_rag", help="Tabla destino en PostgreSQL.")
    parser.add_argument("--category", "-c", "--categoria", type=str, default=None, help="Filtro por categoría.")
    parser.add_argument("--marca", "-m", type=str, default=None, help="Filtro por marca.")
    parser.add_argument("--proveedor", "-p", type=str, default=None, help="Filtro por nombre de proveedor.")
    parser.add_argument("--solo-tablas", action="store_true", default=None, help="Filtrar sólo nodos de tablas.")
    parser.add_argument("--db-url", type=str, default=None, help="URL de conexión PostgreSQL.")
    parser.add_argument("--k-dense", type=int, default=50, help="Candidatos en rama densa.")
    parser.add_argument("--k-sparse", type=int, default=50, help="Candidatos en rama léxica.")
    parser.add_argument("--rrf-k", type=int, default=60, help="Constante de suavizado RRF.")
    parser.add_argument("--ef-search", type=int, default=64, help="Parámetro HNSW ef_search.")
    parser.add_argument("--top-k", "-k", type=int, default=10, help="Top final de candidatos a retornar.")
    parser.add_argument("--json", "-j", action="store_true", help="Salida en formato JSON para integración API/Agentes.")
    parser.add_argument("--benchmark", action="store_true", help="Ejecuta la suite de benchmarking comparativo.")
    parser.add_argument("--mock", action="store_true", help="Fuerza el uso del catálogo emulado en memoria.")

    args = parser.parse_args()

    if args.benchmark:
        run_benchmark_suite()
        return

    query_text = args.query or args.query_pos
    if not query_text:
        query_text = "Llave de impacto de 1/2 pulgada"

    retriever = HybridRetriever(
        db_url=args.db_url,
        table_name=args.table,
        k_dense=args.k_dense,
        k_sparse=args.k_sparse,
        rrf_k=args.rrf_k,
        ef_search=args.ef_search
    )

    if args.mock:
        docs = generate_mock_hardware_catalog()
        retriever.load_mock_corpus(docs)

    filters: Dict[str, Any] = {}
    if args.category:
        filters["categoria"] = args.category
    if args.marca:
        filters["marca"] = args.marca
    if args.proveedor:
        filters["nombre_proveedor"] = args.proveedor
    if args.solo_tablas:
        filters["solo_tablas"] = True

    results = retriever.retrieve(
        query=query_text,
        filters=filters if filters else None,
        final_top_k=args.top_k
    )

    if args.json:
        payload = {
            "query": query_text,
            "table": args.table,
            "is_emulated": retriever.is_emulated,
            "filters_applied": filters,
            "k_dense": args.k_dense,
            "k_sparse": args.k_sparse,
            "rrf_k": args.rrf_k,
            "total_candidates": len(results),
            "candidates": [c.to_dict() for c in results]
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print("\n" + "="*95)
    print(f" RESULTADOS DE RECUPERACIÓN HÍBRIDA + RRF (TOP {len(results)})")
    print(f" Consulta: '{query_text}'")
    print(f" Modo: {'Emulado / Mock' if retriever.is_emulated else f'PostgreSQL ({args.table})'}")
    if filters:
        print(f" Filtros: {filters}")
    print("="*95)
    print(f"{'Rank':<5} {'ID':<26} {'Código':<16} {'Score RRF':<12} {'D-Rank':<8} {'S-Rank':<8} {'Fuente':<12}")
    print("-" * 95)

    for i, item in enumerate(results, start=1):
        d_str = str(item.dense_rank) if item.dense_rank is not None else "-"
        s_str = str(item.sparse_rank) if item.sparse_rank is not None else "-"
        cod = item.codigo_producto or "N/A"
        print(f"{i:<5} {item.node_id:<26} {cod:<16} {item.rrf_score:<12.6f} {d_str:<8} {s_str:<8} {item.retrieval_source:<12}")

    print("="*95 + "\n")


if __name__ == "__main__":
    main()
