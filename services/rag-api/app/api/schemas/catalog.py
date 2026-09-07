#!/usr/bin/env python3
"""
app/api/schemas/catalog.py
==========================
DTOs para endpoints de inventario e ingesta de catálogos.
"""

from typing import List, Literal, Optional
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


class ProviderDocumentSummary(BaseModel):
    documento_id: str = Field(
        ...,
        description="Identidad lógica del documento declarada por el operador (ej: 'LISTA GENERAL')",
        example="LISTA GENERAL"
    )
    total_productos: int = Field(..., ge=0, description="Cantidad de productos indexados de este documento")
    ultimo_archivo: Optional[str] = Field(
        default=None,
        description="Último archivo de origen indexado para este documento (si hay)"
    )
    actualizado_en: Optional[str] = Field(
        default=None,
        description="Fecha/hora de la última actualización del documento (ISO 8601)"
    )


class ProviderDocumentsResponse(BaseModel):
    codigo_proveedor: str = Field(..., description="Código del proveedor consultado", example="MSA")
    documentos: List[ProviderDocumentSummary] = Field(
        default_factory=list,
        description="Documentos indexados del proveedor (lista vacía si no hay ninguno o el proveedor no existe)"
    )


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
    documento_id: Optional[str] = Field(default=None, description="Identidad lógica del documento/lista declarada por el operador (ej: 'LISTA GENERAL'); si se omite, el orquestador aplica el default 'LISTA GENERAL'", example="LISTA GENERAL")
    delete_scope: Literal["proveedor", "documento"] = Field(
        default="proveedor",
        description="Alcance del borrado previo: 'proveedor' (reemplazo total del proveedor) o 'documento' (incremental, solo este documento)"
    )
    table_name: str = Field(default="catalogo_productos_rag", description="Tabla destino en PostgreSQL")
    sync: bool = Field(default=False, description="Si es True, espera sincrónicamente la finalización")
