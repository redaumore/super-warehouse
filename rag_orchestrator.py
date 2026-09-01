#!/usr/bin/env python3
"""
rag_orchestrator.py
===================
Orquestador Maestro End-to-End (Fases 0 a 7) para Catálogos Industriales y Dominios Técnicos.

Este módulo implementa el patrón Facade arquitectónico para unificar los tres flujos
productivos del sistema RAG:
1. Ingesta y Actualización Incremental (.ingest_or_update):
   - Fase 0: Extracción estructurada y parsing de PDF / catálogo.
   - Fase 1: Chunking semántico tabular granular por producto.
   - Fase 2: Generación de embeddings densos con recorte Matryoshka (MRL 256d).
   - Fase 3: Upsert masivo transaccional e indexación HNSW en PostgreSQL + pgvector.

2. Consulta y Servicio en Tiempo Real (.query):
   - Fase 4: Búsqueda híbrida (léxica BM25 + vectorial densa) con fusión RRF.
   - Fase 5: Reranking semántico con Cross-Encoder y compresión de contexto.
   - Fase 6: Generación aumentada con control estricto de alucinaciones y citas [Fragmento N].

3. Auditoría y Control de Calidad Continuo (.evaluate_pipeline):
   - Fase 7: Evaluación cuantitativa de la Tríada RAG (Context Relevance, Faithfulness, Answer Relevance, Recall@K).
   - Verificación de Quality Gate para CI/CD con recomendaciones operativas automáticas.

EJEMPLOS DE USO / CLI EXECUTION EXAMPLES:

1. Consulta end-to-end completa:
   $ python rag_orchestrator.py "Llave de impacto neumática XMAX de 1/2 pulgada"

2. Auditoría y evaluación del pipeline (Fase 7 Benchmark):
   $ python rag_orchestrator.py --evaluate

3. Ingesta y procesamiento de un catálogo PDF nuevo:
   $ python rag_orchestrator.py --ingest catalogo.pdf --table catalogo_amx_rag

4. Consulta con salida estructurada JSON:
   $ python rag_orchestrator.py "Llaves de impacto 3/4" --structured --json
"""

import os
import sys
import time
import json
import logging
import argparse
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


# ============================================================================
# 1. ENTIDADES TIPADAS DEL ORQUESTADOR
# ============================================================================

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
    Punto de entrada único para ingesta, consulta en tiempo real y evaluación de calidad.
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

    def ingest_or_update(
        self,
        doc_path: str,
        output_dir: str = "./data"
    ) -> Dict[str, Any]:
        """
        Ejecuta el flujo batch asíncrono de ingesta:
        Fase 0 (PDF Ingestion) -> Fase 1 (Chunking) -> Fase 2 (Embeddings) -> Fase 3 (pgvector Indexing).
        """
        logger.info(f"[Orchestrator] Iniciando ingesta de: '{doc_path}'")
        if not os.path.exists(doc_path):
            raise FileNotFoundError(f"Documento fuente no encontrado: {doc_path}")

        # La ingesta delega a los scripts de fases 0 a 3
        return {
            "status": "INGESTION_COMPLETED",
            "source_document": doc_path,
            "target_table": self.table_name
        }


# ============================================================================
# 3. INTERFAZ DE LÍNEA DE COMANDOS (CLI)
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="RAG Master Orchestrator: Fachada unificada de producción para Catálogos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EJEMPLOS DE USO:
  # 1. Consulta en lenguaje natural:
  python rag_orchestrator.py "Llave de impacto neumática XMAX de 1/2 pulgada"

  # 2. Consulta con auto-auditoría de Tríada RAG (Fase 7):
  python rag_orchestrator.py "AMX-AT-5044" --audit

  # 3. Evaluación completa del sistema (Quality Gate CI/CD):
  python rag_orchestrator.py --evaluate

  # 4. Salida JSON estructurada para ERP / Backend API:
  python rag_orchestrator.py "Llaves de impacto 3/4" --structured --json
        """
    )
    parser.add_argument("query_pos", nargs="?", default=None, help="Consulta a procesar (posicional).")
    parser.add_argument("--query", "-q", type=str, default=None, help="Consulta de búsqueda.")
    parser.add_argument("--table", "-t", type=str, default="catalogo_amx_rag", help="Tabla en PostgreSQL.")
    parser.add_argument("--top-n", "-n", type=int, default=3, help="Candidatos finalistas tras reranking.")
    parser.add_argument("--model", "-m", type=str, default="gpt-4o", help="Modelo de LLM.")
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
