#!/usr/bin/env python3
"""
app/main.py
===========
Punto de entrada principal y fábrica de la aplicación FastAPI.
"""

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.api.v1.router import api_v1_router
from app.api.v1.endpoints.health import router as health_router

logger = logging.getLogger("App")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestión de ciclo de vida (startup y shutdown events)."""
    logger.info("=" * 80)
    logger.info(f"Iniciando {settings.APP_NAME} v{settings.APP_VERSION}")
    logger.info(f"Base Directory: {settings.BASE_PATH}")
    logger.info(f"Artifacts Directory: {settings.ARTIFACTS_DIR}")
    logger.info(f"Database Target: {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}")
    logger.info("=" * 80)
    yield
    logger.info("Deteniendo servidor FastAPI...")


def create_application() -> FastAPI:
    """Fábrica de la aplicación FastAPI con middlewares y rutas."""
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="""
API REST de Producción para el Pipeline RAG Multi-Proveedor de Catálogos Industriales.

### Capacidades Principales:
* **Ingesta Batch (Fases 0 a 3):** Procesamiento integral de catálogos en PDF con extracción multimodal (GPT-5.6 Luna), chunking semántico granular, embeddings Matryoshka (256d) e indexación HNSW en PostgreSQL / pgvector.
* **Búsqueda y Generación Online (Fases 4 a 6):** Búsqueda híbrida (BM25 + Densa) con RRF, Reranker Cross-Encoder y generación LLM determinista con citas verificadas.
* **Auditoría Continua (Fase 7):** Benchmark de calidad con métricas de la Tríada RAG (Context Relevance, Faithfulness, Answer Relevance).
* **Multi-Tenant / Multi-Proveedor:** Convivencia e inspección de múltiples catálogos concurrentes sin colisiones de identificadores.
        """,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan
    )

    # Configuración de CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Rutas raíz de salud y routers versionados
    app.include_router(health_router, tags=["Health"])
    app.include_router(api_v1_router)

    return app


app = create_application()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
