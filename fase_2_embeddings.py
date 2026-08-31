#!/usr/bin/env python3
"""
fase_2_embeddings.py

Pipeline de Generación y Validación de Embeddings RAG (Fase 2):
Transforma los nodos estructurados de la Fase 1 en representaciones vectoriales
optimizadas mediante Matryoshka Representation Learning (MRL), ejecuta una
suite exhaustiva de control de calidad (QA) y exporta los vectores listos
para la base de datos vectorial (Fase 3).
"""

import argparse
import datetime
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

import numpy as np
from dotenv import load_dotenv
from openai import APIConnectionError, APIError, OpenAI, RateLimitError
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from tqdm import tqdm

# Cargar variables de entorno desde .env
load_dotenv()

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fase-2-embeddings")


class QualityAssuranceError(Exception):
    """Excepción lanzada cuando una comprobación crítica de QA falla."""
    pass


def load_input_nodes(input_path: Path) -> List[Dict[str, Any]]:
    """
    Carga y valida el archivo JSON de nodos de la Fase 1.
    Soporta formatos tanto de lista directa como de diccionarios encapsulados.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"No se encontró el archivo de entrada: {input_path}")

    logger.info("Cargando nodos desde: %s", input_path)
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        nodes = data
    elif isinstance(data, dict):
        if "nodes" in data and isinstance(data["nodes"], list):
            nodes = data["nodes"]
        elif "records" in data and isinstance(data["records"], list):
            nodes = data["records"]
        else:
            raise ValueError(
                "El JSON de entrada debe ser una lista o un objeto con las claves 'nodes' o 'records'."
            )
    else:
        raise ValueError(f"Formato no reconocido en {input_path}: se esperaba list o dict.")

    if not nodes:
        raise ValueError(f"El archivo {input_path} no contiene nodos para procesar.")

    logger.info("Total de nodos cargados exitosamente: %d", len(nodes))
    return nodes


def get_openai_client(api_key: Optional[str] = None) -> OpenAI:
    """
    Inicializa el cliente de OpenAI recuperando la clave de API desde argumentos,
    variables de entorno o el archivo .env.
    """
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        raise ValueError(
            "API Key de OpenAI no encontrada. Configúrala mediante el argumento --api-key, "
            "la variable de entorno OPENAI_API_KEY o en el archivo .env."
        )
    return OpenAI(api_key=key)


def build_embedding_fetcher(client: OpenAI, model: str, dimensions: int):
    """
    Crea una función decorada con Tenacity para procesar lotes con reintentos
    automáticos y retroceso exponencial ante fallos transitorios o rate limits (HTTP 429).
    """
    @retry(
        retry=retry_if_exception_type((RateLimitError, APIConnectionError, APIError)),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(6),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _fetch_batch_embeddings(texts: List[str]) -> List[List[float]]:
        # Sanitizar textos: OpenAI no acepta strings vacíos
        sanitized_texts: List[str] = [t if (t and t.strip()) else "N/A" for t in texts]

        # Modelos text-embedding-3 soportan parámetro 'dimensions' nativo (MRL)
        if "text-embedding-3" in model:
            response = client.embeddings.create(
                input=cast(Any, sanitized_texts),
                model=model,
                dimensions=dimensions,
            )
        else:
            # Fallback para modelos que no soportan el parámetro dimensions en API
            response = client.embeddings.create(
                input=cast(Any, sanitized_texts),
                model=model,
            )

        # Ordenar resultados por índice para garantizar alineación 1:1 estricta
        sorted_data = sorted(response.data, key=lambda item: item.index)
        embeddings = [item.embedding for item in sorted_data]

        # Truncar manualmente si el modelo no aceptó el parámetro dimensions
        if "text-embedding-3" not in model and len(embeddings[0]) > dimensions:
            embeddings = [emb[:dimensions] for emb in embeddings]

        return embeddings

    return _fetch_batch_embeddings


def generate_all_embeddings(
    nodes: List[Dict[str, Any]],
    client: OpenAI,
    model: str,
    dimensions: int,
    batch_size: int,
) -> np.ndarray:
    """
    Genera los embeddings para todos los nodos en lotes configurables con barra de progreso.
    Aplica compresión Matryoshka (MRL) y normalización unitaria L2.
    """
    fetch_batch = build_embedding_fetcher(client, model, dimensions)
    total_nodes = len(nodes)
    all_vectors: List[List[float]] = []

    logger.info(
        "Iniciando generación de embeddings (Modelo: %s | Dimensiones: %d | Batch Size: %d)...",
        model,
        dimensions,
        batch_size,
    )

    texts = [node.get("text_to_embed", "") for node in nodes]

    with tqdm(total=total_nodes, desc="Generando embeddings", unit="nodo") as pbar:
        for i in range(0, total_nodes, batch_size):
            batch_texts = texts[i : i + batch_size]
            batch_embeddings = fetch_batch(batch_texts)
            all_vectors.extend(batch_embeddings)
            pbar.update(len(batch_texts))

    raw_matrix = np.array(all_vectors, dtype=np.float32)

    # Forzar normalización L2 unitaria: vector / ||vector||_2
    logger.info("Aplicando normalización L2 a los vectores resultantes...")
    norms = np.linalg.norm(raw_matrix, axis=1, keepdims=True)
    # Evitar división por cero si existiera algún vector nulo
    norms[norms == 0.0] = 1.0
    normalized_matrix = raw_matrix / norms

    return normalized_matrix


def find_test_pairs(
    nodes: List[Dict[str, Any]]
) -> Tuple[Tuple[int, int, str], Tuple[int, int, str]]:
    """
    Identifica de forma heurística y automática dos pares de prueba dentro del catálogo:
    - Par A (Similar): Nodos de la misma marca y subcategoría/categoría.
    - Par B (Disímil): Nodos de categorías completamente distintas.
    """
    similar_pair: Optional[Tuple[int, int, str]] = None
    dissimilar_pair: Optional[Tuple[int, int, str]] = None

    total = len(nodes)

    # 1. Buscar par similar
    for i in range(total):
        meta_i = nodes[i].get("metadata", {})
        cat_i = meta_i.get("categoria", "")
        marca_i = meta_i.get("marca", "")
        subcat_i = meta_i.get("subcategoria", "")

        for j in range(i + 1, total):
            meta_j = nodes[j].get("metadata", {})
            cat_j = meta_j.get("categoria", "")
            marca_j = meta_j.get("marca", "")
            subcat_j = meta_j.get("subcategoria", "")

            if cat_i and cat_i == cat_j and marca_i and marca_i == marca_j:
                desc_i = nodes[i].get("node_id", f"idx_{i}")
                desc_j = nodes[j].get("node_id", f"idx_{j}")
                reason = f"Misma marca ({marca_i}) y categoría ({cat_i})"
                similar_pair = (i, j, reason)
                break
        if similar_pair:
            break

    # Fallback par similar si no hay metadata común
    if not similar_pair and total >= 2:
        similar_pair = (0, 1, "Primeros dos nodos contiguos (Fallback)")

    # 2. Buscar par disímil
    for i in range(total):
        meta_i = nodes[i].get("metadata", {})
        cat_i = meta_i.get("categoria", "")

        for j in range(total - 1, i, -1):
            meta_j = nodes[j].get("metadata", {})
            cat_j = meta_j.get("categoria", "")

            if cat_i and cat_j and cat_i != cat_j:
                reason = f"Categorías distintas: '{cat_i}' vs '{cat_j}'"
                dissimilar_pair = (i, j, reason)
                break
        if dissimilar_pair:
            break

    # Fallback par disímil
    if not dissimilar_pair and total >= 2:
        dissimilar_pair = (0, total - 1, "Extremos del catálogo (Fallback)")

    return (
        similar_pair or (0, min(1, total - 1), "N/A"),
        dissimilar_pair or (0, max(0, total - 1), "N/A"),
    )


def run_qa_suite(
    nodes: List[Dict[str, Any]],
    matrix: np.ndarray,
    target_dim: int,
) -> None:
    """
    Suite integrada de validación y control de calidad (QA).
    Verifica integridad dimensional, ausencia de corrupción numérica,
    normalización L2 estricta, correspondencia 1:1 y sanity check semántico.
    """
    logger.info("==================================================")
    logger.info("INICIANDO SUITE DE CONTROL DE CALIDAD (QA VECTORES)")
    logger.info("==================================================")

    total_nodes = len(nodes)
    total_vectors, actual_dim = matrix.shape

    # 1. Consistencia 1:1
    if total_nodes != total_vectors:
        raise QualityAssuranceError(
            f"[CRÍTICO] Inconsistencia en cantidad: {total_nodes} nodos de entrada vs {total_vectors} vectores generados."
        )
    logger.info("✓ [1/5] Correspondencia 1:1: %d nodos == %d vectores.", total_nodes, total_vectors)

    # 2. Integridad Dimensional
    if actual_dim != target_dim:
        raise QualityAssuranceError(
            f"[CRÍTICO] Fallo de dimensión: Se esperaba dimensión {target_dim}, pero los vectores tienen {actual_dim}."
        )
    logger.info("✓ [2/5] Integridad dimensional: Todos los vectores tienen longitud exacta %d.", target_dim)

    # 3. Ausencia de Corrupción Numérica
    has_nan = np.isnan(matrix).any()
    has_inf = np.isinf(matrix).any()
    zero_vectors = np.all(matrix == 0.0, axis=1).sum()

    if has_nan:
        raise QualityAssuranceError("[CRÍTICO] Se detectaron valores NaN en la matriz de embeddings.")
    if has_inf:
        raise QualityAssuranceError("[CRÍTICO] Se detectaron valores Infinitos en la matriz de embeddings.")
    if zero_vectors > 0:
        raise QualityAssuranceError(f"[CRÍTICO] Se detectaron {zero_vectors} vectores nulos (todo ceros).")
    logger.info("✓ [3/5] Integridad numérica: Sin valores NaN, Inf ni vectores nulos.")

    # 4. Normalización L2 (Norma = 1.0 +- 1e-5)
    vector_norms = np.linalg.norm(matrix, axis=1)
    norm_diffs = np.abs(vector_norms - 1.0)
    max_diff = float(np.max(norm_diffs))
    if max_diff > 1e-5:
        raise QualityAssuranceError(
            f"[CRÍTICO] Fallo en normalización L2: Desviación máxima observada ({max_diff:.8f}) excede 1e-5."
        )
    logger.info("✓ [4/5] Normalización L2 unitaria: Norma media = %.6f (Desviación máxima: %.2e).", float(np.mean(vector_norms)), max_diff)

    # 5. Sanity Check Semántico Automático
    similar_test, dissimilar_test = find_test_pairs(nodes)

    idx_s1, idx_s2, reason_sim = similar_test
    idx_d1, idx_d2, reason_dis = dissimilar_test

    # Para vectores unitarios, el producto escalar es idéntico a la similitud de coseno
    sim_score_similar = float(np.dot(matrix[idx_s1], matrix[idx_s2]))
    sim_score_dissimilar = float(np.dot(matrix[idx_d1], matrix[idx_d2]))

    logger.info("✓ [5/5] Sanity Check Semántico:")
    logger.info("   ├─ PAR SIMILAR [Nodo %s vs %s] (%s):", nodes[idx_s1].get("node_id"), nodes[idx_s2].get("node_id"), reason_sim)
    logger.info("   │  └─ Similitud de Coseno: %.4f", sim_score_similar)
    logger.info("   └─ PAR DISÍMIL [Nodo %s vs %s] (%s):", nodes[idx_d1].get("node_id"), nodes[idx_d2].get("node_id"), reason_dis)
    logger.info("      └─ Similitud de Coseno: %.4f", sim_score_dissimilar)

    # Alerta preventiva si el score similar es inferior a lo esperado
    if sim_score_similar <= sim_score_dissimilar:
        logger.warning(
            "[ATENCIÓN] La similitud del par similar (%.4f) no supera a la del par disímil (%.4f). "
            "Revisa la calidad de los textos de entrada o la selección de prueba.",
            sim_score_similar,
            sim_score_dissimilar,
        )
    elif sim_score_similar < 0.65:
        logger.warning(
            "[ATENCIÓN] La similitud del par similar es menor a 0.65 (obtenido: %.4f).",
            sim_score_similar,
        )

    logger.info("==================================================")
    logger.info("SUITE DE QA SUPERADA CON ÉXITO")
    logger.info("==================================================")


def export_embeddings_deliverable(
    nodes: List[Dict[str, Any]],
    matrix: np.ndarray,
    output_path: Path,
    source_file_name: str,
    model: str,
    dimensions: int,
) -> None:
    """
    Construye y exporta el archivo JSON final con la estructura requerida para la Fase 3.
    """
    total_vectors = len(nodes)
    vector_norms = np.linalg.norm(matrix, axis=1)

    records: List[Dict[str, Any]] = []
    for i, node in enumerate(nodes):
        emb_list = matrix[i].tolist()
        record = {
            "node_id": node.get("node_id", f"node_{i}"),
            "embedding": emb_list,
            "dimension": dimensions,
            "norm_l2": round(float(vector_norms[i]), 6),
            "text_content": node.get("text_to_embed", ""),
            "metadata": node.get("metadata", {}),
        }
        records.append(record)

    output_payload = {
        "metadata": {
            "source_nodes_file": source_file_name,
            "embedding_model": model,
            "dimensions": dimensions,
            "total_vectors": total_vectors,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        "records": records,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Guardando entregable de embeddings en: %s ...", output_path)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=2, ensure_ascii=False)

    file_size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info("Archivo generado correctamente (%.2f MB).", file_size_mb)


def generate_output_filename(codigo_proveedor: str, phase: str = "embedding") -> str:
    """Genera el nombre de archivo automático: <id_proveedor>_<phase>_<YYMMDDHHmmSS>.json"""
    timestamp = datetime.datetime.now().strftime("%y%m%d%H%M%S")
    prov_code = (codigo_proveedor or "PROV").strip().upper()[:3]
    return f"{prov_code}_{phase}_{timestamp}.json"


def extract_codigo_proveedor(nodes: List[Dict[str, Any]], fallback: str = "PROV") -> str:
    """Extrae el código de 3 caracteres del proveedor desde los nodos cargados."""
    for node in nodes:
        meta = node.get("metadata", {})
        if isinstance(meta, dict) and meta.get("codigo_proveedor"):
            return str(meta["codigo_proveedor"]).strip().upper()[:3]
    return fallback


def run_pipeline(
    input_path_str: str,
    output_path_str: Optional[str],
    model: str,
    dimensions: int,
    batch_size: int,
    api_key: Optional[str] = None,
    codigo_proveedor: Optional[str] = None,
) -> None:
    """
    Orquesta el flujo completo de la Fase 2:
    1. Carga de nodos estructurados.
    2. Generación y compresión de embeddings (MRL).
    3. Normalización L2.
    4. Ejecución de suite de QA.
    5. Exportación del artefacto final para la base de datos vectorial.
    """
    input_path = Path(input_path_str)

    # Si no encuentra el default 'catalogo_nodos.json', comprobar si existe 'catalogo_nodes.json'
    if not input_path.exists() and input_path_str == "catalogo_nodos.json":
        fallback_path = Path("catalogo_nodes.json")
        if fallback_path.exists():
            logger.info("Archivo 'catalogo_nodos.json' no encontrado. Usando '%s'.", fallback_path)
            input_path = fallback_path

    nodes = load_input_nodes(input_path)

    if not codigo_proveedor:
        codigo_proveedor = extract_codigo_proveedor(nodes)

    if not output_path_str:
        output_path_str = generate_output_filename(codigo_proveedor, "embedding")

    output_path = Path(output_path_str)
    client = get_openai_client(api_key=api_key)

    vectors_matrix = generate_all_embeddings(
        nodes=nodes,
        client=client,
        model=model,
        dimensions=dimensions,
        batch_size=batch_size,
    )

    run_qa_suite(
        nodes=nodes,
        matrix=vectors_matrix,
        target_dim=dimensions,
    )

    export_embeddings_deliverable(
        nodes=nodes,
        matrix=vectors_matrix,
        output_path=output_path,
        source_file_name=input_path.name,
        model=model,
        dimensions=dimensions,
    )

    logger.info("¡Fase 2 de Embeddings completada exitosamente! Guardado en: %s", output_path)


def parse_arguments() -> argparse.Namespace:
    """Configura y parsea los argumentos de línea de comandos."""
    parser = argparse.ArgumentParser(
        description="Fase 2 - Generación de Embeddings MRL y Validación QA para Catálogo RAG."
    )
    parser.add_argument(
        "--input",
        "-i",
        default="catalogo_nodos.json",
        help="Ruta al archivo JSON con los nodos estructurados de la Fase 1 (default: catalogo_nodos.json).",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Ruta donde se exportará el JSON con los embeddings (default: automático <COD>_embedding_<YYMMDDHHmmSS>.json).",
    )
    parser.add_argument(
        "--codigo_proveedor", "--cod_prov",
        default=None,
        help="Código de 3 caracteres del proveedor (opcional, se infiere de los nodos de entrada).",
    )
    parser.add_argument(
        "--model",
        "-m",
        default="text-embedding-3-large",
        help="Modelo de embeddings a utilizar en OpenAI (default: text-embedding-3-large).",
    )
    parser.add_argument(
        "--dimensions",
        "-d",
        type=int,
        default=256,
        help="Dimensión objetivo de salida para compresión Matryoshka (default: 256).",
    )
    parser.add_argument(
        "--batch_size",
        "-b",
        type=int,
        default=100,
        help="Tamaño de lote para llamadas a la API (default: 100).",
    )
    parser.add_argument(
        "--api-key",
        "-k",
        default=None,
        help="API Key de OpenAI (opcional, por defecto lee desde .env u OPENAI_API_KEY).",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    try:
        run_pipeline(
            input_path_str=args.input,
            output_path_str=args.output,
            model=args.model,
            dimensions=args.dimensions,
            batch_size=args.batch_size,
            api_key=args.api_key,
            codigo_proveedor=args.codigo_proveedor,
        )
    except Exception as err:
        logger.error("Error fatal en la ejecución del pipeline: %s", err, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
