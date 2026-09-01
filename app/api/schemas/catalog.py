#!/usr/bin/env python3
"""
app/api/schemas/catalog.py
==========================
DTOs para endpoints de inventario e ingesta de catálogos.
"""

from typing import List, Optional
from pydantic import BaseModel, Field


class CatalogItem(BaseModel):
    codigo_proveedor: str
    nombre_proveedor: str
    total_productos: int


class CatalogListResponse(BaseModel):
    total_proveedores: int
    total_productos: int
    table_name: str
    catalogs: List[CatalogItem]


class IngestPathRequest(BaseModel):
    pdf_path: str = Field(..., description="Ruta local o del servidor al archivo PDF", example="data/raw_pdfs/FN Catalogo.pdf")
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
