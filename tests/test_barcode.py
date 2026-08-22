"""Barcode tests (task 3.8).

Unit (no DB): image decoding through a mocked pyzbar — values, symbology,
unreadable images and decoder failures.
Integration (Postgres, skipped when down): barcode values resolving to one
SKU, several SKUs (duplicate flagged, never guessed) or none (unknown).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image as PILImage
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from src.barcode.decoder import (
    Barcode,
    BarcodeDecodeError,
    BarcodeLookupKind,
    decode_image,
    lookup_barcode,
)
from src.config import get_settings
from src.db.models import Catalogo, Proveedor


def _real_png(path: Path) -> None:
    """Write a minimal valid PNG so PIL can open it."""
    PILImage.new("RGB", (10, 10), color="white").save(path)


def test_decode_image_returns_values_and_symbologies(tmp_path):
    """Una imagen con códigos devuelve datos y simbología de cada código."""
    image = tmp_path / "box.png"
    _real_png(image)
    results = [
        SimpleNamespace(data=b"7791234567890", type="EAN13"),
        SimpleNamespace(data=b"https://example.com/qr", type="QRCODE"),
    ]
    with patch("src.barcode.decoder.decode", return_value=results) as decode_mock:
        decoded = decode_image(image)
    decode_mock.assert_called_once()
    assert decoded == [
        Barcode(data="7791234567890", symbology="EAN13"),
        Barcode(data="https://example.com/qr", symbology="QRCODE"),
    ]


def test_decode_image_without_codes_returns_empty_list(tmp_path):
    """Una imagen sin códigos decodifica a una lista vacía."""
    image = tmp_path / "blank.png"
    _real_png(image)
    with patch("src.barcode.decoder.decode", return_value=[]):
        assert decode_image(image) == []


def test_decode_failure_raises_clear_error(tmp_path):
    """Una imagen ilegible falla con un error claro de decodificación."""
    image = tmp_path / "blurred.png"
    image.write_bytes(b"not-an-image")
    with pytest.raises(BarcodeDecodeError, match="could not decode"):
        decode_image(image)


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
                "clientes, lista_precios RESTART IDENTITY CASCADE"
            )
        )


def _seed_catalog(db_session, *, barcodes: list[str], skus: list[str]) -> None:
    db_session.add(
        Proveedor(
            proveedor_id=1,
            razon_social="Proveedor Test",
            margen_predeterminado=Decimal(0),
        )
    )
    for index, (sku, barcode) in enumerate(zip(skus, barcodes, strict=True)):
        db_session.add(
            Catalogo(
                id=index + 1,
                codigo_interno=sku,
                proveedor_id=1,
                nombre_oficial=f"Producto {sku}",
                costo_proveedor=Decimal("100.00"),
                margen_aplicado_pct=Decimal(0),
                precio_lista_base=Decimal("100.00"),
                stock_disponible=10,
                sinonimos=[],
                codigo_barras=barcode,
            )
        )
    db_session.flush()


def test_single_barcode_maps_to_one_sku(db_session):
    """Un código único mapea a un solo SKU del catálogo."""
    _seed_catalog(db_session, barcodes=["7790001"], skus=["CLV-001"])
    lookup = lookup_barcode(db_session, "7790001")
    assert lookup.kind is BarcodeLookupKind.SINGLE
    assert len(lookup.candidates) == 1
    assert lookup.candidates[0].codigo_interno == "CLV-001"


def test_duplicate_barcode_flags_candidates_for_owner(db_session):
    """Un código compartido por dos SKU se marca DUPLICATE sin elegir por nadie."""
    _seed_catalog(db_session, barcodes=["7790002", "7790002"], skus=["CLV-001", "CLV-002"])
    lookup = lookup_barcode(db_session, "7790002")
    assert lookup.kind is BarcodeLookupKind.DUPLICATE
    assert {c.codigo_interno for c in lookup.candidates} == {"CLV-001", "CLV-002"}


def test_unknown_barcode_is_reported(db_session):
    """Un código sin match se reporta como UNKNOWN para resolución manual."""
    _seed_catalog(db_session, barcodes=["7790003"], skus=["CLV-001"])
    lookup = lookup_barcode(db_session, "9999999999999")
    assert lookup.kind is BarcodeLookupKind.UNKNOWN
    assert lookup.candidates == ()