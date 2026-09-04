#!/usr/bin/env python3
"""
app/api/v1/endpoints/jobs.py
============================
Controlador de consulta de estado para tareas asíncronas de ingesta.
"""

from fastapi import APIRouter, HTTPException, status
from app.api.schemas.job import JobStatusResponse
from app.services.job_manager import job_manager

router = APIRouter()


@router.get("/jobs/{job_id}", response_model=JobStatusResponse, summary="Consultar estado de tarea asíncrona")
def get_job_status(job_id: str) -> JobStatusResponse:
    """Consulta el estado, avance o resultado de una tarea de ingesta batch."""
    job_data = job_manager.get_job(job_id)
    if not job_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No se encontró el trabajo de ingesta con ID '{job_id}'"
        )
    return JobStatusResponse(**job_data)
