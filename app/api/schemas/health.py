#!/usr/bin/env python3
"""
app/api/schemas/health.py
=========================
DTOs para endpoints de diagnóstico y salud del sistema.
"""

from typing import List
from pydantic import BaseModel, Field


class HealthCheckResponse(BaseModel):
    status: str = Field(..., description="Estado general ('HEALTHY' o 'DEGRADED')")
    db_connected: bool = Field(..., description="Conectividad con PostgreSQL")
    pgvector_enabled: bool = Field(..., description="Disponibilidad de la extensión vector")
    target_table_exists: bool = Field(..., description="Existencia de la tabla principal")
    total_products_indexed: int = Field(..., description="Cantidad total de productos en base de datos")
    active_providers: List[str] = Field(default_factory=list, description="Proveedores activos en la tabla")
    timestamp: str = Field(..., description="Timestamp ISO de la verificación")
