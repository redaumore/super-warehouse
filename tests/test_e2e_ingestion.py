"""E2E ingestion flow (task 4.7): upload → vision preview → confirm inventory.

Drives the supplier-document ingestion path end-to-end: an uploaded remito
photo goes through vision analysis (mock provider), renders an editable
preview grid, and — on confirm — writes to inventory: existing SKUs gain
stock, unknown SKUs become new catalog products. Also covers the barcode
stock-query flow (decoder mocked, catalog lookup real).

Skipped cleanly when Postgres is not running.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.backoffice.ingestion import confirm_items, extract_document_items, to_grid_rows
from src.barcode.decoder import BarcodeLookupKind, decode_image, lookup_barcode
from src.config import get_settings
from src.db.models import Catalogo, Proveedor

REMITO_TEXT = """REMITO 55
10 x Clavos Paris 2 Pulgadas
PINT-001 Pintura Látex Blanco 4 3200,00"""


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
def _clean_schema(db_engine):
    yield
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE order_items, orders, stock_reservations, catalogo, proveedores, "
                "clientes, lista_precios, proveedor_sku_mapping RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def supplier(db_session):
    db_session.add(
        Proveedor(
            proveedor_id=1,
            razon_social="Proveedor Mayorista",
            margen_predeterminado=Decimal("0.10"),
        )
    )
    db_session.add(
        Catalogo(
            id=1,
            codigo_interno="CLV-PRS-2",
            proveedor_id=1,
            nombre_oficial="Clavos Paris 2 Pulgadas",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            stock_disponible=50,
            sinonimos=["clavos"],
        )
    )
    db_session.flush()
    db_session.execute(text("SELECT setval(pg_get_serial_sequence('catalogo', 'id'), 1, true)"))
    return {"session": db_session}


class FakeAnalyzer:
    """Vision provider returning the canned remito text."""

    def analyze(self, image_url, prompt):
        return SimpleNamespace(text=REMITO_TEXT, confidence=1.0)


def test_e2e_remito_upload_previews_and_confirms_inventory(supplier, tmp_path):
    """Un remito subido se previsualiza y al confirmar actualiza el inventario."""
    session = supplier["session"]
    image = tmp_path / "remito.jpg"
    image.write_bytes(b"fake")
    with patch("src.supplier.ocr.image_to_data_url", return_value="data:image/jpeg;base64,AA=="):
        extraction = extract_document_items(FakeAnalyzer(), image)  # type: ignore[arg-type]
    grid = to_grid_rows(extraction)
    assert grid == [
        ["", "Clavos Paris 2 Pulgadas", "10", ""],
        ["PINT-001", "Pintura Látex Blanco", "4", "3200.00"],
    ]

    # Owner confirms (grid editable — no corrections needed here).
    result = confirm_items(session, grid, proveedor_id=1)
    assert result.updated == 1
    assert result.created == 1

    existing = session.scalar(
        select(Catalogo).where(Catalogo.codigo_interno == "CLV-PRS-2")
    )
    assert existing.stock_disponible == 60  # 50 + 10
    created = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "PINT-001"))
    assert created.stock_disponible == 4
    assert created.precio_lista_base == Decimal("3520.00")  # 3200 × 1.10


def test_e2e_owner_corrections_override_raw_extraction(supplier, tmp_path):
    """Correcciones del dueño en la grilla reemplazan la extracción cruda."""
    session = supplier["session"]
    image = tmp_path / "remito.jpg"
    image.write_bytes(b"fake")
    with patch("src.supplier.ocr.image_to_data_url", return_value="data:image/jpeg;base64,AA=="):
        extraction = extract_document_items(FakeAnalyzer(), image)  # type: ignore[arg-type]
    grid = to_grid_rows(extraction)
    # Owner fixes quantity and cost before confirming.
    grid[1] = ["PINT-001", "Pintura Látex Blanco", "6", "3100.00"]
    confirm_items(session, grid, proveedor_id=1)
    created = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "PINT-001"))
    assert created.stock_disponible == 6
    assert created.costo_proveedor == Decimal("3100.00")


def test_e2e_barcode_stock_query_decodes_and_resolves(supplier, tmp_path):
    """Una foto de código de barras decodifica y responde el stock disponible."""
    session = supplier["session"]
    product = session.scalar(
        select(Catalogo).where(Catalogo.codigo_interno == "CLV-PRS-2")
    )
    product.codigo_barras = "7790000000001"
    session.flush()

    image = tmp_path / "barcode.png"
    from PIL import Image as PILImage

    PILImage.new("RGB", (10, 10), color="white").save(image)
    with patch(
        "src.barcode.decoder.decode",
        return_value=[SimpleNamespace(data=b"7790000000001", type="EAN13")],
    ):
        decoded = decode_image(image)
    assert decoded[0].data == "7790000000001"
    lookup = lookup_barcode(session, decoded[0].data)
    assert lookup.kind is BarcodeLookupKind.SINGLE
    assert lookup.candidates[0].codigo_interno == "CLV-PRS-2"
    assert lookup.candidates[0].stock_disponible == 50