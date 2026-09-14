"""Unit tests for chunker node_id disambiguation.

The node_id is derived from (codigo_proveedor, codigo). Suppliers may print
the same catalog code for two different products on the same page, so the
derived node_id collides and the ``ON CONFLICT (node_id)`` upsert would
silently collapse both rows into one. The chunker must disambiguate the
repeated occurrences with a ``#N`` suffix instead of failing the ingestion.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.ingestion.chunker import disambiguate_node_ids, run_pipeline


def _node(node_id: str, codigo: str, pagina: int) -> dict:
    return {
        "node_id": node_id,
        "text_to_embed": f"producto {codigo}",
        "metadata": {"codigo": codigo, "pagina": pagina, "codigo_proveedor": "SCO"},
    }


# ------------------------------------------------- disambiguate_node_ids


def test_disambiguate_appends_suffix_to_second_occurrence():
    """Second occurrence of a duplicated node_id gets '#2'; the first keeps the original id."""
    nodes = [
        _node("node_prod_SCO_A", "SM 409-86", 72),
        _node("node_prod_SCO_A", "SM 409-86", 72),
    ]
    conflicts = disambiguate_node_ids(nodes)

    assert nodes[0]["node_id"] == "node_prod_SCO_A"
    assert "node_id_suffix" not in nodes[0]["metadata"]
    assert nodes[1]["node_id"] == "node_prod_SCO_A#2"
    assert nodes[1]["metadata"]["node_id_suffix"] == 2
    assert set(conflicts) == {"node_prod_SCO_A"}


def test_disambiguate_unique_node_ids_are_untouched():
    """Distinct node_ids pass through unchanged with no conflicts reported."""
    nodes = [
        _node("node_prod_SCO_A", "AA-1", 1),
        _node("node_prod_SCO_B", "BB-2", 2),
    ]
    conflicts = disambiguate_node_ids(nodes)

    assert [n["node_id"] for n in nodes] == ["node_prod_SCO_A", "node_prod_SCO_B"]
    assert conflicts == {}


def test_disambiguate_three_occurrences_get_sequential_suffixes():
    """Three shared occurrences get #2 and #3 suffixes in input order."""
    nodes = [
        _node("node_prod_SCO_A", "AA-1", 1),
        _node("node_prod_SCO_A", "AA-2", 1),
        _node("node_prod_SCO_A", "AA-3", 1),
    ]
    disambiguate_node_ids(nodes)

    assert [n["node_id"] for n in nodes] == [
        "node_prod_SCO_A",
        "node_prod_SCO_A#2",
        "node_prod_SCO_A#3",
    ]


def test_disambiguate_conflicts_describe_every_occurrence():
    """The conflict map names codigo and página for every occurrence, in order."""
    nodes = [
        _node("node_prod_SCO_A", "SM 409-86", 72),
        _node("node_prod_SCO_A", "SM 409-86", 72),
    ]
    conflicts = disambiguate_node_ids(nodes)

    assert conflicts["node_prod_SCO_A"] == [
        "codigo 'SM 409-86' (página 72)",
        "codigo 'SM 409-86' (página 72)",
    ]


# -------------------------------------------------- run_pipeline end-to-end


def test_run_pipeline_disambiguates_duplicate_codes(tmp_path: Path):
    """run_pipeline emits distinct node_ids when the catalog repeats a code."""

    def _product(codigo: str, nombre: str, precio: float) -> dict:
        return {
            "codigo": codigo,
            "codigo_orig": codigo,
            "codigo_proveedor": "SCO",
            "nombre_producto": nombre,
            "nombre_proveedor": "SCON",
            "pagina": 72,
            "precio": precio,
            "moneda": "USD",
        }

    products = {
        "products_flat": [
            _product("SCO-SM 409 -86", "Mecha de widea 10 x 130 mm", 6.93),
            _product("SCO-SM 409 -86", "Mecha de widea 12 x 100 mm", 6.16),
        ]
    }
    input_path = tmp_path / "SCO_ingestion.json"
    input_path.write_text(json.dumps(products, ensure_ascii=False), encoding="utf-8")
    output_path = tmp_path / "SCO_nodes.json"

    nodes, _, conflicts = run_pipeline(input_path=str(input_path), output_path=str(output_path))

    assert len(nodes) == 2
    node_ids = [n["node_id"] for n in nodes]
    assert len(set(node_ids)) == 2
    assert node_ids[0].endswith("SCO-SM 409 -86")
    assert node_ids[1] == f"{node_ids[0]}#2"
    assert nodes[1]["metadata"]["node_id_suffix"] == 2
    assert list(conflicts) == [node_ids[0].removesuffix("#2")]
