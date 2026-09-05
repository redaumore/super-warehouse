"""RAG-backed supplier catalog searcher tests.

Unit + integration tests over ``RagSupplierCatalogSearcher``: RAG hits mapped
to real suppliers by ``codigo_proveedor``, INACTIVO/unknown providers dropped,
RAG failures degrading to an empty result (Case C safe path), dedupe, and the
classify wiring proving a missing item with a mapped provider → Case B.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from src.config import get_settings
from src.db.models import Supplier, SupplierStatus
from src.integrations.rag import RagProduct, RagProductError
from src.orchestrator.session import ResolvedItem
from src.sourcing.classify import SourcingCase, classify_case
from src.supplier.rag_searcher import RagSupplierCatalogSearcher


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


class FakeRagClient:
    """Configurable RAG stand-in: returns products or raises, and records calls."""

    def __init__(self, products: tuple[RagProduct, ...] = (), error: Exception | None = None):
        self.products = products
        self.error = error
        self.calls: list[str] = []

    def query(self, text: str) -> tuple[RagProduct, ...]:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return self.products


@pytest.fixture
def session_factory(db_engine):
    """Session factory bound to the test engine (the searcher opens its own sessions)."""
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def shop(db_session):
    """Two suppliers: FDN active, OFF inactive (soft-deleted)."""
    db_session.add(
        Supplier(
            id=1,
            code="FDN",
            business_name="Ferretería Don Nails",
            default_margin_pct=Decimal(0),
        )
    )
    db_session.add(
        Supplier(
            id=2,
            code="OFF",
            business_name="Off Supplier",
            default_margin_pct=Decimal(0),
            status=SupplierStatus.INACTIVO,
        )
    )
    db_session.commit()
    return db_session


CLAVOS = RagProduct(
    sku="CLV-001",
    name="Clavos Paris 2 Pulgadas",
    provider="Ferretería Don Nails",
    codigo_proveedor="FDN",
)


def test_description_search_maps_rag_hit_to_supplier(session_factory, shop):
    """A RAG hit with a known ACTIVO code becomes a supplier candidate."""
    searcher = RagSupplierCatalogSearcher(
        session_factory=session_factory, rag_client=FakeRagClient((CLAVOS,))
    )
    candidates = searcher.search(description="clavos paris 2 pulgadas")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.supplier_id == 1
    assert candidate.business_name == "Ferretería Don Nails"
    assert candidate.sku == "CLV-001"
    assert candidate.description == "Clavos Paris 2 Pulgadas"
    assert candidate.available_quantity is None  # RAG has no availability data
    assert candidate.status == "ACTIVO"


def test_inactive_supplier_is_dropped(session_factory, shop):
    """Seam contract: INACTIVO suppliers never surface as candidates."""
    inactive_hit = RagProduct(sku="OFF-1", name="Tornillo", provider="Off", codigo_proveedor="OFF")
    searcher = RagSupplierCatalogSearcher(
        session_factory=session_factory, rag_client=FakeRagClient((inactive_hit,))
    )
    assert searcher.search(description="tornillo") == ()


def test_unknown_supplier_code_is_dropped(session_factory, shop):
    """A RAG hit whose provider has no supplier row yields no candidate."""
    ghost = RagProduct(
        sku="GHO-1", name="Producto fantasma", provider="Ghost", codigo_proveedor="ZZZ"
    )
    searcher = RagSupplierCatalogSearcher(
        session_factory=session_factory, rag_client=FakeRagClient((ghost,))
    )
    assert searcher.search(description="producto fantasma") == ()


def test_last_unmapped_codes_starts_empty():
    """The diagnostic attribute exists and starts empty before any search."""
    searcher = RagSupplierCatalogSearcher(session_factory=Mock(), rag_client=FakeRagClient(()))
    assert searcher.last_unmapped_codes == ()


def test_last_unmapped_codes_records_unknown_code_and_resets_on_clean_search(session_factory, shop):
    """Dropped unknown codes are exposed and cleared by a later clean search."""
    ghost = RagProduct(
        sku="GHO-1", name="Producto fantasma", provider="Ghost", codigo_proveedor="SM"
    )
    searcher = RagSupplierCatalogSearcher(
        session_factory=session_factory, rag_client=FakeRagClient((ghost,))
    )
    assert searcher.search(description="mezcla fantasma") == ()
    assert searcher.last_unmapped_codes == ("SM",)

    searcher.rag_client = FakeRagClient((CLAVOS,))
    assert len(searcher.search(description="clavos paris 2 pulgadas")) == 1
    assert searcher.last_unmapped_codes == ()


def test_last_unmapped_codes_counts_inactive_drop(session_factory, shop):
    """An INACTIVO supplier drop surfaces in the diagnostic too."""
    inactive_hit = RagProduct(sku="OFF-1", name="Tornillo", provider="Off", codigo_proveedor="OFF")
    searcher = RagSupplierCatalogSearcher(
        session_factory=session_factory, rag_client=FakeRagClient((inactive_hit,))
    )
    assert searcher.search(description="tornillo") == ()
    assert searcher.last_unmapped_codes == ("OFF",)


def test_last_unmapped_codes_resets_even_when_rag_fails(session_factory, shop):
    """A degraded search (RAG down) also resets the diagnostic to empty."""
    searcher = RagSupplierCatalogSearcher(
        session_factory=session_factory,
        rag_client=FakeRagClient(error=RagProductError("rag down")),
    )
    searcher.last_unmapped_codes = ("OLD",)
    assert searcher.search(description="clavos") == ()
    assert searcher.last_unmapped_codes == ()


def test_rag_error_degrades_to_empty(session_factory, shop):
    """An unreachable RAG degrades to () (Case C safe path), never raises."""
    client = FakeRagClient(error=RagProductError("rag down"))
    searcher = RagSupplierCatalogSearcher(session_factory=session_factory, rag_client=client)
    assert searcher.search(description="clavos") == ()
    assert client.calls == ["clavos"]


def test_sku_only_search_queries_with_the_sku(session_factory, shop):
    """Without description the SKU is the query text (stripped)."""
    client = FakeRagClient((CLAVOS,))
    searcher = RagSupplierCatalogSearcher(session_factory=session_factory, rag_client=client)
    candidates = searcher.search(sku="  CLV-001  ")
    assert client.calls == ["CLV-001"]
    assert len(candidates) == 1
    assert candidates[0].supplier_id == 1


def test_duplicate_supplier_sku_hits_are_deduped(session_factory, shop):
    """The same (supplier, sku) appearing twice in RAG output yields one candidate."""
    client = FakeRagClient((CLAVOS, CLAVOS))
    searcher = RagSupplierCatalogSearcher(session_factory=session_factory, rag_client=client)
    candidates = searcher.search(description="clavos")
    assert len(candidates) == 1
    assert candidates[0].sku == "CLV-001"


def test_no_text_returns_empty_without_calling_client(session_factory, shop):
    """Neither SKU nor description: no query is issued."""
    client = FakeRagClient((CLAVOS,))
    searcher = RagSupplierCatalogSearcher(session_factory=session_factory, rag_client=client)
    assert searcher.search() == ()
    assert client.calls == []


def test_missing_item_with_mapped_provider_classifies_case_b(session_factory, shop):
    """Wiring: a missing item with a RAG-mapped provider classifies as Case B."""
    searcher = RagSupplierCatalogSearcher(
        session_factory=session_factory, rag_client=FakeRagClient((CLAVOS,))
    )
    items = (ResolvedItem(sku="CLV-001", cantidad=10, description="clavos 2 pulgadas"),)
    decision = classify_case(items, lambda sku: 0, searcher)
    assert decision.case is SourcingCase.B
    assert decision.missing[0].candidates[0].supplier_id == 1
