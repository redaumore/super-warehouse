#!/usr/bin/env python3
"""
rag_orchestrator.py
===================
Orquestador Maestro End-to-End (Fases 0 a 7) para Catálogos Industriales y Dominios Técnicos.

Este módulo implementa el patrón Facade arquitectónico para unificar los tres flujos
productivos del sistema RAG:
1. Ingesta Batch Integral (.ingest_catalog_pdf / .ingest_or_update):
   - Fase 0: Extracción estructurada y parsing de PDF / catálogo (GPT-5.6 Luna Multimodal).
   - Fase 1: Chunking semántico tabular granular por producto.
   - Fase 2: Generación de embeddings densos con recorte Matryoshka (MRL 256d) y QA suite.
   - Fase 3: Upsert masivo transaccional e indexación HNSW en PostgreSQL + pgvector.

2. Consulta y Servicio en Tiempo Real (.query):
   - Fase 4: Búsqueda híbrida (léxica BM25 + vectorial densa) con fusión RRF.
   - Fase 5: Reranking semántico con Cross-Encoder y compresión de contexto.
   - Fase 6: Generación aumentada con control estricto de alucinaciones y citas [Fragmento N].

3. Auditoría y Control de Calidad Continuo (.evaluate_pipeline):
   - Fase 7: Evaluación cuantitativa de la Tríada RAG (Context Relevance, Faithfulness, Answer Relevance, Recall@K).
   - Verificación de Quality Gate para CI/CD con recomendaciones operativas automáticas.

EJEMPLOS DE USO / CLI EXECUTION EXAMPLES:

1. Ingesta y procesamiento completo de un catálogo PDF en un solo comando:
   $ python rag_orchestrator.py --ingest "data/FN Catalogo.pdf" --table "catalogo_productos_rag" --cod-prov "FDN" --recreate-table

2. Ingesta con rango de páginas y omisión de portadas:
   $ python rag_orchestrator.py --ingest "data/FN Catalogo.pdf" --start-page 1 --max-pages 5 --skip-pages "1-2"

3. Consulta end-to-end completa:
   $ python rag_orchestrator.py "Llave de impacto neumática XMAX de 1/2 pulgada" --table "catalogo_productos_rag"

4. Auditoría y evaluación del pipeline (Fase 7 Benchmark):
   $ python rag_orchestrator.py --evaluate

5. Consulta con salida estructurada JSON:
   $ python rag_orchestrator.py "Llaves de impacto 3/4" --table "catalogo_productos_rag" --structured --json
"""

import os
import sys
import time
import json
import logging
import argparse
import importlib
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Union
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("RAG_Master_Orchestrator")

# Importaciones de Fases RAG
from fase_6_generator import (
    run_rag_pipeline,
    GenerationResult,
    InputContextChunk
)
from fase_7_evaluator import (
    RAGTriadEvaluator,
    ContinuousEvaluationPipeline,
    EvaluationSample,
    EvaluationResult,
    AggregateReport,
    QualityGatePolicy,
    get_hardware_catalog_test_samples
)
from fase_2_embeddings import run_pipeline as run_embeddings_pipeline
from fase_3_pgvector import PgVectorManager, load_embeddings_json, build_db_url


# Helpers para carga dinámica de módulos con guiones en el nombre
def _get_fase_0_module():
    """Carga dinámicamente el módulo de Fase 0 (PDF Ingestion)."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)
    return importlib.import_module("fase-0-pdf-ingestion")


def _get_fase_1_module():
    """Carga dinámicamente el módulo de Fase 1 (Chunking)."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)
    return importlib.import_module("fase-1-chunking")


# ============================================================================
# 1. ENTIDADES TIPADAS DEL ORQUESTADOR
# ============================================================================

@dataclass
class IngestionResult:
    """Resultado estructurado de la ingesta batch de un catálogo completo."""
    status: str
    source_document: str
    target_table: str
    codigo_proveedor: str
    nombre_proveedor: str
    pages_processed: int
    total_products_extracted: int
    total_nodes_generated: int
    total_embeddings_created: int
    total_records_indexed: int
    total_tokens_used: int
    total_elapsed_seconds: float
    output_files: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RAGResponse:
    """Respuesta unificada entregada al cliente o sistema destino."""
    query: str
    response_text: str
    is_refusal: bool
    status: str
    citations: List[str]
    is_fully_grounded: bool
    structured_json: Optional[Dict[str, Any]]
    context_chunks: List[Dict[str, Any]]
    total_latency_ms: float
    model_name: str
    evaluation: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================================
# 2. ORQUESTADOR MAESTRO (FACHADA INTEGRAL)
# ============================================================================

class RAGOrchestrator:
    """
    Punto de entrada único para ingesta de catálogos, consulta en tiempo real y evaluación de calidad.
    """

    def __init__(
        self,
        table_name: str = "catalogo_amx_rag",
        llm_model: str = "gpt-4o",
        llm_provider: str = "auto",
        reranker_model: str = "BAAI/bge-reranker-v2-m3",
        policy: Optional[QualityGatePolicy] = None,
        auto_audit: bool = False
    ):
        self.table_name = table_name
        self.llm_model = llm_model
        self.llm_provider = llm_provider
        self.reranker_model = reranker_model
        self.policy = policy or QualityGatePolicy()
        self.auto_audit = auto_audit
        self.evaluator = RAGTriadEvaluator(self.policy)
        self.evaluation_pipeline = ContinuousEvaluationPipeline(evaluator=self.evaluator)

    def ingest_catalog_pdf(
        self,
        pdf_path: str,
        codigo_proveedor: str = "FDN",
        nombre_proveedor: str = "Ferretera del Norte",
        proveedor_id: Optional[str] = None,
        marca: Optional[str] = None,
        start_page: int = 1,
        max_pages: Optional[int] = None,
        skip_pages: Optional[str] = None,
        use_vision: bool = True,
        luna_model: str = "gpt-5.6-luna",
        embedding_model: str = "text-embedding-3-large",
        embedding_dimensions: int = 256,
        recreate_table: bool = False,
        output_dir: Optional[str] = None,
        db_url: Optional[str] = None,
        batch_size: int = 100,
        skip_qa: bool = False
    ) -> IngestionResult:
        """
        Ejecuta el pipeline de ingesta batch de punta a punta:
        Fase 0 (Extracción PDF) -> Fase 1 (Chunking) -> Fase 2 (Embeddings MRL) -> Fase 3 (Indexación HNSW pgvector).
        """
        t0 = time.perf_counter()
        logger.info("=" * 80)
        logger.info(f"[Orchestrator] INICIANDO PIPELINE DE INGESTA INTEGRAL")
        logger.info(f"Archivo PDF:        {pdf_path}")
        logger.info(f"Tabla Destino:      {self.table_name}")
        logger.info(f"Proveedor:          {nombre_proveedor} (Código: {codigo_proveedor})")
        logger.info(f"Recrear Tabla:      {recreate_table}")
        logger.info("=" * 80)

        if not os.path.exists(pdf_path):
            error_msg = f"El archivo PDF fuente no existe: '{pdf_path}'"
            logger.error(error_msg)
            return IngestionResult(
                status="ERROR",
                source_document=pdf_path,
                target_table=self.table_name,
                codigo_proveedor=codigo_proveedor,
                nombre_proveedor=nombre_proveedor,
                pages_processed=0,
                total_products_extracted=0,
                total_nodes_generated=0,
                total_embeddings_created=0,
                total_records_indexed=0,
                total_tokens_used=0,
                total_elapsed_seconds=0.0,
                error=error_msg
            )

        cod_prov = (codigo_proveedor or "PRO").strip().upper()[:3]
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        output_files: Dict[str, str] = {}

        try:
            # -----------------------------------------------------------------
            # FASE 0: EXTRACCIÓN Y PARSING PDF CON OPENAI MULTIMODAL
            # -----------------------------------------------------------------
            logger.info("\n>>> [PASO 1/4] Ejecutando Fase 0: Extracción estructurada con Visión...")
            fase_0_module = _get_fase_0_module()
            processor = fase_0_module.DirectLunaCatalogProcessor(model=luna_model)

            fase_0_out = os.path.join(output_dir, f"{cod_prov}_ingestion.json") if output_dir else None

            payload_f0, fase_0_json_path = processor.process_catalog(
                pdf_path=pdf_path,
                output_json=fase_0_out,
                start_page=start_page,
                max_pages=max_pages,
                use_vision=use_vision,
                skip_pages=skip_pages,
                proveedor_id=proveedor_id,
                proveedor=nombre_proveedor,
                nombre_proveedor=nombre_proveedor,
                codigo_proveedor=cod_prov,
                marca=marca
            )
            output_files["fase_0_ingestion"] = fase_0_json_path

            meta_f0 = payload_f0.get("metadata", {})
            exec_summary = meta_f0.get("execution_summary", {})
            token_usage = meta_f0.get("token_usage", {})
            pages_processed = exec_summary.get("pages_processed", 0)
            total_products = exec_summary.get("total_products_extracted", len(payload_f0.get("products_flat", [])))
            total_tokens = token_usage.get("total_tokens", 0)

            logger.info(f"Fase 0 finalizada: {pages_processed} páginas procesadas, {total_products} productos extraídos.")

            # -----------------------------------------------------------------
            # FASE 1: CHUNKING SEMÁNTICO TABULAR
            # -----------------------------------------------------------------
            logger.info("\n>>> [PASO 2/4] Ejecutando Fase 1: Chunking semántico y construcción de nodos...")
            fase_1_module = _get_fase_1_module()
            fase_1_out = os.path.join(output_dir, f"{cod_prov}_nodes.json") if output_dir else None

            nodes, fase_1_json_path = fase_1_module.run_pipeline(
                input_path=fase_0_json_path,
                output_path=fase_1_out,
                encoding_name="cl100k_base",
                codigo_proveedor=cod_prov
            )
            output_files["fase_1_nodes"] = fase_1_json_path
            total_nodes = len(nodes)
            logger.info(f"Fase 1 finalizada: {total_nodes} nodos generados.")

            # -----------------------------------------------------------------
            # FASE 2: GENERACIÓN DE EMBEDDINGS DENSOS MRL (256D) Y QA SUITE
            # -----------------------------------------------------------------
            logger.info(f"\n>>> [PASO 3/4] Ejecutando Fase 2: Embeddings Matryoshka ({embedding_dimensions}d) con QA...")
            fase_2_out = os.path.join(output_dir, f"{cod_prov}_embeddings.json") if output_dir else None

            vectors_matrix, fase_2_json_path = run_embeddings_pipeline(
                input_path_str=fase_1_json_path,
                output_path_str=fase_2_out,
                model=embedding_model,
                dimensions=embedding_dimensions,
                batch_size=batch_size,
                codigo_proveedor=cod_prov
            )
            output_files["fase_2_embeddings"] = fase_2_json_path
            total_embeddings = len(vectors_matrix)
            logger.info(f"Fase 2 finalizada: {total_embeddings} vectores generados y validados.")

            # -----------------------------------------------------------------
            # FASE 3: INDEXACIÓN HNSW EN POSTGRESQL + PGVECTOR
            # -----------------------------------------------------------------
            logger.info(f"\n>>> [PASO 4/4] Ejecutando Fase 3: Upsert masivo e indexación HNSW en tabla '{self.table_name}'...")
            effective_db_url = db_url or build_db_url()
            records, dimension = load_embeddings_json(fase_2_json_path)

            manager = PgVectorManager(db_url=effective_db_url, table_name=self.table_name, dimension=dimension)
            manager.init_schema(recreate=recreate_table)
            manager.ingest_records(records, batch_size=batch_size)
            manager.create_indexes()

            if not skip_qa and records:
                sample_vec = records[0].get("embedding", [])
                sample_marca = records[0].get("metadata", {}).get("marca")
                manager.run_qa_suite(
                    expected_count=len(records),
                    sample_vector=sample_vec,
                    sample_marca=sample_marca,
                    codigo_proveedor=cod_prov
                )

            total_records_indexed = len(records)
            total_elapsed = round(time.perf_counter() - t0, 2)

            logger.info("=" * 80)
            logger.info(f"¡INGESTA INTEGRAL FINALIZADA CON ÉXITO EN {total_elapsed:.2f}s!")
            logger.info(f"Productos: {total_products} | Nodos: {total_nodes} | Vectores indexados: {total_records_indexed}")
            logger.info("=" * 80)

            return IngestionResult(
                status="SUCCESS",
                source_document=pdf_path,
                target_table=self.table_name,
                codigo_proveedor=cod_prov,
                nombre_proveedor=nombre_proveedor,
                pages_processed=pages_processed,
                total_products_extracted=total_products,
                total_nodes_generated=total_nodes,
                total_embeddings_created=total_embeddings,
                total_records_indexed=total_records_indexed,
                total_tokens_used=total_tokens,
                total_elapsed_seconds=total_elapsed,
                output_files=output_files
            )

        except Exception as exc:
            total_elapsed = round(time.perf_counter() - t0, 2)
            logger.error(f"Fallo crítico durante la ingesta integral: {exc}", exc_info=True)
            return IngestionResult(
                status="ERROR",
                source_document=pdf_path,
                target_table=self.table_name,
                codigo_proveedor=cod_prov,
                nombre_proveedor=nombre_proveedor,
                pages_processed=0,
                total_products_extracted=0,
                total_nodes_generated=0,
                total_embeddings_created=0,
                total_records_indexed=0,
                total_tokens_used=0,
                total_elapsed_seconds=total_elapsed,
                output_files=output_files,
                error=str(exc)
            )

    def ingest_or_update(
        self,
        doc_path: str,
        output_dir: Optional[str] = "./data",
        **kwargs: Any
    ) -> IngestionResult:
        """Alias compatible con la API del orquestador para ingesta directa de documentos."""
        return self.ingest_catalog_pdf(pdf_path=doc_path, output_dir=output_dir, **kwargs)

    def query(
        self,
        query_text: str,
        k_input: int = 20,
        top_n: int = 3,
        threshold: float = 0.45,
        structured_json: bool = False,
        audit_sample: bool = False,
        mock: bool = False
    ) -> RAGResponse:
        """
        Ejecuta el flujo online síncrono:
        Fase 4 (Recuperación Híbrida) -> Fase 5 (Reranking) -> Fase 6 (Generación determinista).
        """
        logger.info(f"[Orchestrator] Procesando consulta: '{query_text}'")
        t0 = time.perf_counter()

        gen_result: GenerationResult = run_rag_pipeline(
            query=query_text,
            table_name=self.table_name,
            k_input=k_input,
            top_n=top_n,
            threshold=threshold,
            reranker_model=self.reranker_model,
            llm_model=self.llm_model,
            provider=self.llm_provider,
            structured_json=structured_json,
            mock=mock
        )

        total_latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        # Extraer contextos para auditoría
        raw_ctx = gen_result.rag_triplet.get("context", []) or gen_result.rag_triplet.get("context_chunks", [])
        contexts = []
        for item in raw_ctx:
            if isinstance(item, str):
                contexts.append(item)
            elif isinstance(item, dict):
                contexts.append(item.get("content", str(item)))

        eval_result_dict = None
        if audit_sample or self.auto_audit:
            sample = EvaluationSample(
                sample_id=f"AUDIT-{int(time.time())}",
                query=query_text,
                contexts=contexts,
                response=gen_result.response_text,
                metadata={"latency_ms": total_latency_ms}
            )
            audit_res = self.evaluator.evaluate_sample(sample)
            eval_result_dict = audit_res.to_dict()

        return RAGResponse(
            query=query_text,
            response_text=gen_result.response_text,
            is_refusal=gen_result.is_refusal,
            status=gen_result.status,
            citations=gen_result.verification.citations_found,
            is_fully_grounded=gen_result.verification.is_fully_grounded,
            structured_json=gen_result.structured_json,
            context_chunks=raw_ctx,
            total_latency_ms=total_latency_ms,
            model_name=gen_result.model_name,
            evaluation=eval_result_dict
        )

    def evaluate_pipeline(
        self,
        dataset: Optional[List[EvaluationSample]] = None,
        output_dir: str = "./scratch/fase-7-evaluacion"
    ) -> AggregateReport:
        """
        Ejecuta el bucle cerrado de evaluación sobre el Golden Dataset.
        """
        samples = dataset or get_hardware_catalog_test_samples()
        report = self.evaluation_pipeline.run_evaluation_suite(samples)

        os.makedirs(output_dir, exist_ok=True)
        md_path = os.path.join(output_dir, "reporte-evaluacion-triada-rag.md")
        json_path = os.path.join(output_dir, "reporte-evaluacion-triada-rag.json")

        self.evaluation_pipeline.export_markdown_report(report, md_path)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)

        logger.info(f"[Orchestrator] Evaluación completada. Reportes en '{output_dir}'.")
        return report


# ============================================================================
# 3. INTERFAZ DE LÍNEA DE COMANDOS (CLI)
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="RAG Master Orchestrator: Fachada unificada de producción para Catálogos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EJEMPLOS DE USO:
  # 1. Ingesta completa de catálogo PDF (Fases 0 a 3 en un solo comando):
  python rag_orchestrator.py --ingest "data/FN Catalogo.pdf" --table "catalogo_productos_rag" --cod-prov "FDN" --recreate-table

  # 2. Ingesta acotada por páginas:
  python rag_orchestrator.py --ingest "data/FN Catalogo.pdf" --start-page 1 --max-pages 5 --skip-pages "1-2"

  # 3. Consulta en lenguaje natural:
  python rag_orchestrator.py "Llave de impacto neumática XMAX de 1/2 pulgada" --table "catalogo_productos_rag"

  # 4. Consulta con auto-auditoría de Tríada RAG (Fase 7):
  python rag_orchestrator.py "AMX-AT-5044" --audit

  # 5. Evaluación completa del sistema (Quality Gate CI/CD):
  python rag_orchestrator.py --evaluate

  # 6. Salida JSON estructurada para ERP / Backend API:
  python rag_orchestrator.py "Llaves de impacto 3/4" --structured --json
        """
    )
    # Argumentos de Ingesta
    parser.add_argument("--ingest", "-i", type=str, default=None, help="Ruta al archivo PDF para ingesta batch completa (Fases 0 a 3).")
    parser.add_argument("--codigo-proveedor", "--cod-prov", dest="codigo_proveedor", type=str, default="FDN", help="Código corto (3 caracteres) del proveedor.")
    parser.add_argument("--nombre-proveedor", "--proveedor", dest="nombre_proveedor", type=str, default="Ferretera del Norte", help="Nombre descriptivo del proveedor.")
    parser.add_argument("--proveedor-id", dest="proveedor_id", type=str, default=None, help="Slug único del proveedor.")
    parser.add_argument("--marca", type=str, default=None, help="Marca forzada para los productos.")
    parser.add_argument("--start-page", type=int, default=1, help="Página de inicio (1-indexed).")
    parser.add_argument("--max-pages", type=int, default=None, help="Cantidad máxima de páginas a procesar.")
    parser.add_argument("--skip-pages", type=str, default=None, help="Páginas o rangos a omitir (ej: '1-3,5').")
    parser.add_argument("--no-vision", action="store_true", help="Desactivar visión multimodal (solo capa texto).")
    parser.add_argument("--recreate-table", action="store_true", help="Elimina y recrea la tabla en PostgreSQL antes de ingestar.")
    parser.add_argument("--output-dir", type=str, default=None, help="Directorio para almacenar los JSON intermediarios.")

    # Argumentos de Consulta / Query
    parser.add_argument("query_pos", nargs="?", default=None, help="Consulta a procesar (posicional).")
    parser.add_argument("--query", "-q", type=str, default=None, help="Consulta de búsqueda.")
    parser.add_argument("--table", "-t", type=str, default="catalogo_amx_rag", help="Tabla en PostgreSQL.")
    parser.add_argument("--top-n", "-n", type=int, default=3, help="Candidatos finalistas tras reranking.")
    parser.add_argument("--model", "-m", type=str, default="gpt-4o", help="Modelo de LLM para generación.")
    parser.add_argument("--structured", "-s", action="store_true", help="Salida JSON estructurada.")
    parser.add_argument("--audit", "-a", action="store_true", help="Auditar la consulta con la Tríada RAG.")
    parser.add_argument("--evaluate", "-e", action="store_true", help="Ejecutar suite de evaluación completa.")
    parser.add_argument("--mock", action="store_true", help="Modo sintético offline.")
    parser.add_argument("--json", "-j", action="store_true", help="Imprimir respuesta en formato JSON.")

    args = parser.parse_args()

    orchestrator = RAGOrchestrator(
        table_name=args.table,
        llm_model=args.model,
        auto_audit=args.audit
    )

    # 1. Modo Ingesta Batch
    if args.ingest:
        result = orchestrator.ingest_catalog_pdf(
            pdf_path=args.ingest,
            codigo_proveedor=args.codigo_proveedor,
            nombre_proveedor=args.nombre_proveedor,
            proveedor_id=args.proveedor_id,
            marca=args.marca,
            start_page=args.start_page,
            max_pages=args.max_pages,
            skip_pages=args.skip_pages,
            use_vision=not args.no_vision,
            recreate_table=args.recreate_table,
            output_dir=args.output_dir
        )
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        else:
            print("\n" + "=" * 80)
            print("RESUMEN DE INGESTA INTEGRAL")
            print("=" * 80)
            print(f"Estado                 : {result.status}")
            print(f"Documento Origen       : {result.source_document}")
            print(f"Tabla PostgreSQL       : {result.target_table}")
            print(f"Páginas Procesadas     : {result.pages_processed}")
            print(f"Productos Extraídos    : {result.total_products_extracted}")
            print(f"Nodos Generados        : {result.total_nodes_generated}")
            print(f"Embeddings Creados     : {result.total_embeddings_created}")
            print(f"Registros Indexados    : {result.total_records_indexed}")
            print(f"Tokens Consumidos      : {result.total_tokens_used:,}")
            print(f"Tiempo Total           : {result.total_elapsed_seconds:.2f} s")
            if result.error:
                print(f"Error                  : {result.error}")
            print("=" * 80 + "\n")
        return

    # 2. Modo Evaluación Continua (Fase 7)
    if args.evaluate:
        report = orchestrator.evaluate_pipeline()
        print("\n" + "=" * 80)
        print("RESUMEN DE EVALUACIÓN CONTINUA (ORQUESTADOR MAESTRO)")
        print("=" * 80)
        print(f"Total Muestras Evaluadas : {report.total_samples}")
        print(f"Muestras Aprobadas       : {report.passed_samples} ({report.pass_rate:.1%})")
        print(f"Context Relevance Media  : {report.mean_context_relevance:.2%}")
        print(f"Faithfulness Media       : {report.mean_faithfulness:.2%}")
        print(f"Answer Relevance Media   : {report.mean_answer_relevance:.2%}")
        print(f"Recall@K Media           : {report.mean_recall_at_k:.2%}")
        print(f"Quality Gate Global      : {'✅ PASS' if report.all_passed_quality_gate else '❌ FAIL'}")
        print("=" * 80 + "\n")
        return

    # 3. Modo Consulta en Tiempo Real
    query_text = args.query or args.query_pos
    if not query_text:
        query_text = "Llave de impacto neumática XMAX de 1/2 pulgada AT-5044"

    response = orchestrator.query(
        query_text=query_text,
        top_n=args.top_n,
        structured_json=args.structured,
        audit_sample=args.audit,
        mock=args.mock
    )

    if args.json:
        print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2))
        return

    print("\n" + "=" * 80)
    print("RESPUESTA DEL ORQUESTADOR RAG")
    print(f"Consulta: \"{query_text}\"")
    print(f"Estado: {response.status} | Latencia Total: {response.total_latency_ms:.2f} ms | Modelo: {response.model_name}")
    print(f"Citas: {response.citations} | Grounded: {response.is_fully_grounded}")
    print("=" * 80)
    print(response.response_text)
    print("=" * 80)

    if response.evaluation:
        ev = response.evaluation
        print("\nAUDITORÍA TRÍADA RAG (FASE 7):")
        print(f" * Context Relevance : {ev['context_relevance']['score']:.2%}")
        print(f" * Faithfulness      : {ev['faithfulness']['score']:.2%}")
        print(f" * Answer Relevance  : {ev['answer_relevance']['score']:.2%}")
        print(f" * Quality Gate      : {'✅ PASS' if ev['overall_passed'] else '❌ FAIL'}")
        print("-" * 80 + "\n")


if __name__ == "__main__":
    main()
