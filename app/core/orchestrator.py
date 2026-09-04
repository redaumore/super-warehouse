#!/usr/bin/env python3
"""
app/core/orchestrator.py
========================
Orquestador Maestro End-to-End (Fases 0 a 7) para Catálogos Industriales y Dominios Técnicos.

Implementa el patrón Fachada (Facade) para unificar los tres flujos del sistema:
1. Ingesta Batch Integral (.ingest_catalog_pdf):
   - Ingesta y Parsing PDF (Fase 0: pdf_parser).
   - Chunking Semántico y Construcción de Nodos (Fase 1: chunker).
   - Generación de Embeddings Matryoshka 256d y QA (Fase 2: embedder).
   - Indexación HNSW en PostgreSQL / pgvector (Fase 3: vector_store).

2. Consulta y Servicio en Tiempo Real (.query):
   - Recuperación Híbrida BM25 + Vectorial Densa (Fase 4: hybrid).
   - Reranking Semántico Cross-Encoder (Fase 5: reranker).
   - Generación LLM con citaciones y verificación Grounded (Fase 6: generator).

3. Auditoría y Calidad Continua (.evaluate_pipeline):
   - Evaluación cuantitativa de la Tríada RAG y Quality Gate (Fase 7: evaluator).
"""

import os
import sys
import time
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional

from app.config import settings
from app.core.ingestion.pdf_parser import DirectLunaCatalogProcessor
from app.core.ingestion.chunker import run_pipeline as run_chunking_pipeline
from app.core.ingestion.embedder import run_pipeline as run_embedding_pipeline
from app.core.ingestion.vector_store import PgVectorManager, load_embeddings_json
from app.core.retrieval.generator import run_rag_pipeline, GenerationResult
from app.core.evaluation.evaluator import (
    RAGTriadEvaluator,
    ContinuousEvaluationPipeline,
    EvaluationSample,
    AggregateReport,
    QualityGatePolicy,
    get_hardware_catalog_test_samples
)

logger = logging.getLogger("RAG_Orchestrator")


# ============================================================================
# 1. ENTIDADES TIPADAS
# ============================================================================

@dataclass
class IngestionResult:
    """Resultado estructurado de la ingesta batch de un catálogo."""
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
    """Respuesta unificada de consulta RAG entregada al cliente."""
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
        table_name: Optional[str] = None,
        llm_model: Optional[str] = None,
        llm_provider: str = "auto",
        reranker_model: Optional[str] = None,
        policy: Optional[QualityGatePolicy] = None,
        auto_audit: bool = False
    ):
        self.table_name = table_name or settings.DEFAULT_TABLE_NAME
        self.llm_model = llm_model or settings.DEFAULT_LLM_MODEL
        self.llm_provider = llm_provider
        self.reranker_model = reranker_model or settings.DEFAULT_RERANKER_MODEL
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
        luna_model: Optional[str] = None,
        embedding_model: Optional[str] = None,
        embedding_dimensions: Optional[int] = None,
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
        luna_m = luna_model or settings.DEFAULT_LUNA_MODEL
        emb_m = embedding_model or settings.DEFAULT_EMBEDDING_MODEL
        emb_dim = embedding_dimensions or settings.DEFAULT_EMBEDDING_DIM
        out_directory = output_dir or str(settings.ARTIFACTS_DIR)

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
        os.makedirs(out_directory, exist_ok=True)
        output_files: Dict[str, str] = {}

        try:
            # -----------------------------------------------------------------
            # FASE 0: EXTRACCIÓN Y PARSING PDF CON OPENAI MULTIMODAL
            # -----------------------------------------------------------------
            logger.info("\n>>> [PASO 1/4] Ejecutando Fase 0: Extracción estructurada con Visión...")
            processor = DirectLunaCatalogProcessor(model=luna_m)
            fase_0_out = os.path.join(out_directory, f"{cod_prov}_ingestion.json")

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
            fase_1_out = os.path.join(out_directory, f"{cod_prov}_nodes.json")

            nodes, fase_1_json_path = run_chunking_pipeline(
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
            logger.info(f"\n>>> [PASO 3/4] Ejecutando Fase 2: Embeddings Matryoshka ({emb_dim}d) con QA...")
            fase_2_out = os.path.join(out_directory, f"{cod_prov}_embeddings.json")

            vectors_matrix, fase_2_json_path = run_embedding_pipeline(
                input_path_str=fase_1_json_path,
                output_path_str=fase_2_out,
                model=emb_m,
                dimensions=emb_dim,
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
            effective_db_url = db_url or settings.get_db_url()
            records, dimension = load_embeddings_json(fase_2_json_path)

            manager = PgVectorManager(db_url=effective_db_url, table_name=self.table_name, dimension=dimension)
            manager.init_schema(recreate=recreate_table)

            # Limpieza previa de productos existentes del proveedor
            if not recreate_table and cod_prov:
                deleted_prev = manager.delete_records_by_provider(codigo_proveedor=cod_prov)
                logger.info(f"Limpieza previa de productos: {deleted_prev} registros anteriores eliminados para proveedor '{cod_prov}'.")

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
        output_dir: Optional[str] = None
    ) -> AggregateReport:
        """
        Ejecuta el bucle cerrado de evaluación sobre el Golden Dataset.
        """
        out_directory = output_dir or os.path.join(str(settings.DATA_DIR), "evaluation")
        samples = dataset or get_hardware_catalog_test_samples()
        report = self.evaluation_pipeline.run_evaluation_suite(samples)

        os.makedirs(out_directory, exist_ok=True)
        md_path = os.path.join(out_directory, "reporte-evaluacion-triada-rag.md")
        json_path = os.path.join(out_directory, "reporte-evaluacion-triada-rag.json")

        self.evaluation_pipeline.export_markdown_report(report, md_path)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)

        logger.info(f"[Orchestrator] Evaluación completada. Reportes en '{out_directory}'.")
        return report
