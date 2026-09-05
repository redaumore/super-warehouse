"""Adopción de un producto RAG del catálogo de proveedores al catálogo local.

Use case puro de backoffice (patrón session-in / caller-commits, igual que
``confirm_items`` en ``ingestion.py``): el owner POSTea un producto RAG tipado
(DTO con provenance) más el stock inicial, y una sola transacción crea
``Catalogo`` + ``Inventory`` + ``StockAdjustment``. El embedding se calcula
ANTES de cualquier escritura: si falla, no hay nada que persistir y el caller
hace rollback. El RAG nunca se muta.

Los errores de dominio (proveedor desconocido/inactivo, provenance ausente,
colisión de SKU, stock no positivo, embedding fallido) se traducen a códigos
en la capa API (task 3.1).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.disambiguation import normalize_text
from src.db.models import Catalogo, Inventory, StockAdjustment, Supplier, SupplierStatus
from src.pricing.engine import compute_base
from src.supplier.guards import SupplierInactiveError

_CENT = Decimal("0.01")
_SKU_PREFIX = "RAG"
_SKU_MAX_LEN = 64
_ADOPTION_REASON = "product_adoption"
_EMBED_DIMS = 1536


class AdoptRequest(BaseModel):
    """Payload que el owner POSTea por cada producto RAG adoptado.

    ``sku`` es el ``codigo_orig`` del DTO RAG; ``node_id`` es obligatorio y
    falla cerrado cuando no se puede resolver (provenance write-once).
    """

    sku: str
    nombre: str
    codigo_proveedor: str
    marca: str | None = None
    categoria: str | None = None
    subcategoria: str | None = None
    precio: float | None = None
    moneda: str | None = None
    archivo_origen: str | None = None
    pagina: int | None = None
    node_id: str  # fail closed cuando está vacío
    stock: int  # debe ser > 0


@dataclass(frozen=True)
class OwnerContext:
    """Identidad del owner autenticado; se vuelve el actor del audit (sin FK)."""

    owner_id: str


class Embedder(Protocol):
    """Cualquier cliente de embeddings que mapee textos a vectores de dim fija."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class AdoptionError(Exception):
    """Base de los errores de adopción (mapeados al error body del endpoint)."""


class SupplierUnknownError(AdoptionError):
    """Ningún proveedor coincide con el ``codigo_proveedor`` pedido."""


class MissingProvenanceError(AdoptionError):
    """El producto RAG no trae ``node_id`` resuelto — fail closed."""


class SkuCollisionError(AdoptionError):
    """El SKU determinístico ya existe en ``catalogo``."""


class InvalidStockError(AdoptionError):
    """El stock provisto por el owner debe ser positivo."""


class EmbeddingUnavailableError(AdoptionError):
    """El cliente de embeddings falló; la transacción se revierte."""


def _normalize_sku_part(code: str) -> str:
    """Normaliza un código para la plantilla de SKU: mayúsculas, sin espacios."""
    return re.sub(r"\s+", "-", code.strip().upper()).strip("-")


def build_sku(supplier_code: str, codigo_orig: str) -> str:
    """SKU determinístico ``RAG-{code}-{codigo_orig normalizado}``, máx. 64 chars.

    La plantilla es idempotente: el mismo ``codigo_orig`` siempre produce el
    mismo SKU, y la colisión se rechaza en 409 en el use case.
    """
    return f"{_SKU_PREFIX}-{supplier_code}-{_normalize_sku_part(codigo_orig)}"[:_SKU_MAX_LEN]


def _compose_embedding_text(dto: AdoptRequest) -> str:
    """Texto de embedding: ``nombre + marca + categoria + subcategoria`` normalizados."""
    parts = [
        normalize_text(part)
        for part in (dto.nombre, dto.marca, dto.categoria, dto.subcategoria)
        if part
    ]
    return " ".join(parts)


def _coerce_precio(raw: float | None) -> Decimal:
    """Redondea el precio float del RAG a centavos (ROUND_HALF_UP); None → 0.00."""
    if raw is None:
        return Decimal("0.00")
    return Decimal(str(raw)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _resolve_supplier(session: Session, code: str) -> Supplier:
    """Resuelve un proveedor ACTIVO por código; errores explícitos si no."""
    supplier = session.scalar(select(Supplier).where(Supplier.code == _normalize_sku_part(code)))
    if supplier is None:
        raise SupplierUnknownError(f"unknown supplier: {code}")
    if supplier.status is not SupplierStatus.ACTIVO:
        raise SupplierInactiveError(f"supplier {code} is INACTIVO")
    return supplier


def adopt_product(
    session: Session,
    dto: AdoptRequest,
    owner_ctx: OwnerContext,
    embedder: Embedder,
) -> Catalogo:
    """Persiste una adopción en una sola transacción; el caller hace commit.

    Orden del data flow: stock → proveedor ACTIVO → SKU + colisión → node_id
    fail-closed → precio/moneda → embedding (antes de cualquier escritura) →
    filas Catalogo + Inventory + StockAdjustment → flush. El embedding falla
    cerrado: el caller revierte y no persiste nada.
    """
    if dto.stock <= 0:
        raise InvalidStockError(f"stock must be positive, got {dto.stock}")
    supplier = _resolve_supplier(session, dto.codigo_proveedor)
    sku = build_sku(supplier.code, dto.sku)
    if session.scalar(select(Catalogo).where(Catalogo.codigo_interno == sku)) is not None:
        raise SkuCollisionError(f"codigo_interno already exists: {sku}")
    if not dto.node_id:
        raise MissingProvenanceError("node_id is required (fail closed)")
    precio = _coerce_precio(dto.precio)
    moneda = dto.moneda.strip().upper() if dto.moneda and dto.moneda.strip() else None
    text = _compose_embedding_text(dto)
    try:
        vectors = embedder.embed([text])
        embedding = vectors[0]
    except Exception as exc:  # cualquier falla del embedder cierra
        raise EmbeddingUnavailableError(f"embedding failed: {exc}") from exc
    if len(embedding) != _EMBED_DIMS:
        raise EmbeddingUnavailableError(f"embedding has {len(embedding)} dims, expected {_EMBED_DIMS}")
    margen = supplier.default_margin_pct
    origen: dict[str, Any] = {
        "rag": {
            "node_id": dto.node_id,
            "archivo_origen": dto.archivo_origen,
            "pagina_origen": dto.pagina,
        }
    }
    product = Catalogo(
        codigo_interno=sku,
        supplier_id=supplier.id,
        nombre_oficial=dto.nombre,
        costo_proveedor=precio,
        margen_aplicado_pct=margen,
        precio_lista_base=compute_base(precio, margen),
        stock_disponible=dto.stock,
        sinonimos=[dto.nombre],
        marca=dto.marca or None,
        categoria=dto.categoria or None,
        subcategoria=dto.subcategoria or None,
        moneda=moneda,
        origen=origen,
        embedding=embedding,
    )
    session.add(product)
    session.add(Inventory(sku_id=sku, quantity_on_hand=dto.stock))
    session.add(
        StockAdjustment(
            sku=sku,
            delta=dto.stock,
            reason=_ADOPTION_REASON,
            actor=f"owner:{owner_ctx.owner_id}",
        )
    )
    session.flush()
    return product