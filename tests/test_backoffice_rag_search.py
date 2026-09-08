"""Tests for the RAG catalog search in the Productos tab.

Two layers, mirroring the established backoffice test patterns:

- SQL layer: ``search_rag_products`` runs against the disposable test database
  with a minimal ``catalogo_productos_rag`` table (created by the rag-api
  service in real deployments; the Alembic schema does not own it, so the
  fixture creates and drops it here).
- Handler layer: ``_rag_search`` seeds via ``SessionLocal`` + explicit commit
  (handlers open their own SessionLocal and only see committed rows).
- Tree layer: the Productos tab exposes the filter inputs and results grid.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.backoffice.app import _rag_search, build_app
from src.backoffice.catalog import _rag_products_table, search_rag_products
from src.config import get_settings
from src.db.session import SessionLocal

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
        "node_prod_SCO_SM-483-9", "SM 483-9", "SM 483-9", "Sanitarios del Centro", "SCO",
        "GENERICA", "Sanitarios y Grifería", "Griferías", "Monocomandos de pared",
        14.5, "USD", 62, "lista-sco.pdf",
        "proveedor: SCON codigo_proveedor: SCO monocomando de pared",
    ),
    (
        "node_prod_AMX_AT-5044", "AT-5044", "AT-5044", "Tornimax", "AMX",
        "Fischer", "Fijaciones", "Tarugos", "Plástico",
        135.5, "ARS", 12, "lista-amx.pdf",
        "codigo_proveedor: AMX tarugo de nylon 8mm",
    ),
]


@pytest.fixture()
def rag_table(db_engine):
    """Create the minimal RAG table on the test database and drop it after."""
    with db_engine.begin() as conn:
        conn.execute(text(_CREATE_RAG_TABLE))
    yield
    with db_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS catalogo_productos_rag"))


def _seed(session) -> None:
    table = _rag_products_table(get_settings().rag_table_name)
    columns = (
        "node_id", "codigo_producto", "codigo_orig", "nombre_proveedor", "codigo_proveedor",
        "marca", "categoria_padre", "categoria", "subcategoria", "precio", "moneda",
        "pagina_origen", "archivo_origen", "text_content",
    )
    session.execute(
        table.insert().values([dict(zip(columns, row, strict=True)) for row in _ROWS])
    )
    session.commit()


# ---------------------------------------------------------------- SQL layer


def test_search_rag_products_exact_proveedor_is_normalized(rag_table, db_session):
    """Proveedor filter is exact and case-insensitive ('sco' matches 'SCO')."""
    _seed(db_session)
    rows = search_rag_products(db_session, codigo_proveedor=" sco ")
    assert [r["codigo"] for r in rows] == ["SM 483-8", "SM 483-9"]
    assert all(r["proveedor"] == "SCO" for r in rows)


def test_search_rag_products_substring_filters(rag_table, db_session):
    """Marca, categoría (incl. padre), código y texto filtran por substring."""
    _seed(db_session)
    assert [r["codigo"] for r in search_rag_products(db_session, marca="fischer")] == ["AT-5044"]
    # categoría matchea también categoria_padre
    assert len(search_rag_products(db_session, categoria="Sanitarios")) == 2
    # categoría sin acento matchea "Griferías" (folding de acentos)
    assert len(search_rag_products(db_session, categoria="griferias")) == 2
    # código matchea codigo_orig con substring parcial
    assert [r["codigo"] for r in search_rag_products(db_session, codigo="483")] == [
        "SM 483-8",
        "SM 483-9",
    ]
    # texto sobre text_content
    rows = search_rag_products(db_session, texto="cocina")
    assert [r["codigo"] for r in rows] == ["SM 483-8"]
    assert rows[0]["precio"] == 15.0
    assert rows[0]["moneda"] == "USD"


def test_search_rag_products_combines_filters_with_and(rag_table, db_session):
    """Varios filtros se combinan con AND."""
    _seed(db_session)
    rows = search_rag_products(db_session, codigo_proveedor="SCO", texto="pared")
    assert [r["codigo"] for r in rows] == ["SM 483-9"]


def test_search_rag_products_requires_at_least_one_filter(rag_table, db_session):
    """Sin filtros se rechaza con error claro (nunca full scan accidental)."""
    _seed(db_session)
    with pytest.raises(ValueError, match="al menos un filtro"):
        search_rag_products(db_session)


def test_search_rag_products_respects_limit(rag_table, db_session):
    """El límite recorta la cantidad de filas devueltas."""
    _seed(db_session)
    rows = search_rag_products(db_session, codigo_proveedor="SCO", limit=1)
    assert len(rows) == 1


# ------------------------------------------------------------- handler layer


def test_rag_search_handler_maps_rows_and_status(rag_table):
    """El handler devuelve la grilla armada y el estado con el conteo."""
    with SessionLocal() as session:
        _seed(session)
    grid, status = _rag_search("SCO", "", "", "", "", 100)
    assert [row[0] for row in grid] == ["SM 483-8", "SM 483-9"]
    assert grid[0][:6] == ["SM 483-8", "SM 483-8", "SCO", "GENERICA", "Griferías", "Monocomandos de cocina"]
    assert grid[0][6] == 15.0 and grid[0][7] == "USD" and grid[0][8] == 58
    assert status == "2 producto(s) encontrado(s)."


def test_rag_search_handler_empty_results_message(rag_table):
    """Sin coincidencias: grilla vacía y mensaje explícito."""
    with SessionLocal() as session:
        _seed(session)
    grid, status = _rag_search("", "", "", "", "inexistente-xyz", 100)
    assert grid == []
    assert status == "Sin resultados para los filtros indicados."


def test_rag_search_handler_surfaces_validation_error(rag_table):
    """Sin ningún filtro el handler muestra el error y no rompe."""
    grid, status = _rag_search("", "", "", "", "", 100)
    assert grid == []
    assert status.startswith("Error: ")
    assert "al menos un filtro" in status


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


def test_build_app_productos_tab_has_rag_search_section():
    """El tab Productos expone los filtros, la búsqueda y la grilla RAG."""
    demo = build_app()
    tab = next(t for t in _tabs_block(demo).children if t.label == "Productos")
    labels = _all_labels(tab)
    assert "Proveedor (código)" in labels
    assert "Marca" in labels
    assert "Categoría" in labels
    assert "Código" in labels
    assert "Texto en descripción (substring)" in labels
    assert "Buscar en RAG" in labels
    assert "Resultados RAG (Productos)" in labels
    assert "Estado RAG" in labels
