"""Structured order intake tests (task 3.5).

Pure unit tests for the NL parser and the fuzzy Spanish delivery-date resolver:
quantities + descriptions extraction, customer-name best effort, date phrases
("hoy", "mañana", "pasado mañana", weekdays, "la semana que viene", "el 5"),
missing-date tolerance, and the empty/non-order outcomes.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.agents.intake import ParsedItem, SimpleOrderParser, resolve_delivery_date


@pytest.fixture
def parser() -> SimpleOrderParser:
    return SimpleOrderParser()


# ------------------------------------------------------------ date resolver


@pytest.mark.parametrize(
    ("phrase", "today", "expected"),
    [
        ("para hoy", date(2026, 8, 27), date(2026, 8, 27)),
        ("para mañana", date(2026, 8, 27), date(2026, 8, 28)),
        ("pasado mañana", date(2026, 8, 27), date(2026, 8, 29)),
        ("para el viernes a la tarde", date(2026, 8, 24), date(2026, 8, 28)),
        ("el lunes", date(2026, 8, 27), date(2026, 8, 31)),
        ("el sábado", date(2026, 8, 27), date(2026, 8, 29)),
        ("la semana que viene", date(2026, 8, 27), date(2026, 9, 3)),
        ("para el finde", date(2026, 8, 27), date(2026, 8, 29)),
        ("fin de semana", date(2026, 8, 24), date(2026, 8, 29)),
        ("para el 5", date(2026, 8, 27), date(2026, 9, 5)),
        ("para el 5/12", date(2026, 8, 27), date(2026, 12, 5)),
        ("para el 3/1", date(2026, 12, 27), date(2027, 1, 3)),
    ],
)
def test_resolve_delivery_date_phrases(phrase, today, expected):
    """Las frases de fecha en español se resuelven a una fecha concreta."""
    assert resolve_delivery_date(phrase, now=today) == expected


def test_resolve_delivery_date_weekday_on_same_day_is_next_week():
    """Decir 'el viernes' siendo viernes apunta al viernes siguiente."""
    friday = date(2026, 8, 28)
    assert resolve_delivery_date("el viernes", now=friday) == date(2026, 9, 4)


def test_resolve_delivery_date_missing_returns_none():
    """Sin frase de fecha no se resuelve ninguna fecha."""
    assert resolve_delivery_date("quiero 10 clavos") is None


def test_resolve_delivery_date_invalid_day_returns_none():
    """Un día inexistente (el 31/2) no resuelve fecha."""
    assert resolve_delivery_date("para el 31/2", now=date(2026, 8, 27)) is None


# ---------------------------------------------------------------- parsing


def test_parse_extracts_items_and_quantities(parser):
    """El parser extrae artículos con sus cantidades."""
    order = parser.parse("quiero 10 clavos de 2 pulgadas y 5 pintura")
    assert order is not None
    assert order.items[0].description == "clavos de 2 pulgadas"
    assert order.items[0].quantity == 10
    assert order.items[1].description == "pintura"
    assert order.items[1].quantity == 5


def test_parse_quantity_before_description(parser):
    """La cantidad puede ir antes de la descripción sin 'de'."""
    order = parser.parse("necesito 3 cepillos")
    assert order is not None
    assert order.items == (ParsedItem(description="cepillos", quantity=3),)


def test_parse_delivery_date_captured(parser):
    """El parser resuelve la fecha de entrega de la frase."""
    order = parser.parse("quiero 10 clavos para el viernes", now=date(2026, 8, 27))
    assert order is not None
    assert order.delivery_date == date(2026, 8, 28)  # Friday after Thursday the 27th


def test_parse_missing_delivery_date_tolerated(parser):
    """Sin fecha el pedido se parsea con delivery_date nulo."""
    order = parser.parse("quiero 10 clavos")
    assert order is not None
    assert order.delivery_date is None


def test_parse_customer_name_best_effort(parser):
    """El nombre del cliente se extrae de 'soy/para/de parte de'."""
    assert parser.parse("soy juan y quiero 10 clavos").customer_name == "Juan"
    assert parser.parse("para don roberto, 10 clavos").customer_name == "Don roberto"


def test_parse_customer_name_with_cliente_keyword(parser):
    """El dueño identifica al cliente con 'cliente <nombre>' (owner pivot)."""
    order = parser.parse("cliente ferretería don juan: 10 clavos")
    assert order.customer_name == "Ferretería don juan"
    order = parser.parse("cliente pinturería san martín, 5 latas")
    assert order.customer_name == "Pinturería san martín"


def test_parse_non_order_message_returns_none(parser):
    """Un mensaje sin intención de pedido no se parsea como orden."""
    assert parser.parse("hola que tal") is None
    assert parser.parse("gracias") is None
    assert parser.parse("") is None
    assert parser.parse("   ") is None


def test_parse_order_intent_without_items_is_empty(parser):
    """Intención de pedido sin artículos parsea vacío (el flujo pide detalle)."""
    order = parser.parse("quiero cosas")
    assert order is not None
    assert order.items == ()


def test_parse_fuzzy_variant_handles_accents(parser):
    """El parser tolera acentos y mayúsculas."""
    order = parser.parse("QUIERO 2 tornillos")
    assert order is not None
    assert order.items[0].description == "tornillos"
