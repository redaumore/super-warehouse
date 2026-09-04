#!/usr/bin/env python3
"""
app/api/v1/endpoints/evaluate.py
================================
Controlador para auditoría y evaluación cuantitativa de la Tríada RAG (Fase 7).
"""

import os
import logging
from fastapi import APIRouter, HTTPException, status

from app.config import settings
from app.api.schemas.evaluation import EvaluateRequest, EvaluateResponse
from app.core.orchestrator import RAGOrchestrator

router = APIRouter()
logger = logging.getLogger("EvaluateEndpoint")


@router.post("/evaluate", response_model=EvaluateResponse, summary="Ejecutar suite de evaluación RAG")
def evaluate_pipeline(req: EvaluateRequest) -> EvaluateResponse:
    """Ejecuta la suite de evaluación sobre el Golden Dataset y retorna las métricas de la Tríada RAG."""
    try:
        orchestrator = RAGOrchestrator(table_name=req.table_name or settings.DEFAULT_TABLE_NAME)
        out_dir = req.output_dir or str(settings.DATA_DIR / "evaluation")
        report = orchestrator.evaluate_pipeline(output_dir=out_dir)

        md_path = os.path.join(out_dir, "reporte-evaluacion-triada-rag.md")
        json_path = os.path.join(out_dir, "reporte-evaluacion-triada-rag.json")

        return EvaluateResponse(
            total_samples=report.total_samples,
            passed_samples=report.passed_samples,
            pass_rate=report.pass_rate,
            mean_context_relevance=report.mean_context_relevance,
            mean_faithfulness=report.mean_faithfulness,
            mean_answer_relevance=report.mean_answer_relevance,
            mean_recall_at_k=report.mean_recall_at_k,
            quality_gate_passed=report.all_passed_quality_gate,
            markdown_report_path=md_path,
            json_report_path=json_path
        )

    except Exception as e:
        logger.error(f"Error ejecutando evaluación de calidad: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Fallo durante la evaluación de la Tríada RAG: {str(e)}"
        )
