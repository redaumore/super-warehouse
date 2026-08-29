"""Supplier OCR tests (task 3.9).

Unit (no DB): deterministic line parsing of remito/invoice and price-list text
— including the illegible-document rejection — with a fake VisionAnalyzer.
Integration (Postgres, skipped when down): price-list rows map to existing
SKUs or suggest new ones, storing/updating supplier mappings without
duplicates.
"""

from __future__ import annotations

import base64
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.agents.perception import VisionError
from src.config import get_settings
from src.db.models import Catalogo, Proveedor, ProveedorSkuMapping
from src.supplier.ocr import (
    DocumentExtraction,
    ExtractedItem,
    IllegibleDocumentError,
    PriceListRow,
    extract_document,
    image_to_data_url,
    ingest_price_list_rows,
    parse_line_items,
    parse_price_list,
)


class FakeAnalyzer:
    """VisionAnalyzer stand-in returning canned text."""

    def __init__(self, text: str) -> None:
        self._text = text

    def analyze(self, image_url: str, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(text=self._text, confidence=1.0)


REMITO_TEXT = """REMITO NRO 1234
10 x Clavos Paris 2 Pulgadas
CLV-001 Tornillos M6 500 1250,00
CMT-002 Cemento Portland 2 24000.00
Firma: legible"""


def test_parse_line_items_extracts_quantity_and_cost_rows():
    """El texto de un remito se parsea en filas con cantidad y costo."""
    extraction = parse_line_items(REMITO_TEXT)
    assert extraction.items == (
        ExtractedItem(codigo="", descripcion="Clavos Paris 2 Pulgadas", cantidad=10),
        ExtractedItem(
            codigo="CLV-001", descripcion="Tornillos M6", cantidad=500, costo=Decimal("1250.00")
        ),
        ExtractedItem(
            codigo="CMT-002", descripcion="Cemento Portland", cantidad=2, costo=Decimal("24000.00")
        ),
    )


def test_parse_line_items_keeps_unparsed_lines_flagged():
    """Las líneas no interpretables quedan señaladas, nunca se descartan."""
    extraction = parse_line_items("10 x Clavos\nmanchas ilegibles\n5 x Tornillos")
    assert len(extraction.items) == 2
    assert extraction.unparsed_lines == ("manchas ilegibles",)


def test_parse_line_items_empty_text_has_no_items():
    """Un texto vacío no produce filas ni líneas pendientes."""
    extraction = parse_line_items("   \n  ")
    assert extraction == DocumentExtraction(items=())


def test_extract_document_returns_parsed_items(tmp_path):
    """Extraer un documento legible devuelve las filas parseadas."""
    image = tmp_path / "remito.jpg"
    image.write_bytes(b"fake")
    with patch("src.supplier.ocr.image_to_data_url", return_value="data:image/jpeg;base64,AA=="):
        extraction = extract_document(FakeAnalyzer(REMITO_TEXT), image)
    assert len(extraction.items) == 3
    assert extraction.items[0].descripcion == "Clavos Paris 2 Pulgadas"


def test_extract_document_rejects_illegible_with_clear_error(tmp_path):
    """Un documento ilegible se rechaza con un error claro, sin escribir nada."""
    image = tmp_path / "mancha.jpg"
    image.write_bytes(b"fake")
    with (
        patch("src.supplier.ocr.image_to_data_url", return_value="data:image/jpeg;base64,AA=="),
        pytest.raises(IllegibleDocumentError, match="illegible"),
    ):
        extract_document(FakeAnalyzer("texto ilegible sin filas"), image)


def test_extract_document_vision_failure_propagates(tmp_path):
    """Un fallo del proveedor de visión se propaga como VisionError."""

    class FailingAnalyzer:
        def analyze(self, image_url, prompt):
            raise RuntimeError("vision down")

    image = tmp_path / "remito.jpg"
    image.write_bytes(b"fake")
    with (
        patch("src.supplier.ocr.image_to_data_url", return_value="data:image/jpeg;base64,AA=="),
        pytest.raises(VisionError),
    ):
        extract_document(FailingAnalyzer(), image)  # type: ignore[arg-type]


def test_parse_price_list_extracts_code_description_cost():
    """Una lista de precios se parsea en código, descripción y costo."""
    rows = parse_price_list("CLV-001 Clavos Paris 2 1250,00\nCMT-002 Cemento Portland 24000.00")
    assert rows == [
        PriceListRow(codigo="CLV-001", descripcion="Clavos Paris 2", costo=Decimal("1250.00")),
        PriceListRow(codigo="CMT-002", descripcion="Cemento Portland", costo=Decimal("24000.00")),
    ]


def test_image_to_data_url_embeds_file_bytes(tmp_path):
    """Una imagen local se codifica como data URL con su MIME."""
    path = tmp_path / "doc.jpg"
    path.write_bytes(b"\xff\xd8fake")
    assert image_to_data_url(path).startswith("data:image/jpeg;base64,")
    assert image_to_data_url(path).endswith(base64.b64encode(b"\xff\xd8fake").decode())


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
def supplier_ctx(db_session):
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
            codigo_interno="CLV-001",
            proveedor_id=1,
            nombre_oficial="Clavos Paris 2 Pulgadas",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            stock_disponible=10,
            sinonimos=["clavos"],
        )
    )
    db_session.flush()
    return {"session": db_session}


def test_ingest_price_list_maps_and_suggests(supplier_ctx):
    """La lista de precios mapea SKU existentes y sugiere nuevos."""
    rows = [
        PriceListRow(
            codigo="CLV-001", descripcion="Clavos Paris 2 Pulgadas", costo=Decimal("95.00")
        ),
        PriceListRow(
            codigo="NEW-777", descripcion="Pintura Látex Blanco", costo=Decimal("3200.00")
        ),
    ]
    result = ingest_price_list_rows(supplier_ctx["session"], proveedor_id=1, rows=rows)
    assert result.mapped == 1
    assert result.suggested == 1
    mappings = supplier_ctx["session"].scalars(select(ProveedorSkuMapping)).all()
    assert len(mappings) == 2
    by_code = {m.codigo_proveedor: m for m in mappings}
    assert by_code["CLV-001"].sku_interno == "CLV-001"
    assert by_code["CLV-001"].confianza == Decimal("0.90")
    assert by_code["NEW-777"].sku_interno == "NEW-777"
    assert by_code["NEW-777"].confianza == Decimal("0.50")


def test_ingest_price_list_updates_existing_mapping_without_duplicates(supplier_ctx):
    """Re-ingestar la misma fila actualiza el mapeo sin duplicarlo."""
    rows = [
        PriceListRow(
            codigo="CLV-001", descripcion="Clavos Paris 2 Pulgadas", costo=Decimal("95.00")
        )
    ]
    ingest_price_list_rows(supplier_ctx["session"], proveedor_id=1, rows=rows)
    ingest_price_list_rows(supplier_ctx["session"], proveedor_id=1, rows=rows)
    mappings = supplier_ctx["session"].scalars(select(ProveedorSkuMapping)).all()
    assert len(mappings) == 1
    assert mappings[0].descripcion_raw == "Clavos Paris 2 Pulgadas"


def test_ingest_price_list_matches_by_normalized_name(supplier_ctx):
    """Una descripción normalizada mapea al SKU sin coincidencia de código."""
    rows = [
        PriceListRow(codigo="X-999", descripcion="clavos paris 2 pulgadas", costo=Decimal("90.00"))
    ]
    result = ingest_price_list_rows(supplier_ctx["session"], proveedor_id=1, rows=rows)
    assert result.mapped == 1
    mapping = supplier_ctx["session"].scalar(select(ProveedorSkuMapping))
    assert mapping.sku_interno == "CLV-001"
