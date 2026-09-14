"""Tests for the unified Productos tab search (LOCAL catalog + PROV RAG).

Two layers, mirroring the established backoffice test patterns:

- Handler layer: ``_productos_search`` runs against the disposable test
  database with a minimal ``catalogo_productos_rag`` table (created by the
  rag-api service in real deployments; the Alembic schema does not own it, so
  the fixture creates and drops it here). Handlers open their own
  ``SessionLocal`` and only see committed rows, so the seed is committed.
- Tree layer: the Productos tab exposes the unified filter section (six
  filters + scope + Buscar) and the legacy "Buscar en RAG" section is GONE.

Use-case semantics (scope legs, vector path, post-filtering) live in
``tests/test_manual_order_product_search.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

import src.backoffice.app as app_module
from src.backoffice.app import _productos_search, build_app
from src.config import get_settings
from src.db.models import Catalogo, Inventory, Supplier
from src.integrations.rag import RagProduct, RagProductError
from src.sourcing.product_search import rag_products_table

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

# (codigo, codigo_orig, proveedor, nombre_proveedor, marca, categoria_padre,
#  categoria, subcategoria, precio, moneda, pagina, archivo, text_content)
_ROWS = [
    (
        "node_prod_SCO_SM-483-8", "SM 483-8", "SM 483-8", "Sanitarios del Centro", "SCO",
        "GENERICA", "Sanitarios y Grifería", "Griferías", "Monocomandos de cocina",
        15.0, "USD", 58, "lista-sco.pdf",
        "proveedor: SCON codigo_proveedor: SCO monocomando de cocina acero",
    ),
    (
        "node_prod_AMX_AT-5044", "AT-5044", "AT-5044", "Tornimax", "AMX",
        "Fischer", "Fijaciones", "Tarugos", "Plástico",
        135.5, "ARS", 12, "lista-amx.pdf",
        "codigo_proveedor: AMX tarugo de nylon 8mm",
    ),
]

# (id, codigo_interno, supplier_id, nombre, costo, margen, base, marca,
#  categoria, subcategoria, stock)
_LOCAL_ROWS = [
    (1, "AT-5044", 2, "Tarugo de nylon 8mm", "50.00", "0.20", "60.00",
     "Fischer", "Fijaciones", "Tarugos", 3),
    (2, "CLV-001", 1, "Grifería monocomando de cocina", "100.00", "0.35", "135.00",
     "FV", "Griferías", "Cocina", 3),
]


@pytest.fixture(autouse=True)
def _clean_schema(clean_schema):
    """Truncate the sourcing tables after each test (handlers commit their seed)."""
    yield


@pytest.fixture()
def rag_table(db_engine):
    """Create the minimal RAG table on the test database and drop it after."""
    with db_engine.begin() as conn:
        conn.execute(text(_CREATE_RAG_TABLE))
    yield
    with db_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS catalogo_productos_rag"))


def _seed(db_session) -> None:
    """Seed suppliers, local catalog rows and indexed RAG rows (committed)."""
    db_session.add(Supplier(id=1, code="SUP", business_name="Supplier", default_margin_pct=Decimal(0)))
    db_session.add(Supplier(id=2, code="AMX", business_name="Tornimax", default_margin_pct=Decimal(0)))
    for (
        pid, codigo, supplier_id, nombre, costo, margen, base, marca, categoria, subcategoria, stock
    ) in _LOCAL_ROWS:
        db_session.add(
            Catalogo(
                id=pid,
                codigo_interno=codigo,
                supplier_id=supplier_id,
                nombre_oficial=nombre,
                costo_proveedor=Decimal(costo),
                margen_aplicado_pct=Decimal(margen),
                precio_lista_base=Decimal(base),
                sinonimos=[],
                marca=marca,
                categoria=categoria,
                subcategoria=subcategoria,
            )
        )
        db_session.add(Inventory(sku_id=codigo, quantity_on_hand=stock))
    columns = (
        "node_id", "codigo_producto", "codigo_orig", "nombre_proveedor", "codigo_proveedor",
        "marca", "categoria_padre", "categoria", "subcategoria", "precio", "moneda",
        "pagina_origen", "archivo_origen", "text_content",
    )
    table = rag_products_table(get_settings().rag_table_name)
    db_session.execute(table.insert().values([dict(zip(columns, row, strict=True)) for row in _ROWS]))
    db_session.commit()


class _FakeVectorRag:
    """RagProductClient stand-in with canned vector products (records queries)."""

    def __init__(self, products: tuple[RagProduct, ...]) -> None:
        self.products = products
        self.calls: list[str] = []

    def query(self, text: str) -> tuple[RagProduct, ...]:
        self.calls.append(text)
        return self.products


class _FailingVectorRag:
    """RagProductClient stand-in whose vector query is unavailable."""

    def query(self, text: str) -> tuple[RagProduct, ...]:
        raise RagProductError(f"rag query failed for {text!r}: down")


def _rag_product(**overrides: Any) -> RagProduct:
    base: dict[str, Any] = {
        "sku": "SM 483-8",
        "name": "Monocomando de cocina acero",
        "codigo_proveedor": "SCO",
        "brand": "GENERICA",
        "price": 15.0,
        "currency": "USD",
        "categoria_padre": "Sanitarios y Grifería",
        "categoria": "Griferías",
        "subcategoria": "Monocomandos de cocina",
    }
    base.update(overrides)
    return RagProduct(**base)


# ------------------------------------------------------------- handler layer


def test_productos_search_requires_filter(rag_table, db_session):
    """Sin ningún filtro el handler muestra el error y no rompe."""
    grid, status = _productos_search("", "", "", "", "", "", "both")
    assert grid == []
    assert status.startswith("Error: ")
    assert "al menos un filtro" in status


def test_productos_search_lists_local_first_then_prov(rag_table, db_session):
    """LOCAL rows come first (Origen=LOCAL), RAG rows follow tagged PROV."""
    _seed(db_session)
    grid, status = _productos_search("", " amx ", "", "", "", "", "both")

    assert [row[0] for row in grid] == ["LOCAL", "PROV"]
    local, prov = grid
    # Catalog-grid column shape: Origen, Proveedor, Código, Nombre, Marca,
    # Stock, Moneda, Costo, Precio lista (AR$), Margen, Categoría, Subcategoría.
    assert local[:5] == ["LOCAL", "AMX", "AT-5044", "Tarugo de nylon 8mm", "Fischer"]
    assert local[5] == 3 and local[6:9] == ["ARS", "50.00", "60.00"]
    assert local[9:] == ["0.20", "Fijaciones", "Tarugos"]
    assert prov[0] == "PROV" and prov[2] == "AT-5044"
    assert prov[3] == "Fischer Tarugos Plástico"  # composed from RAG metadata
    assert prov[5] == ""  # PROV rows carry no stock
    assert prov[6:9] == ["ARS", "135.5", ""]  # offer price lands in Costo
    assert prov[9:] == ["", "Tarugos", "Plástico"]
    assert status == "2 producto(s) encontrado(s)."


def test_productos_search_scope_prov_skips_local_rows(rag_table, db_session):
    """Scope PROV runs only the RAG leg: every row is tagged PROV."""
    _seed(db_session)
    grid, status = _productos_search("", "AMX", "", "", "", "", "prov")
    assert [row[0] for row in grid] == ["PROV"]
    assert "1 producto(s) encontrado(s)." in status


def test_productos_search_scope_local_filters_by_nombre(rag_table, db_session):
    """Scope LOCAL resolves nombre as an accent-folded substring of the name."""
    _seed(db_session)
    grid, _status = _productos_search("", "", "", "", "", "monocomando", "local")
    assert [row[0] for row in grid] == ["LOCAL"]
    assert grid[0][2] == "CLV-001"
    assert grid[0][3] == "Grifería monocomando de cocina"


def test_productos_search_empty_result_message(rag_table, db_session):
    """Sin coincidencias: grilla vacía y mensaje explícito."""
    _seed(db_session)
    grid, status = _productos_search("INEXISTENTE-99", "", "", "", "", "", "both")
    assert grid == []
    assert status == "Sin resultados para los filtros indicados."


def test_productos_search_vector_failure_falls_back_to_sql(rag_table, db_session, monkeypatch):
    """When the vector service is down the leg degrades to SQL and notes it."""
    _seed(db_session)
    monkeypatch.setattr(app_module, "RagProductClient", lambda: _FailingVectorRag())
    grid, status = _productos_search("", "", "", "", "", "monocomando", "prov")
    assert [row[0] for row in grid] == ["PROV"]
    assert grid[0][2] == "SM 483-8"  # seeded text_content matches the nombre
    assert "similitud no disponible" in status


def test_productos_search_vector_results_render_as_prov(rag_table, db_session, monkeypatch):
    """Vector hits from the rag-api render in the unified grid tagged PROV."""
    _seed(db_session)
    fake = _FakeVectorRag((_rag_product(),))
    monkeypatch.setattr(app_module, "RagProductClient", lambda: fake)
    grid, status = _productos_search("", "", "", "", "", "monocomando", "prov")

    assert fake.calls == ["monocomando"]
    row = grid[0]
    assert row[0] == "PROV"
    assert row[1:5] == ["SCO", "SM 483-8", "Monocomando de cocina acero", "GENERICA"]
    assert row[5] == "" and row[6:9] == ["USD", "15.0", ""]
    assert row[9:] == ["", "Griferías", "Monocomandos de cocina"]
    assert status == "1 producto(s) encontrado(s)."


# ---------------------------------------------------------------- tree layer


def _tabs_block(demo) -> object:
    """The Tabs layout inside the Blocks tree (ignoring Markdown siblings)."""
    return next(c for c in demo.children if type(c).__name__ == "Tabs")


def _all_labels(block) -> set:
    """Collect every descendant component label of a Blocks subtree."""
    labels: set = set()
    for child in getattr(block, "children", ()):
        if type(child).__name__ == "Button":
            labels.add(getattr(child, "value", None))
        else:
            labels.add(getattr(child, "label", None))
        labels |= _all_labels(child)
    return labels


def _all_components(block, type_name: str) -> list:
    """Collect every descendant component of the given Gradio class name."""
    found = []
    for child in getattr(block, "children", ()):
        if type(child).__name__ == type_name:
            found.append(child)
        found.extend(_all_components(child, type_name))
    return found


def test_build_app_productos_tab_has_unified_search_section():
    """El tab Productos expone los filtros unificados, el ámbito y el botón."""
    demo = build_app()
    tab = next(t for t in _tabs_block(demo).children if t.label == "Productos")
    labels = _all_labels(tab)
    assert "Código" in labels
    assert "Proveedor (código)" in labels
    assert "Marca" in labels
    assert "Categoría" in labels
    assert "Subcategoría" in labels
    assert "Nombre" in labels
    assert "Ámbito" in labels
    assert "Buscar" in labels
    assert "Estado búsqueda" in labels
    assert "Productos" in labels  # unified results grid


def test_build_app_productos_tab_scope_defaults_to_ambas():
    """El selector de ámbito ofrece exactamente las tres opciones (Ambas default)."""
    demo = build_app()
    tab = next(t for t in _tabs_block(demo).children if t.label == "Productos")
    radios = _all_components(tab, "Radio")
    assert len(radios) == 1
    radio = radios[0]
    assert [(label, value) for label, value in radio.choices] == [
        ("Ambas", "both"),
        ("Local", "local"),
        ("Catálogo de proveedores", "prov"),
    ]
    assert radio.value == "both"


def test_build_app_productos_tab_has_no_rag_section():
    """La sección RAG de la pestaña Productos ya no existe."""
    demo = build_app()
    tab = next(t for t in _tabs_block(demo).children if t.label == "Productos")
    labels = _all_labels(tab)
    assert "Buscar en RAG" not in labels
    assert "Resultados RAG (Productos)" not in labels
    assert "Estado RAG" not in labels
    assert "Texto en descripción (substring)" not in labels
    assert "Máx. resultados" not in labels
