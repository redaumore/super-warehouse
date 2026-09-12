"""Supplier SKU mappings: normalization, idempotent recording, ingestion
integration and code-based search.

Covers the approved design where ``codigo_interno`` stays opaque and supplier
codes resolve through ``supplier_sku_mappings``: the helper contract
(normalize/record/find), the adoption hook, the receipt-ingestion
existing-product detection (mappings before ``build_sku`` — the duplicate-SKU
bug regression), and the order-form search over mapped codes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.backoffice.adoption import AdoptRequest, OwnerContext, adopt_product
from src.backoffice.catalog import list_products, resolve_product_code
from src.backoffice.ingestion import (
    PendingReason,
    ReceiptLine,
    ResolvedLine,
    ingest_receipt_lines,
)
from src.backoffice.sku_mappings import (
    find_product_by_any_supplier_code,
    find_product_by_supplier_code,
    normalize_supplier_code,
    primary_supplier_codes,
    record_supplier_sku,
)
from src.config import get_settings
from src.db.models import Catalogo, Inventory, Supplier, SupplierSkuMapping
from src.integrations.rag import RagProduct
from src.sourcing.product_search import search_order_products


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


_CREATE_RAG_TABLE = """
CREATE TABLE IF NOT EXISTS catalogo_productos_rag (
    node_id varchar PRIMARY KEY,
    codigo_producto varchar,
    codigo_orig varchar,
    nombre_proveedor varchar,
    codigo_proveedor varchar,
    marca varchar,
    categoria_padre varchar,
    categoria varchar,
    subcategoria varchar,
    precio numeric,
    moneda varchar,
    pagina_origen int,
    archivo_origen varchar,
    documento_id varchar,
    es_tabla boolean,
    text_content text
)
"""


@pytest.fixture()
def rag_table(db_engine):
    """Create the minimal RAG table on the test database and drop it after."""
    with db_engine.begin() as conn:
        conn.execute(text(_CREATE_RAG_TABLE))
    yield
    with db_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS catalogo_productos_rag"))


@pytest.fixture
def shop_ctx(db_session):
    """Proveedor MSA (id 1) + un producto ya adoptado (MSA-CLV-001)."""
    db_session.add(
        Supplier(id=1, code="MSA", business_name="Mayorista SA", default_margin_pct=Decimal("0.10"))
    )
    db_session.add(
        Catalogo(
            id=1,
            codigo_interno="MSA-CLV-001",
            supplier_id=1,
            nombre_oficial="Clavos Paris 2 Pulgadas",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            sinonimos=["clavos"],
        )
    )
    db_session.flush()
    # Explicit ids do not advance the sequence: bump it for auto-id inserts.
    db_session.execute(text("SELECT setval(pg_get_serial_sequence('catalogo', 'id'), 1, true)"))
    return db_session


def _embedder(*, fail: bool = False):
    """Fake 1536-dim embedder; ``fail=True`` raises like an unavailable service."""

    class _FakeEmbedder:
        def embed(self, texts):
            if fail:
                raise RuntimeError("embedding service down")
            return [[0.0] * 1536 for _ in texts]

    return _FakeEmbedder()


def _rag_product(*, sku: str, node_id: str = "node-1", name: str = "Tarugo Fischer 8mm"):
    return RagProduct(sku=sku, name=name, codigo_proveedor="MSA", node_id=node_id)


def _receipt(codigo_orig: str | None, cantidad: int = 4) -> ReceiptLine:
    return ReceiptLine(
        codigo_orig=codigo_orig,
        descripcion="Tarugo Fischer 8mm",
        cantidad=cantidad,
        costo=Decimal("95.00"),
        pagina=1,
    )


# ------------------------------------------------------- normalize (pure)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ax 302-8", "AX 302-8"),
        ("  AX 302-8  ", "AX 302-8"),
        ("AX  302-8", "AX 302-8"),
        ("AX\t302-8\n", "AX 302-8"),
        ("AX302-8", "AX302-8"),
    ],
    ids=["upper", "strip-edges", "collapse-internal", "mixed-whitespace", "no-spaces"],
)
def test_normalize_supplier_code(raw, expected):
    """Normaliza el código: mayúsculas, bordes y espacios internos colapsados."""
    assert normalize_supplier_code(raw) == expected


# ------------------------------------------------------- record / find (DB)


def test_record_supplier_sku_same_code_twice_inserts_one_row(shop_ctx):
    """Grabar el mismo código dos veces deja una sola fila (idempotente)."""
    record_supplier_sku(shop_ctx, 1, "AX 302-8", "MSA-CLV-001")
    record_supplier_sku(shop_ctx, 1, "  ax  302-8 ", "MSA-CLV-001")  # same normalized code

    mappings = shop_ctx.scalars(select(SupplierSkuMapping)).all()
    assert len(mappings) == 1
    assert mappings[0].supplier_sku_code == "AX 302-8"  # stored normalized
    assert mappings[0].internal_sku == "MSA-CLV-001"
    assert mappings[0].confidence == Decimal("100.00")  # owner-confirmed default


def test_record_supplier_sku_conflicting_internal_sku_first_wins(shop_ctx):
    """Código ya mapeado a otro SKU interno: el primer mapeo gana, no se repunta."""
    record_supplier_sku(shop_ctx, 1, "AX 302-8", "MSA-CLV-001")
    record_supplier_sku(shop_ctx, 1, "AX 302-8", "MSA-OTHER-99")

    mapping = shop_ctx.scalar(select(SupplierSkuMapping))
    assert mapping.internal_sku == "MSA-CLV-001"  # untouched
    assert len(shop_ctx.scalars(select(SupplierSkuMapping)).all()) == 1


def test_record_supplier_sku_blank_code_is_noop(shop_ctx):
    """Un código vacío no registra nada (no hay clave de búsqueda)."""
    record_supplier_sku(shop_ctx, 1, "   ", "MSA-CLV-001")
    assert shop_ctx.scalars(select(SupplierSkuMapping)).all() == []


def test_find_product_by_supplier_code_resolves_or_returns_none(shop_ctx):
    """El código mapeado resuelve al producto; uno sin mapeo devuelve None."""
    record_supplier_sku(shop_ctx, 1, "AX 302-8", "MSA-CLV-001")

    product = find_product_by_supplier_code(shop_ctx, 1, "  ax  302-8 ")
    assert product is not None
    assert product.codigo_interno == "MSA-CLV-001"
    assert find_product_by_supplier_code(shop_ctx, 1, "SM 302-8") is None
    assert find_product_by_supplier_code(shop_ctx, 999, "AX 302-8") is None


# --------------------------------------------- any-supplier lookup / catalog grid


def test_find_product_by_any_supplier_code_resolves_without_supplier_id(shop_ctx):
    """El código resuelve sin conocer el proveedor: el mapeo más antiguo gana."""
    shop_ctx.add(
        Supplier(id=2, code="AMX", business_name="Tornimax", default_margin_pct=Decimal(0))
    )
    shop_ctx.flush()
    # Two suppliers map the same code to the same product; earliest id wins.
    record_supplier_sku(shop_ctx, 1, "AX 302-8", "MSA-CLV-001")
    record_supplier_sku(shop_ctx, 2, "AX 302-8", "MSA-CLV-001")

    product = find_product_by_any_supplier_code(shop_ctx, "  ax  302-8 ")
    assert product is not None
    assert product.codigo_interno == "MSA-CLV-001"
    assert find_product_by_any_supplier_code(shop_ctx, "SM 302-8") is None
    assert find_product_by_any_supplier_code(shop_ctx, "   ") is None


def test_primary_supplier_codes_prefers_highest_confidence_then_earliest_id(shop_ctx):
    """El mapeo primario es el de mayor confianza; a igualdad, el más antiguo."""
    record_supplier_sku(shop_ctx, 1, "LOW-1", "MSA-CLV-001", confidence=Decimal(50))
    record_supplier_sku(shop_ctx, 1, "HIGH-1", "MSA-CLV-001", confidence=Decimal(100))
    record_supplier_sku(shop_ctx, 1, "TIE-A", "MSA-CLV-001", confidence=Decimal(80))
    record_supplier_sku(shop_ctx, 1, "TIE-B", "MSA-CLV-001", confidence=Decimal(80))

    primary = primary_supplier_codes(shop_ctx, ["MSA-CLV-001", "MSA-NOPE"])
    assert primary == {"MSA-CLV-001": "HIGH-1"}  # unmapped SKU absent from the dict


def test_list_products_returns_supplier_code_and_primary_mapped_code(shop_ctx):
    """La grilla de catálogo trae proveedor + código mapeado primario por producto."""
    record_supplier_sku(shop_ctx, 1, "AX 302-8", "MSA-CLV-001")

    rows = list_products(shop_ctx)
    assert rows[0]["supplier_code"] == "MSA"
    assert rows[0]["supplier_sku_code"] == "AX 302-8"
    assert rows[0]["codigo_interno"] == "MSA-CLV-001"  # kept for internal use


def test_list_products_blank_supplier_code_when_unmapped(shop_ctx):
    """Producto sin mapeo: el código de proveedor del producto queda vacío."""
    rows = list_products(shop_ctx)
    assert rows[0]["supplier_code"] == "MSA"
    assert rows[0]["supplier_sku_code"] == ""


def test_resolve_product_code_prefers_exact_internal_sku(shop_ctx):
    """El SKU interno exacto gana aunque exista un mapeo que apunte a otro."""
    shop_ctx.add(
        Catalogo(
            id=2,
            codigo_interno="MSA-OTRO-99",
            supplier_id=1,
            nombre_oficial="Otro producto",
            costo_proveedor=Decimal("50.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("67.50"),
            sinonimos=[],
        )
    )
    record_supplier_sku(shop_ctx, 1, "MSA-CLV-001", "MSA-OTRO-99")  # misleading mapping

    product = resolve_product_code(shop_ctx, "MSA-CLV-001")
    assert product.codigo_interno == "MSA-CLV-001"  # exact internal match first

    mapped = resolve_product_code(shop_ctx, " msa-clv-001 ")
    assert mapped.codigo_interno == "MSA-OTRO-99"  # normalized supplier-code fallback


def test_resolve_product_code_error_mentions_both_options(shop_ctx):
    """Código desconocido: el error menciona SKU interno y código de proveedor."""
    with pytest.raises(KeyError, match="SKU interno|código de proveedor"):
        resolve_product_code(shop_ctx, "ZZZ-404")  # other supplier


# ------------------------------------------------------- adoption hook (DB)


def test_adopt_product_records_supplier_mapping(shop_ctx):
    """Adoptar un producto registra el codigo_orig del RAG como mapeo."""
    dto = AdoptRequest(
        sku="AT-5044",
        nombre="Tarugo Fischer 8mm",
        codigo_proveedor="MSA",
        precio=95.0,
        node_id="node-1",
        stock=5,
    )
    product = adopt_product(shop_ctx, dto, OwnerContext(owner_id="t"), _embedder())

    mapping = shop_ctx.scalar(
        select(SupplierSkuMapping).where(SupplierSkuMapping.supplier_sku_code == "AT-5044")
    )
    assert mapping is not None
    assert mapping.supplier_id == 1
    assert mapping.internal_sku == product.codigo_interno == "MSA-AT-5044"
    assert mapping.raw_description == "Tarugo Fischer 8mm"


# ------------------------------------------------- ingestion detection (DB)


def test_ingest_second_remito_different_code_bumps_stock_not_duplicate(shop_ctx):
    """[bug owner] Segundo remito con otro código del mismo producto → bump, no duplicado.

    Flujo real del owner: el primer remito trae "SM 302-8" resuelto por RAG a
    un producto cuyo codigo_orig es "AX 302-8"; la adopción registra AMBOS
    códigos. El segundo remito trae "AX 302-8" sin candidatos RAG y debe
    golpear el mapeo (no build_sku) y hacer bump del stock existente.
    """
    session = shop_ctx
    first = ResolvedLine(
        receipt=_receipt("SM 302-8", cantidad=4),
        product=_rag_product(sku="AX 302-8", node_id="node-1"),
    )
    result = ingest_receipt_lines(session, 1, [first], OwnerContext(owner_id="t"), _embedder())
    assert result.created == 1  # adopted MSA-SM-302-8

    second = ResolvedLine(
        receipt=_receipt("AX 302-8", cantidad=3),
        product=None,
        pending_reason=PendingReason.NO_CANDIDATES,
    )
    result = ingest_receipt_lines(session, 1, [second], OwnerContext(owner_id="t"), _embedder())
    assert result.created == 0  # NO duplicate product
    assert result.updated == 1

    skus = [
        p.codigo_interno
        for p in session.scalars(
            select(Catalogo).where(Catalogo.codigo_interno == "MSA-SM-302-8")
        ).all()
    ]
    assert skus == ["MSA-SM-302-8"]  # single row, no duplicate
    inventory = session.scalar(select(Inventory).where(Inventory.sku_id == "MSA-SM-302-8"))
    assert inventory.quantity_on_hand == 7  # 4 + 3 bumped
    mapping = session.scalar(
        select(SupplierSkuMapping).where(SupplierSkuMapping.supplier_sku_code == "AX 302-8")
    )
    assert mapping.internal_sku == "MSA-SM-302-8"


def test_ingest_assign_to_existing_records_receipt_code_mapping(shop_ctx):
    """[assign-to-existing] Hit por el sku del RAG registra el código del remito."""
    session = shop_ctx
    record_supplier_sku(session, 1, "CLV-001", "MSA-CLV-001")
    line = ResolvedLine(
        receipt=_receipt("SM 302-8", cantidad=2),
        product=_rag_product(sku="CLV-001", node_id="node-2", name="Clavos Paris 2 Pulgadas"),
    )
    result = ingest_receipt_lines(session, 1, [line], OwnerContext(owner_id="t"), _embedder())

    assert result.updated == 1
    assert result.created == 0
    inventory = session.scalar(select(Inventory).where(Inventory.sku_id == "MSA-CLV-001"))
    assert inventory.quantity_on_hand == 2
    mapping = session.scalar(
        select(SupplierSkuMapping).where(SupplierSkuMapping.supplier_sku_code == "SM 302-8")
    )
    assert mapping.internal_sku == "MSA-CLV-001"


def test_ingest_adopt_new_records_rag_and_receipt_codes(shop_ctx):
    """[adopt-new] La adopción desde RAG registra el sku del RAG Y el del remito."""
    session = shop_ctx
    line = ResolvedLine(
        receipt=_receipt("SM 302-8", cantidad=4),
        product=_rag_product(sku="AX 302-8", node_id="node-1"),
    )
    result = ingest_receipt_lines(session, 1, [line], OwnerContext(owner_id="t"), _embedder())
    assert result.created == 1

    codes = {
        m.supplier_sku_code: m.internal_sku
        for m in session.scalars(select(SupplierSkuMapping)).all()
    }
    assert codes == {"AX 302-8": "MSA-SM-302-8", "SM 302-8": "MSA-SM-302-8"}


def test_ingest_adopt_new_same_code_records_single_mapping(shop_ctx):
    """[adopt-new] Cuando el código del remito iguala el del RAG, un solo mapeo."""
    session = shop_ctx
    line = ResolvedLine(
        receipt=_receipt("AT-5044", cantidad=4),
        product=_rag_product(sku="AT-5044", node_id="node-1"),
    )
    ingest_receipt_lines(session, 1, [line], OwnerContext(owner_id="t"), _embedder())

    mappings = session.scalars(select(SupplierSkuMapping)).all()
    assert [(m.supplier_sku_code, m.internal_sku) for m in mappings] == [
        ("AT-5044", "MSA-AT-5044")
    ]


def test_ingest_adopt_from_document_records_receipt_code(shop_ctx):
    """[adopt-document] La adopción sin candidatos registra el código del remito."""
    session = shop_ctx
    line = ResolvedLine(
        receipt=_receipt("AT-5044", cantidad=4),
        product=None,
        pending_reason=PendingReason.NO_CANDIDATES,
    )
    result = ingest_receipt_lines(session, 1, [line], OwnerContext(owner_id="t"), _embedder())
    assert result.created == 1

    mapping = session.scalar(
        select(SupplierSkuMapping).where(SupplierSkuMapping.supplier_sku_code == "AT-5044")
    )
    assert mapping.internal_sku == "MSA-AT-5044"
    assert mapping.raw_description == "Tarugo Fischer 8mm"


# --------------------------------------------------------- search (DB)


def test_search_order_products_finds_product_by_mapped_supplier_code(rag_table, shop_ctx):
    """[bug owner] Buscar por "AX 302-8" encuentra el producto adoptado como SM 302-8."""
    session = shop_ctx
    session.add(
        Catalogo(
            id=2,
            codigo_interno="MSA-SM-302-8",
            supplier_id=1,
            nombre_oficial="Inodoro GENERICA",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            sinonimos=[],
        )
    )
    record_supplier_sku(session, 1, "AX 302-8", "MSA-SM-302-8")
    session.flush()

    hits = search_order_products(session, codigo="AX 302-8")
    local = [h for h in hits if h.source == "LOCAL"]
    assert [h.sku for h in local] == ["MSA-SM-302-8"]


def test_search_order_products_scopes_mapping_to_proveedor_filter(rag_table, shop_ctx):
    """Con filtro de proveedor, el mapeo cuenta solo para el proveedor del producto."""
    session = shop_ctx
    session.add(
        Supplier(id=2, code="AMX", business_name="Tornimax", default_margin_pct=Decimal(0))
    )
    # Mapping owned by supplier 2 (AMX) pointing at supplier 1's product.
    session.add(
        SupplierSkuMapping(
            supplier_id=2,
            supplier_sku_code="ZZ-1",
            internal_sku="MSA-CLV-001",
            confidence=Decimal(100),
        )
    )
    session.flush()

    # Without proveedor: the mapping matches regardless of owner.
    hits = search_order_products(session, codigo="ZZ-1")
    assert [h.sku for h in hits if h.source == "LOCAL"] == ["MSA-CLV-001"]
    # With proveedor=MSA: the mapping belongs to AMX, not to the product's
    # supplier → the mapping leg must NOT match.
    hits = search_order_products(session, codigo="ZZ-1", proveedor="MSA")
    assert [h.sku for h in hits if h.source == "LOCAL"] == []


def test_search_order_products_display_code_maps_local_keeps_rag(rag_table, db_engine, shop_ctx):
    """LOCAL muestra el código mapeado (o el SKU interno sin mapeo); RAG no cambia."""
    session = shop_ctx
    session.add(
        Catalogo(
            id=2,
            codigo_interno="MSA-SM-302-8",
            supplier_id=1,
            nombre_oficial="Inodoro GENERICA",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("35.00"),
            precio_lista_base=Decimal("135.00"),
            sinonimos=[],
        )
    )
    record_supplier_sku(session, 1, "AX 302-8", "MSA-SM-302-8")
    session.flush()
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO catalogo_productos_rag "
                "(node_id, codigo_producto, codigo_proveedor, precio, moneda) "
                "VALUES ('node-rag-1', 'AT-999', 'AMX', 10, 'ARS')"
            )
        )

    hits = search_order_products(session, codigo="MSA-")
    local = {h.sku: h.display_code for h in hits if h.source == "LOCAL"}
    assert local == {
        "MSA-CLV-001": "MSA-CLV-001",  # unmapped: falls back to the internal SKU
        "MSA-SM-302-8": "AX 302-8",  # mapped: supplier code is displayed
    }

    rag_hits = search_order_products(session, proveedor="AMX")
    assert all(h.source == "RAG" for h in rag_hits)
    assert all(h.display_code == h.sku for h in rag_hits)  # RAG keeps its own code
