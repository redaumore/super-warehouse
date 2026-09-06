"""Backoffice price lists CRUD tests (Settings tab).

DB-backed (Postgres, skipped when down): create/update/delete with the
fraction storage convention (0.10 = 10%, negative = recargo), unique-name
validation, delete guard while clients reference the list, and the
``default_price_list_id`` resolution of the seeded "Default" row. The Gradio
handlers are exercised directly (imported from app.py), including the
% points ↔ fraction conversion and the wiring arity regression checks.
"""

from __future__ import annotations

import re
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError

from src.backoffice.app import (
    _delete_price_list,
    _price_list_row_selected,
    _price_lists_grid,
    _save_price_list,
    build_app,
)
from src.backoffice.clients import default_price_list_id
from src.backoffice.price_lists import (
    InvalidPriceListDataError,
    create_price_list,
    delete_price_list,
    list_price_lists,
    update_price_list,
)
from src.config import get_settings
from src.db.models import Cliente, ListaPrecios
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


# ------------------------------------------------------------ service layer


@pytest.mark.parametrize(
    ("nombre", "fraccion"),
    [
        ("Gremio A", Decimal("0.10")),
        ("Sin descuento", Decimal(0)),
        ("Recargo", Decimal("-0.05")),
    ],
)
def test_create_price_list_stores_fraction_as_given(db_session, nombre: str, fraccion: Decimal):
    """Crear una lista guarda el descuento como fracción tal cual (0.10 = 10%, negativo = recargo)."""
    lista = create_price_list(db_session, nombre=nombre, descuento_pct=fraccion)
    assert lista.lista_id is not None
    assert db_session.get(ListaPrecios, lista.lista_id).descuento_lista_pct == fraccion


def test_create_price_list_rejects_blank_name(db_session):
    """El nombre vacío o solo espacios se rechaza con un error claro."""
    with pytest.raises(InvalidPriceListDataError, match="name is required"):
        create_price_list(db_session, nombre="   ", descuento_pct=Decimal(0))


def test_create_price_list_rejects_duplicate_name_case_insensitive(db_session):
    """El nombre duplicado se rechaza aunque cambie la mayúscula."""
    create_price_list(db_session, nombre="Gremio A", descuento_pct=Decimal("0.10"))
    with pytest.raises(InvalidPriceListDataError, match="already exists"):
        create_price_list(db_session, nombre="GREMIO A", descuento_pct=Decimal("0.20"))


@pytest.mark.parametrize("invalid", ["abc", "1.2.3", None])
def test_create_price_list_rejects_non_numeric_discount(db_session, invalid):
    """Un descuento no numérico o ausente se rechaza con un error claro."""
    with pytest.raises(InvalidPriceListDataError, match="discount"):
        create_price_list(db_session, nombre="Gremio B", descuento_pct=invalid)


def test_update_price_list_edits_name_and_discount(db_session):
    """Editar una lista persiste el nuevo nombre y el nuevo descuento."""
    lista = create_price_list(db_session, nombre="Gremio A", descuento_pct=Decimal("0.10"))
    update_price_list(db_session, lista.lista_id, nombre="Mayorista", descuento_pct="0.15")
    updated = db_session.get(ListaPrecios, lista.lista_id)
    assert updated.nombre == "Mayorista"
    assert updated.descuento_lista_pct == Decimal("0.15")


def test_update_price_list_unique_check_excludes_self(db_session):
    """Reenviar el mismo nombre (como hace la UI) no dispara el chequeo de unicidad."""
    lista = create_price_list(db_session, nombre="Gremio A", descuento_pct=Decimal("0.10"))
    update_price_list(db_session, lista.lista_id, nombre="Gremio A", descuento_pct=Decimal("0.20"))
    assert db_session.get(ListaPrecios, lista.lista_id).descuento_lista_pct == Decimal("0.20")


def test_update_price_list_rejects_name_of_other_list(db_session):
    """Editar con el nombre de otra lista falla y no muta ninguna fila."""
    first = create_price_list(db_session, nombre="Gremio A", descuento_pct=Decimal("0.10"))
    second = create_price_list(db_session, nombre="Gremio B", descuento_pct=Decimal("0.20"))
    with pytest.raises(InvalidPriceListDataError, match="already exists"):
        update_price_list(db_session, second.lista_id, nombre="Gremio A", descuento_pct=0)
    assert db_session.get(ListaPrecios, first.lista_id).nombre == "Gremio A"
    assert db_session.get(ListaPrecios, second.lista_id).nombre == "Gremio B"


def test_delete_price_list_refuses_while_clients_reference_it(db_session):
    """No se puede borrar una lista asignada a clientes; hay que reasignarlos antes."""
    from src.backoffice.clients import create_client

    lista = create_price_list(db_session, nombre="Gremio A", descuento_pct=Decimal("0.10"))
    create_client(
        db_session,
        nombre_comercial="Ferretería Don Juan",
        telefono_raw="+54 9 11 5555-1234",
        lista_precios_id=lista.lista_id,
    )
    with pytest.raises(InvalidPriceListDataError, match="assigned to 1 client"):
        delete_price_list(db_session, lista.lista_id)
    assert db_session.get(ListaPrecios, lista.lista_id) is not None


def test_delete_price_list_removes_row_when_unreferenced(db_session):
    """Una lista sin clientes asignados se borra definitivamente."""
    lista = create_price_list(db_session, nombre="Gremio A", descuento_pct=Decimal("0.10"))
    assert delete_price_list(db_session, lista.lista_id) == "Gremio A"
    assert db_session.get(ListaPrecios, lista.lista_id) is None


def test_list_price_lists_reports_client_counts(db_session):
    """El listado de la grilla incluye la cantidad de clientes por lista."""
    from src.backoffice.clients import create_client

    lista = create_price_list(db_session, nombre="Gremio A", descuento_pct=Decimal("0.10"))
    create_price_list(db_session, nombre="Gremio B", descuento_pct=Decimal("0.20"))
    create_client(
        db_session,
        nombre_comercial="Ferretería Don Juan",
        telefono_raw="+54 9 11 5555-1234",
        lista_precios_id=lista.lista_id,
    )
    rows = {r["nombre"]: r["clientes"] for r in list_price_lists(db_session)}
    assert rows == {"Gremio A": 1, "Gremio B": 0}


def test_default_price_list_id_prefers_default_row(db_session):
    """La lista 'Default' (sembrada) gana sobre 'Base' y sobre cualquier otra."""
    default = create_price_list(db_session, nombre="Default", descuento_pct=Decimal(0))
    create_price_list(db_session, nombre="Base", descuento_pct=Decimal(0))
    create_price_list(db_session, nombre="Gremio A", descuento_pct=Decimal("0.10"))
    assert default_price_list_id(db_session) == default.lista_id


def test_default_price_list_id_still_matches_legacy_base(db_session):
    """Sin lista 'Default', el helper legacy sigue resolviendo la lista 'Base'."""
    base = create_price_list(db_session, nombre="Base", descuento_pct=Decimal(0))
    assert default_price_list_id(db_session) == base.lista_id


# ------------------------------------------------ app handler functions (DB)
# NOTE: the app handlers open their own SessionLocal, which only sees COMMITTED
# rows — so these tests seed via SessionLocal + explicit commit.


def test_save_price_list_handler_creates_and_converts_percent_to_fraction():
    """Guardar una lista nueva desde la UI convierte % a fracción (10% → 0.10)."""
    result = _save_price_list(0, "Gremio A", 10.0)
    assert result[0] == "Lista creada: Gremio A"
    assert result[2] == 0  # selection reset so the next save is a new list
    assert tuple(result[3:5]) == ("", 0.0)  # form cleared after create
    assert any(
        re.fullmatch(r"Gremio A \(-?[\d.]+%\)", label) for label, _ in result[5]
    )  # client dropdown label shows the list's %
    with SessionLocal() as session:
        lista = session.scalar(select(ListaPrecios).where(ListaPrecios.nombre == "Gremio A"))
        assert lista is not None
        assert lista.descuento_lista_pct == Decimal("0.10")


def test_save_price_list_handler_stores_negative_percent_as_surcharge():
    """Un % negativo se guarda como recargo (fracción negativa) y la grilla lo muestra en %."""
    result = _save_price_list(0, "Recargo Especial", -5.0)
    assert result[0] == "Lista creada: Recargo Especial"
    with SessionLocal() as session:
        lista = session.scalar(
            select(ListaPrecios).where(ListaPrecios.nombre == "Recargo Especial")
        )
        assert lista is not None
        assert lista.descuento_lista_pct == Decimal("-0.05")
    grid_rows = {row[1]: row[2] for row in _price_lists_grid()}
    assert grid_rows["Recargo Especial"] == -5.0


def test_save_price_list_handler_updates_selected_row():
    """Guardar con una fila seleccionada edita la lista existente."""
    with SessionLocal() as session:
        lista = create_price_list(session, nombre="Gremio A", descuento_pct=Decimal("0.10"))
        session.commit()
        lista_id = lista.lista_id
    result = _save_price_list(lista_id, "Gremio A Renovada", 15.0)
    assert result[0] == "Lista guardada"
    assert result[2] == lista_id  # selection kept after update
    assert tuple(result[3:5]) == ("Gremio A Renovada", 15.0)  # form echoed
    with SessionLocal() as session:
        updated = session.get(ListaPrecios, lista_id)
        assert updated.nombre == "Gremio A Renovada"
        assert updated.descuento_lista_pct == Decimal("0.15")


def test_save_price_list_handler_rejects_duplicate_and_keeps_form():
    """Un nombre duplicado devuelve el error y deja el formulario como estaba."""
    with SessionLocal() as session:
        create_price_list(session, nombre="Gremio A", descuento_pct=Decimal("0.10"))
        session.commit()
    result = _save_price_list(0, "gremio a", 20.0)
    assert result[0].startswith("Error:")
    assert result[2] == 0
    assert tuple(result[3:5]) == ("gremio a", 20.0)


def test_delete_price_list_handler_refuses_when_clients_assigned():
    """Eliminar una lista con clientes asignados devuelve el error y conserva la fila."""
    with SessionLocal() as session:
        lista = create_price_list(session, nombre="Gremio A", descuento_pct=Decimal("0.10"))
        session.flush()
        session.add(
            Cliente(
                nombre_comercial="Ferretería Don Juan",
                telefono_norm="+5491155551234",
                lista_precios_id=lista.lista_id,
                descuento_particular_pct=Decimal(0),
            )
        )
        session.commit()
        lista_id = lista.lista_id
    result = _delete_price_list(lista_id)
    assert result[0].startswith("Error:")
    assert result[2] == lista_id  # selection kept on error
    assert result[3] == "Gremio A"
    assert result[4] == 10.0
    with SessionLocal() as session:
        assert session.get(ListaPrecios, lista_id) is not None


def test_delete_price_list_handler_removes_unreferenced_row_and_clears_form():
    """Eliminar una lista sin clientes la borra y limpia formulario y selección."""
    with SessionLocal() as session:
        lista = create_price_list(session, nombre="Gremio A", descuento_pct=Decimal("0.10"))
        session.commit()
        lista_id = lista.lista_id
    result = _delete_price_list(lista_id)
    assert result[0] == "Lista eliminada: Gremio A"
    assert result[2] == 0
    assert tuple(result[3:5]) == ("", 0.0)
    with SessionLocal() as session:
        assert session.get(ListaPrecios, lista_id) is None


def test_delete_price_list_handler_without_selection_returns_hint():
    """Eliminar sin fila seleccionada devuelve un aviso y no borra nada."""
    assert _delete_price_list(0)[0] == "Seleccione una fila de la grilla primero"


def test_price_list_row_selected_reads_dataframe_with_headers():
    """La selección de fila lee la grilla DataFrame con headers por posición."""
    with SessionLocal() as session:
        lista = create_price_list(session, nombre="Gremio A", descuento_pct=Decimal("0.10"))
        session.commit()
        lista_id = lista.lista_id
    df = pd.DataFrame(
        [[lista_id, "Gremio A", 10.0, 0]],
        columns=["ID", "Nombre", "Descuento %", "Clientes"],
    )
    row = _price_list_row_selected(SimpleNamespace(index=[0]), df)
    assert row == (lista_id, "Gremio A", 10.0)


def test_price_list_wiring_outputs_match_handler_return_arity():
    """El wiring de la UI declara 6 outputs para guardar y eliminar (regresión)."""
    demo = build_app()
    handlers = {
        getattr(bf.fn, "__name__", ""): bf
        for bf in demo.fns.values()
        if getattr(bf.fn, "__name__", "") in {"_save_price_list", "_delete_price_list"}
    }
    assert set(handlers) == {"_save_price_list", "_delete_price_list"}
    assert len(handlers["_save_price_list"].outputs) == 6
    assert len(handlers["_save_price_list"].inputs) == 3
    assert len(handlers["_delete_price_list"].outputs) == 6
    assert len(handlers["_delete_price_list"].inputs) == 1
