#!/usr/bin/env python3
"""
app/services/job_manager.py
===========================
Gestor de tareas asíncronas en background (Job Manager) para ingestas batch.
"""

import uuid
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

logger = logging.getLogger("JobManager")


class IngestionJobManager:
    """Administra el ciclo de vida y estado de tareas de ingesta batch en memoria."""

    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def create_job(self, source_document: str, table_name: str, codigo_proveedor: str) -> str:
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(timezone.utc).isoformat()
        self._jobs[job_id] = {
            "job_id": job_id,
            "status": "PENDING",
            "created_at": now_iso,
            "completed_at": None,
            "source_document": source_document,
            "table_name": table_name,
            "codigo_proveedor": codigo_proveedor,
            "progress_message": "Trabajo encolado",
            "result": None,
            "error": None
        }
        return job_id

    def update_job(
        self,
        job_id: str,
        status: str,
        progress_message: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None
    ) -> None:
        if job_id in self._jobs:
            self._jobs[job_id]["status"] = status
            if progress_message:
                self._jobs[job_id]["progress_message"] = progress_message
            if result:
                self._jobs[job_id]["result"] = result
            if error:
                self._jobs[job_id]["error"] = error
            if status in ["COMPLETED", "FAILED"]:
                self._jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self._jobs.get(job_id)


job_manager = IngestionJobManager()
