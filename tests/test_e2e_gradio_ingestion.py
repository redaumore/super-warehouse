"""Wire-level e2e of the Ingesta de remitos tab through the REAL Gradio server.

Unlike tests/test_e2e_ingestion.py (which calls the handlers directly), this
suite launches the actual Blocks app with ``app.launch()`` and drives it event
by event the way the browser does: gradio_client for the typed/button events
and raw HTTP POSTs (with ``event_data``) for the two ``.select`` listeners,
which gradio_client cannot fire because it has no way to send event data.

The class of bug this guards: handlers that pass unit tests while the event
WIRING loses state — e.g. clicking a pending row never updating the
"Línea pendiente (nº)" Number field, so "Asignar seleccionado" and
"Marcar como nuevo" silently operated on the wrong line.

gradio_client State semantics (verified against gradio 6.25 / gradio_client 2.6):
``gr.State`` inputs/outputs are HIDDEN from the api schema; the client pads
the payload with ``None`` at State positions and the server IGNORES those
values, reading each State from its per-session ``state_holder[session_hash]``
instead. State values therefore persist server-side across calls in one
client session, and the client never sees them back (outputs are stripped).
A select event CAN be fired over the wire by POSTing to
``/gradio_api/api/{api_name}`` with ``event_data`` and the SAME session_hash.

Skipped cleanly when Postgres is not running.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import gradio_client
import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError

from src.config import get_settings
from src.db.models import Catalogo, Inventory, StockAdjustment, Supplier, SupplierStatus
from src.db.session import SessionLocal
from src.integrations.rag import DocumentLine, RagProduct


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
                "TRUNCATE order_items, orders, stock_reservations, stock_adjustments, "
                "inventory, catalogo, suppliers, clientes, lista_precios, "
                "supplier_sku_mappings RESTART IDENTITY CASCADE"
            )
        )


# --------------------------------------------------------------------- fakes


class _E2eFakeRag:
    """RagProductClient stand-in reproducing the owner's real remito.

    Both lines miss the exact lookup and go hybrid: the ducha query returns 3
    wrong candidates (AMBIGUOUS), the arrancador query returns 2 — one correct
    plus one decoy — so the owner must assign manually (the reported failure).
    """

    def parse_document(self, *, filename: str, content: bytes, codigo_proveedor: str):
        return (
            DocumentLine(
                codigo_orig="SM 0048-84",
                codigo=None,
                descripcion="DUCHA CON FLEXIBLE",
                cantidad=7,
                costo=3.10,
                pagina=1,
            ),
            DocumentLine(
                codigo_orig="SM 302-8",
                codigo=None,
                descripcion="ARRANCADOR 15 CM CUADRADA CROMADA",
                cantidad=6,
                costo=2.05,
                pagina=1,
            ),
        )

    def exact_lookup(self, codigo_orig: str, codigo_proveedor: str):
        return ()

    def query(self, text: str) -> tuple[RagProduct, ...]:
        if "0048-84" in text.upper() or "DUCHA" in text.upper():
            return (
                RagProduct(sku="X-1", name="Arrancador 12mm", codigo_proveedor="MSA", node_id="n-x1"),
                RagProduct(sku="X-2", name="Grasa para caja", codigo_proveedor="MSA", node_id="n-x2"),
                RagProduct(sku="X-3", name="Monocomando", codigo_proveedor="MSA", node_id="n-x3"),
            )
        return (
            RagProduct(
                sku="A-1",
                name="Arrancador 15cm cuadrada cromada",
                codigo_proveedor="MSA",
                node_id="n-a1",
            ),
            RagProduct(
                sku="A-2",
                name="Arrancador 15cm cuadrada negra",
                codigo_proveedor="MSA",
                node_id="n-a2",
            ),
        )


class _FakeEmbedder:
    """Deterministic 1536-dim embedder (same shape as tests/test_backoffice.py)."""

    def embed(self, texts):
        return [[0.0] * 1536 for _ in texts]


# ------------------------------------------------------------------- fixture


@dataclass
class _Wire:
    """Everything a test needs to drive the running app over the wire."""

    client: gradio_client.Client
    base_url: str
    session_hash: str
    supplier_id: int
    preview_grid_id: int
    manual_grid_id: int


def _component_id(app: object, label: str) -> int:
    """Find a component id by label walking the server-side Blocks tree."""
    stack = [app]  # type: list[object]
    while stack:
        block = stack.pop()
        if getattr(block, "label", None) == label:
            return int(block._id)  # type: ignore[attr-defined]
        stack.extend(getattr(block, "children", []) or [])
    raise LookupError(label)


@pytest.fixture
def wire(db_engine, monkeypatch, tmp_path: Path) -> _Wire:
    """Launch the REAL app against the disposable test DB with faked RAG/embedder.

    The ``lru_cache`` factories must be patched BEFORE ``build_app()``: the
    wiring snapshots ``gr.State(_get_rag_client())`` at build time.
    """
    import src.backoffice.app as app_mod

    monkeypatch.setattr(app_mod, "_get_rag_client", lambda: _E2eFakeRag())
    monkeypatch.setattr(app_mod, "_get_embedder", lambda: _FakeEmbedder())

    with SessionLocal() as session:
        supplier = Supplier(
            code="MSA",
            business_name="Mercado Sanitario SA",
            status=SupplierStatus.ACTIVO,
            default_margin_pct=Decimal("0.10"),
        )
        session.add(supplier)
        session.commit()
        supplier_id = int(supplier.id)

    app = app_mod.build_app()
    app.launch(prevent_thread_lock=True, quiet=True)
    try:
        client = gradio_client.Client(app.local_url)
        yield _Wire(
            client=client,
            base_url=app.local_url.rstrip("/"),
            session_hash=client.session_hash,
            supplier_id=supplier_id,
            preview_grid_id=_component_id(app, "Revisión (resueltas / pendientes)"),
            manual_grid_id=_component_id(app, "Candidatos RAG"),
        )
    finally:
        app.close()


# -------------------------------------------------------------------- helpers


def _fire_select(
    wire: _Wire, api_name: str, trigger_id: int, *, data: list, index: list[int]
) -> list:
    """Fire a ``.select`` event through the real HTTP API.

    gradio_client cannot send ``event_data``, so the browser's select payload
    is replayed by hand: the server injects a ``gr.SelectData`` from it. The
    session_hash must match the gradio_client session so both clients share
    the same server-side ``gr.State`` values.
    """
    response = httpx.post(
        f"{wire.base_url}/gradio_api/api/{api_name.lstrip('/')}",
        json={
            "data": data,
            "event_data": {"index": index, "value": None, "selected": True},
            "trigger_id": trigger_id,
            "session_hash": wire.session_hash,
        },
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["data"]


def _resolve_endpoints(client: gradio_client.Client) -> dict[str, str]:
    """Pick the event endpoints from the live api schema.

    The ``.then`` chain after upload and the dropdown ``.change`` share the
    ``_ingest_resolve`` name; the change listener is the ``_1`` duplicate.
    """
    named = client.view_api(return_format="dict")["named_endpoints"]
    resolve_change = next(n for n in named if "ingest_resolve" in n and n.endswith("_1"))
    return {
        "parse": "/_ingest_parse",
        "resolve_change": resolve_change,
        "mark_new": "/_ingest_mark_new",
        "manual_search": "/_ingest_manual_search",
        "assign": "/_ingest_assign",
        "confirm": "/_ingest_confirm",
        "pending_row_select": "/_pending_row_selected",
        "manual_row_select": "/_manual_row_selected",
    }


def _upload_and_parse(wire: _Wire, endpoints: dict[str, str], supplier_value: object) -> str:
    """Upload the fake remito PNG with the given dropdown value (State hidden)."""
    image = Path("/tmp") / f"e2e_remito_{wire.session_hash[:8]}.png"
    image.write_bytes(b"fake-png-bytes")
    status = wire.client.predict(
        gradio_client.handle_file(str(image)),
        supplier_value,
        api_name=endpoints["parse"],
    )
    return str(status)


def _drive_confirmation_flow(wire: _Wire, endpoints: dict[str, str]) -> dict[str, object]:
    """Owner flow from supplier pick to confirm; returns the key wire outputs.

    Steps: supplier pick re-resolves → click pending row 1 (the click must
    sync "Línea pendiente (nº)" to 2 AND fill the candidate grid from the
    cached candidates) → mark line 1 as new → manual search line 2 → click
    candidate row 0 → assign → confirm.
    """
    client = wire.client
    grid, resolve_status = client.predict(
        wire.supplier_id, "", api_name=endpoints["resolve_change"]
    )
    pending_select = _fire_select(
        wire,
        endpoints["pending_row_select"],
        wire.preview_grid_id,
        data=[None, 1],  # [resolved_state (hidden), stale "Línea pendiente (nº)" value]
        index=[1],  # clicked the SECOND row (arrancador)
    )
    mark_grid, mark_status = client.predict(1, api_name=endpoints["mark_new"])
    cand_grid, search_status = client.predict(
        2, "arrancador 15", wire.supplier_id, api_name=endpoints["manual_search"]
    )
    _fire_select(
        wire,
        endpoints["manual_row_select"],
        wire.manual_grid_id,
        data=[],
        index=[0],  # clicked the FIRST candidate row
    )
    assign_grid, assign_status = client.predict(2, api_name=endpoints["assign"])
    confirm_status = client.predict(wire.supplier_id, api_name=endpoints["confirm"])
    return {
        "resolve_grid": grid,
        "resolve_status": resolve_status,
        "pending_select": pending_select,
        "mark_grid": mark_grid,
        "mark_status": mark_status,
        "cand_grid": cand_grid,
        "search_status": search_status,
        "assign_grid": assign_grid,
        "assign_status": assign_status,
        "confirm_status": confirm_status,
    }


def _assert_ingested_db_state() -> None:
    """Exactly one Catalogo row per line, right provenance, stock and audit."""
    with SessionLocal() as session:
        products = {
            product.codigo_interno: product for product in session.scalars(select(Catalogo))
        }
        assert set(products) == {"MSA-SM-0048-84", "MSA-SM-302-8"}
        assert "remito" in products["MSA-SM-0048-84"].origen  # document provenance
        assert "rag" in products["MSA-SM-302-8"].origen  # node_id provenance (n-a1)
        assert products["MSA-SM-302-8"].origen["rag"]["node_id"] == "n-a1"
        stock = {
            row.sku_id: row.quantity_on_hand for row in session.scalars(select(Inventory))
        }
        assert stock == {"MSA-SM-0048-84": 7, "MSA-SM-302-8": 6}
        adjustments = session.scalars(select(StockAdjustment)).all()
        assert {(a.sku, a.delta, a.reason, a.actor) for a in adjustments} == {
            ("MSA-SM-0048-84", 7, "receipt_ingestion", "owner:backoffice-ui"),
            ("MSA-SM-302-8", 6, "receipt_ingestion", "owner:backoffice-ui"),
        }


# ---------------------------------------------------------------------- tests


def test_full_owner_flow_through_real_server(wire):
    """Flujo completo del dueño por el servidor real: la fila clickeada sincroniza el número de línea."""
    endpoints = _resolve_endpoints(wire.client)

    # 1. Upload with the placeholder supplier: parse runs, resolution waits.
    parse_status = _upload_and_parse(wire, endpoints, "seleccionar proveedor")
    assert "Documento parseado: 2 líneas" in parse_status
    assert "Seleccioná un proveedor" in parse_status

    results = _drive_confirmation_flow(wire, endpoints)

    # 2. Supplier pick re-resolved BOTH lines as PENDIENTE (ambiguous).
    resolve_grid = results["resolve_grid"]
    assert results["resolve_status"] == "2 líneas; 2 ambigua(s) de asignar manualmente."
    assert [row[4] for row in resolve_grid["data"]] == ["PENDIENTE", "PENDIENTE"]

    # 3. THE FIX: clicking pending row 1 synced the Number field to 2 (was 1)
    # and filled the candidate grid from the line's cached RAG candidates.
    pending_select = results["pending_select"]
    assert pending_select[2] == "2 candidatos recuperados para la línea 2. Seleccioná uno y asignalo."
    assert [row[0] for row in pending_select[0]["data"]] == ["A-1", "A-2"]
    assert pending_select[3] == 2

    # 4. Line 1 marked new (owner override, ADR 0003).
    assert "Línea 1 marcada como producto nuevo" in results["mark_status"]
    mark_grid = results["mark_grid"]
    assert mark_grid["data"][0][4] == "NUEVO (por confirmar)"

    # 5. Manual search for line 2 returned the 2 hybrid candidates.
    assert results["search_status"] == "2 candidato(s) para la línea 2. Seleccioná uno y asignalo."
    assert [row[0] for row in results["cand_grid"]["data"]] == ["A-1", "A-2"]

    # 6. Candidate row 0 was clicked server-side (gr.State): assignment landed
    # on line 2 even though the client never sent the candidate index.
    assert results["assign_status"] == "Línea 2 asignada: A-1"
    assign_grid = results["assign_grid"]
    assert assign_grid["data"][1][4] == "A-1 — Arrancador 15cm cuadrada cromada"

    # 7. Confirm: nothing blocked, both lines adopted as new products.
    assert results["confirm_status"] == "Ingresado: 0 actualizados, 2 nuevos."
    _assert_ingested_db_state()


def test_placeholder_upload_then_supplier_pick_reaches_the_same_ingest(wire):
    """Upload con placeholder → solo parseo; tras elegir proveedor el flujo completa igual."""
    endpoints = _resolve_endpoints(wire.client)

    parse_status = _upload_and_parse(wire, endpoints, "seleccionar proveedor")
    assert "Documento parseado: 2 líneas" in parse_status
    assert "Seleccioná un proveedor para evaluarlo" in parse_status

    results = _drive_confirmation_flow(wire, endpoints)

    assert [row[4] for row in results["resolve_grid"]["data"]] == ["PENDIENTE", "PENDIENTE"]
    assert results["pending_select"][3] == 2  # row click synced the line number
    assert results["assign_status"] == "Línea 2 asignada: A-1"
    assert results["confirm_status"] == "Ingresado: 0 actualizados, 2 nuevos."
    _assert_ingested_db_state()
