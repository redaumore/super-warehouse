"""Product-query precedence chain + add-intent parser tests (W2).

Covers ``parse_product_add`` as parametrized pure-function cases and the
``PrecedenceProductSearcher`` precedence policy with fake local searcher and
fake RAG client: local hit skips the RAG, empty local falls back, refusals map
to NONE, RAG failures map to ERROR, and a local ``SQLAlchemyError`` still falls
back to the RAG. One test wires a real ``RagProductClient`` over
``httpx.MockTransport`` to prove SKU normalization reaches the chain output.
No network, no DB.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from src.agents.disambiguation import SearchCandidate
from src.agents.product_search import (
    PrecedenceProductSearcher,
    ProductEntry,
    ProductSource,
    is_finalize,
    parse_finalize,
    parse_product_add,
    parse_product_remove,
)
from src.config import Settings
from src.integrations.rag import RagProduct, RagProductClient, RagProductError


def _entry(sku: str = "SKU-001", name: str = "Tarugo Fischer 8mm") -> ProductEntry:
    return ProductEntry(sku=sku, name=name, source=ProductSource.LOCAL)


def _options(n: int) -> tuple[ProductEntry, ...]:
    return tuple(_entry(sku=f"SKU-00{i}", name=f"Producto {i}") for i in range(1, n + 1))


@pytest.mark.parametrize(
    ("text", "options", "expected"),
    [
        ("agregalo", (1,), (0, 1)),
        ("agregala", (1,), (0, 1)),
        ("sumá 5 de eso", (1,), (0, 5)),
        ("sumale 3 de eso", (1,), (0, 3)),
        ("agregá 2 de esos", (1,), (0, 2)),
        ("agregale 2", (1,), (0, 2)),
        ("sumale 3", (1,), (0, 3)),
        ("AGREGÁ 1", (1,), (0, 1)),
        ("agregale 2 unidades", (1,), (0, 2)),
        ("agregale 2 de eso", (1,), (0, 2)),
        ("el 2", (3,), (1, 1)),
        ("quiero el 3", (3,), (2, 1)),
        ("agregalo", (0,), None),
        ("el 5", (2,), None),
        ("el 2", (1,), None),
        ("pasame el precio", (2,), None),
        ("", (2,), None),
        ("quiero 2", (1,), (0, 2)),
        ("dame 3", (1,), (0, 3)),
        ("anotame 2", (1,), (0, 2)),
        ("llevo 2 unidades", (1,), (0, 2)),
        ("necesito 2", (1,), (0, 2)),
        ("quiero llevar 2", (1,), (0, 2)),
        ("2 unidades", (1,), (0, 2)),
        ("llevo 2 u.", (1,), (0, 2)),
        ("dos", (1,), (0, 2)),
        ("un", (1,), (0, 1)),
        ("diez", (1,), (0, 10)),
        ("veinte", (1,), (0, 20)),
        ("2", (1,), (0, 2)),
        ("Serían 2", (1,), (0, 2)),
        ("si, está bien", (1,), None),
        ("dale", (1,), None),
        ("nada más", (1,), None),
        ("ok", (1,), None),
        ("sí", (1,), None),
        ("no", (1,), None),
        ("todo bien", (1,), None),
        ("quiero 2 recolectores", (1,), None),
        ("agregale 2 recolectores de aceite", (1,), None),
        ("agregale", (1,), None),
        ("agregale 2", (0,), None),
        ("quiero 2", (0,), None),
    ],
    ids=[
        "agregalo-adds-one",
        "agregala-adds-one",
        "suma-five-de-eso",
        "sumale-three-de-eso",
        "agrega-two-de-esos",
        "agregale-two-verb-quantity",
        "sumale-three-verb-quantity",
        "accented-agrega-one-verb-quantity",
        "agregale-two-unidades",
        "agregale-two-de-eso",
        "el-2-picks-second",
        "quiero-el-3-picks-third",
        "empty-options-none",
        "numbered-out-of-range-none",
        "numbered-with-single-option-none",
        "non-add-phrase-none",
        "empty-text-none",
        "quiero-two-bare",
        "dame-three-bare",
        "anotame-two-bare",
        "llevo-two-unidades",
        "necesito-two-bare",
        "quiero-llevar-two",
        "digit-with-unidades",
        "digit-with-u-abbreviation",
        "dos-word-two",
        "un-word-one",
        "diez-word-ten",
        "veinte-word-twenty",
        "bare-digit",
        "accented-verb-prefix",
        "si-esta-bien-none",
        "dale-none",
        "nada-mas-none",
        "ok-none",
        "si-none",
        "no-none",
        "todo-bien-none",
        "quantity-with-product-name-none",
        "verb-quantity-with-product-name-none",
        "verb-without-number-none",
        "empty-options-verb-quantity-none",
        "empty-options-bare-none",
    ],
)
def test_parse_product_add(text: str, options: tuple[int, ...], expected: tuple[int, int] | None):
    """Add phrases resolve to (index, quantity); bare quantity answers map to the last product."""
    opts = _options(options[0]) if options else ()
    assert parse_product_add(text, opts) == expected


class FakeLocal:
    """Deterministic local searcher: configured candidates, can raise."""

    def __init__(
        self, candidates: tuple[SearchCandidate, ...] = (), error: Exception | None = None
    ) -> None:
        self.candidates = candidates
        self.error = error
        self.queries: list[str] = []

    def search(self, query: str) -> tuple[SearchCandidate, ...]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.candidates


class FakeRagClient:
    """Deterministic RAG client: configured products, can raise RagProductError."""

    def __init__(
        self,
        products: tuple[RagProduct, ...] = (),
        error: RagProductError | None = None,
    ) -> None:
        self.products = products
        self.error = error
        self.queries: list[str] = []

    def query(self, text: str) -> tuple[RagProduct, ...]:
        self.queries.append(text)
        if self.error is not None:
            raise self.error
        return self.products


def _candidate(sku: str, confidence: float) -> SearchCandidate:
    return SearchCandidate(sku=sku, nombre_oficial=f"Producto {sku}", confidence=confidence)


def test_local_hit_skips_rag():
    """Un hit local (>= floor) resuelve LOCAL y nunca llama al RAG."""
    local = FakeLocal(candidates=(_candidate("SKU-001", 0.9),))
    rag = FakeRagClient(products=(RagProduct(sku="RAG-1", name="Rag Item"),))
    searcher = PrecedenceProductSearcher(local, rag)

    result = searcher.search("tarugos")

    assert result.source is ProductSource.LOCAL
    assert result.entries == (
        ProductEntry(sku="SKU-001", name="Producto SKU-001", source=ProductSource.LOCAL),
    )
    assert rag.queries == []


def test_local_below_floor_falls_back_to_rag():
    """Un candidato local bajo el floor no es hit: el RAG se consulta."""
    local = FakeLocal(candidates=(_candidate("SKU-001", 0.4),))
    rag = FakeRagClient(products=(RagProduct(sku="RAG-1", name="Rag Item"),))
    searcher = PrecedenceProductSearcher(local, rag)

    result = searcher.search("tarugos")

    assert result.source is ProductSource.RAG
    assert rag.queries == ["tarugos"]


def test_empty_local_falls_back_to_rag_and_maps_fields():
    """Un local vacío cae al RAG y los campos del producto viajan al entry."""
    local = FakeLocal(candidates=())
    rag = FakeRagClient(
        products=(
            RagProduct(
                sku="AMX-AT-5044",
                name="Tarugo Fischer 8mm",
                provider="AMX",
                brand="Fischer",
                price=135.5,
                currency="ARS",
                unit="bolsa",
                specs="plástico",
                source_file="catalogo-2024.pdf",
                page=12,
            ),
        )
    )
    searcher = PrecedenceProductSearcher(local, rag)

    result = searcher.search("tarugos")

    assert result.source is ProductSource.RAG
    assert result.entries == (
        ProductEntry(
            sku="AMX-AT-5044",
            name="Tarugo Fischer 8mm",
            source=ProductSource.RAG,
            provider="AMX",
            brand="Fischer",
            price=135.5,
            currency="ARS",
            unit="bolsa",
            specs="plástico",
            source_file="catalogo-2024.pdf",
            page=12,
        ),
    )


def test_empty_local_with_refusal_is_none():
    """Un RAG que rechaza (sin productos) resuelve NONE, no un error."""
    local = FakeLocal(candidates=())
    rag = FakeRagClient(products=())
    searcher = PrecedenceProductSearcher(local, rag)

    result = searcher.search("tarugos")

    assert result.source is ProductSource.NONE
    assert result.entries == ()


def test_empty_local_with_rag_error_is_error():
    """Un error del RAG resuelve ERROR sin propagar la excepción."""
    local = FakeLocal(candidates=())
    rag = FakeRagClient(error=RagProductError("rag down"))
    searcher = PrecedenceProductSearcher(local, rag)

    result = searcher.search("tarugos")

    assert result.source is ProductSource.ERROR
    assert result.entries == ()


def test_local_sqlalchemy_error_still_calls_rag():
    """Un SQLAlchemyError del hop local no propaga: el RAG igual se consulta."""
    local = FakeLocal(error=SQLAlchemyError("db down"))
    rag = FakeRagClient(products=(RagProduct(sku="RAG-1", name="Rag Item"),))
    searcher = PrecedenceProductSearcher(local, rag)

    result = searcher.search("tarugos")

    assert result.source is ProductSource.RAG
    assert rag.queries == ["tarugos"]


def test_local_error_and_rag_error_is_error():
    """Hop local caído + RAG caído resuelve ERROR (la cadena nunca lanza)."""
    local = FakeLocal(error=SQLAlchemyError("db down"))
    rag = FakeRagClient(error=RagProductError("rag down"))
    searcher = PrecedenceProductSearcher(local, rag)

    result = searcher.search("tarugos")

    assert result.source is ProductSource.ERROR


def test_chain_normalizes_sku_from_real_client():
    """La cadena con un RagProductClient real normaliza el doble prefijo del codigo."""
    local = FakeLocal(candidates=())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "query": "tarugos",
                "is_refusal": False,
                "status": "SUCCESS",
                "structured_json": {
                    "productos": [
                        {
                            "codigo": "AMX-AMX-AT-5044",
                            "codigo_orig": None,
                            "codigo_proveedor": "AMX",
                            "nombre": "Tarugo Fischer 8mm",
                        }
                    ]
                },
            },
        )

    client = RagProductClient(
        transport=httpx.MockTransport(handler),
        settings=Settings(rag_base_url="http://rag.test"),
    )
    searcher = PrecedenceProductSearcher(local, client)

    result = searcher.search("tarugos")

    assert result.source is ProductSource.RAG
    assert result.entries[0].sku == "AMX-AT-5044"


def test_parse_finalize_extracts_customer_name_from_non_empty_draft():
    """A finalize command returns its customer name only when a draft exists."""
    draft = ((_entry(), 2),)

    assert parse_finalize("cerrá el pedido para Ferretería Don Juan", draft) == (
        "Ferretería Don Juan"
    )
    assert parse_finalize("finalizar orden: Cliente Uno", draft) == "Cliente Uno"
    assert parse_finalize("cerrá el pedido para Cliente Uno", ()) is None


def test_is_finalize_recognizes_command_without_customer_name():
    """The handler can ask for a customer when a draft is being finalized anonymously."""
    assert is_finalize("cerrá el pedido") is True
    assert is_finalize("quiero más clavos") is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sacá los clavos", "clavos"),
        ("quitá la pintura", "pintura"),
        ("eliminá el recolector de aceite", "recolector de aceite"),
        ("borrá tornillos", "tornillos"),
        ("Sacá el 2", "2"),
        ("sacá clavos.", "clavos"),
        ("no sacó nada", None),  # not a command
        ("me gustaría comprar clavos", None),
        ("", None),
    ],
)
def test_parse_product_remove(text, expected):
    """El comando 'sacá X' extrae el producto (artículo incluido); frases largas no."""
    assert parse_product_remove(text) == expected
