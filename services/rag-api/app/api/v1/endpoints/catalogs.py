#!/usr/bin/env python3
"""
app/api/v1/endpoints/catalogs.py
================================
Controlador de inventario y endpoints de ingesta batch de catálogos PDF.
"""

import os
import time
import shutil
import logging
from typing import List, Optional, Dict, Any

import psycopg
from psycopg import sql
from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    UploadFile,
    HTTPException,
    status
)
from fastapi.responses import JSONResponse

from app.config import settings
from app.api.schemas.catalog import (
    CatalogItem,
    CatalogListResponse,
    IngestPathRequest
)
from app.api.schemas.job import JobStatusResponse
from app.services.job_manager import job_manager
from app.core.orchestrator import RAGOrchestrator

router = APIRouter()
logger = logging.getLogger("CatalogsEndpoint")


def _run_background_ingestion(job_id: str, params: Dict[str, Any]) -> None:
    """Worker para procesar la ingesta en segundo plano."""
    logger.info(f"[Job {job_id}] Iniciando tarea en background: {params['pdf_path']}")
    job_manager.update_job(job_id, status="RUNNING", progress_message="Procesando Fases 0 a 3...")

    try:
        orchestrator = RAGOrchestrator(table_name=params.get("table_name", settings.DEFAULT_TABLE_NAME))
        res = orchestrator.ingest_catalog_pdf(
            pdf_path=params["pdf_path"],
            codigo_proveedor=params.get("codigo_proveedor", "FDN"),
            nombre_proveedor=params.get("nombre_proveedor", "Ferretera del Norte"),
            proveedor_id=params.get("proveedor_id"),
            marca=params.get("marca"),
            start_page=params.get("start_page", 1),
            max_pages=params.get("max_pages"),
            skip_pages=params.get("skip_pages"),
            use_vision=not params.get("no_vision", False),
            recreate_table=params.get("recreate_table", False),
            output_dir=params.get("output_dir", str(settings.ARTIFACTS_DIR))
        )

        if res.status == "SUCCESS":
            job_manager.update_job(
                job_id,
                status="COMPLETED",
                progress_message="Ingesta finalizada con éxito",
                result=res.to_dict()
            )
            logger.info(f"[Job {job_id}] Ingesta completada con éxito.")
        else:
            job_manager.update_job(
                job_id,
                status="FAILED",
                progress_message="Fallo durante la ingesta",
                error=res.error
            )
            logger.error(f"[Job {job_id}] Ingesta falló: {res.error}")

    except Exception as e:
        logger.error(f"[Job {job_id}] Excepción no controlada: {e}", exc_info=True)
        job_manager.update_job(
            job_id,
            status="FAILED",
            progress_message="Error no controlado",
            error=str(e)
        )


@router.get("/catalogs", response_model=CatalogListResponse, summary="Listar inventario de catálogos indexados")
def list_catalogs(table_name: str = settings.DEFAULT_TABLE_NAME) -> CatalogListResponse:
    """Retorna la lista consolidada de proveedores y cantidad de productos indexados."""
    try:
        db_url = settings.get_db_url()
        catalogs: List[CatalogItem] = []
        total_global = 0

        with psycopg.connect(db_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s;", (table_name,))
                if not cur.fetchone():
                    return CatalogListResponse(
                        total_proveedores=0,
                        total_productos=0,
                        table_name=table_name,
                        catalogs=[]
                    )

                cur.execute(
                    sql.SQL("""
                        SELECT 
                            codigo_proveedor, 
                            COALESCE(nombre_proveedor, 'Desconocido') as nom_prov, 
                            COUNT(*) as cant
                        FROM {}
                        GROUP BY codigo_proveedor, nombre_proveedor
                        ORDER BY cant DESC;
                    """).format(sql.Identifier(table_name))
                )
                rows = cur.fetchall()
                for cod, nom, cant in rows:
                    catalogs.append(CatalogItem(
                        codigo_proveedor=cod or "GEN",
                        nombre_proveedor=nom,
                        total_productos=cant
                    ))
                    total_global += cant

        return CatalogListResponse(
            total_proveedores=len(catalogs),
            total_productos=total_global,
            table_name=table_name,
            catalogs=catalogs
        )

    except Exception as e:
        logger.error(f"Error listando catálogos: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al consultar el inventario de catálogos: {str(e)}"
        )


@router.post("/catalogs/ingest-path", response_model=JobStatusResponse, summary="Ingestar catálogo desde ruta del servidor")
def ingest_catalog_by_path(
    req: IngestPathRequest,
    background_tasks: BackgroundTasks
) -> Any:
    """Inicia la ingesta de un PDF ubicado en el servidor o volumen montado."""
    if not os.path.exists(req.pdf_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"El archivo PDF especificado no existe: '{req.pdf_path}'"
        )

    job_id = job_manager.create_job(
        source_document=req.pdf_path,
        table_name=req.table_name,
        codigo_proveedor=req.codigo_proveedor
    )

    params = req.model_dump()
    params["output_dir"] = str(settings.ARTIFACTS_DIR)

    if req.sync:
        _run_background_ingestion(job_id, params)
        job_data = job_manager.get_job(job_id)
        return JobStatusResponse(**job_data) # type: ignore

    background_tasks.add_task(_run_background_ingestion, job_id, params)
    job_data = job_manager.get_job(job_id)
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=job_data)


@router.post("/catalogs/ingest-file", response_model=JobStatusResponse, summary="Subir archivo PDF e ingestar")
async def ingest_catalog_by_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Archivo PDF a procesar"),
    codigo_proveedor: str = Form("FDN", description="Código de 3 caracteres del proveedor"),
    nombre_proveedor: str = Form("Ferretera del Norte", description="Nombre del proveedor"),
    proveedor_id: Optional[str] = Form(None, description="Slug del proveedor (opcional)"),
    marca: Optional[str] = Form(None, description="Marca forzada para los productos (opcional)"),
    start_page: int = Form(1, description="Página de inicio (1-indexed)"),
    max_pages: Optional[int] = Form(None, description="Máximo de páginas a procesar"),
    skip_pages: Optional[str] = Form(None, description="Páginas a omitir (ej: '1-2,4')"),
    no_vision: bool = Form(False, description="Desactivar visión multimodal"),
    recreate_table: bool = Form(False, description="Recrear tabla eliminando datos anteriores"),
    table_name: str = Form(settings.DEFAULT_TABLE_NAME, description="Tabla destino en PostgreSQL"),
    sync: bool = Form(False, description="Ejecutar de forma síncrona bloqueante")
) -> Any:
    """Recibe un archivo PDF multipart/form-data y ejecuta la ingesta."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Solo se admiten archivos en formato PDF (.pdf)."
        )

    upload_dir = settings.UPLOADS_DIR
    clean_filename = f"{codigo_proveedor[:3]}_{int(time.time())}_{os.path.basename(file.filename)}"
    dest_path = str(upload_dir / clean_filename)

    with open(dest_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    logger.info(f"Archivo subido guardado en: {dest_path}")

    job_id = job_manager.create_job(
        source_document=dest_path,
        table_name=table_name,
        codigo_proveedor=codigo_proveedor
    )

    params = {
        "pdf_path": dest_path,
        "codigo_proveedor": codigo_proveedor,
        "nombre_proveedor": nombre_proveedor,
        "proveedor_id": proveedor_id,
        "marca": marca,
        "start_page": start_page,
        "max_pages": max_pages,
        "skip_pages": skip_pages,
        "no_vision": no_vision,
        "recreate_table": recreate_table,
        "table_name": table_name,
        "output_dir": str(settings.ARTIFACTS_DIR)
    }

    if sync:
        _run_background_ingestion(job_id, params)
        job_data = job_manager.get_job(job_id)
        return JobStatusResponse(**job_data) # type: ignore

    background_tasks.add_task(_run_background_ingestion, job_id, params)
    job_data = job_manager.get_job(job_id)
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=job_data)
