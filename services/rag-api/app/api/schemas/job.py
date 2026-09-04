#!/usr/bin/env python3
"""
app/api/schemas/job.py
======================
DTOs para tracking de tareas asíncronas de ingesta.
"""

from typing import Dict, Any, Optional
from pydantic import BaseModel, Field


class JobStatusResponse(BaseModel):
    job_id: str
    status: str = Field(..., description="Estado del trabajo: 'PENDING', 'RUNNING', 'COMPLETED', 'FAILED'")
    created_at: str
    completed_at: Optional[str] = None
    source_document: str
    table_name: str
    codigo_proveedor: str
    progress_message: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
