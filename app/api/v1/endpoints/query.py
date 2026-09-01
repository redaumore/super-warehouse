#!/usr/bin/env python3
"""
app/api/v1/endpoints/query.py
=============================
Controlador de consultas y búsqueda semántica híbrida en tiempo real (RAG Online).
"""

import logging
from fastapi import APIRouter, HTTPException, status

from app.config import settings
from app.api.schemas.query import QueryRequest, QueryResponse
from app.core.orchestrator import RAGOrchestrator, RAGResponse

router = APIRouter()
logger = logging.getLogger("QueryEndpoint")


@router.post("/query", response_model=QueryResponse, summary="Ejecutar consulta RAG en tiempo real")
def query_rag(req: QueryRequest) -> QueryResponse:
    """
    Ejecuta el flujo online síncrono completo:
    Recuperación Híbrida (BM25 + Vector) -> Reranker Cross-Encoder -> Generación LLM con citaciones y verificación Grounded.
    """
    try:
        orchestrator = RAGOrchestrator(
            table_name=req.table_name or settings.DEFAULT_TABLE_NAME,
            llm_model=req.model or settings.DEFAULT_LLM_MODEL,
            auto_audit=req.audit
        )

        response: RAGResponse = orchestrator.query(
            query_text=req.query,
            top_n=req.top_n,
            threshold=req.threshold,
            structured_json=req.structured_json,
            audit_sample=req.audit
        )

        return QueryResponse(
            query=response.query,
            response_text=response.response_text,
            is_refusal=response.is_refusal,
            status=response.status,
            citations=response.citations,
            is_fully_grounded=response.is_fully_grounded,
            structured_json=response.structured_json,
            context_chunks=response.context_chunks,
            total_latency_ms=response.total_latency_ms,
            model_name=response.model_name,
            evaluation=response.evaluation
        )

    except Exception as e:
        logger.error(f"Error procesando consulta RAG: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Fallo durante la recuperación y generación RAG: {str(e)}"
        )
