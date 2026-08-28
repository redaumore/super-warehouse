"""Phone normalization tests (owner pivot).

``normalize_phone`` is the canonicalization seam used by the owner gate
(WhatsApp sender compare) and by ``backoffice.clients.create_client`` (the
in-chat creation command). The chat-path customer lookup by phone was removed
with the owner pivot — the customer is resolved by name, so the KNOWN/UNKNOWN/
INVALID lookup tests are obsolete. Pure normalization variants stay as unit
tests.
"""

from __future__ import annotations

import pytest

from src.agents.customer import normalize_phone

_CANONICAL = "+5491155551234"

# The same Argentine mobile number in every plausible inbound format.
PHONE_VARIANTS = [
    "+54 9 11 5555-1234",
    "+5491155551234",
    "5491155551234",
    "11 5555 1234",
    "011-5555-1234",
    "(011) 5555 1234",
]


@pytest.mark.parametrize("variant", PHONE_VARIANTS)
def test_phone_format_variants_normalize_to_same_number(variant):
    """Todos los formatos de un mismo número argentino normalizan al mismo E.164 canónico.

    All spacing / country-code variants reconcile to one canonical E.164.
    """
    assert normalize_phone(variant) == _CANONICAL


@pytest.mark.parametrize("raw", ["", "abc", "5555", "12"])
def test_unparseable_phone_normalizes_to_none(raw):
    """Un teléfono no interpretable normaliza a None."""
    assert normalize_phone(raw) is None


def test_non_mobile_landline_still_normalizes():
    """Una línea fija válida normaliza a su forma E.164 sin prefijo 9."""
    assert normalize_phone("11 5555-0000") == "+5491155550000"
