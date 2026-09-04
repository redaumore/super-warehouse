#!/usr/bin/env python3
"""
app/api/v1/endpoints/health.py
==============================
Controlador de diagnóstico y salud de la API y PostgreSQL.
"""

from datetime import datetime, timezone
from typing import List
import psycopg
from psycopg import sql
from fastapi import APIRouter

from app.config import settings
from app.api.schemas.health import HealthCheckResponse

router = APIRouter()


@router.get("/health", response_model=HealthCheckResponse, summary="Verificar salud de la infraestructura")
def get_health(table_name: str = settings.DEFAULT_TABLE_NAME) -> HealthCheckResponse:
    """Verifica el estado de salud de la base de datos PostgreSQL, extensión vector y tablas."""
    now_iso = datetime.now(timezone.utc).isoformat()
    db_connected = False
    vector_enabled = False
    table_exists = False
    total_products = 0
    active_providers: List[str] = []

    try:
        db_url = settings.get_db_url()
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

    except Exception:
        return HealthCheckResponse(
            status="DEGRADED",
            db_connected=False,
            pgvector_enabled=False,
            target_table_exists=False,
            total_products_indexed=0,
            active_providers=[],
            timestamp=now_iso
        )
