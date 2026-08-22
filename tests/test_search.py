"""Hybrid catalog search tests (tasks 2.4 / Phase 4.5).

Integration tests against the real Postgres + pgvector fixture: informal names
and misspellings resolve to the right product through the fuzzy channel, and
the vector channel auto-maps (or builds a menu) from embedding similarity.
Skipped cleanly when Postgres is not running.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from src.agents.disambiguation import ResolutionKind, resolve_item, search_catalog
from src.config import get_settings
from src.db.models import Catalogo, Proveedor

EMBED_DIMS = 1536


def _embed(*coords: float) -> list[float]:
    """Build a `vector(1536)`-compatible embedding from leading coordinates."""
    vector = [0.0] * EMBED_DIMS
    for index, value in enumerate(coords):
        vector[index] = value
    return vector


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


@pytest.fixture
def catalog(db_session):
    """Seed the fixture catalog: two 2\" nails (Paris vs Espiralado) + cement."""
    db_session.add(
        Proveedor(
            proveedor_id=1,
            razon_social="Proveedor Test",
            margen_predeterminado=Decimal(0),
        )
    )
    db_session.add_all(
        [
            Catalogo(
                id=1,
                codigo_interno="CLV-PRS-2",
                proveedor_id=1,
                nombre_oficial="Clavos Paris 2 Pulgadas (50mm)",
                costo_proveedor=Decimal("100.00"),
                margen_aplicado_pct=Decimal("0.35"),
                precio_lista_base=Decimal("135.00"),
                stock_disponible=50,
                sinonimos=["clavo paris 2", "clavos 2 pulgadas", "clavos paris 2 pulgadas"],
            ),
            Catalogo(
                id=2,
                codigo_interno="CLV-ESP-2",
                proveedor_id=1,
                nombre_oficial="Clavos Espiralados 2 Pulgadas",
                costo_proveedor=Decimal("80.00"),
                margen_aplicado_pct=Decimal("0.30"),
                precio_lista_base=Decimal("104.00"),
                stock_disponible=30,
                sinonimos=["clavo espiralado 2"],
            ),
            Catalogo(
                id=3,
                codigo_interno="CMT-PRT-50",
                proveedor_id=1,
                nombre_oficial="Cemento Portland 50kg",
                costo_proveedor=Decimal("1200.00"),
                margen_aplicado_pct=Decimal("0.25"),
                precio_lista_base=Decimal("1500.00"),
                stock_disponible=20,
                sinonimos=["cemento portland"],
            ),
        ]
    )
    db_session.flush()
    return db_session


def test_informal_name_auto_maps_to_right_product(catalog):
    """Un nombre informal se mapea automáticamente al producto correcto.

    "clavos de 2 pulgadas" resolves to Clavos Paris 2 Pulgadas, no prompt.
    """
    resolution = resolve_item(catalog, "clavos de 2 pulgadas")
    assert resolution.kind is ResolutionKind.AUTO_MAPPED
    assert resolution.candidate is not None
    assert resolution.candidate.sku == "CLV-PRS-2"
    assert resolution.candidate.confidence >= 0.85


def test_misspelling_resolves_to_right_product(catalog):
    """Un nombre con errores de tipeo igual recupera el producto.

    A misspelled request still recovers the intended catalog entry.
    """
    resolution = resolve_item(catalog, "clavos paris 2 pulgads")
    assert resolution.kind is ResolutionKind.AUTO_MAPPED
    assert resolution.candidate is not None
    assert resolution.candidate.sku == "CLV-PRS-2"


def test_exact_synonym_auto_maps_unambiguously(catalog):
    """Un sinónimo exacto mapea sin ambigüedad al SKU oficial.

    A curated synonym maps cleanly to its official SKU at full confidence.
    """
    resolution = resolve_item(catalog, "clavos 2 pulgadas")
    assert resolution.kind is ResolutionKind.AUTO_MAPPED
    assert resolution.candidate is not None
    assert resolution.candidate.sku == "CLV-PRS-2"
    assert resolution.candidate.confidence == pytest.approx(1.0)


def test_unnormalized_input_still_resolves(catalog):
    """Mayúsculas, puntuación y espacios extra no rompen la resolución.

    Caps, punctuation and extra whitespace do not break resolution.
    """
    resolution = resolve_item(catalog, "  CLAVOS DE 2 PULGADAS! ")
    assert resolution.kind is ResolutionKind.AUTO_MAPPED
    assert resolution.candidate is not None
    assert resolution.candidate.sku == "CLV-PRS-2"


def test_search_ranks_right_product_first(catalog):
    """La búsqueda híbrida rankea primero el producto objetivo.

    Hybrid search returns the target product as the top candidate.
    """
    candidates = search_catalog(catalog, "clavos de 2 pulgadas")
    assert candidates[0].sku == "CLV-PRS-2"
    assert candidates[0].confidence >= 0.85


def test_low_confidence_single_candidate_presents_menu(catalog):
    """Un único candidato bajo el umbral no se adivina: presenta menú.

    A lone candidate below the auto-map threshold is not silently guessed.
    """
    resolution = resolve_item(catalog, "paris 2 pulgadas")
    assert resolution.kind is ResolutionKind.AMBIGUOUS
    assert resolution.candidate is None
    assert len(resolution.candidates) == 1
    assert resolution.candidates[0].sku == "CLV-PRS-2"
    assert resolution.candidates[0].confidence < 0.85


def test_no_match_is_reported(catalog):
    """Una consulta sin coincidencia se reporta como NO_ENCONTRADO.

    A query matching nothing yields NOT_FOUND, not a fabricated guess.
    """
    resolution = resolve_item(catalog, "taladro inalambrico")
    assert resolution.kind is ResolutionKind.NOT_FOUND
    assert resolution.candidate is None
    assert resolution.candidates == ()


def test_vector_auto_maps_when_fuzzy_is_weak(catalog):
    """La similitud vectorial rankea correcto cuando el fuzzy es débil.

    pgvector similarity ranks the correct product above unrelated ones.
    """
    catalog.get(Catalogo, 1).embedding = _embed(1.0, 0.0)
    catalog.get(Catalogo, 2).embedding = _embed(0.0, 1.0)
    catalog.flush()
    resolution = resolve_item(catalog, "clavo", embedding=_embed(0.95, 0.1))
    assert resolution.kind is ResolutionKind.AUTO_MAPPED
    assert resolution.candidate is not None
    assert resolution.candidate.sku == "CLV-PRS-2"


def test_vector_ambiguity_presents_two_candidate_menu(catalog):
    """Embeedings equidistantes presentan un menú de dos candidatos.

    Equidistant embeddings produce a numbered two-candidate menu.
    """
    catalog.get(Catalogo, 1).embedding = _embed(1.0, 0.0)
    catalog.get(Catalogo, 2).embedding = _embed(0.0, 1.0)
    catalog.flush()
    resolution = resolve_item(catalog, "clavo", embedding=_embed(0.7, 0.7))
    assert resolution.kind is ResolutionKind.AMBIGUOUS
    assert {c.sku for c in resolution.candidates} == {"CLV-PRS-2", "CLV-ESP-2"}
