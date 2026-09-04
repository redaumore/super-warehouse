#!/usr/bin/env python3
"""
app/api/schemas/query.py
========================
DTOs para endpoints de consulta y búsqueda RAG.
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Texto de la consulta en lenguaje natural", example="Llave de impacto neumática 1/2 pulgada")
    table_name: str = Field(default="catalogo_productos_rag", description="Tabla en PostgreSQL con los embeddings")
    top_n: int = Field(default=3, ge=1, le=20, description="Cantidad de productos finalistas tras reranking")
    threshold: float = Field(default=0.45, ge=0.0, le=1.0, description="Umbral de corte de relevancia")
    structured_json: bool = Field(default=False, description="Forzar respuesta en formato JSON estructurado")
    audit: bool = Field(default=False, description="Ejecutar auditoría en caliente con la Tríada RAG")
    model: str = Field(default="gpt-4o", description="Modelo LLM a utilizar para generación")


class QueryResponse(BaseModel):
    query: str
    response_text: str
    is_refusal: bool
    status: str
    citations: List[str]
    is_fully_grounded: bool
    structured_json: Optional[Dict[str, Any]] = None
    context_chunks: List[Dict[str, Any]] = Field(default_factory=list)
    total_latency_ms: float
    model_name: str
    evaluation: Optional[Dict[str, Any]] = None
