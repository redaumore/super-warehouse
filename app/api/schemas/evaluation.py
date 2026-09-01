#!/usr/bin/env python3
"""
app/api/schemas/evaluation.py
=============================
DTOs para endpoints de evaluación y auditoría de la Tríada RAG.
"""

from pydantic import BaseModel, Field


class EvaluateRequest(BaseModel):
    table_name: str = Field(default="catalogo_productos_rag", description="Tabla a evaluar")
    output_dir: str = Field(default="./data/evaluation", description="Directorio de exportación de reportes")


class EvaluateResponse(BaseModel):
    total_samples: int
    passed_samples: int
    pass_rate: float
    mean_context_relevance: float
    mean_faithfulness: float
    mean_answer_relevance: float
    mean_recall_at_k: float
    quality_gate_passed: bool
    markdown_report_path: str
    json_report_path: str
