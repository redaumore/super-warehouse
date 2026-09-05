"""Pruebas unitarias del use case de adopción de productos RAG (tasks 2.1–2.5).

Cubren el contrato de ``adopt_product`` contra la base real (docker pgvector):
plantilla de SKU determinística y su colisión, normalización de precio/moneda,
composición del texto de embedding, rollback total cuando el embedding falla,
stock no positivo, provenance write-once (y fail-closed si falta) y resolución
de proveedor ACTIVO con errores explícitos.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from src.backoffice.adoption import (
    AdoptRequest,
    EmbeddingUnavailableError,
    InvalidStockError,
    MissingProvenanceError,
    OwnerContext,
    SkuCollisionError,
    SupplierUnknownError,
    adopt_product,
    build_sku,
)
from src.config import get_settings
from src.db.models import Catalogo, Inventory, StockAdjustment, Supplier, SupplierStatus
from src.supplier.guards import SupplierInactiveError

EMBED_VECTOR = [0.1] * 1536


def _postgres_up() -> bool:
    try:
        engine = create_engine(
            get_settings().sqlalchemy_database_url, connect_args={"connect_timeout": 2}
        )
        with engine.connect():
            pass
        engine.dispose()
        return True
    except (OperationalError, OSError):
        return False


pytestmark = pytest.mark.skipif(not _postgres_up(), reason="Postgres not running (make db-up)")


@pytest.fixture(autouse=True)
def _clean_schema(clean_schema):
    yield


class FakeEmbedder:
    """Registra los textos embeddidos y devuelve un vector fijo de 1536 dims."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.texts: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        if self.error is not None:
            raise self.error
        return [EMBED_VECTOR for _ in texts]


def _seed_supplier(
    session: Session, code: str = "AMX", status: SupplierStatus = SupplierStatus.ACTIVO
) -> Supplier:
    supplier = Supplier(
        business_name=f"Proveedor {code}",
        code=code,
        default_margin_pct=Decimal(0),
        status=status,
    )
    session.add(supplier)
    session.flush()
    return supplier


def _dto(**overrides: Any) -> AdoptRequest:
    base: dict[str, Any] = {
        "sku": "at-5044",
        "nombre": "Tornillo 5/16 x 2 pulgadas",
        "codigo_proveedor": "AMX",
        "marca": "Acme",
        "categoria": "Fijaciones",
        "subcategoria": "Tornillos",
        "precio": 125.0,
        "moneda": "usd",
        "archivo_origen": "catalogo_amx_2026.pdf",
        "pagina": 12,
        "node_id": "node-abc-123",
        "stock": 50,
    }
    base.update(overrides)
    return AdoptRequest(**base)


OWNER = OwnerContext(owner_id="owner-1")


def test_adopcion_crea_catalogo_inventory_y_stock_adjustment(db_session):
    """Una adopción feliz crea Catalogo + Inventory + StockAdjustment en una transacción."""
    _seed_supplier(db_session)
    product = adopt_product(db_session, _dto(), OWNER, FakeEmbedder())

    assert product.codigo_interno == "RAG-AMX-AT-5044"
    assert product.stock_disponible == 50
    inventory = db_session.scalar(select(Inventory).where(Inventory.sku_id == product.codigo_interno))
    assert inventory is not None
    assert inventory.quantity_on_hand == 50
    adjustment = db_session.scalar(
        select(StockAdjustment).where(StockAdjustment.sku == product.codigo_interno)
    )
    assert adjustment is not None
    assert adjustment.delta == 50
    assert adjustment.reason == "product_adoption"
    assert adjustment.actor == "owner:owner-1"


def test_sku_sigue_plantilla_deterministica_y_se_trunca_a_64(db_session):
    """El SKU es RAG-{codigo}-{codigo_orig normalizado} y nunca supera 64 chars."""
    _seed_supplier(db_session, code="AMX")
    adopt_product(db_session, _dto(sku=" at-5044 "), OWNER, FakeEmbedder())
    assert db_session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "RAG-AMX-AT-5044"))

    largo = build_sku("AMX", "x" * 200)
    assert len(largo) == 64


def test_sku_colision_rechazada_sin_persistir(db_session):
    """Un SKU ya existente en catalogo devuelve 409 y no persiste nada nuevo."""
    _seed_supplier(db_session)
    seeds = [
        Catalogo(
            codigo_interno=f"RAG-AMX-AT-{i}",
            supplier_id=1,
            nombre_oficial=f"Semilla {i}",
            costo_proveedor=Decimal("1.00"),
            margen_aplicado_pct=Decimal(0),
            precio_lista_base=Decimal("1.00"),
            stock_disponible=1,
            sinonimos=[f"Semilla {i}"],
        )
        for i in range(1, 4)
    ]
    db_session.add_all(seeds)
    db_session.flush()

    with pytest.raises(SkuCollisionError):
        adopt_product(db_session, _dto(sku="at-1"), OWNER, FakeEmbedder())

    # El error se lanza antes de escribir: la adopción no crea Inventory ni audit.
    db_session.rollback()
    assert db_session.scalar(select(Inventory)) is None
    assert db_session.scalar(select(StockAdjustment)) is None


def test_precio_ausente_se_guarda_como_cero(db_session):
    """Precio None se normaliza a Decimal("0.00") siguiendo el patrón _coerce_cost."""
    _seed_supplier(db_session)
    product = adopt_product(db_session, _dto(precio=None), OWNER, FakeEmbedder())
    assert product.costo_proveedor == Decimal("0.00")
    assert product.precio_lista_base == Decimal("0.00")


@pytest.mark.parametrize(
    ("moneda", "esperada"),
    [("usd", "USD"), ("ARS", "ARS"), (None, None)],
)
def test_moneda_se_normaliza_a_mayusculas(db_session, moneda, esperada):
    """La moneda se guarda en mayúsculas; ausente queda None."""
    _seed_supplier(db_session)
    product = adopt_product(db_session, _dto(moneda=moneda), OWNER, FakeEmbedder())
    assert product.moneda == esperada


def test_composicion_del_embedding_texto_normalizado(db_session):
    """El embedding compone nombre+marca+categoria+subcategoria normalizados."""
    _seed_supplier(db_session)
    embedder = FakeEmbedder()
    product = adopt_product(db_session, _dto(), OWNER, embedder)

    assert embedder.texts == ["tornillo 5 16 x 2 pulgadas acme fijaciones tornillos"]
    assert list(product.embedding) == EMBED_VECTOR


def test_embedding_falla_y_rollback_total(db_session):
    """Un embedder que falla revierte la adopción: nada se persiste (502)."""
    _seed_supplier(db_session)
    embedder = FakeEmbedder(error=RuntimeError("openai down"))

    with pytest.raises(EmbeddingUnavailableError):
        adopt_product(db_session, _dto(), OWNER, embedder)

    db_session.rollback()
    assert db_session.scalar(select(Catalogo)) is None
    assert db_session.scalar(select(Inventory)) is None
    assert db_session.scalar(select(StockAdjustment)) is None


@pytest.mark.parametrize("stock", [0, -5])
def test_stock_no_positivo_rechazado(db_session, stock):
    """Stock <= 0 se rechaza con error de validación y no persiste nada."""
    _seed_supplier(db_session)

    with pytest.raises(InvalidStockError):
        adopt_product(db_session, _dto(stock=stock), OWNER, FakeEmbedder())

    db_session.rollback()
    assert db_session.scalar(select(Catalogo)) is None


def test_provenance_almacenada_write_once(db_session):
    """El origen JSONB guarda rag.node_id, archivo_origen y pagina_origen."""
    _seed_supplier(db_session)
    product = adopt_product(db_session, _dto(), OWNER, FakeEmbedder())
    assert product.origen == {
        "rag": {
            "node_id": "node-abc-123",
            "archivo_origen": "catalogo_amx_2026.pdf",
            "pagina_origen": 12,
        }
    }


def test_provenance_faltante_fail_closed(db_session):
    """Sin node_id la adopción falla cerrado y no persiste nada."""
    _seed_supplier(db_session)

    with pytest.raises(MissingProvenanceError):
        adopt_product(db_session, _dto(node_id=""), OWNER, FakeEmbedder())

    db_session.rollback()
    assert db_session.scalar(select(Catalogo)) is None


def test_proveedor_desconocido_error_explicito(db_session):
    """Un codigo_proveedor sin proveedor da error explícito y no persiste nada."""
    _seed_supplier(db_session, code="AMX")

    with pytest.raises(SupplierUnknownError):
        adopt_product(db_session, _dto(codigo_proveedor="ZZZ"), OWNER, FakeEmbedder())

    db_session.rollback()
    assert db_session.scalar(select(Catalogo)) is None


def test_proveedor_inactivo_error_explicito(db_session):
    """Un proveedor INACTIVO da error explícito y no persiste nada."""
    _seed_supplier(db_session, code="AMX", status=SupplierStatus.INACTIVO)

    with pytest.raises(SupplierInactiveError):
        adopt_product(db_session, _dto(), OWNER, FakeEmbedder())

    db_session.rollback()
    assert db_session.scalar(select(Catalogo)) is None