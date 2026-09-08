"""Unit tests for PgVectorManager duplicate node_id validation.

Duplicate node_ids collapse into a single row through the
``ON CONFLICT (node_id) DO UPDATE`` upsert, silently breaking the
row-parity QA check (N expected vs N-1 found). The validation must
fail fast, before any DB connection, naming the conflicting
codigo/página pairs. No live database is required: the connection
factory is patched to blow up if it is ever reached.
"""

from __future__ import annotations

import pytest

from app.core.ingestion.vector_store import PgVectorManager


def _record(node_id: str, codigo: str, pagina: int) -> dict:
    return {
        "node_id": node_id,
        "metadata": {"codigo": codigo, "pagina": pagina, "codigo_proveedor": "SCO"},
        "text_content": f"producto {codigo}",
        "embedding": [0.1, 0.2],
    }


@pytest.fixture()
def manager() -> PgVectorManager:
    return PgVectorManager(
        db_url="postgresql://fake:fake@localhost:5432/fake",
        table_name="catalogo_productos_rag",
    )


# ------------------------------------------------- describe_duplicate_node_ids


def test_describe_duplicates_reports_codigo_and_pages(manager: PgVectorManager):
    """Two records sharing a node_id are reported with both codigo/página pairs."""
    records = [
        _record("node_prod_X", "SM 483-8", 58),
        _record("node_prod_X", "SM 483-8", 62),
    ]
    duplicates = manager.describe_duplicate_node_ids(records)
    assert list(duplicates) == ["node_prod_X"]
    assert duplicates["node_prod_X"] == [
        "codigo 'SM 483-8' (página 58)",
        "codigo 'SM 483-8' (página 62)",
    ]


def test_describe_duplicates_empty_when_all_unique(manager: PgVectorManager):
    """Distinct node_ids produce no duplicates report."""
    records = [
        _record("node_prod_A", "AA-1", 1),
        _record("node_prod_B", "BB-2", 2),
    ]
    assert manager.describe_duplicate_node_ids(records) == {}


def test_describe_duplicates_reports_each_duplicated_node(manager: PgVectorManager):
    """Every duplicated node_id is listed, unique ones are omitted."""
    records = [
        _record("node_prod_A", "AA-1", 1),
        _record("node_prod_A", "AA-1", 2),
        _record("node_prod_B", "BB-2", 3),
        _record("node_prod_C", "CC-3", 4),
        _record("node_prod_C", "CC-3", 5),
    ]
    duplicates = manager.describe_duplicate_node_ids(records)
    assert set(duplicates) == {"node_prod_A", "node_prod_C"}
    assert len(duplicates["node_prod_A"]) == 2


# ---------------------------------------------------------------- fail-fast


def test_ingest_records_duplicate_node_ids_raise_before_db_connection(
    manager: PgVectorManager, monkeypatch: pytest.MonkeyPatch
):
    """Duplicate node_ids raise ValueError naming the conflict, without opening a DB connection."""

    def _no_connection():  # pragma: no cover - reached only if validation is skipped
        raise AssertionError("_get_connection must not be called on duplicate validation")

    monkeypatch.setattr(manager, "_get_connection", _no_connection)

    records = [
        _record("node_prod_SCO_X", "SM 483-8", 58),
        _record("node_prod_SCO_X", "SM 483-8", 62),
        _record("node_prod_SCO_Y", "YY-9", 70),
    ]
    with pytest.raises(ValueError, match="node_ids duplicados") as exc_info:
        manager.ingest_records(records, batch_size=10)

    message = str(exc_info.value)
    assert "node_prod_SCO_X" in message
    assert "SM 483-8" in message
    assert "página 58" in message and "página 62" in message
