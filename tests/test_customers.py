"""Customer-by-name resolution tests (tasks 2.6).

Pure unit tests cover the folded matcher (exact / folded / ambiguous / not
found), the ``nuevo cliente`` command parser, the numbered pick and the menu
renderer. DB integration covers the resolver against real rows (one / many /
zero), the Base price-list helper (parametrized) and the in-chat creation flow
including the duplicate-phone scenario (locked input #2).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.agents.customer import SourcingDeps, build_handler
from src.agents.customers import (
    CustomerNameMatch,
    CustomerResolutionKind,
    format_customer_menu,
    match_by_name,
    parse_create_client_command,
    parse_customer_pick,
    resolve_customer_name,
)
from src.agents.inventory import seed_inventory
from src.backoffice.clients import InvalidClientDataError, default_price_list_id
from src.channels.base import InboundMessage
from src.config import get_settings
from src.db.models import (
    Catalogo,
    Cliente,
    ListaPrecios,
    Supplier,
)
from src.orchestrator.router import AgentName, RoutingDecision
from src.supplier.searcher import FakeSupplierCatalogSearcher

OWNER_SENDER = "+5491100000000"

# ------------------------------------------------------------ pure matcher

ROWS = [
    (1, "Ferretería Don Juan"),
    (2, "Ferretería El Zorro"),
    (3, "Pinturería San Martín"),
]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Ferretería Don Juan", CustomerNameMatch(CustomerResolutionKind.EXACT, (1,))),
        ("ferreteria don juan", CustomerNameMatch(CustomerResolutionKind.EXACT, (1,))),
        ("FERRETERÍA DON JUAN", CustomerNameMatch(CustomerResolutionKind.EXACT, (1,))),
        ("Ferreteria Don Juan", CustomerNameMatch(CustomerResolutionKind.EXACT, (1,))),
        ("ferreteria", CustomerNameMatch(CustomerResolutionKind.AMBIGUOUS, (1, 2))),
        ("Ferretería", CustomerNameMatch(CustomerResolutionKind.AMBIGUOUS, (1, 2))),
        ("don juan", CustomerNameMatch(CustomerResolutionKind.FOLDED, (1,))),
        ("San Martín", CustomerNameMatch(CustomerResolutionKind.FOLDED, (3,))),
        ("Almacén La Esquina", CustomerNameMatch(CustomerResolutionKind.NOT_FOUND)),
        ("", CustomerNameMatch(CustomerResolutionKind.NOT_FOUND)),
    ],
)
def test_match_by_name_resolution_matrix(name, expected):
    """La matriz de resolución cubre exacto, plegado, ambiguo y no encontrado."""
    assert match_by_name(name, ROWS) == expected


def test_match_by_name_exact_beats_containment():
    """Un match exacto gana aunque otros nombres lo contengan."""
    rows = [(1, "Ferretería Don Juan"), (2, "Ferretería Don Juan S.A.")]
    assert match_by_name("Ferretería Don Juan", rows).kind is CustomerResolutionKind.EXACT
    assert match_by_name("Ferretería Don Juan", rows).ids == (1,)


def test_match_by_name_two_exact_is_ambiguous():
    """Dos nombres idénticos (folded) resultan AMBIGUOUS, nunca auto-pick."""
    rows = [(1, "Ferretería Don Juan"), (2, "ferreteria don juan")]
    match = match_by_name("Ferretería Don Juan", rows)
    assert match.kind is CustomerResolutionKind.AMBIGUOUS
    assert set(match.ids) == {1, 2}


# ------------------------------------------------------------ command parser


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("nuevo cliente Ferretería Don Juan 1133445566", ("Ferretería Don Juan", "1133445566")),
        ("NUEVO CLIENTE Juan 011-5555-1234", ("Juan", "011-5555-1234")),
        (
            "  nuevo cliente  Pinturería San Martín  +5491133334444  ",
            ("Pinturería San Martín", "+5491133334444"),
        ),
        ("nuevo cliente Juan", None),  # missing phone
        ("nuevo cliente", None),  # nothing after the command
        ("hola que tal", None),
        ("", None),
        ("quiero 10 clavos", None),
    ],
)
def test_parse_create_client_command(text, expected):
    """El comando 'nuevo cliente <nombre> <teléfono>' se parsea como par nombre/tel."""
    assert parse_create_client_command(text) == expected


# ------------------------------------------------------------ pick + menu


def test_parse_customer_pick_maps_number_to_candidate():
    """El pick numerado mapea 1-based al candidato correspondiente."""
    candidates = (
        Cliente(customer_id=1, nombre_comercial="A"),
        Cliente(customer_id=2, nombre_comercial="B"),
    )
    assert parse_customer_pick("2", candidates).customer_id == 2
    assert parse_customer_pick("1", candidates).customer_id == 1


def test_parse_customer_pick_rejects_invalid_input():
    """Un pick fuera de rango o no numérico no resuelve candidato."""
    candidates = (Cliente(customer_id=1, nombre_comercial="A"),)
    assert parse_customer_pick("0", candidates) is None
    assert parse_customer_pick("3", candidates) is None
    assert parse_customer_pick("no", candidates) is None
    assert parse_customer_pick("", candidates) is None


def test_format_customer_menu_lists_numbered_names():
    """El menú numera a los candidatos para que el dueño elija."""
    candidates = (
        Cliente(customer_id=1, nombre_comercial="Ferretería Don Juan"),
        Cliente(customer_id=2, nombre_comercial="Ferretería El Zorro"),
    )
    menu = format_customer_menu(candidates)
    assert "1) Ferretería Don Juan" in menu
    assert "2) Ferretería El Zorro" in menu


# ------------------------------------------------------------ DB integration


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
                "TRUNCATE supplier_purchase_order_items, supplier_purchase_orders, "
                "sourcing_needs, inventory, order_items, orders, stock_reservations, "
                "catalogo, suppliers, clientes, lista_precios RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def shop(db_session):
    """Base list plus three clients with overlapping names."""
    db_session.add(ListaPrecios(lista_id=1, nombre="Base", descuento_lista_pct=Decimal(0)))
    db_session.add(ListaPrecios(lista_id=2, nombre="Gremio A", descuento_lista_pct=Decimal(10)))
    db_session.add(
        Cliente(
            customer_id=1,
            nombre_comercial="Ferretería Don Juan",
            telefono_norm="+5491155551234",
            lista_precios_id=1,
            descuento_particular_pct=Decimal(0),
        )
    )
    db_session.add(
        Cliente(
            customer_id=2,
            nombre_comercial="Ferretería El Zorro",
            telefono_norm="+5491133334444",
            lista_precios_id=1,
            descuento_particular_pct=Decimal(0),
        )
    )
    db_session.add(
        Cliente(
            customer_id=3,
            nombre_comercial="Pinturería San Martín",
            telefono_norm="+5491122223333",
            lista_precios_id=2,
            descuento_particular_pct=Decimal(0),
        )
    )
    db_session.flush()
    db_session.execute(
        text("SELECT setval(pg_get_serial_sequence('clientes', 'customer_id'), 3, true)")
    )
    # Commit the seeds: the sourcing handlers own a session per call (closing it
    # rolls pending transactions back), so fixtures must survive that close.
    db_session.commit()
    return {"session": db_session}


def test_resolve_exact_name_auto_picks(shop):
    """Un nombre exacto resuelve a un único cliente sin preguntar."""
    resolution = resolve_customer_name(shop["session"], "ferreteria don juan")
    assert resolution.kind is CustomerResolutionKind.EXACT
    assert resolution.candidate.customer_id == 1


def test_resolve_folded_containment_picks_single(shop):
    """Un nombre que contiene (plegado) a un solo cliente lo selecciona."""
    resolution = resolve_customer_name(shop["session"], "San Martín")
    assert resolution.kind is CustomerResolutionKind.FOLDED
    assert resolution.candidate.customer_id == 3


def test_resolve_ambiguous_name_lists_candidates(shop):
    """Dos clientes que contienen el nombre disparan el menú de desambiguación."""
    resolution = resolve_customer_name(shop["session"], "ferreteria")
    assert resolution.kind is CustomerResolutionKind.AMBIGUOUS
    assert {c.customer_id for c in resolution.candidates} == {1, 2}


def test_resolve_unknown_name_offers_creation(shop):
    """Un nombre sin match ofrece la creación en chat."""
    resolution = resolve_customer_name(shop["session"], "Almacén La Esquina")
    assert resolution.kind is CustomerResolutionKind.NOT_FOUND
    assert resolution.candidate is None
    assert resolution.candidates == ()


# ------------------------------------------------- default price list helper


def test_default_price_list_id_returns_base(shop):
    """La lista Base es la lista por defecto para clientes creados en chat."""
    assert default_price_list_id(shop["session"]) == 1


def test_default_price_list_id_falls_back_when_base_missing(db_session):
    """Sin lista 'Base', se usa la primera lista existente."""
    db_session.add(ListaPrecios(lista_id=7, nombre="Mayorista", descuento_lista_pct=Decimal(5)))
    db_session.flush()
    assert default_price_list_id(db_session) == 7


def test_default_price_list_id_raises_without_lists(db_session):
    """Sin ninguna lista de precios, el helper falla con un error claro."""
    with pytest.raises(InvalidClientDataError, match="no price list"):
        default_price_list_id(db_session)


# ------------------------------------------- in-chat creation + disambiguation


class FakeResponder:
    """Never reached in the sourcing flow; raises if the LLM chat path runs."""

    def respond(self, messages):
        raise AssertionError("the sourcing flow must not call the LLM responder")


@pytest.fixture
def shop_with_catalog(shop):
    """Extends ``shop`` with a catalog product (50 units) for the Case A flow."""
    db_session = shop["session"]
    db_session.add(
        Supplier(
            id=1,
            code="TES",
            business_name="Test Supplier",
            default_margin_pct=Decimal(0),
        )
    )
    db_session.add(
        Catalogo(
            id=1,
            codigo_interno="CLV-PRS-2",
            supplier_id=1,
            nombre_oficial="Clavos Paris 2 Pulgadas (50mm)",
            costo_proveedor=Decimal("100.00"),
            margen_aplicado_pct=Decimal("0.35"),
            precio_lista_base=Decimal("135.00"),
            stock_disponible=50,
            sinonimos=["clavos", "clavo paris 2", "clavos 2 pulgadas"],
        )
    )
    db_session.flush()
    seed_inventory(db_session)
    db_session.commit()  # survive the handlers' per-call session close
    return shop


def _handler(session):
    deps = SourcingDeps(session_factory=lambda: session, searcher=FakeSupplierCatalogSearcher())
    return build_handler(FakeResponder(), sourcing=deps)  # type: ignore[arg-type]


def test_create_client_in_chat_creates_and_reports(shop_with_catalog):
    """El comando 'nuevo cliente' crea el cliente y puede resolverse por nombre."""
    session = shop_with_catalog["session"]
    handler = _handler(session)
    message = InboundMessage(
        channel="whatsapp",
        sender_id=OWNER_SENDER,
        text="nuevo cliente Ferretería La Esquina 1133445566",
    )
    outcome = handler(message, None, RoutingDecision(agent=AgentName.CUSTOMER))

    assert "di de alta a Ferretería La Esquina" in outcome.reply  # type: ignore[operator]
    created = session.scalar(select(Cliente).where(Cliente.telefono_norm == "+5491133445566"))
    assert created is not None
    assert created.nombre_comercial == "Ferretería La Esquina"
    assert created.lista_precios_id == 1  # default Base list
    # It resolves by name on subsequent orders.
    resolution = resolve_customer_name(session, "Ferretería La Esquina")
    assert resolution.kind is CustomerResolutionKind.EXACT
    assert resolution.candidate.customer_id == created.customer_id


def test_create_client_duplicate_phone_reports_existing(shop_with_catalog):
    """Un teléfono ya registrado reporta el cliente existente, sin duplicado."""
    session = shop_with_catalog["session"]
    handler = _handler(session)
    message = InboundMessage(
        channel="whatsapp",
        sender_id=OWNER_SENDER,
        text="nuevo cliente Otro Nombre 1155551234",  # same phone as client 1
    )
    outcome = handler(message, None, RoutingDecision(agent=AgentName.CUSTOMER))

    assert "ya es de Ferretería Don Juan" in outcome.reply  # type: ignore[operator]
    assert "no creé un duplicado" in outcome.reply  # type: ignore[operator]
    clients = session.scalars(
        select(Cliente).where(Cliente.telefono_norm == "+5491155551234")
    ).all()
    assert len(clients) == 1  # no duplicate row


def test_create_client_invalid_phone_reports_error(shop_with_catalog):
    """Un teléfono no interpretable reporta el error sin crear nada."""
    session = shop_with_catalog["session"]
    handler = _handler(session)
    message = InboundMessage(
        channel="whatsapp", sender_id=OWNER_SENDER, text="nuevo cliente Ferretería X abc"
    )
    outcome = handler(message, None, RoutingDecision(agent=AgentName.CUSTOMER))

    assert "No pude crear el cliente" in outcome.reply  # type: ignore[operator]
    assert session.scalar(select(Cliente).where(Cliente.nombre_comercial == "Ferretería X")) is None
