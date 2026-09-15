"""Pure-function tests for the RAG display-name parser (no DB).

The RAG catalog table has no ``nombre`` column: the literal product name
travels inside the node's ``text_content`` as a ``nombre: <value>`` line
embedded by the rag-api chunker. ``rag_name_from_text_content`` extracts it
for the backoffice SQL paths (unified search + manual order lines); these
tests pin the parse rules without touching Postgres.
"""

from __future__ import annotations

import pytest

from src.sourcing.product_search import rag_name_from_text_content


@pytest.mark.parametrize(
    ("text_content", "expected"),
    [
        # Literal catalog name, verbatim (owner requirement).
        (
            (
                "proveedor: Ferretera del Norte S.R.L.\n"
                "nombre: MECHA DE WIDEA 10 *130 x 10 unid\n"
                "descripcion: Mecha de widia 10 x 130 mm, paquete x 10 unidades\n"
            ),
            "MECHA DE WIDEA 10 *130 x 10 unid",
        ),
        # Surrounding whitespace of the line and value is trimmed only.
        ("header: x\n   nombre:   Tarugo de nylon 8mm   \n", "Tarugo de nylon 8mm"),
        # First occurrence wins when the key repeats.
        ("nombre: Primera denominación\nnombre: Segunda denominación\n", "Primera denominación"),
    ],
)
def test_extracts_first_nombre_line_trimmed(text_content: str, expected: str):
    """The first ``nombre:`` line is returned verbatim, whitespace-trimmed."""
    assert rag_name_from_text_content(text_content) == expected


def test_returns_none_when_line_is_absent():
    """No ``nombre:`` line means no parsed name (callers use the fallback)."""
    assert rag_name_from_text_content("proveedor: X\ndescripcion: Y\n") is None


def test_returns_none_on_empty_value():
    """An empty ``nombre:`` line yields None — never an empty name."""
    assert rag_name_from_text_content("nombre:\ndescripcion: Y\n") is None


def test_returns_none_on_whitespace_only_value():
    """A whitespace-only value is empty after trimming, hence None."""
    assert rag_name_from_text_content("nombre:    \n") is None


def test_returns_none_on_empty_or_missing_content():
    """Empty and None content have nothing to parse."""
    assert rag_name_from_text_content("") is None
    assert rag_name_from_text_content(None) is None


def test_keys_merely_starting_with_nombre_do_not_match():
    """Only the exact ``nombre:`` key counts (``nombre_proveedor:`` does not)."""
    assert rag_name_from_text_content("nombre_proveedor: Ferretera del Norte\n") is None
