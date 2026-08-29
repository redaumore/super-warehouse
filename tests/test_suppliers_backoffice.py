"""Backoffice suppliers CRUD tests (task 3.3).

DB-backed (Postgres, skipped when down): create/list/edit/toggle, quick-search
and status filters, reactive code resolution with the immutability guard
(code blocked once linked to a catalog/PO/need/mapping row), margin edits that
never re-price existing catalog rows, and re-validation of CUIT/email/phones on
create and update. The DB uniqueness backstops (cuit, code) are asserted
through direct ORM inserts.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError, OperationalError

from src.backoffice.app import _save_supplier, _supplier_row_selected, _supplier_toggle
from src.backoffice.suppliers import (
    InvalidSupplierDataError,
    create_supplier,
    list_suppliers,
    toggle_status,
    update_supplier,
)
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Supplier,
    SupplierStatus,
)
from src.db.session import SessionLocal


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


def test_create_supplier_persists_with_code_and_active_status(db_session):
    """Crear un supplier persiste columnas en inglés y status ACTIVO."""
    supplier = create_supplier(db_session, business_name="Mayorista SA")
    assert supplier.id is not None
    assert supplier.code == "MSA"  # suggested from the business name
    assert supplier.status is SupplierStatus.ACTIVO
    assert supplier.default_margin_pct == Decimal("0.00")
    assert db_session.get(Supplier, supplier.id).business_name == "Mayorista SA"


def test_create_supplier_accepts_full_master_data(db_session):
    """Los campos maestros se persisten y los teléfonos se normalizan."""
    supplier = create_supplier(
        db_session,
        business_name="Distribuidora del Sur",
        cuit="20111111112",
        email="ventas@surdist.com",
        phone="11 5555-1234",
        whatsapp="11 6666-7777",
        contact_name="Ana",
        address="Av. Siempre Viva 742",
        default_margin_pct=Decimal("0.20"),
        iva_condition="MONOTRIBUTO",
        terms="30 días",
    )
    assert supplier.cuit == "20111111112"
    assert supplier.email == "ventas@surdist.com"
    assert supplier.phone == "+541155551234"  # strict E.164, no 9
    assert supplier.whatsapp == "+5491166667777"  # WhatsApp form
    assert supplier.contact_name == "Ana"
    assert supplier.iva_condition.value == "MONOTRIBUTO"
    assert supplier.default_margin_pct == Decimal("0.20")


def test_create_supplier_rotates_code_on_collision(db_session):
    """Un código ya tomado rota a una variante libre."""
    first = create_supplier(db_session, business_name="Mayorista SA")
    assert first.code == "MSA"
    second = create_supplier(db_session, business_name="Mayorista SA")
    assert second.code == "MSB"  # third-char rotation over A-Z0-9


@pytest.mark.parametrize("cuit", ["20111111110", "123", "abc"])
def test_create_supplier_rejects_invalid_cuit(db_session, cuit: str):
    with pytest.raises(InvalidSupplierDataError, match="invalid CUIT"):
        create_supplier(db_session, business_name="Ferre SRL", cuit=cuit)


@pytest.mark.parametrize("email", ["not-an-email", "a@b"])
def test_create_supplier_rejects_invalid_email(db_session, email: str):
    with pytest.raises(InvalidSupplierDataError, match="invalid email"):
        create_supplier(db_session, business_name="Ferre SRL", email=email)


@pytest.mark.parametrize("phone", ["no-es-telefono", "123"])
def test_create_supplier_rejects_invalid_phone(db_session, phone: str):
    with pytest.raises(InvalidSupplierDataError, match="invalid phone"):
        create_supplier(db_session, business_name="Ferre SRL", phone=phone)


def test_create_supplier_rejects_invalid_whatsapp(db_session):
    with pytest.raises(InvalidSupplierDataError, match="invalid whatsapp"):
        create_supplier(db_session, business_name="Ferre SRL", whatsapp="no-es-telefono")


def test_list_suppliers_filters_by_query(db_session):
    """La búsqueda rápida filtra por nombre, CUIT o código."""
    create_supplier(db_session, business_name="Mayorista SA", cuit="20111111112")
    create_supplier(db_session, business_name="Pinturería X", cuit="20304050609")
    assert len(list_suppliers(db_session)) == 2
    assert [r["code"] for r in list_suppliers(db_session, query="MSA")] == ["MSA"]
    assert [r["code"] for r in list_suppliers(db_session, query="pintur")] == ["PXI"]
    assert [r["code"] for r in list_suppliers(db_session, query="20304050609")] == ["PXI"]


def test_list_suppliers_filters_by_status(db_session):
    """El filtro de estado separa ACTIVO de INACTIVO."""
    supplier = create_supplier(db_session, business_name="Mayorista SA")
    toggle_status(db_session, supplier.id)
    assert [r["id"] for r in list_suppliers(db_session, status=SupplierStatus.INACTIVO)] == [
        supplier.id
    ]
    assert list_suppliers(db_session, status=SupplierStatus.ACTIVO) == []


def test_toggle_status_flips_and_back(db_session):
    """toggle_status alterna ACTIVO ↔ INACTIVO."""
    supplier = create_supplier(db_session, business_name="Mayorista SA")
    toggle_status(db_session, supplier.id)
    assert db_session.get(Supplier, supplier.id).status is SupplierStatus.INACTIVO
    toggle_status(db_session, supplier.id)
    assert db_session.get(Supplier, supplier.id).status is SupplierStatus.ACTIVO


def test_update_supplier_edits_fields(db_session):
    """Editar un supplier persiste los cambios de campos maestros."""
    supplier = create_supplier(db_session, business_name="Mayorista SA")
    update_supplier(
        db_session,
        supplier.id,
        business_name="Mayorista SA (Renovada)",
        contact_name="Juan",
        default_margin_pct=Decimal("0.25"),
    )
    updated = db_session.get(Supplier, supplier.id)
    assert updated.business_name == "Mayorista SA (Renovada)"
    assert updated.contact_name == "Juan"
    assert updated.default_margin_pct == Decimal("0.25")


def test_margin_edit_does_not_reprice_existing_catalog_rows(db_session):
    """Editar el margen NO repreciera filas de catálogo existentes."""
    supplier = create_supplier(
        db_session, business_name="Mayorista SA", default_margin_pct=Decimal("0.10")
    )
    db_session.add(
        Catalogo(
            id=1,
            codigo_interno="CLV-001",
            supplier_id=supplier.id,
            nombre_oficial="Clavos Paris 2 Pulgadas",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            stock_disponible=10,
            sinonimos=["clavos"],
        )
    )
    db_session.flush()
    update_supplier(db_session, supplier.id, default_margin_pct=Decimal("0.50"))
    product = db_session.get(Catalogo, 1)
    assert product.margen_aplicado_pct == Decimal("0.35")
    assert product.precio_lista_base == Decimal("135.00")


def test_update_supplier_code_blocked_when_linked_to_catalog(db_session):
    """El código es inmutable cuando el supplier tiene filas vinculadas."""
    supplier = create_supplier(db_session, business_name="Mayorista SA")
    db_session.add(
        Catalogo(
            id=1,
            codigo_interno="CLV-001",
            supplier_id=supplier.id,
            nombre_oficial="Clavos",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            stock_disponible=10,
            sinonimos=["clavos"],
        )
    )
    db_session.flush()
    with pytest.raises(InvalidSupplierDataError, match="immutable"):
        update_supplier(db_session, supplier.id, code="NEW")
    assert db_session.get(Supplier, supplier.id).code == "MSA"


def test_update_supplier_code_allowed_when_unlinked(db_session):
    """Sin filas vinculadas el código puede editarse."""
    supplier = create_supplier(db_session, business_name="Mayorista SA")
    update_supplier(db_session, supplier.id, code="ABC")
    assert db_session.get(Supplier, supplier.id).code == "ABC"


def test_update_supplier_keeps_own_code_when_resubmitted(db_session):
    """Reenviar el mismo código (como hace la UI) no lo rota."""
    supplier = create_supplier(db_session, business_name="Mayorista SA")
    assert supplier.code == "MSA"
    update_supplier(db_session, supplier.id, code="MSA", contact_name="Juan")
    assert db_session.get(Supplier, supplier.id).code == "MSA"


def test_update_supplier_revalidates_contact_fields(db_session):
    """Editar revalida CUIT/email/teléfono."""
    supplier = create_supplier(db_session, business_name="Mayorista SA")
    with pytest.raises(InvalidSupplierDataError, match="invalid CUIT"):
        update_supplier(db_session, supplier.id, cuit="999")
    with pytest.raises(InvalidSupplierDataError, match="invalid email"):
        update_supplier(db_session, supplier.id, email="nope")
    with pytest.raises(InvalidSupplierDataError, match="invalid phone"):
        update_supplier(db_session, supplier.id, phone="nope")


def test_duplicate_cuit_rejected_by_database(db_session):
    """La base rechaza dos suppliers con el mismo CUIT no nulo."""
    db_session.add(Supplier(business_name="A", code="AAA", cuit="20111111112"))
    db_session.add(Supplier(business_name="B", code="BBB", cuit="20111111112"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_duplicate_code_rejected_by_database(db_session):
    """La base rechaza dos suppliers con el mismo código."""
    db_session.add(Supplier(business_name="A", code="AAA"))
    db_session.add(Supplier(business_name="B", code="AAA"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_multiple_suppliers_with_null_cuit_allowed(db_session):
    """CUIT NULL no colisiona (índice único parcial)."""
    db_session.add(Supplier(business_name="A", code="AAA"))
    db_session.add(Supplier(business_name="B", code="BBB"))
    db_session.flush()
    assert len(db_session.scalars(select(Supplier)).all()) == 2


# ------------------------------------------------ app handler functions (DB)
# NOTE: the app handlers open their own SessionLocal, which only sees COMMITTED
# rows — so these tests seed via SessionLocal + explicit commit.


def test_save_supplier_handler_persists_new_row():
    """Guardar un supplier nuevo desde la UI persiste la fila con su código."""
    message, _ = _save_supplier(0, "Mayorista SA", "", "", "", "", "", "", "", "", 0.0, "")
    assert message == "Supplier created (code MSA)"
    with SessionLocal() as session:
        suppliers = session.scalars(select(Supplier)).all()
    assert len(suppliers) == 1
    assert suppliers[0].code == "MSA"
    assert suppliers[0].status is SupplierStatus.ACTIVO


def test_save_supplier_handler_updates_existing_row():
    """Guardar desde la UI edita el supplier ya existente."""
    with SessionLocal() as session:
        supplier = create_supplier(session, business_name="Mayorista SA", code="MSA")
        session.commit()
        supplier_id = supplier.id
    message, _ = _save_supplier(
        supplier_id, "Mayorista SA Renovada", "MSA", "", "", "", "", "", "", "", 0.0, ""
    )
    assert message == "Supplier saved"
    with SessionLocal() as session:
        assert session.get(Supplier, supplier_id).business_name == "Mayorista SA Renovada"


def test_supplier_toggle_without_selection_returns_hint():
    """Alternar estado sin fila seleccionada devuelve un aviso y no crea nada."""
    assert _supplier_toggle(0) == "Select a supplier row first"
    with SessionLocal() as session:
        assert session.scalars(select(Supplier)).all() == []


def test_supplier_toggle_handler_flips_status_and_persists():
    """Alternar estado desde la UI persiste el nuevo estado del supplier."""
    with SessionLocal() as session:
        supplier = create_supplier(session, business_name="Mayorista SA", code="MSA")
        session.commit()
        supplier_id = supplier.id
    message = _supplier_toggle(supplier_id)
    assert "is now INACTIVO" in message
    with SessionLocal() as session:
        assert session.get(Supplier, supplier_id).status is SupplierStatus.INACTIVO


def test_supplier_row_selected_reads_dataframe_with_headers():
    """La selección de fila lee la grilla DataFrame con headers por posición."""
    with SessionLocal() as session:
        supplier = create_supplier(session, business_name="Mayorista SA", code="MSA")
        session.commit()
        supplier_id = supplier.id
    df = pd.DataFrame(
        [[supplier_id, "MSA", "Mayorista SA", "", "", "", "", "", ""]],
        columns=["ID", "Code", "Name", "CUIT", "Contact", "Phone", "Margin", "IVA", "Status"],
    )
    row = _supplier_row_selected(SimpleNamespace(index=[0]), df)
    assert row[0] == supplier_id
    assert row[2] == "Mayorista SA"
