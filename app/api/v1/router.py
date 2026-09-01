#!/usr/bin/env python3
"""
app/api/v1/router.py
====================
Agregador de routers y endpoints para la versión 1 de la API REST.
"""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    health,
    catalogs,
    query,
    jobs,
    evaluate
)

api_v1_router = APIRouter(prefix="/api/v1")

api_v1_router.include_router(health.router, tags=["Health"])
api_v1_router.include_router(catalogs.router, tags=["Catalogs & Ingestion"])
api_v1_router.include_router(query.router, tags=["RAG Query"])
api_v1_router.include_router(jobs.router, tags=["Async Jobs"])
api_v1_router.include_router(evaluate.router, tags=["Evaluation"])
