"""Sourcing case classification tests (task 3.5).

Pure unit tests over ``classify_case``: full stock → Case A, quantity above
availability → missing (Case B when a supplier exists, Case C when none does),
unknown SKU treated as missing, and empty orders never classified.
"""

from __future__ import annotations

from src.orchestrator.session import ResolvedItem
from src.sourcing.classify import SourcingCase, classify_case
from src.supplier.searcher import (
    FakeSupplierCatalogSearcher,
    SupplierCandidate,
)


def _availability(mapping: dict[str, int]):
    return lambda sku: mapping.get(sku, 0)


CLAVOS = SupplierCandidate(
    supplier_id=1, business_name="Proveedor X", sku="CLV-001",
    description="Clavos Paris 2 Pulgadas", available_quantity=50,
)
PINTURA = SupplierCandidate(
    supplier_id=2, business_name="Pinturería Y", sku="PINT-001",
    description="Pintura Látex Blanco", available_quantity=20,
)


def test_full_stock_is_case_a():
    """Todo el stock cubre el pedido → Caso A (PENDING_ASSEMBLY)."""
    items = (ResolvedItem(sku="CLV-001", cantidad=4),)
    searcher = FakeSupplierCatalogSearcher((CLAVOS,))
    decision = classify_case(items, _availability({"CLV-001": 10}), searcher)
    assert decision.case is SourcingCase.A
    assert decision.missing == ()


def test_partial_stock_with_supplier_is_case_b():
    """Falta stock pero hay proveedor → Caso B."""
    items = (ResolvedItem(sku="CLV-001", cantidad=10),)
    searcher = FakeSupplierCatalogSearcher((CLAVOS,))
    decision = classify_case(items, _availability({"CLV-001": 4}), searcher)
    assert decision.case is SourcingCase.B
    assert len(decision.missing) == 1
    missing = decision.missing[0]
    assert missing.sku == "CLV-001"
    assert missing.missing_quantity == 6  # 10 requested − 4 on hand
    assert missing.candidates == (CLAVOS,)


def test_missing_item_with_no_supplier_is_case_c():
    """Falta stock y no hay proveedor → Caso C."""
    items = (ResolvedItem(sku="CLV-001", cantidad=10),)
    decision = classify_case(items, _availability({"CLV-001": 4}), FakeSupplierCatalogSearcher())
    assert decision.case is SourcingCase.C
    assert decision.missing[0].candidates == ()


def test_unknown_sku_is_treated_as_missing():
    """Un SKU desconocido (sin inventario) se trata como faltante, nunca se cae."""
    items = (ResolvedItem(sku="OTRO-999", cantidad=2),)
    decision = classify_case(items, _availability({}), FakeSupplierCatalogSearcher((CLAVOS,)))
    assert decision.case is SourcingCase.C  # no supplier for it
    assert decision.missing[0].sku == "OTRO-999"
    assert decision.missing[0].missing_quantity == 2


def test_mixed_items_any_no_supplier_forces_case_c():
    """Si un faltante no tiene proveedor, el pedido entero es Caso C."""
    items = (
        ResolvedItem(sku="CLV-001", cantidad=10),  # supplier exists
        ResolvedItem(sku="PINT-001", cantidad=5),  # no supplier
    )
    searcher = FakeSupplierCatalogSearcher((CLAVOS,))
    decision = classify_case(items, _availability({}), searcher)
    assert decision.case is SourcingCase.C
    assert {m.sku for m in decision.missing} == {"CLV-001", "PINT-001"}


def test_search_by_description_finds_candidates_for_unknown_sku():
    """La búsqueda por descripción encuentra proveedores para SKU desconocido."""
    items = (ResolvedItem(sku="pintura latex blanco", cantidad=2, description="pintura latex blanco"),)
    searcher = FakeSupplierCatalogSearcher((PINTURA,))
    decision = classify_case(items, _availability({}), searcher)
    assert decision.case is SourcingCase.B
    assert decision.missing[0].candidates == (PINTURA,)


def test_exact_quantity_available_is_not_missing():
    """Cantidad exactamente igual al stock disponible no genera faltante."""
    items = (ResolvedItem(sku="CLV-001", cantidad=10),)
    decision = classify_case(items, _availability({"CLV-001": 10}), FakeSupplierCatalogSearcher())
    assert decision.case is SourcingCase.A


def test_empty_items_are_case_a_without_missing():
    """Un pedido vacío no clasifica como faltante (el flujo pide artículos)."""
    decision = classify_case((), _availability({}), FakeSupplierCatalogSearcher())
    assert decision.case is SourcingCase.A
    assert decision.missing == ()