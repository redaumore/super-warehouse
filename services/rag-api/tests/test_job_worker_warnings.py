"""Unit tests for the ingestion job worker success path.

When the chunker disambiguates duplicated supplier codes, the job must
surface the conflicts to the operator through ``progress_message`` instead
of hiding them in the service log: the backoffice prints that message on
the COMPLETED status.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from app.api.v1.endpoints import catalogs as catalogs_ep
from app.api.v1.endpoints.catalogs import _run_background_ingestion
from app.core.orchestrator import IngestionResult
from app.services.job_manager import job_manager


def _success_result(warnings: list[str]) -> IngestionResult:
    return IngestionResult(
        status="SUCCESS",
        source_document="SCO.pdf",
        target_table="catalogo_productos_rag",
        codigo_proveedor="SCO",
        nombre_proveedor="SCON",
        pages_processed=7,
        total_products_extracted=74,
        total_nodes_generated=74,
        total_embeddings_created=74,
        total_records_indexed=74,
        total_tokens_used=0,
        total_elapsed_seconds=1.0,
        warnings=warnings,
    )


@pytest.fixture()
def job_id() -> str:
    return job_manager.create_job(
        source_document="SCO.pdf",
        table_name="catalogo_productos_rag",
        codigo_proveedor="SCO",
    )


def _run(monkeypatch: pytest.MonkeyPatch, job_id: str, result: IngestionResult) -> None:
    def _fake_ingest(self: Any, **kwargs: Any) -> IngestionResult:
        return result

    monkeypatch.setattr(catalogs_ep.RAGOrchestrator, "ingest_catalog_pdf", _fake_ingest)
    _run_background_ingestion(job_id, {"pdf_path": "SCO.pdf"})


def test_completed_job_with_disambiguation_warning_surfaces_conflict(
    monkeypatch: pytest.MonkeyPatch, job_id: str
):
    """The COMPLETED progress message names the duplicated codigo and página."""

    _run(
        monkeypatch,
        job_id,
        _success_result(
            [
                "Código de producto duplicado en el PDF "
                "(codigo 'SM 409 -86' (página 72); codigo 'SM 409 -86' (página 72)). "
                "El node_id 'node_prod_SCO_SCO-SM 409 -86' se desambiguó con sufijo #N "
                "para no perder registros."
            ]
        ),
    )

    job: Dict[str, Any] = job_manager.get_job(job_id)  # type: ignore[assignment]
    assert job["status"] == "COMPLETED"
    assert "Aviso:" in job["progress_message"]
    assert "SM 409 -86" in job["progress_message"]
    assert "página 72" in job["progress_message"]


def test_completed_job_without_warnings_keeps_plain_message(
    monkeypatch: pytest.MonkeyPatch, job_id: str
):
    """A clean ingestion keeps the plain success message without an Aviso section."""

    _run(monkeypatch, job_id, _success_result([]))

    job: Dict[str, Any] = job_manager.get_job(job_id)  # type: ignore[assignment]
    assert job["status"] == "COMPLETED"
    assert job["progress_message"] == "Ingesta finalizada con éxito"
