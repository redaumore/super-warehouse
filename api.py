#!/usr/bin/env python3
"""
api.py
======
API REST de Producción para el Pipeline RAG Multi-Proveedor de Catálogos Industriales.

Exposiciones:
1. Ingesta Batch Asíncrona (Job Pattern) y Síncrona de Catálogos en PDF.
2. Consulta y Búsqueda Semántica Híbrida (BM25 + pgvector HNSW + Reranker + Generator con citaciones).
3. Inventario y Métricas de Catálogos por Proveedor.
4. Auditoría y Evaluación de Calidad de la Tríada RAG.
5. Verificación de Salud de Infraestructura y Base de Datos.
"""

import os
import sys
import uuid
import time
import shutil
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Union
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg import sql
from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    UploadFile,
    HTTPException,
    Query,
    status
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Cargar variables de entorno
load_dotenv()

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("RAG_REST_API")

# Importar Orquestador Maestro y Helpers de Base de Datos
from rag_orchestrator import (
    RAGOrchestrator,
    RAGResponse,
    IngestionResult
)
from fase_3_pgvector import build_db_url


# ============================================================================
# 1. MODELOS DE DATOS (PYDANTIC SCHEMAS)
# ============================================================================

class HealthCheckResponse(BaseModel):
    status: str = Field(..., description="Estado general ('HEALTHY' o 'DEGRADED')")
    db_connected: bool = Field(..., description="Conectividad con PostgreSQL")
    pgvector_enabled: bool = Field(..., description="Disponibilidad de la extensión vector")
    target_table_exists: bool = Field(..., description="Existencia de la tabla principal")
    total_products_indexed: int = Field(..., description="Cantidad total de productos en base de datos")
    active_providers: List[str] = Field(default_factory=list, description="Proveedores activos en la tabla")
    timestamp: str = Field(..., description="Timestamp ISO de la verificación")


class CatalogItem(BaseModel):
    codigo_proveedor: str
    nombre_proveedor: str
    total_productos: int


class CatalogListResponse(BaseModel):
    total_proveedores: int
    total_productos: int
    table_name: str
    catalogs: List[CatalogItem]


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


class IngestPathRequest(BaseModel):
    pdf_path: str = Field(..., description="Ruta local o del servidor al archivo PDF", example="data/FN Catalogo.pdf")
    codigo_proveedor: str = Field(default="FDN", max_length=3, description="Código de 3 caracteres del proveedor", example="FDN")
    nombre_proveedor: str = Field(default="Ferretera del Norte", description="Nombre legible del proveedor", example="Ferretera del Norte")
    proveedor_id: Optional[str] = Field(default=None, description="Slug único del proveedor (opcional)")
    marca: Optional[str] = Field(default=None, description="Marca forzada para el catálogo (opcional)")
    start_page: int = Field(default=1, ge=1, description="Página de inicio (1-indexed)")
    max_pages: Optional[int] = Field(default=None, ge=1, description="Máximo de páginas a procesar")
    skip_pages: Optional[str] = Field(default=None, description="Páginas a omitir separadas por coma o rangos (ej: '1-2,4')")
    no_vision: bool = Field(default=False, description="Desactivar visión multimodal (usar solo texto)")
    recreate_table: bool = Field(default=False, description="Si es True, recrea la tabla eliminando datos anteriores")
    table_name: str = Field(default="catalogo_productos_rag", description="Tabla destino en PostgreSQL")
    sync: bool = Field(default=False, description="Si es True, espera sincrónicamente la finalización")


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


class EvaluateRequest(BaseModel):
    table_name: str = Field(default="catalogo_productos_rag", description="Tabla a evaluar")
    output_dir: str = Field(default="./scratch/fase-7-evaluacion", description="Directorio de exportación de reportes")


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


# ============================================================================
# 2. GESTOR DE TRABAJOS ASÍNCRONOS EN MEMORIA (JOB MANAGER)
# ============================================================================

class IngestionJobManager:
    """Administra el ciclo de vida y estado de tareas de ingesta batch en segundo plano."""

    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._executor = ThreadPoolExecutor(max_workers=3)

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


def _run_ingestion_task(job_id: str, params: Dict[str, Any]) -> None:
    """Función de background worker para ejecutar la ingesta."""
    logger.info(f"[Job {job_id}] Iniciando ejecución en background de: {params['pdf_path']}")
    job_manager.update_job(job_id, status="RUNNING", progress_message="Procesando Fases 0 a 3...")
    
    try:
        orchestrator = RAGOrchestrator(table_name=params.get("table_name", "catalogo_productos_rag"))
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
            output_dir=params.get("output_dir", "./data")
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


# ============================================================================
# 3. CREACIÓN DE LA APLICACIÓN FASTAPI
# ============================================================================

app = FastAPI(
    title="RAG Multi-Proveedor Catalog API",
    version="1.0.0",
    description="""
API REST de Producción para el Pipeline RAG Multi-Proveedor de Catálogos Industriales.

Permite:
- **Ingesta Batch (Fases 0-3):** Carga y procesamiento de catálogos en PDF con extracción multimodal (GPT-5.6 Luna), chunking semántico, embeddings Matryoshka (256d) e indexación HNSW en PostgreSQL / pgvector.
- **Consulta en Tiempo Real (Fases 4-6):** Búsqueda híbrida (BM25 + vector), reranking semántico (Cross-Encoder) y generación determinista con citaciones verificadas.
- **Evaluación Continua (Fase 7):** Benchmark de calidad con métricas de la Tríada RAG (Context Relevance, Faithfulness, Answer Relevance).
- **Inventario Multi-Proveedor:** Convivencia e inspección de catálogos concurrentes sin colisiones de identificadores.
    """,
    docs_url="/docs",
    redoc_url="/redoc"
)

# Configuración de CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_v1 = APIRouter(prefix="/api/v1")


# ============================================================================
# 4. ENDPOINTS: HEALTH & INVENTARIO
# ============================================================================

@app.get("/health", tags=["Health"], response_model=HealthCheckResponse)
@api_v1.get("/health", tags=["Health"], response_model=HealthCheckResponse)
def get_health(table_name: str = "catalogo_productos_rag") -> HealthCheckResponse:
    """Verifica el estado de salud de la base de datos PostgreSQL, extensión vector y tablas."""
    now_iso = datetime.now(timezone.utc).isoformat()
    db_connected = False
    vector_enabled = False
    table_exists = False
    total_products = 0
    active_providers: List[str] = []

    try:
        db_url = build_db_url()
        with psycopg.connect(db_url, autocommit=True) as conn:
            db_connected = True
            with conn.cursor() as cur:
                # 1. Verificar extensión vector
                cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector';")
                vector_enabled = bool(cur.fetchone())

                # 2. Verificar existencia de la tabla
                cur.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_name = %s;",
                    (table_name,)
                )
                table_exists = bool(cur.fetchone())

                # 3. Métricas si la tabla existe
                if table_exists:
                    cur.execute(sql.SQL("SELECT COUNT(*) FROM {};").format(sql.Identifier(table_name)))
                    cnt = cur.fetchone()
                    total_products = cnt[0] if cnt else 0

                    cur.execute(
                        sql.SQL("SELECT DISTINCT codigo_proveedor FROM {} ORDER BY codigo_proveedor;").format(
                            sql.Identifier(table_name)
                        )
                    )
                    active_providers = [row[0] for row in cur.fetchall() if row[0]]

        overall_status = "HEALTHY" if (db_connected and vector_enabled and table_exists) else "DEGRADED"

        return HealthCheckResponse(
            status=overall_status,
            db_connected=db_connected,
            pgvector_enabled=vector_enabled,
            target_table_exists=table_exists,
            total_products_indexed=total_products,
            active_providers=active_providers,
            timestamp=now_iso
        )

    except Exception as e:
        logger.error(f"Error en health check: {e}")
        return HealthCheckResponse(
            status="DEGRADED",
            db_connected=False,
            pgvector_enabled=False,
            target_table_exists=False,
            total_products_indexed=0,
            active_providers=[],
            timestamp=now_iso
        )


@api_v1.get("/catalogs", tags=["Catalogs"], response_model=CatalogListResponse)
def list_catalogs(table_name: str = "catalogo_productos_rag") -> CatalogListResponse:
    """Retorna la lista consolidada de catálogos y cantidad de productos indexados por proveedor."""
    try:
        db_url = build_db_url()
        catalogs: List[CatalogItem] = []
        total_global = 0

        with psycopg.connect(db_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                # Verificar si la tabla existe
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


# ============================================================================
# 5. ENDPOINTS: INGESTA BATCH DE CATÁLOGOS
# ============================================================================

@api_v1.post("/catalogs/ingest-path", tags=["Ingestion"], response_model=JobStatusResponse)
def ingest_catalog_by_path(
    req: IngestPathRequest,
    background_tasks: BackgroundTasks
) -> JobStatusResponse:
    """
    Inicia la ingesta de un archivo PDF ubicado en el servidor o volumen local.
    Si sync=True, ejecuta sincrónicamente y retorna el resultado final.
    De lo contrario, retorna HTTP 202 con job_id para seguimiento asíncrono.
    """
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

    if req.sync:
        # Ejecución síncrona
        _run_ingestion_task(job_id, params)
        job_data = job_manager.get_job(job_id)
        return JobStatusResponse(**job_data) # type: ignore

    # Ejecución asíncrona
    background_tasks.add_task(_run_ingestion_task, job_id, params)
    job_data = job_manager.get_job(job_id)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=job_data
    ) # type: ignore


@api_v1.post("/catalogs/ingest-file", tags=["Ingestion"], response_model=JobStatusResponse)
async def ingest_catalog_by_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Archivo PDF del catálogo a procesar"),
    codigo_proveedor: str = Form("FDN", description="Código de 3 caracteres del proveedor"),
    nombre_proveedor: str = Form("Ferretera del Norte", description="Nombre del proveedor"),
    proveedor_id: Optional[str] = Form(None, description="Slug del proveedor (opcional)"),
    marca: Optional[str] = Form(None, description="Marca forzada para los productos (opcional)"),
    start_page: int = Form(1, description="Página de inicio (1-indexed)"),
    max_pages: Optional[int] = Form(None, description="Máximo de páginas a procesar"),
    skip_pages: Optional[str] = Form(None, description="Páginas a omitir (ej: '1-2,4')"),
    no_vision: bool = Form(False, description="Desactivar visión multimodal"),
    recreate_table: bool = Form(False, description="Recrear tabla eliminando datos anteriores"),
    table_name: str = Form("catalogo_productos_rag", description="Tabla destino en PostgreSQL"),
    sync: bool = Form(False, description="Ejecutar de forma síncrona bloqueante")
) -> JobStatusResponse:
    """
    Recibe un archivo PDF vía multipart/form-data, lo guarda en el directorio de subidas y dispara la ingesta.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Solo se admiten archivos en formato PDF (.pdf)."
        )

    upload_dir = "./data/uploads"
    os.makedirs(upload_dir, exist_ok=True)
    clean_filename = f"{codigo_proveedor[:3]}_{int(time.time())}_{os.path.basename(file.filename)}"
    dest_path = os.path.join(upload_dir, clean_filename)

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
        "output_dir": "./data"
    }

    if sync:
        _run_ingestion_task(job_id, params)
        job_data = job_manager.get_job(job_id)
        return JobStatusResponse(**job_data) # type: ignore

    background_tasks.add_task(_run_ingestion_task, job_id, params)
    job_data = job_manager.get_job(job_id)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=job_data
    ) # type: ignore


@api_v1.get("/jobs/{job_id}", tags=["Ingestion"], response_model=JobStatusResponse)
def get_job_status(job_id: str) -> JobStatusResponse:
    """Consulta el estado, avance o resultado de una tarea de ingesta batch."""
    job_data = job_manager.get_job(job_id)
    if not job_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No se encontró el trabajo de ingesta con ID '{job_id}'"
        )
    return JobStatusResponse(**job_data)


# ============================================================================
# 6. ENDPOINT: CONSULTA RAG EN TIEMPO REAL
# ============================================================================

@api_v1.post("/query", tags=["RAG"], response_model=QueryResponse)
def query_rag(req: QueryRequest) -> QueryResponse:
    """
    Ejecuta el flujo online síncrono completo:
    Recuperación Híbrida (BM25 + Vector) -> Reranker Cross-Encoder -> Generación LLM con citaciones y verificación Grounded.
    """
    try:
        orchestrator = RAGOrchestrator(
            table_name=req.table_name,
            llm_model=req.model,
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


# ============================================================================
# 7. ENDPOINT: EVALUACIÓN CONTINUA (FASE 7)
# ============================================================================

@api_v1.post("/evaluate", tags=["Evaluation"], response_model=EvaluateResponse)
def evaluate_pipeline(req: EvaluateRequest) -> EvaluateResponse:
    """Ejecuta la suite de evaluación sobre el Golden Dataset y retorna las métricas de la Tríada RAG."""
    try:
        orchestrator = RAGOrchestrator(table_name=req.table_name)
        report = orchestrator.evaluate_pipeline(output_dir=req.output_dir)

        md_path = os.path.join(req.output_dir, "reporte-evaluacion-triada-rag.md")
        json_path = os.path.join(req.output_dir, "reporte-evaluacion-triada-rag.json")

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


# Montar router principal
app.include_router(api_v1)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    logger.info(f"Iniciando servidor REST en http://0.0.0.0:{port}...")
    uvicorn.run("api:app", host="0.0.0.0", port=port, reload=True)
