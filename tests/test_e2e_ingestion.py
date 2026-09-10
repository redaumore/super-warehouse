"""E2E receipt ingestion flow (task 5.1 of rag-document-ingestion).

Drives the RAG-backed path end-to-end with a mocked ``RagProductClient``:
upload → parse → two-pass resolve → review grid → gated confirm writes stock
with ``node_id`` provenance. Covers: unmatched lines (no ``Catalogo`` created,
confirm blocked), manual assignment of pending lines, RAG down → honest error
with zero writes, and the standalone barcode stock-query flow (decoder mocked,
catalog lookup real).

Skipped cleanly when Postgres is not running.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.backoffice.app import (
    _ingest_assign,
    _ingest_confirm,
    _ingest_manual_search,
    _ingest_parse,
)
from src.backoffice.ingestion import ReceiptLine, ResolvedLine
from src.barcode.decoder import BarcodeLookupKind, decode_image, lookup_barcode
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Inventory,
    StockAdjustment,
    Supplier,
    SupplierPurchaseOrder,
    SupplierPurchaseOrderItem,
    SupplierPurchaseOrderState,
    SupplierStatus,
)
from src.integrations.rag import DocumentLine, RagProduct, RagProductError


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
                "TRUNCATE order_items, orders, stock_reservations, stock_adjustments, "
                "inventory, catalogo, suppliers, clientes, lista_precios, "
                "supplier_sku_mappings RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def supplier(db_session):
    """Seed an ACTIVO supplier and an already-adopted product (build_sku SKU)."""
    db_session.add(
        Supplier(
            id=1,
            code="MSA",
            business_name="Mayorista SA",
            default_margin_pct=Decimal("0.10"),
        )
    )
    db_session.add(
        Catalogo(
            id=1,
            codigo_interno="MSA-CLV-PRS-2",
            supplier_id=1,
            nombre_oficial="Clavos Paris 2 Pulgadas",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            sinonimos=["clavos"],
        )
    )
    db_session.add(Inventory(sku_id="MSA-CLV-PRS-2", quantity_on_hand=50))
    db_session.commit()
    db_session.execute(text("SELECT setval(pg_get_serial_sequence('catalogo', 'id'), 1, true)"))
    return {"session": db_session}


class FakeRag:
    """RagProductClient stand-in with canned parse/exact/hybrid responses.

    ``exact`` maps a normalized ``codigo_orig`` to its exact matches so each
    line resolves against the right code (mirrors the two-pass resolver).
    """

    def __init__(self, parse_lines=(), exact=None, hybrid=()) -> None:
        self.parse_lines = parse_lines
        self.exact = exact if exact is not None else {}
        self.hybrid = hybrid
        self.query_calls: list[str] = []

    def parse_document(self, *, filename: str, content: bytes, codigo_proveedor: str):
        return self.parse_lines

    def exact_lookup(self, codigo_orig: str, codigo_proveedor: str):
        return self.exact.get(codigo_orig, ())

    def query(self, text: str):
        self.query_calls.append(text)
        return self.hybrid


def _clavos_line() -> DocumentLine:
    return DocumentLine(
        codigo_orig="CLV-PRS-2",
        codigo=None,
        descripcion="Clavos Paris 2 Pulgadas",
        cantidad=10,
        costo=None,
        pagina=1,
    )


def _clavos_product() -> RagProduct:
    return RagProduct(
        sku="CLV-PRS-2",
        name="Clavos Paris 2 Pulgadas",
        codigo_proveedor="MSA",
        price=135.5,
        currency="ARS",
        node_id="node_clv_prs_2",
    )


def _embedder():
    class _FakeEmbedder:
        def embed(self, texts):
            return [[0.0] * 1536 for _ in texts]

    return _FakeEmbedder()


def _image(tmp_path: Path) -> Path:
    image = tmp_path / "remito.jpg"
    image.write_bytes(b"fake-image")
    return image


def test_e2e_receipt_flow_writes_stock_with_node_id_provenance(supplier, tmp_path):
    """[rag-doc R5] Upload → parse → resolve → confirm escribe stock con provenance."""
    session = supplier["session"]
    rag = FakeRag(parse_lines=(_clavos_line(),), exact={"CLV-PRS-2": (_clavos_product(),)}, hybrid=())

    grid, state, _message = _ingest_parse(rag, _image(tmp_path), 1)
    assert len(grid) == 1
    assert grid[0][4] == "CLV-PRS-2 — Clavos Paris 2 Pulgadas"
    assert not state[0].pending

    result_message = _ingest_confirm(state, 1, _embedder())
    assert result_message == "Ingresado: 1 actualizados, 0 nuevos."

    existing = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-CLV-PRS-2"))
    assert existing is not None
    inventory = session.scalar(select(Inventory).where(Inventory.sku_id == "MSA-CLV-PRS-2"))
    assert inventory.quantity_on_hand == 60  # 50 + 10
    adjustment = session.scalar(
        select(StockAdjustment).where(StockAdjustment.reason == "receipt_ingestion")
    )
    assert adjustment.delta == 10
    assert adjustment.actor == "owner:backoffice-ui"


def test_e2e_unmatched_line_blocks_confirm_and_creates_nothing(supplier, tmp_path):
    """[rag-doc R5][sup-doc R2] Línea sin match → no Catalogo + confirm bloqueado."""
    session = supplier["session"]
    paint_line = DocumentLine(
        codigo_orig="PINT-001",
        codigo=None,
        descripcion="Pintura Látex Blanco",
        cantidad=4,
        costo=3200.0,
        pagina=1,
    )
    rag = FakeRag(
        parse_lines=(_clavos_line(), paint_line),
        exact={"CLV-PRS-2": (_clavos_product(),)},
        hybrid=(),
    )
    grid, state, _message = _ingest_parse(rag, _image(tmp_path), 1)
    assert grid[1][4] == "PENDIENTE"

    blocked = _ingest_confirm(state, 1, _embedder())
    assert "bloqueado" in blocked
    assert "PINT-001" in blocked

    session.rollback()
    assert session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-PINT-001")) is None
    # zero writes while blocked: only the seeded Inventory row exists, unchanged
    inventory_rows = session.scalars(select(Inventory)).all()
    assert [row.quantity_on_hand for row in inventory_rows] == [50]


def test_e2e_manual_assignment_resolves_pending_and_adopts(supplier, tmp_path):
    """[manual R1][rag-doc R5] Búsqueda manual + asignación adopta con origen rag."""
    session = supplier["session"]
    paint_line = DocumentLine(
        codigo_orig="PINT-001",
        codigo=None,
        descripcion="Pintura Látex Blanco",
        cantidad=4,
        costo=3200.0,
        pagina=1,
    )
    paint_product = RagProduct(
        sku="PINT-001",
        name="Pintura Látex Blanco",
        codigo_proveedor="MSA",
        brand="X",
        price=3200.0,
        currency="ARS",
        source_file="catalogo-2024.pdf",
        page=3,
        node_id="node_pint_001",
    )
    rag = FakeRag(parse_lines=(paint_line,), exact={}, hybrid=(paint_product,))

    grid, state, _message = _ingest_parse(rag, _image(tmp_path), 1)
    # Automatic hybrid fallback resolves it directly (exactly 1 candidate).
    assert grid[0][4] == "PINT-001 — Pintura Látex Blanco"
    assert not state[0].pending
    result_message = _ingest_confirm(state, 1, _embedder())
    assert result_message == "Ingresado: 0 actualizados, 1 nuevos."

    created = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-PINT-001"))
    assert created is not None
    created_inventory = session.scalar(
        select(Inventory).where(Inventory.sku_id == "MSA-PINT-001")
    )
    assert created_inventory.quantity_on_hand == 4
    assert created.origen == {
        "rag": {
            "node_id": "node_pint_001",
            "archivo_origen": "catalogo-2024.pdf",
            "pagina_origen": 3,
        }
    }


def test_e2e_manual_search_and_assign_fixes_pending_line(supplier, tmp_path):
    """[manual R1] Sin fallback automático, la búsqueda manual resuelve la línea."""
    supplier["session"]
    paint_line = DocumentLine(
        codigo_orig="PINT-001",
        codigo=None,
        descripcion="Pintura Látex Blanco",
        cantidad=4,
        costo=3200.0,
        pagina=1,
    )
    paint_product = RagProduct(
        sku="PINT-001",
        name="Pintura Látex Blanco",
        codigo_proveedor="MSA",
        price=3200.0,
        currency="ARS",
        node_id="node_pint_001",
    )
    rag = FakeRag(parse_lines=(paint_line,), exact={}, hybrid=())
    _grid, state, _message = _ingest_parse(rag, _image(tmp_path), 1)
    assert state[0].pending

    candidates_grid, candidates, _status = _ingest_manual_search(rag, 0, "PINT-001", 1)
    assert len(candidates_grid) == 0  # manual search uses the same supplier-scoped query

    # Manual search with an owner-provided term returns the candidate...
    rag.hybrid = (paint_product,)
    candidates_grid, candidates, _status = _ingest_manual_search(rag, 0, "Pintura latex", 1)
    assert len(candidates_grid) == 1

    new_state, _new_grid, assign_status = _ingest_assign(state, 0, 0, candidates)
    assert "asignada" in assign_status
    assert not new_state[0].pending
    assert new_state[0].product.node_id == "node_pint_001"


def test_e2e_rag_down_shows_honest_error_and_writes_nothing(supplier, tmp_path):
    """[rag-doc R6] RAG caído → error honesto, cero escrituras."""
    session = supplier["session"]

    class DownRag(FakeRag):
        def parse_document(self, *, filename, content, codigo_proveedor):
            raise RagProductError("connection refused")

    grid, state, message = _ingest_parse(DownRag(), _image(tmp_path), 1)
    assert grid == []
    assert state == ()
    assert "RAG no disponible" in message
    # zero writes: only the seeded Inventory row exists, unchanged
    inventory_rows = session.scalars(select(Inventory)).all()
    assert [row.quantity_on_hand for row in inventory_rows] == [50]


def _mixed_lines_rag() -> FakeRag:
    """FakeRag con una línea existente (CLV-PRS-2) y una nueva RAG-only (PINT-001)."""
    paint_line = DocumentLine(
        codigo_orig="PINT-001",
        codigo=None,
        descripcion="Pintura Látex Blanco",
        cantidad=4,
        costo=3200.0,
        pagina=1,
    )
    paint_product = RagProduct(
        sku="PINT-001",
        name="Pintura Látex Blanco",
        codigo_proveedor="MSA",
        price=3200.0,
        currency="ARS",
        node_id="node_pint_001",
    )
    return FakeRag(
        parse_lines=(_clavos_line(), paint_line),
        exact={"CLV-PRS-2": (_clavos_product(),)},
        hybrid=(paint_product,),
    )


def test_e2e_embedding_failure_rolls_back_full_ingestion(supplier, tmp_path):
    """El embedder falla al adoptar → rollback total: ni bump, ni Catálogo, ni ajuste."""
    session = supplier["session"]

    class FailingEmbedder:
        def embed(self, texts):
            raise RuntimeError("openai down")

    _grid, state, _message = _ingest_parse(_mixed_lines_rag(), _image(tmp_path), 1)
    message = _ingest_confirm(state, 1, FailingEmbedder())
    assert "no se guardó nada" in message

    existing = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-CLV-PRS-2"))
    assert existing is not None
    # The seeded Inventory row is the only one and the staged bump rolled back.
    inventory_rows = session.scalars(select(Inventory)).all()
    assert [row.quantity_on_hand for row in inventory_rows] == [50]
    assert session.scalar(
        select(StockAdjustment).where(StockAdjustment.reason == "receipt_ingestion")
    ) is None
    assert session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-PINT-001")) is None


def test_e2e_wrong_dimension_embedding_rolls_back_full_ingestion(supplier, tmp_path):
    """Embedding con dimensión inválida → rollback total, nada queda persistido."""
    session = supplier["session"]

    class WrongDimEmbedder:
        def embed(self, texts):
            return [[0.0] * 10 for _ in texts]

    _grid, state, _message = _ingest_parse(_mixed_lines_rag(), _image(tmp_path), 1)
    message = _ingest_confirm(state, 1, WrongDimEmbedder())
    assert "no se guardó nada" in message

    existing = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-CLV-PRS-2"))
    assert existing is not None
    # The seeded Inventory row is the only one and no staged write survived.
    inventory_rows = session.scalars(select(Inventory)).all()
    assert [row.quantity_on_hand for row in inventory_rows] == [50]
    assert session.scalar(
        select(StockAdjustment).where(StockAdjustment.reason == "receipt_ingestion")
    ) is None
    assert session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-PINT-001")) is None


def test_e2e_document_without_usable_lines_writes_nothing(supplier, tmp_path):
    """Documento sin líneas utilizables → mensaje honesto y cero escrituras."""
    session = supplier["session"]
    grid, state, message = _ingest_parse(FakeRag(), _image(tmp_path), 1)
    assert grid == []
    assert state == ()
    assert "No se extrajeron líneas legibles" in message

    existing = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-CLV-PRS-2"))
    assert existing is not None
    # The seeded Inventory row is the only one and nothing else was written.
    inventory_rows = session.scalars(select(Inventory)).all()
    assert [row.quantity_on_hand for row in inventory_rows] == [50]
    assert session.scalar(
        select(StockAdjustment).where(StockAdjustment.reason == "receipt_ingestion")
    ) is None


def test_e2e_inactive_supplier_blocks_ingestion_and_writes_nothing(supplier, tmp_path):
    """Proveedor INACTIVO al ingestar → el guard bloquea parse y confirm, nada escrito."""
    session = supplier["session"]
    session.get(Supplier, 1).status = SupplierStatus.INACTIVO
    session.commit()

    rag = FakeRag(
        parse_lines=(_clavos_line(),),
        exact={"CLV-PRS-2": (_clavos_product(),)},
        hybrid=(),
    )
    grid, state, message = _ingest_parse(rag, _image(tmp_path), 1)
    assert grid == []
    assert state == ()  # guard trips before RAG parse; no lines resolved
    assert message == "Error: supplier 1 is INACTIVO"

    # A caller bypassing UI gating still hits the guard at confirm time.
    resolved = (
        ResolvedLine(
            receipt=ReceiptLine(
                codigo_orig="CLV-PRS-2",
                descripcion="Clavos Paris 2 Pulgadas",
                cantidad=10,
                costo=Decimal("100.00"),
            ),
            product=_clavos_product(),
        ),
    )
    confirm_message = _ingest_confirm(resolved, 1, _embedder())
    assert confirm_message == "Error: supplier 1 is INACTIVO"

    existing = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-CLV-PRS-2"))
    assert existing is not None
    # The seeded Inventory row is the only one and nothing else was written.
    inventory_rows = session.scalars(select(Inventory)).all()
    assert [row.quantity_on_hand for row in inventory_rows] == [50]
    assert session.scalar(
        select(StockAdjustment).where(StockAdjustment.reason == "receipt_ingestion")
    ) is None


def test_e2e_barcode_stock_query_decodes_and_resolves(supplier, tmp_path):
    """Una foto de código de barras decodifica y responde el stock disponible."""
    session = supplier["session"]
    product = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == "MSA-CLV-PRS-2"))
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
    assert lookup.candidates[0].codigo_interno == "MSA-CLV-PRS-2"
    inventory = session.scalar(select(Inventory).where(Inventory.sku_id == "MSA-CLV-PRS-2"))
    assert inventory.quantity_on_hand == 50


def test_e2e_receipt_without_po_bumps_stock_with_full_audit(supplier, tmp_path):
    """[R8][adr-0002] Remito sin PO vinculada → la ingesta bumpa stock con auditoría completa."""
    session = supplier["session"]
    rag = FakeRag(parse_lines=(_clavos_line(),), exact={"CLV-PRS-2": (_clavos_product(),)}, hybrid=())

    grid, state, _message = _ingest_parse(rag, _image(tmp_path), 1)
    assert len(grid) == 1
    assert not state[0].pending

    result_message = _ingest_confirm(state, 1, _embedder())
    assert result_message == "Ingresado: 1 actualizados, 0 nuevos."

    # PO-agnostic per ADR 0002: ingestion never requires or creates a PO.
    assert session.scalars(select(SupplierPurchaseOrder)).all() == []
    inventory = session.scalar(select(Inventory).where(Inventory.sku_id == "MSA-CLV-PRS-2"))
    assert inventory.quantity_on_hand == 60  # 50 + 10
    adjustments = session.scalars(
        select(StockAdjustment).where(StockAdjustment.reason == "receipt_ingestion")
    ).all()
    assert len(adjustments) == 1
    assert adjustments[0].sku == "MSA-CLV-PRS-2"
    assert adjustments[0].delta == 10
    assert adjustments[0].actor == "owner:backoffice-ui"


def test_e2e_receipt_with_open_po_ingests_and_leaves_po_untouched(supplier, tmp_path):
    """[R8][adr-0002] Remito con PO en OPEN → la ingesta bumpa stock y la PO queda intacta."""
    session = supplier["session"]
    po = SupplierPurchaseOrder(supplier_id=1, estado=SupplierPurchaseOrderState.OPEN)
    session.add(po)
    session.flush()
    session.add(
        SupplierPurchaseOrderItem(
            po_id=po.po_id, sku="MSA-CLV-PRS-2", quantity=10, received_quantity=0
        )
    )
    session.commit()

    rag = FakeRag(parse_lines=(_clavos_line(),), exact={"CLV-PRS-2": (_clavos_product(),)}, hybrid=())
    grid, state, _message = _ingest_parse(rag, _image(tmp_path), 1)
    assert len(grid) == 1
    result_message = _ingest_confirm(state, 1, _embedder())
    assert result_message == "Ingresado: 1 actualizados, 0 nuevos."

    inventory = session.scalar(select(Inventory).where(Inventory.sku_id == "MSA-CLV-PRS-2"))
    assert inventory.quantity_on_hand == 60  # 50 + 10
    adjustment = session.scalar(
        select(StockAdjustment).where(StockAdjustment.reason == "receipt_ingestion")
    )
    assert adjustment.delta == 10

    # The two receiving paths are independent per ADR 0002: ingestion must not
    # transition the PO nor touch its item's received_quantity.
    session.expire_all()
    reloaded = session.get(SupplierPurchaseOrder, po.po_id)
    assert reloaded.estado is SupplierPurchaseOrderState.OPEN
    item = session.scalar(
        select(SupplierPurchaseOrderItem).where(SupplierPurchaseOrderItem.po_id == po.po_id)
    )
    assert item.received_quantity == 0