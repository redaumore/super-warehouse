"""Backoffice Gradio app (task 3.5): seven tabs for the owner.

A lightweight web interface with seven tabs — Catalog, Clients, Orders/Monitor,
Purchase Orders, Ingestion, Suppliers and Customer Orders — wired to the pure
DB operations in ``src.backoffice``. The build function only constructs the Blocks tree (no
server); ``launch()`` is guarded so importing the module never starts a server,
which keeps tests and CI safe.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from functools import lru_cache, partial
from typing import Literal, cast

import gradio as gr
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.backoffice.adoption import (
    AdoptRequest,
    Embedder,
    EmbeddingUnavailableError,
    InvalidStockError,
    MissingProvenanceError,
    OwnerContext,
    SkuCollisionError,
    SupplierUnknownError,
    adopt_product,
)
from src.backoffice.catalog import (
    list_products,
    resolve_product_code,
    update_margin,
    update_price,
    update_stock,
)
from src.backoffice.clients import create_client, list_clients, list_price_lists
from src.backoffice.customer_orders import (
    cancel_order_action,
    complete_picking_action,
    confirm_order_action,
    create_manual_order_action,
    deliver_order_action,
    get_default_margin,
    is_editable_order,
    legal_actions,
    list_customer_orders,
    list_exchange_rates,
    open_draft_for_customer,
    order_detail,
    order_state_diagram,
    recompute_pending_conversion,
    search_order_products_action,
    set_default_margin,
    set_exchange_rate,
    start_picking_action,
    update_manual_order_action,
)
from src.backoffice.ingestion import (
    PendingReason,
    ReceiptLine,
    ResolvedLine,
    UnresolvedLineError,
    hybrid_candidates,
    ingest_receipt_lines,
    resolve_lines,
    to_receipt_lines,
)
from src.backoffice.monitor import list_orders
from src.backoffice.po import (
    cancel_po_action,
    list_purchase_orders,
    po_detail,
    receive_po_action,
    send_po_action,
)
from src.backoffice.price_lists import (
    create_price_list,
    delete_price_list,
    update_price_list,
)
from src.backoffice.price_lists import (
    list_price_lists as list_price_list_rows,
)
from src.backoffice.sessions import list_sessions, session_events_grid
from src.backoffice.suppliers import (
    create_supplier,
    list_suppliers,
    toggle_status,
    update_supplier,
)
from src.config import Settings, get_settings
from src.db.models import IvaCondition, ListaPrecios, Supplier, SupplierStatus
from src.db.session import SessionLocal
from src.integrations.openai import OpenAIEmbedder
from src.integrations.rag import RagProduct, RagProductClient, RagProductError
from src.integrations.sheets import SheetsWriter
from src.sourcing.draft_order import ManualLineInput
from src.sourcing.product_search import ProductSearchHit, search_products_unified
from src.supplier.guards import SupplierInactiveError, ensure_active_supplier
from src.supplier.validation import suggest_code
from src.tz import to_buenos_aires

_SHEETS = SheetsWriter()  # append-only; quarantines internally when unconfigured

logger = logging.getLogger(__name__)

_IVA_CHOICES = [("—", "")] + [(c.value, c.value) for c in IvaCondition]
_STATUS_CHOICES = ["All"] + [s.value for s in SupplierStatus]

# Default logical document/lista identity for the "Ingesta de catálogo" tab:
# mirrors the RAG service default (settings.DEFAULT_DOCUMENTO_ID). It pre-fills
# the "Documento / lista" dropdown and resets it on supplier change, so every
# ingest ends up tagged (the service applies the same default when absent).
DEFAULT_DOCUMENTO_ID = "LISTA GENERAL"


_CATALOG_GRID_HEADERS = [
    "Proveedor",
    "Código proveedor",
    "Nombre",
    "Marca",
    "Stock",
    "Moneda",
    "Costo",
    "Precio lista (AR$)",
    "Margen",
    "Categoría",
    "Subcategoría",
]

_CATALOG_GRID_DATATYPES: tuple[Literal["str", "number"], ...] = (
    "str",
    "str",
    "str",
    "str",
    "number",
    "str",
    "str",
    "str",
    "str",
    "str",
    "str",
)

# Unified Productos grid: the catalog columns led by an "Origen" column
# tagging each row LOCAL (inventory catalog) or PROV (supplier RAG catalog).
_PRODUCTOS_GRID_HEADERS = ["Origen", *_CATALOG_GRID_HEADERS]
_PRODUCTOS_GRID_DATATYPES: tuple[Literal["str", "number"], ...] = (
    "str",
    *_CATALOG_GRID_DATATYPES,
)


def _catalog_grid() -> list[list[object]]:
    with SessionLocal() as session:
        rows = list_products(session)
    return [
        [
            "LOCAL",
            r["supplier_code"],
            r["supplier_sku_code"],
            r["nombre_oficial"],
            r["marca"],
            r["on_hand"],
            r["moneda"],
            r["costo_proveedor"],
            r["precio_lista_ars"],
            r["margen_aplicado_pct"],
            r["categoria"],
            r["subcategoria"],
        ]
        for r in rows
    ]


def _productos_row(hit: ProductSearchHit) -> list[object]:
    """One unified-grid row: Origen first, then the catalog-grid columns.

    ``hit.source`` keeps the use-case vocabulary ("LOCAL" | "RAG"); the grid
    displays RAG rows as PROV (catálogo de proveedores). Fields a source does
    not carry stay empty: PROV rows have no stock (the offer price is the
    supplier cost, so it lands in the Costo column with its own currency).
    The pricing snapshot (``precio_lista_ars`` + ``margen_pct``) is display
    time for BOTH sources: LOCAL shows the stored product margin, PROV shows
    the margin adoption would apply (supplier default, global fallback) and
    the AR$ conversion of the offer cost; a missing offer price leaves the
    columns empty.
    """
    is_local = hit.source == "LOCAL"
    costo = hit.costo if is_local else hit.price
    moneda = hit.moneda_original or hit.moneda
    return [
        "LOCAL" if is_local else "PROV",
        hit.supplier or "",
        hit.display_code,
        hit.name,
        hit.marca or "",
        hit.stock if hit.stock is not None else "",
        moneda or "",
        str(costo) if costo is not None else "",
        str(hit.precio_lista_ars) if hit.precio_lista_ars is not None else "",
        str(hit.margen_pct) if hit.margen_pct is not None else "",
        hit.categoria or "",
        hit.subcategoria or "",
    ]


def _productos_search(
    codigo: str, proveedor: str, marca: str, categoria: str, subcategoria: str, nombre: str, scope: str
) -> tuple[list[list[object]], str]:
    """Unified Productos search over both catalogs (LOCAL SQL + PROV SQL/vector).

    ``nombre`` drives the vector query on the rag-api for the PROV leg; when
    the service is unavailable the leg degrades to the indexed SQL table and
    the fallback surfaces as a status note. The no-filter guard error is
    surfaced in the status box — never a full scan.

    The vector client is built per call (never the cached ``_get_rag_client()``
    singleton): a real vector query builds the singleton's ``httpx.Client``,
    and ``build_app`` deep-copies that same object into ``gr.State`` for the
    Adoption tab — a built transport would make every later ``build_app()``
    call fail. The client is only needed when ``nombre`` is set.
    """
    nombre_text = str(nombre or "").strip()
    try:
        with SessionLocal() as session:
            hits, notes = search_products_unified(
                session,
                proveedor=str(proveedor or ""),
                marca=str(marca or ""),
                categoria=str(categoria or ""),
                subcategoria=str(subcategoria or ""),
                codigo=str(codigo or ""),
                nombre=nombre_text,
                scope=str(scope or "both"),
                limit=100,
                rag_client=RagProductClient() if nombre_text else None,
            )
    except ValueError as exc:
        return [], f"Error: {exc}"
    rows = [_productos_row(hit) for hit in hits]
    message = (
        f"{len(rows)} producto(s) encontrado(s)."
        if rows
        else "Sin resultados para los filtros indicados."
    )
    if notes:
        message += " " + " ".join(notes)
    return rows, message


def _clients_grid() -> list[list[object]]:
    with SessionLocal() as session:
        rows = list_clients(session)
    return [
        [
            r["customer_id"],
            r["nombre_comercial"],
            r["telefono_norm"],
            r["lista_precios_id"],
            r["descuento_particular_pct"],
        ]
        for r in rows
    ]


def _monitor_grid() -> list[list[object]]:
    with SessionLocal() as session:
        rows = list_orders(session, sheets=_SHEETS)
    return [
        [
            r["order_id"],
            r["customer"],
            r["estado"],
            r["needs_requote"],
            r["active_reservations"],
            r["sheets_synced"],
        ]
        for r in rows
    ]


def _po_grid() -> list[list[object]]:
    with SessionLocal() as session:
        rows = list_purchase_orders(session)
    return [[r["po_id"], r["supplier"], r["estado"], r["items"], r["received"]] for r in rows]


def _po_row_selected(evt: gr.SelectData) -> list[list[object]]:
    """Populate the PO detail grid from the clicked row in the PO grid.

    Same pattern as ``_order_row_selected``: the PO id lives at column 0 of the
    row value (positional, labels never matter). Deselecting a row — or any
    event without a usable row — clears the detail grid.
    """
    po_id = None
    if getattr(evt, "selected", False) and getattr(evt, "row_value", None):
        po_id = evt.row_value[0]  # "PO" column — positional
    if po_id is None:
        return []
    try:
        po_number = int(str(po_id))
    except (TypeError, ValueError):
        return []
    with SessionLocal() as session:
        try:
            detail = po_detail(session, po_number)
        except KeyError:
            return []
    return [
        [
            line["sku"],
            line["codigo_proveedor"],
            line["name"],
            line["quantity"],
            line["received_quantity"],
        ]
        for line in cast(list[dict[str, object]], detail["lines"])
    ]


def _po_send(po_id: object) -> str:
    with SessionLocal() as session:
        try:
            return send_po_action(session, int(str(po_id)))
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            return f"Error: {exc}"


def _po_receive(po_id: object, sku: str, quantity: object) -> str:
    with SessionLocal() as session:
        try:
            return receive_po_action(session, int(str(po_id)), sku, int(float(str(quantity))))
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            return f"Error: {exc}"


def _po_cancel(po_id: object) -> str:
    with SessionLocal() as session:
        try:
            return cancel_po_action(session, int(str(po_id)))
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            return f"Error: {exc}"


def _register_client(
    nombre: str, telefono: str, lista_id: object, descuento: float
) -> tuple[object, object, object, object, object, object]:
    """Register a client; on success reload the grid and clear the form.

    Returns ``(status, grid_rows, name, phone, price_list, discount)``. On
    failure every non-status component is left untouched via ``gr.update()``.
    """
    with SessionLocal() as session:
        try:
            create_client(
                session,
                nombre_comercial=nombre,
                telefono_raw=telefono,
                lista_precios_id=int(str(lista_id)),
                descuento_particular_pct=descuento,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            return (
                f"Error: {exc}",
                gr.update(),
                gr.update(),
                gr.update(),
                gr.update(),
                gr.update(),
            )
    return "Cliente registrado", _clients_grid(), "", "", None, 0


def _catalog_edit(code: str, stock: int | None, price: float | None, margin: float | None) -> str:
    """Apply stock/price/margin edits to the product the typed code resolves to.

    The box accepts the opaque internal SKU (fallback) or any supplier's
    mapped code; the resolved product is what gets edited, the confirmation
    echoes the code the owner typed (codigo_interno stays out of the UI).
    """
    with SessionLocal() as session:
        try:
            product = resolve_product_code(session, code)
            sku = product.codigo_interno
            if stock is not None:
                update_stock(session, sku, int(stock))
            if price is not None:
                update_price(session, sku, price)
            if margin is not None:
                update_margin(session, sku, margin)
            session.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            return f"Error: {exc}"
    return f"Guardado: {code}"


# ------------------------------------------------ ingestion tab (RAG receipts)

# Literal first choice (and default value) of the "Ingesta de remitos"
# supplier dropdown: forces an explicit selection before any resolution or
# confirmation runs, preventing wrong-supplier ingestions.
_SUPPLIER_PLACEHOLDER = "seleccionar proveedor"


def _active_supplier_choices() -> list[tuple[str, int]]:
    """(business_name → id) pairs for ACTIVO suppliers — no free numeric ID.

    The dropdown displays ``business_name`` (spec: supplier-first selection)
    and retains the supplier ID internally as the component value.
    """
    with SessionLocal() as session:
        suppliers = session.scalars(
            select(Supplier)
            .where(Supplier.status == SupplierStatus.ACTIVO)
            .order_by(Supplier.business_name)
        )
        return [(supplier.business_name, supplier.id) for supplier in suppliers]


def _ingesta_supplier_choices() -> list[tuple[str, int | str]]:
    """ACTIVO suppliers prefixed with the literal ``"seleccionar proveedor"``.

    The placeholder maps to its own literal (not ``None``) so Gradio accepts
    it as the dropdown's default ``value`` without an invalid-choice warning;
    ``_selected_supplier_id`` treats the literal as "nothing selected". Only
    the Ingesta tab uses this — the other tabs' handlers expect a castable id.
    """
    return [(_SUPPLIER_PLACEHOLDER, _SUPPLIER_PLACEHOLDER), *_active_supplier_choices()]


def _supplier_choices_update(include_placeholder: bool = False) -> gr.Dropdown:
    """Re-query ACTIVO suppliers so the dropdown reflects additions at runtime.

    Gradio freezes ``choices=`` evaluated at Blocks build time; returning a
    component instance from a click handler updates only the given props (the
    selected ``value`` is left untouched, so a refresh never wipes the parsed
    remito's supplier choice).
    """
    choices = (
        _ingesta_supplier_choices() if include_placeholder else _active_supplier_choices()
    )
    return gr.Dropdown(choices=choices)


def _selected_supplier_id(raw: object) -> int | None:
    """Extract the selected supplier id, or ``None`` when nothing is chosen.

    The Ingesta tab's dropdown starts on a literal placeholder ("seleccionar
    proveedor", mapped to ``None``): that sentinel, an empty/blank value, or
    non-numeric junk all yield ``None`` so handlers can guard BEFORE touching
    the database (``int("seleccionar proveedor")`` would raise an uncaught
    ``ValueError``).
    """
    text = str(raw).strip() if raw is not None else ""
    if not text or text == _SUPPLIER_PLACEHOLDER:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _resolved_grid(lines: Sequence[ResolvedLine]) -> list[list[object]]:
    """Render resolved/pending receipt lines for the review grid.

    Pending labels follow ADR 0003: a NO_CANDIDATES line shows
    ``NUEVO (por confirmar)`` — it becomes a definitive product on confirm —
    while an AMBIGUOUS line keeps ``PENDIENTE`` (manual assignment required).
    Zero/negative-quantity lines are never ingested and stay ``PENDIENTE``.
    """
    rows: list[list[object]] = []
    for resolved in lines:
        receipt = resolved.receipt
        if resolved.product is not None:
            resolution = f"{resolved.product.sku} — {resolved.product.name}"
        elif resolved.pending_reason is PendingReason.AMBIGUOUS:
            resolution = "PENDIENTE"
        elif receipt.cantidad > 0:
            resolution = "NUEVO (por confirmar)"
        else:
            resolution = "PENDIENTE"
        rows.append(
            [
                receipt.codigo_orig or "",
                receipt.descripcion,
                receipt.cantidad,
                receipt.costo if receipt.costo is not None else "",
                resolution,
            ]
        )
    return rows


def _ingest_parse(
    client: RagProductClient, upload: object, supplier_id: object
) -> tuple[tuple[ReceiptLine, ...], str]:
    """Upload → RAG parse → supplier-independent ``ReceiptLine`` state. Zero writes.

    Parsing is supplier-agnostic (the rag-api parser ignores
    ``codigo_proveedor``), so it runs even while the dropdown still shows the
    placeholder — the code is passed as ``""`` and the owner selects the
    supplier afterwards. Resolution is a separate step (``_ingest_resolve``)
    so changing the supplier re-evaluates the same parsed lines without
    re-uploading. RAG/Luna unavailability surfaces as an honest error and
    writes nothing (spec: RAG down → no inventory/catalog write).
    """
    if upload is None:
        return (), "Subí un remito o factura (PDF o foto)."
    path = getattr(upload, "path", None) or str(upload)
    filename = os.path.basename(str(path))
    with open(str(path), "rb") as fh:
        content = fh.read()
    supplier_pk = _selected_supplier_id(supplier_id)
    supplier_code = ""
    supplier_name = ""
    if supplier_pk is not None:
        with SessionLocal() as session:
            try:
                supplier = ensure_active_supplier(session, supplier_pk)
            except (KeyError, SupplierInactiveError) as exc:
                return (), f"Error: {exc}"
            supplier_code = supplier.code
            supplier_name = supplier.business_name
    try:
        document_lines = client.parse_document(
            filename=filename, content=content, codigo_proveedor=supplier_code
        )
    except RagProductError as exc:
        return (), f"Error: RAG no disponible ({exc})"
    receipt_lines = to_receipt_lines(document_lines, source_file=filename)
    if not receipt_lines:
        return receipt_lines, "No se extrajeron líneas legibles."
    if supplier_pk is None:
        return receipt_lines, (
            f"Documento parseado: {len(receipt_lines)} líneas. "
            "Seleccioná un proveedor para evaluarlo."
        )
    return receipt_lines, (
        f"Documento parseado: {len(receipt_lines)} líneas. "
        f"Evaluando con {supplier_name}..."
    )


def _ingest_resolve(
    client: RagProductClient,
    parsed: object,
    supplier_id: object,
    current: object,
    previous_status: object = "",
) -> tuple[list[list[object]], tuple[ResolvedLine, ...], str]:
    """Re-evaluate the already-parsed remito against the SELECTED supplier.

    Runs on dropdown change and chained after each parse: parsing is
    supplier-independent, so switching the supplier re-scopes the same cached
    ``ReceiptLine`` rows — no re-upload, and any previous manual assignments
    are intentionally lost (different supplier scope). With no parsed
    document or the placeholder supplier the current resolved state is kept
    untouched; a chained parse error keeps its honest message instead of
    being replaced.
    """
    lines = tuple(parsed) if isinstance(parsed, (tuple, list)) else ()
    current_lines = tuple(current) if isinstance(current, (tuple, list)) else ()
    if not lines:
        if isinstance(previous_status, str) and previous_status.startswith("Error:"):
            return _resolved_grid(current_lines), current_lines, previous_status
        return _resolved_grid(current_lines), current_lines, "Primero subí un documento."
    supplier_pk = _selected_supplier_id(supplier_id)
    if supplier_pk is None:
        return (
            _resolved_grid(current_lines),
            current_lines,
            "Seleccioná un proveedor para evaluar el documento.",
        )
    with SessionLocal() as session:
        try:
            resolved = resolve_lines(session, client, lines, supplier_id=supplier_pk)
        except (KeyError, SupplierInactiveError) as exc:
            return _resolved_grid(current_lines), current_lines, f"Error: {exc}"
    grid = _resolved_grid(resolved)
    parts = [f"{len(grid)} líneas"]
    ambiguous = sum(
        1
        for line in resolved
        if line.receipt.cantidad > 0
        and line.pending
        and line.pending_reason is PendingReason.AMBIGUOUS
    )
    new_products = sum(
        1
        for line in resolved
        if line.receipt.cantidad > 0
        and line.pending
        and line.pending_reason is not PendingReason.AMBIGUOUS
    )
    if ambiguous:
        parts.append(f"{ambiguous} ambigua(s) de asignar manualmente")
    if new_products:
        parts.append(f"{new_products} sin match en el índice (se adoptan al confirmar)")
    return grid, resolved, "; ".join(parts) + "."


def _candidate_rows(candidates: Sequence[RagProduct]) -> list[list[object]]:
    """Render RAG candidates for the manual-assignment grid (1:1 with state)."""
    return [
        [
            product.sku,
            product.name,
            product.brand or "",
            product.price if product.price is not None else "",
            product.node_id or "",
        ]
        for product in candidates
    ]


def _ingest_manual_search(
    client: RagProductClient, line_index: object, query_text: object, supplier_id: object
) -> tuple[list[list[object]], tuple[RagProduct, ...], str]:
    """Per-line RAG product-code search for a pending line (supplier-scoped).

    Exact-code lookup first: a query like ``SM 0048-84`` vectorizes poorly and
    the hybrid endpoint returns irrelevant products, so an exact code match
    (even several — the owner picks) short-circuits the hybrid fallback.
    """
    supplier_pk = _selected_supplier_id(supplier_id)
    if supplier_pk is None:
        return [], (), "Seleccioná un proveedor para buscar candidatos."
    line_idx = _as_index(line_index) - 1  # typed as a 1-based human line number
    if line_idx < 0:
        return [], (), "Seleccioná el número de línea pendiente (1-based)."
    text = str(query_text or "").strip()
    if not text:
        return [], (), "Escribí un código o término de búsqueda."
    with SessionLocal() as session:
        try:
            supplier = ensure_active_supplier(session, supplier_pk)
        except (KeyError, SupplierInactiveError) as exc:
            return [], (), f"Error: {exc}"
        supplier_code = supplier.code
    try:
        exact_matches = client.exact_lookup(text.upper(), codigo_proveedor=supplier_code)
        if exact_matches:
            candidates = tuple(exact_matches)
            note = " (coincidencia exacta de código)"
        else:
            candidates = hybrid_candidates(client, supplier_code, text)
            note = ""
    except RagProductError as exc:
        return [], (), f"Error: RAG no disponible ({exc})"
    rows = _candidate_rows(candidates)
    message = (
        f"{len(rows)} candidato(s){note} para la línea {line_idx + 1}. Seleccioná uno y asignalo."
        if rows
        else "Sin resultados: la línea sigue pendiente."
    )
    return rows, candidates, message


def _pending_row_selected(
    evt: gr.SelectData, state: object, current_index: object
) -> tuple[list[list[object]], tuple[RagProduct, ...], str, object]:
    """Populate the candidate grid from the cached candidates of a pending row.

    Selecting a row in the main resolved grid maps 1:1 to the line index; an
    AMBIGUOUS line whose resolution cached RAG candidates shows them right
    away — no re-search. The 4th return value keeps the "Línea pendiente (nº)"
    Number field in sync with the clicked row (the owner's mental model is
    "click the row, then act"): a PENDING line with a positive quantity writes
    its 1-based number — with cached candidates so the owner can assign right
    away, or NO_CANDIDATES so "Marcar como nuevo" hits the right line. Every
    other path (resolved row, ambiguous without cached candidates, zero
    quantity, deselection, invalid index) passes ``current_index`` through
    UNCHANGED so a number the owner typed manually is never clobbered.
    Read-only: no COMMIT, pure UI state.
    """
    lines = list(state) if isinstance(state, (tuple, list)) else []
    if not getattr(evt, "selected", False):
        return [], (), "", current_index
    idx = _manual_row_selected(evt)  # same click → index mapping as the candidate grid
    if idx is None or idx < 0 or idx >= len(lines):
        return [], (), "", current_index
    line = lines[idx]
    if (
        line.pending
        and line.pending_reason is PendingReason.AMBIGUOUS
        and line.candidates
    ):
        return (
            _candidate_rows(line.candidates),
            line.candidates,
            (
                f"{len(line.candidates)} candidatos recuperados para la línea {idx + 1}. "
                "Seleccioná uno y asignalo."
            ),
            idx + 1,
        )
    if line.pending and line.pending_reason is PendingReason.AMBIGUOUS:
        return [], (), (
            f"La línea {idx + 1} es ambigua pero no tiene candidatos cacheados: "
            "usá la búsqueda manual."
        ), current_index
    if line.pending:
        # NO_CANDIDATES or zero-quantity pending: adoption path. Only a
        # positive-quantity line is actionable ("Marcar como nuevo"), so only
        # that one syncs the typed line number.
        number = idx + 1 if line.receipt.cantidad > 0 else current_index
        return [], (), (
            f"La línea {idx + 1} no tiene candidatos en el índice: al confirmar se "
            "adopta como producto nuevo. Solo las ambiguas requieren asignación."
        ), number
    return [], (), f"La línea {idx + 1} ya está resuelta: no requiere asignación.", current_index


def _manual_row_selected(evt: gr.SelectData) -> int | None:
    """Map the clicked candidate row back to its position in the state."""
    if not getattr(evt, "selected", False):
        return None
    index = evt.index
    raw = index[0] if isinstance(index, (list, tuple)) else index
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _ingest_assign(
    state: object,
    line_index: object,
    candidate_index: object,
    candidates: object,
) -> tuple[tuple[ResolvedLine, ...], list[list[object]], str]:
    """Attach the selected candidate to the pending line (node_id provenance)."""
    lines = list(state) if isinstance(state, (tuple, list)) else []
    line_idx = _as_index(line_index) - 1  # typed as a 1-based human line number
    cand_idx = _as_index(candidate_index)
    if line_idx < 0 or line_idx >= len(lines):
        return tuple(lines), _resolved_grid(lines), "Seleccioná una línea pendiente válida."
    if cand_idx < 0:
        return tuple(lines), _resolved_grid(lines), "Seleccioná un candidato de la grilla."
    if not isinstance(candidates, (tuple, list)):
        return tuple(lines), _resolved_grid(lines), "Buscá candidatos primero."
    if cand_idx >= len(candidates):
        return tuple(lines), _resolved_grid(lines), "Seleccioná un candidato de la grilla."
    product = candidates[cand_idx]
    if not product.node_id:
        return tuple(lines), _resolved_grid(lines), "Error: el candidato no tiene procedencia (node_id)."
    current = lines[line_idx]
    if not current.pending:
        return tuple(lines), _resolved_grid(lines), "Esa línea ya está resuelta."
    lines[line_idx] = ResolvedLine(
        receipt=current.receipt, product=product, candidates=current.candidates
    )
    updated = tuple(lines)
    return updated, _resolved_grid(updated), f"Línea {line_idx + 1} asignada: {product.sku}"


def _ingest_mark_new(
    state: object,
    line_index: object,
) -> tuple[tuple[ResolvedLine, ...], list[list[object]], str]:
    """Reclassify an AMBIGUOUS pending line as new (owner override, ADR 0003).

    When none of the retrieved candidates is the actual product (e.g. the
    product's source page was never ingested), the owner can force the line
    into the ``NO_CANDIDATES`` path: it stops gating confirmation and is
    adopted as a definitive product with ``origen={"remito": ...}`` at confirm
    time. The cached candidates are dropped — keeping them would mislead the
    row-select flow into offering assignments for a line already marked new.
    """
    lines = list(state) if isinstance(state, (tuple, list)) else []
    line_idx = _as_index(line_index) - 1  # typed as a 1-based human line number
    if line_idx < 0 or line_idx >= len(lines):
        return tuple(lines), _resolved_grid(lines), "Seleccioná una línea pendiente válida."
    current = lines[line_idx]
    if not current.pending:
        return tuple(lines), _resolved_grid(lines), "Esa línea ya está resuelta."
    if current.receipt.cantidad <= 0:
        return tuple(lines), _resolved_grid(lines), (
            "Las líneas con cantidad cero o negativa nunca se ingesta: "
            "marcarlas como nuevo no aplica."
        )
    if current.pending_reason is PendingReason.NO_CANDIDATES:
        return tuple(lines), _resolved_grid(lines), (
            f"La línea {line_idx + 1} ya está marcada como producto nuevo."
        )
    if current.pending_reason is not PendingReason.AMBIGUOUS:
        return tuple(lines), _resolved_grid(lines), (
            "Esa línea pendiente no es ambigua: no se puede marcar como nueva."
        )
    lines[line_idx] = ResolvedLine(
        receipt=current.receipt, pending_reason=PendingReason.NO_CANDIDATES
    )
    updated = tuple(lines)
    return updated, _resolved_grid(updated), (
        f"Línea {line_idx + 1} marcada como producto nuevo: se adoptará como "
        "definitivo al confirmar. Esta decisión anula la ambigüedad del RAG "
        "(ningún candidato recuperado es el producto)."
    )


def _ingest_confirm(state: object, supplier_id: object, embedder: Embedder) -> str:
    """Gated confirmation: blocked only while AMBIGUOUS lines are unresolved.

    Per ADR 0003 a NO_CANDIDATES line does not block: it is adopted as a
    definitive new product inside ``ingest_receipt_lines``.
    """
    supplier_pk = _selected_supplier_id(supplier_id)
    if supplier_pk is None:
        return "Seleccioná un proveedor antes de confirmar."
    lines = list(state) if isinstance(state, (tuple, list)) else []
    if not lines:
        return "Primero parseá un documento."
    ambiguous = [
        line
        for line in lines
        if line.pending
        and line.receipt.cantidad > 0
        and line.pending_reason is PendingReason.AMBIGUOUS
    ]
    if ambiguous:
        detail = "; ".join(
            line.receipt.codigo_orig or line.receipt.descripcion for line in ambiguous[:5]
        )
        return f"Ingreso bloqueado: líneas ambiguas requieren asignación manual: {detail}"
    with SessionLocal() as session:
        try:
            result = ingest_receipt_lines(
                session,
                supplier_pk,
                lines,
                OwnerContext(owner_id="backoffice-ui"),
                embedder,
            )
            session.commit()
        except (KeyError, SupplierInactiveError) as exc:
            return f"Error: {exc}"
        except MissingProvenanceError:
            return "Error: una línea no tiene procedencia (node_id); no se guardó nada."
        except EmbeddingUnavailableError:
            return "Error: el servicio de embeddings falló; no se guardó nada. Intentá de nuevo."
        except UnresolvedLineError as exc:
            return f"Ingreso bloqueado: {exc}"
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            return f"Error al ingresar: {exc}"
    return f"Ingresado: {result.updated} actualizados, {result.created} nuevos."


# ------------------------------------------------ catalog ingest tab (RAG PDF)

_SKIP_PAGES_RE = re.compile(r"^\s*\d+(\s*-\s*\d+)?(\s*,\s*\d+(\s*-\s*\d+)?)*\s*$")


def _coerce_page_int(raw: object) -> int | None:
    """Coerce a Gradio Number input to an int (``None`` when blank/empty).

    Raises ``ValueError`` for non-numeric junk; fractional values truncate
    via ``float`` because Gradio may deliver ``1.0`` even with precision 0.
    """
    if raw is None or str(raw).strip() == "":
        return None
    return int(float(str(raw)))


def _validate_advanced_ingest_options(
    start_page: object,
    max_pages: object,
    skip_pages: object,
    marca: object,
) -> tuple[str, int | None, str | None, str | None] | str:
    """Validate the advanced ingest form values BEFORE calling the RAG API.

    Returns ``(marca_forzada, max_pages, skip_pages, start_page)`` on success
    or a friendly Spanish error message for the status textbox on failure
    (the caller must not launch any job then).
    """
    try:
        clean_start = _coerce_page_int(start_page)
    except (TypeError, ValueError):
        return "Error: la Página inicial debe ser un número entero mayor o igual a 1."
    if clean_start is None:
        clean_start = 1
    if clean_start < 1:
        return "Error: la Página inicial debe ser un número entero mayor o igual a 1."
    clean_max = None
    try:
        clean_max = _coerce_page_int(max_pages)
    except (TypeError, ValueError):
        return (
            "Error: el Máximo de páginas debe ser un número entero mayor o igual a 1 "
            "(o vacío = sin límite)."
        )
    if clean_max is not None and clean_max < 1:
        return (
            "Error: el Máximo de páginas debe ser un número entero mayor o igual a 1 "
            "(o vacío = sin límite)."
        )
    clean_skip = str(skip_pages or "").strip()
    if clean_skip:
        if not _SKIP_PAGES_RE.match(clean_skip):
            return (
                "Error: Páginas a saltar inválidas. Usá el formato '1-2,4': rangos o "
                "páginas sueltas separadas por coma."
            )
        for start_txt, end_txt in re.findall(r"(\d+)\s*-\s*(\d+)", clean_skip):
            if int(start_txt) > int(end_txt):
                return (
                    "Error: Páginas a saltar inválidas. En los rangos, la página inicial "
                    "no puede ser mayor que la final (ej: '4-2' es inválido)."
                )
    clean_marca = str(marca or "").strip()
    return clean_marca or None, clean_max, clean_skip or None, clean_start


def _catalog_ingest(
    client: RagProductClient,
    upload: object,
    supplier_id: object,
    documento_id: object,
    incremental: object,
    start_page: object = None,
    max_pages: object = None,
    skip_pages: object = None,
    no_vision: object = False,
    marca: object = None,
) -> tuple[str | None, str]:
    """Upload the supplier catalog PDF to the RAG async ingestion queue.

    Returns ``(job_id, status)``: the job id travels in ``gr.State`` so the
    "Consultar estado" button can poll it. Two modes: full replace (default)
    deletes ALL previously indexed rows for the supplier's code; incremental
    ("documento") deletes only rows of the declared document/lista — a
    supplier catalog may span multiple PDFs. This handler validates supplier +
    file (+ documento_id when incremental) and launches the job; RAG
    unavailability surfaces as an honest error and launches nothing.

    Advanced options (accordion in the tab) thread through to the client:
    page windowing (``start_page``/``max_pages``/``skip_pages``), text-only
    extraction (``no_vision``) and forced brand (``marca``). They are
    validated here so invalid input never reaches the API.
    """
    if upload is None:
        return None, "Subí el PDF del catálogo del proveedor."
    doc_id = str(documento_id or "").strip()
    if bool(incremental) and not doc_id:
        return None, (
            "Error: para la ingesta incremental tenés que declarar el "
            "Documento / lista (ej: 'LISTA GENERAL')."
        )
    validated = _validate_advanced_ingest_options(start_page, max_pages, skip_pages, marca)
    if isinstance(validated, str):
        return None, validated
    marca_forzada, max_pages_clean, skip_pages_clean, start_page_clean = validated
    with SessionLocal() as session:
        try:
            supplier = ensure_active_supplier(session, int(str(supplier_id)))
        except (KeyError, SupplierInactiveError) as exc:
            return None, f"Error: {exc}"
        supplier_code = supplier.code
        supplier_name = supplier.business_name
        supplier_pk = str(supplier.id)
    path = getattr(upload, "path", None) or str(upload)
    filename = os.path.basename(str(path))
    with open(str(path), "rb") as fh:
        content = fh.read()
    try:
        job_id = client.ingest_catalog(
            filename=filename,
            content=content,
            codigo_proveedor=supplier_code,
            nombre_proveedor=supplier_name,
            proveedor_id=supplier_pk,
            documento_id=doc_id or None,
            delete_scope="documento" if bool(incremental) else "proveedor",
            start_page=start_page_clean,
            max_pages=max_pages_clean,
            skip_pages=skip_pages_clean,
            no_vision=bool(no_vision),
            marca=marca_forzada,
        )
    except RagProductError as exc:
        return None, f"Error: RAG no disponible ({exc})"
    modo = "incremental (documento)" if bool(incremental) else "reemplazo total"
    return job_id, f"Ingesta lanzada en modo {modo} (job {job_id}). Consultá el estado."


def _catalog_job_status(client: RagProductClient, job_id: object) -> str:
    """Render the async ingestion job snapshot for the tab's status box."""
    clean_job_id = str(job_id or "").strip()
    if not clean_job_id:
        return "Todavía no se lanzó ninguna ingesta en esta sesión."
    try:
        job = client.get_job(clean_job_id)
    except RagProductError as exc:
        return f"Error: RAG no disponible ({exc})"
    if job.status in ("PENDING", "RUNNING"):
        detail = job.progress_message or "En proceso..."
        return f"Job {job.job_id}: {job.status}. {detail}"
    if job.status == "COMPLETED":
        result = job.result or {}
        summary_bits = [
            str(result[key])
            for key in ("total_productos", "productos_indexados", "total_paginas", "paginas")
            if result.get(key) is not None
        ]
        summary = f" ({', '.join(summary_bits)})" if summary_bits else ""
        return f"Job {job.job_id}: COMPLETED{summary}. {job.progress_message or ''}".strip()
    if job.status == "FAILED":
        detail = job.error or "Error desconocido."
        return f"Job {job.job_id}: FAILED. {detail}"
    return f"Job {job.job_id}: {job.status}."


def _load_provider_documents(client: RagProductClient, supplier_selection: object) -> gr.Dropdown:
    """Refresh the "Documento / lista" dropdown with the supplier's known documents.

    Resolves the supplier selection to ``codigo_proveedor`` exactly like
    ``_catalog_ingest`` (active-supplier lookup by id via
    ``ensure_active_supplier``) and asks the RAG for the indexed
    ``documento_id`` values, so the operator picks a known document — or types
    a brand-new one — instead of free-typing a typo'd id that would leave
    orphan rows. Every failure mode (unknown/inactive supplier, RAG
    unavailability, empty provider) degrades gracefully to an empty choices
    update — never a crash. The value resets to ``DEFAULT_DOCUMENTO_ID``
    ("LISTA GENERAL") instead of clearing: when it is among the fetched
    choices the dropdown selects that item; ``allow_custom_value`` covers the
    rest (a fresh supplier with no indexed documents still shows the default).
    """
    try:
        supplier_pk = int(str(supplier_selection))
    except (TypeError, ValueError):
        logger.debug("Dropdown documentos: sin proveedor seleccionado (%r).", supplier_selection)
        return gr.update(choices=[], value=DEFAULT_DOCUMENTO_ID)
    with SessionLocal() as session:
        try:
            supplier = ensure_active_supplier(session, supplier_pk)
        except (KeyError, SupplierInactiveError) as exc:
            logger.warning("Dropdown documentos: proveedor inválido (%s): %s", supplier_pk, exc)
            return gr.update(choices=[], value=DEFAULT_DOCUMENTO_ID)
        supplier_code = supplier.code
    try:
        response = client.list_documents(supplier_code)
    except RagProductError as exc:
        logger.warning("Dropdown documentos: RAG no disponible para %s: %s", supplier_code, exc)
        return gr.update(choices=[], value=DEFAULT_DOCUMENTO_ID)
    choices = [doc.documento_id for doc in response.documents]
    logger.info(
        "Dropdown documentos: %d documento(s) para el proveedor %s.", len(choices), supplier_code
    )
    return gr.update(choices=choices, value=DEFAULT_DOCUMENTO_ID)


def _as_index(raw: object) -> int:
    """Coerce a Gradio numeric/None input to a 0-based index (-1 when blank)."""
    if raw is None or str(raw).strip() == "":
        return -1
    try:
        return int(float(str(raw)))
    except (TypeError, ValueError):
        return -1


# ------------------------------------------------ adoption tab (RAG products)


@lru_cache
def _get_rag_client() -> RagProductClient:
    """Lazily build the RAG product client (same lazy pattern as the embedder)."""
    return RagProductClient()


@lru_cache
def _get_embedder() -> Embedder:
    """Real adoption embedder, constructed exactly like the REST endpoint's."""
    cfg = get_settings()
    return OpenAIEmbedder(
        settings=cfg,
        timeout=cfg.adoption_embed_timeout_seconds,
        retries=cfg.adoption_embed_retries,
    )


def _adoption_search(
    client: RagProductClient, query_text: str
) -> tuple[list[list[object]], tuple[RagProduct, ...], str]:
    """Query the RAG for ``query_text`` and render one row per product.

    Returns ``(grid_rows, raw_results, status)``: the raw ``RagProduct`` tuple
    travels in ``gr.State`` so the selected grid row maps back to its typed
    product. RAG unavailability and refusals surface as status text — never a
    crash. (Single-row selection: multiple selection is deferred.)
    """
    text = (query_text or "").strip()
    if not text:
        return [], (), "Escribí un término de búsqueda."
    try:
        products = client.query(text)
    except RagProductError as exc:
        return [], (), f"Error: RAG no disponible ({exc})"
    rows = [
        [
            product.sku,
            product.name,
            product.brand or "",
            product.categoria or "",
            product.price if product.price is not None else "",
            product.currency or "",
        ]
        for product in products
    ]
    message = (
        f"{len(rows)} resultado(s). Seleccioná una fila y adoptala."
        if rows
        else "Sin resultados: el producto no está en los catálogos actuales."
    )
    return rows, products, message


def _adoption_row_selected(evt: gr.SelectData) -> int | None:
    """Map the clicked grid row back to its position in the results state."""
    if not getattr(evt, "selected", False):
        return None
    index = evt.index
    raw = index[0] if isinstance(index, (list, tuple)) else index
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _adoption_confirm(
    results: object, selected_index: object, stock: object, embedder: Embedder
) -> str:
    """Adopt the selected RAG product into inventory via the pure use case.

    Builds the ``AdoptRequest`` from the selected ``RagProduct`` (provenance
    ``node_id`` included — empty means the use case fails closed) and commits
    with the session-in / caller-commits pattern. Domain errors map to
    friendly owner-facing messages.
    """
    if not results:
        return "Buscá productos en el RAG primero."
    index = int(str(selected_index)) if selected_index is not None else -1
    if index < 0 or index >= len(results):
        return "Seleccioná un producto de la grilla."
    product: RagProduct = results[index]
    stock_int = int(float(str(stock))) if stock is not None else 0
    if stock_int <= 0:
        return "Error: el stock inicial debe ser mayor que cero."
    dto = AdoptRequest(
        sku=product.sku,
        nombre=product.name,
        codigo_proveedor=product.codigo_proveedor or "",
        marca=product.brand,
        categoria=product.categoria,
        subcategoria=product.subcategoria,
        precio=product.price,
        moneda=product.currency,
        archivo_origen=product.source_file,
        pagina=product.page,
        node_id=product.node_id or "",
        stock=stock_int,
    )
    with SessionLocal() as session:
        try:
            created = adopt_product(session, dto, OwnerContext(owner_id="backoffice-ui"), embedder)
            session.commit()
        except SkuCollisionError:
            return "Error: ya existe un producto con ese código (SKU en uso)."
        except InvalidStockError:
            return "Error: el stock inicial debe ser mayor que cero."
        except SupplierUnknownError:
            return "Error: proveedor desconocido; cargá el proveedor antes de adoptar."
        except SupplierInactiveError:
            return "Error: el proveedor está inactivo; activalo antes de adoptar."
        except MissingProvenanceError:
            return "Error: el producto no tiene procedencia (node_id); no se puede adoptar."
        except EmbeddingUnavailableError:
            return "Error: el servicio de embeddings falló; no se guardó nada. Intentá de nuevo."
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            return f"Error: {exc}"
    return f"Adoptado: {created.codigo_interno}"


def _suppliers_grid(query: str, status: str) -> list[list[object]]:
    with SessionLocal() as session:
        rows = list_suppliers(
            session,
            query=query.strip() or None,
            status=SupplierStatus[status] if status and status != "All" else None,
        )
    return [
        [
            r["id"],
            r["code"],
            r["business_name"],
            r["cuit"] or "",
            r["contact_name"] or "",
            r["phone"] or "",
            str(r["default_margin_pct"]),
            r["iva_condition"] or "",
            r["status"],
        ]
        for r in rows
    ]


def _supplier_row_selected(evt: gr.SelectData, grid: pd.DataFrame) -> tuple[object, ...]:
    """Populate the edit form + state from the selected grid row.

    Gradio delivers a pandas DataFrame when ``headers`` are set, so the row is
    read via iloc — positional, the header labels never matter.
    """
    row_index = evt.index[0]
    row = grid.iloc[row_index]
    supplier_id = int(row.iloc[0])  # "ID" column — positional, labels never matter
    with SessionLocal() as session:
        supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        return (0, "", "", "", "", "", "", "", "", "", 0.0, "", "")
    return (
        supplier.id,
        supplier.code,
        supplier.business_name,
        supplier.cuit or "",
        supplier.contact_name or "",
        supplier.phone or "",
        supplier.whatsapp or "",
        supplier.email or "",
        supplier.address or "",
        supplier.iva_condition.value if supplier.iva_condition else "",
        float(supplier.default_margin_pct),
        supplier.terms or "",
        supplier.moneda or "",
    )


def _supplier_code_suggestion(business_name: str) -> str:
    """Reactive code assistant: suggest a 3-char code from the business name."""
    return suggest_code(business_name)


# Form fields (name/code/cuit/contact/phone/whatsapp/email/address/iva/margin/
# terms/moneda) restored after a successful create so the next save starts a
# new supplier.
_CLEARED_SUPPLIER_FORM = ("", "", "", "", "", "", "", "", "", 0.0, "", "")


def _save_supplier(
    supplier_id: object,
    business_name: str,
    code: str,
    cuit: str,
    contact_name: str,
    phone: str,
    whatsapp: str,
    email: str,
    address: str,
    iva_condition: str,
    margin: float,
    terms: str,
    moneda: str,
    status_filter: str = "ACTIVO",
) -> tuple[str, list[list[object]], tuple[object, ...], int]:
    """Save (create or update) a supplier and return the form state to render.

    Returns ``(message, grid, selected_id, *form_values)`` matching the
    ``supplier_save.click`` outputs (status, grid, state, then the 12 form
    fields in wiring order). A successful create clears the form and resets
    the selection to 0 so the next save is a new supplier; a successful update
    or a validation error echoes the submitted values back so the form stays
    as the user left it.
    """
    submitted = (
        business_name,
        code,
        cuit,
        contact_name,
        phone,
        whatsapp,
        email,
        address,
        iva_condition,
        margin,
        terms,
        moneda,
    )
    current_id = int(str(supplier_id or 0))
    with SessionLocal() as session:
        try:
            if current_id:
                update_supplier(
                    session,
                    current_id,
                    business_name=business_name,
                    code=code,
                    cuit=cuit,
                    contact_name=contact_name,
                    phone=phone,
                    whatsapp=whatsapp,
                    email=email,
                    address=address,
                    iva_condition=iva_condition,
                    default_margin_pct=Decimal(str(margin)),
                    terms=terms,
                    moneda=moneda,
                )
                message = "Supplier saved"
                form_values, selected_id = submitted, current_id
            else:
                created = create_supplier(
                    session,
                    business_name=business_name,
                    code=code or None,
                    cuit=cuit,
                    contact_name=contact_name,
                    phone=phone,
                    whatsapp=whatsapp,
                    email=email,
                    address=address,
                    iva_condition=iva_condition,
                    default_margin_pct=Decimal(str(margin)),
                    terms=terms,
                    moneda=moneda,
                )
                message = f"Supplier created (code {created.code})"
                form_values, selected_id = _CLEARED_SUPPLIER_FORM, 0
            session.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            return (
                f"Error: {exc}",
                _suppliers_grid("", status_filter),
                current_id,
                *submitted,
            )
    return message, _suppliers_grid("", status_filter), selected_id, *form_values


def _supplier_toggle(supplier_id: object) -> str:
    target_id = int(str(supplier_id or 0))
    if not target_id:
        return "Select a supplier row first"
    with SessionLocal() as session:
        try:
            supplier = toggle_status(session, target_id)
            session.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            return f"Error: {exc}"
    return f"Supplier {supplier.id} is now {supplier.status.value}"


def _price_list_choices() -> list[dict[str, object]]:
    with SessionLocal() as session:
        return list_price_lists(session)


def _price_list_dropdown_choices() -> list[tuple[str, int]]:
    """Build fresh ``(label, id)`` choices showing each list's % in the label.

    Storage holds a FRACTION (0.10 = 10%, -0.05 = 5% surcharge), so the label
    converts back to % points: ``"Gremio A (10%)"``, ``"Default (0%)"``.
    """
    choices: list[tuple[str, int]] = []
    for lista in _price_list_choices():
        pct = f"{Decimal(str(lista['descuento_lista_pct'])) * 100:f}"
        if "." in pct:  # "0" has no decimal point; don't rstrip it to ""
            pct = pct.rstrip("0").rstrip(".")
        choices.append((f"{lista['nombre']} ({pct}%)", int(str(lista["lista_id"]))))
    return choices


def _price_lists_grid() -> list[list[object]]:
    """Render the price lists for the Settings block (discount shown as % points).

    Storage holds a FRACTION (0.10 = 10%, -0.05 = 5% surcharge), so the grid
    multiplies by 100; the save handler divides back.
    """
    with SessionLocal() as session:
        rows = list_price_list_rows(session)
    return [
        [
            r["lista_id"],
            r["nombre"],
            float(Decimal(str(r["descuento_lista_pct"])) * 100),
            r["clientes"],
        ]
        for r in rows
    ]


def _price_list_row_selected(evt: gr.SelectData, grid: pd.DataFrame) -> tuple[object, ...]:
    """Populate the price-list form + state from the selected grid row.

    Gradio delivers a pandas DataFrame when ``headers`` are set, so the row is
    read via iloc — positional, the header labels never matter.
    """
    row_index = evt.index[0]
    row = grid.iloc[row_index]
    lista_id = int(row.iloc[0])  # "ID" column — positional, labels never matter
    with SessionLocal() as session:
        lista = session.get(ListaPrecios, lista_id)
    if lista is None:
        return 0, "", 0.0
    return lista.lista_id, lista.nombre, float(lista.descuento_lista_pct) * 100


def _save_price_list(
    price_list_id: object,
    nombre: str,
    descuento_pct: float | None,
) -> tuple[str, list[list[object]], int, str, float, list[tuple[str, int]]]:
    """Save (create or update) a price list, converting % points → fraction.

    Returns ``(message, grid, selected_id, nombre, descuento_pct, dropdown_choices)``
    matching the ``price_list_save.click`` outputs; the trailing choices refresh the
    client-form dropdown so new lists are selectable right away. A successful create
    clears the form and resets the selection to 0 so the next save is a new list; a
    successful update or a validation error echoes the submitted values back so the
    form stays as the user left it.
    """
    submitted_pct = float(descuento_pct) if descuento_pct is not None else 0.0
    submitted = (nombre or "", submitted_pct)
    current_id = int(str(price_list_id or 0))
    with SessionLocal() as session:
        try:
            fraction = None if descuento_pct is None else Decimal(str(descuento_pct)) / 100
            if current_id:
                update_price_list(session, current_id, nombre=nombre or "", descuento_pct=fraction)
                message = "Lista guardada"
                selected_id, form_values = current_id, submitted
            else:
                created = create_price_list(session, nombre=nombre or "", descuento_pct=fraction)
                message = f"Lista creada: {created.nombre}"
                selected_id, form_values = 0, ("", 0.0)
            session.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            return (
                f"Error: {exc}",
                _price_lists_grid(),
                current_id,
                *submitted,
                _price_list_dropdown_choices(),
            )
    return (
        message,
        _price_lists_grid(),
        selected_id,
        *form_values,
        _price_list_dropdown_choices(),
    )


def _delete_price_list(
    price_list_id: object,
) -> tuple[str, list[list[object]], int, str, float, list[tuple[str, int]]]:
    """Delete the selected price list (refused while clients reference it).

    Returns ``(message, grid, selected_id, nombre, descuento_pct, dropdown_choices)``:
    a successful delete clears the form and the selection; an error keeps the
    selection and restores the row's values. The trailing choices refresh the
    client-form dropdown so removed lists disappear from it.
    """
    target_id = int(str(price_list_id or 0))
    if not target_id:
        return (
            "Seleccione una fila de la grilla primero",
            _price_lists_grid(),
            0,
            "",
            0.0,
            _price_list_dropdown_choices(),
        )
    with SessionLocal() as session:
        try:
            nombre = delete_price_list(session, target_id)
            session.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            with SessionLocal() as fresh:
                lista = fresh.get(ListaPrecios, target_id)
            if lista is not None:
                form: tuple[str, float] = (
                    lista.nombre,
                    float(lista.descuento_lista_pct) * 100,
                )
            else:
                form = ("", 0.0)
            return (
                f"Error: {exc}",
                _price_lists_grid(),
                target_id,
                *form,
                _price_list_dropdown_choices(),
            )
    return (
        f"Lista eliminada: {nombre}",
        _price_lists_grid(),
        0,
        "",
        0.0,
        _price_list_dropdown_choices(),
    )


# Position of the "Editar" affordance column in the customer-orders grid
# (same positional contract as the row selection: id at 0, estado at 2).
_ORDER_GRID_EDIT_INDEX = 6
_ORDER_EDIT_MARKER = "✏️"


def _order_edit_marker(estado: object) -> str:
    """Render the "Editar" grid cell: a pencil for DRAFT rows, empty otherwise.

    The marker is decided by ``is_editable_order`` (DRAFT-only, mirroring the
    DRAFT-only ``update_manual_order`` use case), so the grid never offers an
    edit affordance for states the backend would refuse.
    """
    return _ORDER_EDIT_MARKER if is_editable_order(str(estado)) else ""


def _customer_orders_grid() -> list[list[object]]:
    """Render persisted customer orders for the seventh tab.

    The final "Editar" column marks the rows the manual form can modify
    (DRAFT only); clicking that cell loads the draft into the entry form.
    """
    with SessionLocal() as session:
        rows = list_customer_orders(session)
    return [
        [
            row["order_id"],
            row["customer"],
            row["estado"],
            row["subtotal"] or "—",
            row["total"] or "—",
            row["conversion_pending"],
            _order_edit_marker(row["estado"]),
        ]
        for row in rows
    ]


def _order_action(
    action: Callable[[Session, int], str],
) -> Callable[[object], str]:
    """Wrap a fulfillment action: open a session, run it, surface errors."""

    def run(order_id: object) -> str:
        with SessionLocal() as session:
            try:
                return action(session, int(str(order_id)))
            except Exception as exc:  # noqa: BLE001 — surfaced in the UI
                session.rollback()
                return f"Error: {exc}"

    return run


def _order_action_with_diagram(
    action: Callable[[Session, int], str],
) -> Callable[[object], tuple[str, str]]:
    """Wrap a fulfillment action: run it, then refresh the state diagram."""

    def run(order_id: object) -> tuple[str, str]:
        status = _order_action(action)(order_id)
        return status, _order_state_diagram(order_id)

    return run


def _order_state_diagram(order_id: object) -> str:
    """Render the state-progress diagram of the selected customer order."""
    if not order_id:
        return order_state_diagram("")
    with SessionLocal() as session:
        try:
            row = order_detail(session, int(str(order_id)))
        except KeyError:
            return order_state_diagram("")
    return order_state_diagram(str(row["estado"]))


def _order_row_selected(evt: gr.SelectData) -> tuple[object, str, str, list[list[object]]]:
    """Populate the Customer Orders tab from the clicked row in the orders grid.

    Returns ``(selected_order_id, legal_actions_label, state_diagram, detail_rows)``.
    Deselecting a row (or an event without a usable row) clears the whole panel.
    """
    order_id = None
    if getattr(evt, "selected", False) and getattr(evt, "row_value", None):
        order_id = evt.row_value[0]  # "Order" column — positional, labels never matter
    return (
        order_id,
        _legal_actions_label(order_id),
        _order_state_diagram(order_id),
        _customer_order_detail_grid(order_id),
    )


def _order_edit_request(col_index: object, row_value: object) -> int | None:
    """Pure decision for the orders-grid edit flow: which click means "edit".

    Returns the order id to load into the manual form when the click landed on
    the "Editar" column of a DRAFT row; ``None`` for every other case (data
    columns, non-DRAFT rows, malformed events), which the caller renders as a
    no-op. Positions follow the grid contract: order id at column 0, estado at
    column 2 — labels never matter, indexes do.
    """
    if not isinstance(row_value, (list, tuple)) or not row_value:
        return None
    try:
        if int(str(col_index)) != _ORDER_GRID_EDIT_INDEX:
            return None
    except (TypeError, ValueError):
        return None
    if len(row_value) <= _ORDER_GRID_EDIT_INDEX:
        return None
    if not is_editable_order(str(row_value[2])):
        return None
    try:
        return int(str(row_value[0]))
    except (TypeError, ValueError):
        return None


def _order_edit_selected(evt: gr.SelectData) -> tuple[object, ...]:
    """Second select listener on the orders grid: the "Editar" cell opens the form.

    Both listeners fire on every selection; this one loads the DRAFT into the
    manual form — the same load path as "Cargar borrador" — and switches to the
    "order-entry" sub-tab (``gr.Tabs`` as target with ``selected=``) only when
    the click landed on the edit column of a DRAFT row. Every other click is a
    full no-op via ``gr.skip()``.
    """
    no_op = tuple(gr.skip() for _ in range(6))
    if not getattr(evt, "selected", False):
        return no_op
    index = getattr(evt, "index", None)
    col_index = index[1] if isinstance(index, (list, tuple)) and len(index) > 1 else None
    order_id = _order_edit_request(col_index, getattr(evt, "row_value", None))
    if order_id is None:
        return no_op
    lines, grid, loaded_id, customer_id, status = _load_manual_draft(order_id)
    return gr.Tabs(selected="order-entry"), lines, grid, loaded_id, customer_id, status


def _legal_actions_label(order_id: object) -> str:
    """Show the legal fulfillment actions of the selected order (state-driven)."""
    if not order_id:
        return "Seleccioná un pedido."
    with SessionLocal() as session:
        try:
            row = order_detail(session, int(str(order_id)))
        except KeyError:
            return "Pedido inexistente."
    actions = legal_actions(row["estado"])
    if not actions:
        return f"Estado {row['estado']}: sin acciones de cumplimiento."
    return f"Acciones legales: {', '.join(actions)}."


def _customer_order_detail_grid(order_id: object) -> list[list[object]]:
    """Render the frozen lines for the selected customer order."""
    if not order_id:
        return []
    with SessionLocal() as session:
        try:
            detail = order_detail(session, int(str(order_id)))
        except KeyError:
            return []
    return [
        [
            line["sku"],
            line["codigo_proveedor"],
            line["name"] or "—",
            line["cantidad"],
            line["moneda"] or "—",
            line["precio_original"] or "—",
            line["margin_pct"] or "—",
            line["base_price"] or "—",
            line["final_price"] or "—",
            line["line_total"] or "—",
        ]
        for line in cast(list[dict[str, object]], detail["lines"])
    ]


def _manual_lines_view(rows: object) -> list[list[object]]:
    """Coerce the manual lines state into fresh [sku, cantidad, origen] rows.

    Three columns is the canonical shape (source-aware lines); two-column rows
    from an older saved state default to LOCAL.
    """
    view: list[list[object]] = []
    for row in rows if isinstance(rows, list) else []:
        if not row:
            continue
        source = str(row[2]).strip().upper() if len(row) > 2 else "LOCAL"
        view.append([row[0], row[1], source])
    return view


def _manual_inputs_view(rows: object) -> list[ManualLineInput]:
    """Map the manual lines state into source-tagged use-case inputs."""
    return [
        ManualLineInput(sku=str(row[0]), cantidad=int(str(row[1])), source=str(row[2]))
        for row in _manual_lines_view(rows)
    ]


def _client_dropdown_choices() -> list[tuple[str, int]]:
    """Client choices for the manual order form: name label, id value."""
    with SessionLocal() as session:
        return [
            (str(row["nombre_comercial"]), int(str(row["customer_id"])))
            for row in list_clients(session)
        ]


def _manual_product_search(
    proveedor: str, marca: str, categoria: str, codigo: str, texto: str, limit: float | None
) -> tuple[list[list[object]], list[dict[str, object]], str]:
    """Search LOCAL inventory + RAG catalog for the manual order form (no LLM).

    LOCAL hits come first and carry their stock; the same article may appear
    once per source — the owner picks which line completes the order.
    """
    try:
        with SessionLocal() as session:
            hits = search_order_products_action(
                session,
                proveedor=str(proveedor or ""),
                marca=str(marca or ""),
                categoria=str(categoria or ""),
                codigo=str(codigo or ""),
                texto=str(texto or ""),
                limit=int(limit) if limit else 100,
            )
    except ValueError as exc:
        return [], [], f"Error: {exc}"
    if not hits:
        return [], [], "Sin resultados para los filtros indicados."
    grid = [
        [
            hit["source"],
            hit["display_code"],
            hit["name"],
            hit["marca"] or "—",
            hit["categoria"] or "—",
            hit["supplier"] or "—",
            hit["price"],
            hit["moneda"] or "—",
            hit["stock"] if hit["stock"] is not None else "",
        ]
        for hit in hits
    ]
    return grid, hits, f"{len(hits)} producto(s) encontrado(s) — LOCAL primero, RAG después."


def _manual_order_add_line(
    rows: object,
    sku: object,
    cantidad: object,
    source: object,
) -> tuple[list[list[object]], list[list[object]], str, float, str]:
    """Append (or accumulate) a source-tagged line to the manual-order state.

    Returns ``(state_rows, grid_rows, sku_reset, qty_reset, status)``. Lines
    accumulate per (SKU, origen): the same SKU may hold one LOCAL and one RAG
    line at once (stock + RAG remainder); invalid input keeps the lines
    untouched and explains why in the status box.
    """
    current = _manual_lines_view(rows)
    sku_text = str(sku or "").strip()
    source_text = str(source or "LOCAL").strip().upper()
    if not sku_text:
        return current, _manual_lines_view(current), "", 1.0, "Ingresá el SKU del producto."
    if source_text not in ("LOCAL", "RAG"):
        return current, _manual_lines_view(current), "", 1.0, "El origen debe ser LOCAL o RAG."
    try:
        quantity = int(str(cantidad))
    except ValueError:
        return current, _manual_lines_view(current), "", 1.0, "Ingresá una cantidad válida."
    if quantity <= 0:
        return current, _manual_lines_view(current), "", 1.0, "La cantidad debe ser mayor que cero."
    for row in current:
        if str(row[0]) == sku_text and str(row[2]) == source_text:
            row[1] = int(str(row[1])) + quantity
            break
    else:
        current.append([sku_text, quantity, source_text])
    return (
        current,
        _manual_lines_view(current),
        "",
        1.0,
        f"Línea agregada ({source_text}): {quantity} × {sku_text}.",
    )


def _manual_order_add_selected(
    index: object,
    hits: object,
    rows: object,
    cantidad: object,
) -> tuple[list[list[object]], list[list[object]], float, str]:
    """Add the product row selected in the search grid to the manual-order state.

    The result row already carries its source (LOCAL/RAG) and stored SKU, so
    the line is added as-is; quantity accumulation follows the same per-(SKU,
    origen) rule as the manual SKU entry.
    """
    current = _manual_lines_view(rows)
    if index is None or not isinstance(hits, list):
        return current, _manual_lines_view(current), 1.0, "Seleccioná un producto de la grilla primero."
    position = int(str(index))
    if position < 0 or position >= len(hits):
        return current, _manual_lines_view(current), 1.0, "Seleccioná un producto de la grilla primero."
    try:
        quantity = int(str(cantidad))
    except ValueError:
        return current, _manual_lines_view(current), 1.0, "Ingresá una cantidad válida."
    if quantity <= 0:
        return current, _manual_lines_view(current), 1.0, "La cantidad debe ser mayor que cero."
    hit = hits[position]
    sku_text = str(hit["sku"])
    source_text = str(hit["source"]).strip().upper()
    state, grid, _sku, _qty, status = _manual_order_add_line(
        current, sku_text, quantity, source_text
    )
    return state, grid, 1.0, status


def _manual_line_selected(evt: gr.SelectData) -> object:
    """Remember the index of the line selected in the manual-order lines grid."""
    if getattr(evt, "selected", False) and getattr(evt, "index", None):
        return int(evt.index[0])
    return None


def _manual_order_remove_line(
    index: object, rows: object
) -> tuple[list[list[object]], list[list[object]], str]:
    """Remove the manual-order line chosen in the lines grid (by stored index)."""
    current = _manual_lines_view(rows)
    if index is None:
        return current, _manual_lines_view(current), "Seleccioná una línea de la grilla primero."
    position = int(str(index))
    if position < 0 or position >= len(current):
        return current, _manual_lines_view(current), "Seleccioná una línea de la grilla primero."
    removed = current.pop(position)
    return current, _manual_lines_view(current), f"Línea quitada: {removed[0]}."


def _draft_banner_text(order_id: int) -> str:
    """Warning shown when the selected client already has a DRAFT in progress."""
    return (
        f"⚠️ El cliente ya tiene el pedido borrador #{order_id} en curso "
        "(se permite un solo borrador por cliente). Podés modificarlo o elegir otro cliente."
    )


def _client_open_draft(customer_id: object) -> int | None:
    """Id of the client's open DRAFT order, if any (thin session wrapper)."""
    if customer_id is None:
        return None
    with SessionLocal() as session:
        draft = open_draft_for_customer(session, int(str(customer_id)))
    return draft.order_id if draft is not None else None


def _manual_client_selected(customer_id: object) -> tuple[bool, str]:
    """Show the draft warning when the picked client already has a DRAFT.

    Wired to the client dropdown's ``.input`` (user-driven picks only), so a
    programmatic value set — e.g. ``Cargar borrador`` filling the client —
    does not re-show the banner over an already-loaded form.
    """
    draft_id = _client_open_draft(customer_id)
    if draft_id is None:
        return False, ""
    return True, _draft_banner_text(draft_id)


def _load_existing_draft(
    customer_id: object,
) -> tuple[object, str, object, object, object, object, str]:
    """Load the client's existing DRAFT into the form (banner "Modificar" path).

    Same load path as "Cargar borrador"; the banner is hidden on the way out.
    If the draft vanished meanwhile (confirmed/canceled in another session)
    nothing else changes — hiding the stale warning is the honest outcome.
    """
    order_id = _client_open_draft(customer_id)
    if order_id is None:
        return (
            gr.update(visible=False),
            "",
            gr.skip(),
            gr.skip(),
            gr.skip(),
            gr.skip(),
            gr.skip(),
        )
    lines, grid, loaded_id, loaded_customer, status = _load_manual_draft(order_id)
    return gr.update(visible=False), "", lines, grid, loaded_id, loaded_customer, status


def _dismiss_draft_banner() -> tuple[object, str]:
    """Hide the draft warning banner (the owner chose to ignore it)."""
    return gr.update(visible=False), ""


def _create_manual_order(
    customer_id: object, rows: object
) -> tuple[str, list[list[object]], list[list[object]], bool, str]:
    """Create the manual DRAFT order, refresh the orders grid, clear the form.

    Lines carry their source (LOCAL/RAG). Before hitting the backend, the
    one-DRAFT-per-customer guard is checked here: if the client already has a
    draft, the warning banner is shown and creation is not attempted (the
    backend error is the race backstop, not the UX). Other errors (unknown
    client/SKU, a missing exchange rate, ...) surface in the status box and
    keep the form intact so the owner can fix the input.
    """
    inputs = _manual_inputs_view(rows)
    if customer_id is None:
        return (
            "Seleccioná un cliente.",
            _customer_orders_grid(),
            _manual_lines_view(rows),
            False,
            "",
        )
    draft_id = _client_open_draft(customer_id)
    if draft_id is not None:
        return (
            f"El cliente ya tiene el pedido borrador #{draft_id}: no se creó uno nuevo.",
            _customer_orders_grid(),
            _manual_lines_view(rows),
            True,
            _draft_banner_text(draft_id),
        )
    with SessionLocal() as session:
        try:
            order = create_manual_order_action(session, int(str(customer_id)), inputs)
            message = f"Pedido #{order.order_id} creado (borrador) — total {order.total} ARS."
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            session.rollback()
            return (
                f"Error: {exc}",
                _customer_orders_grid(),
                _manual_lines_view(rows),
                False,
                "",
            )
    return message, _customer_orders_grid(), [], False, ""


def _load_manual_draft(
    order_id: object,
) -> tuple[list[list[object]], list[list[object]], int | None, int | None, str]:
    """Load a DRAFT order's lines into the manual form for modification.

    Returns ``(state_rows, grid_rows, loaded_order_id, customer_id, status)``.
    Only DRAFT orders can be modified: confirmed orders already converted
    their reservations and are refused here with a clear message.
    """
    if not order_id:
        return [], [], None, None, "Ingresá el número de pedido a modificar."
    with SessionLocal() as session:
        try:
            detail = order_detail(session, int(str(order_id)))
        except KeyError:
            return [], [], None, None, f"Pedido inexistente: {order_id}"
    if detail["estado"] != "DRAFT":
        estado = str(detail["estado"])
        return (
            [],
            [],
            None,
            None,
            f"Pedido #{order_id} está en estado {estado}: solo borradores se pueden modificar.",
        )
    lines = [
        [line["sku"], int(str(line["cantidad"])), str(line["source"] or "LOCAL").upper()]
        for line in cast(list[dict[str, object]], detail["lines"])
    ]
    return (
        lines,
        _manual_lines_view(lines),
        int(str(order_id)),
        cast(int | None, detail.get("customer_id")),
        f"Pedido #{order_id} cargado ({len(lines)} línea(s)). Agregá o quitá líneas y guardá.",
    )


def _save_manual_order_changes(
    order_id: object, rows: object
) -> tuple[str, list[list[object]], list[list[object]]]:
    """Persist the manual-form edits on a loaded DRAFT order and refresh grids.

    The form state is the target line set: new (SKU, origen) pairs are priced
    on save, removed lines disappear, quantities move. Errors keep the form
    intact; success clears it and refreshes the orders grid.
    """
    inputs = _manual_inputs_view(rows)
    if not order_id:
        return "Cargá un borrador con 'Cargar borrador' antes de guardar.", _customer_orders_grid(), _manual_lines_view(rows)
    with SessionLocal() as session:
        try:
            order = update_manual_order_action(session, int(str(order_id)), inputs)
            message = f"Pedido #{order.order_id} actualizado (borrador) — total {order.total} ARS."
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            session.rollback()
            return f"Error: {exc}", _customer_orders_grid(), _manual_lines_view(rows)
    return message, _customer_orders_grid(), []


def _exchange_rates_grid() -> list[list[object]]:
    """Render the exchange-rate table with ARS marked read-only."""
    with SessionLocal() as session:
        rows = list_exchange_rates(session)

    def _render_updated_at(value: object) -> str:
        """Show updated_at in Buenos Aires local time (stored UTC); None renders empty."""
        if not isinstance(value, datetime):
            return ""
        return to_buenos_aires(value).strftime("%Y-%m-%d %H:%M:%S")

    return [
        [
            row["currency"],
            row["rate_to_ars"],
            _render_updated_at(row["updated_at"]),
            row["editable"],
        ]
        for row in rows
    ]


def _default_margin_value() -> float:
    with SessionLocal() as session:
        return float(get_default_margin(session))


def _save_exchange_rate(
    currency: str, rate_to_ars: object
) -> tuple[str, list[list[object]], list[list[object]]]:
    """Save a rate, recompute pending orders, and refresh both grids."""
    with SessionLocal() as session:
        try:
            set_exchange_rate(session, currency, float(str(rate_to_ars)))
            recomputed = recompute_pending_conversion(session)
            session.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            session.rollback()
            return f"Error: {exc}", _exchange_rates_grid(), _customer_orders_grid()
    return (
        f"Rate {currency.strip().upper()} saved; recomputed {recomputed} pending order(s).",
        _exchange_rates_grid(),
        _customer_orders_grid(),
    )


def _save_default_margin(value: object) -> str:
    """Persist the default RAG margin setting."""
    with SessionLocal() as session:
        try:
            saved = set_default_margin(session, float(str(value)))
            session.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            session.rollback()
            return f"Error: {exc}"
    return f"Default margin saved: {saved}%"


GRID_SELECTION_CSS = """
/* Grid selection UX: Gradio's default selected-cell styling draws a per-cell
   ring plus floating menu/selection buttons (the side tabs), which do not
   identify the selected row. Replace it with a full-row highlight. */
[data-testid^="cell-"] .cell-menu-button,
[data-testid^="cell-"] .selection-button {
    display: none !important;
}
[data-testid^="cell-"].cell-selected,
[data-testid^="cell-"].cell-selected .text {
    --ring-color: var(--color-accent);
    box-shadow: none !important;
}
[data-testid^="cell-"].cell-selected .text {
    background: transparent !important;
}
.virtual-row:has(.body-cell.cell-selected) .body-cell {
    background: color-mix(
        in srgb, var(--color-accent) 16%, var(--background-fill-primary)
    ) !important;
}
.virtual-row:has(.body-cell.cell-selected) .body-cell:first-child {
    box-shadow: inset 3px 0 0 0 var(--color-accent) !important;
}
"""


def build_app(settings: Settings | None = None) -> gr.Blocks:
    """Construct the seven-tab Blocks tree (no server is started).

    Fase 4 gates the backoffice: when disabled the app refuses to build
    (``FeatureDisabledError``) — a clean stop at the boundary.
    """
    from src.features import require_fase

    cfg = settings or get_settings()
    require_fase(4, cfg)
    with gr.Blocks(title="Ferretería — Backoffice") as demo:
        gr.Markdown(
            f"# Backoffice Ferretería\n"
            f"Fase 4 habilitada: {cfg.fase4_enabled}. "
            "Los datos van a la base local (Postgres + pgvector)."
        )
        with gr.Tab("Productos"):
            gr.Markdown("### Búsqueda en catálogo (local y proveedores)")
            with gr.Group():
                with gr.Row():
                    pf_codigo = gr.Textbox(label="Código", placeholder="483-8")
                    pf_proveedor = gr.Textbox(label="Proveedor (código)", placeholder="SCO")
                    pf_marca = gr.Textbox(label="Marca", placeholder="Fischer")
                with gr.Row():
                    pf_categoria = gr.Textbox(label="Categoría", placeholder="Griferías")
                    pf_subcategoria = gr.Textbox(label="Subcategoría", placeholder="Monocomandos")
                with gr.Row():
                    pf_nombre = gr.Textbox(
                        label="Nombre",
                        placeholder="monocomando de cocina (similitud en proveedores)",
                        scale=3,
                    )
                    pf_scope = gr.Radio(
                        choices=[
                            ("Ambas", "both"),
                            ("Local", "local"),
                            ("Catálogo de proveedores", "prov"),
                        ],
                        value="both",
                        label="Ámbito",
                        scale=1,
                    )
                    productos_search_btn = gr.Button(
                        "Buscar", variant="primary", scale=0, min_width=150
                    )
            productos_search_status = gr.Textbox(label="Estado búsqueda", interactive=False)
            productos_grid = gr.Dataframe(
                headers=_PRODUCTOS_GRID_HEADERS,
                datatype=_PRODUCTOS_GRID_DATATYPES,
                value=_catalog_grid,
                label="Productos",
            )
            productos_search_btn.click(
                _productos_search,
                inputs=[
                    pf_codigo,
                    pf_proveedor,
                    pf_marca,
                    pf_categoria,
                    pf_subcategoria,
                    pf_nombre,
                    pf_scope,
                ],
                outputs=[productos_grid, productos_search_status],
            )

            gr.Markdown("### Catálogo y stock")
            with gr.Row():
                edit_sku = gr.Textbox(
                    label="Código de proveedor o SKU",
                    placeholder="AX 302-8 o CLV-001",
                )
                edit_stock = gr.Number(label="Stock", precision=0)
                edit_price = gr.Number(label="Precio lista")
                edit_margin = gr.Number(label="Margen %")
            catalog_save = gr.Button("Guardar cambios", variant="primary")
            catalog_status = gr.Textbox(label="Estado", interactive=False)
            catalog_save.click(
                _catalog_edit,
                inputs=[edit_sku, edit_stock, edit_price, edit_margin],
                outputs=catalog_status,
            )
            # Refresh pattern: icon-only 🔄, sm + scale=0 so it never stretches.
            with gr.Row():
                catalog_refresh = gr.Button(
                    "🔄", variant="secondary", size="sm", scale=0, min_width=48
                )
            catalog_refresh.click(_catalog_grid, outputs=productos_grid)

        with gr.Tab("Clientes"):
            gr.Markdown("### Clientes y listas de precios")
            clients_grid = gr.Dataframe(
                headers=["ID", "Nombre", "Teléfono", "Lista", "Descuento particular"],
                datatype=["number", "str", "str", "number", "str"],
                value=_clients_grid,
                label="Clientes",
            )
            with gr.Row():
                client_name = gr.Textbox(label="Nombre comercial")
                client_phone = gr.Textbox(label="Teléfono WhatsApp")
                client_list = gr.Dropdown(
                    choices=_price_list_dropdown_choices(),
                    label="Lista de precios",
                )
                client_discount = gr.Number(label="Descuento particular %", value=0)
            client_save = gr.Button("Registrar cliente", variant="primary")
            client_status = gr.Textbox(label="Estado", interactive=False)
            client_save.click(
                _register_client,
                inputs=[client_name, client_phone, client_list, client_discount],
                outputs=[
                    client_status,
                    clients_grid,
                    client_name,
                    client_phone,
                    client_list,
                    client_discount,
                ],
            )
            with gr.Row():
                client_refresh = gr.Button(
                    "🔄", variant="secondary", size="sm", scale=0, min_width=48
                )
            client_refresh.click(_clients_grid, outputs=clients_grid)
            client_refresh.click(_price_list_dropdown_choices, None, client_list)

        with gr.Tab("Proveedores"):
            gr.Markdown("### Datos maestros de proveedores")
            with gr.Row():
                supplier_search = gr.Textbox(label="Buscar (nombre, CUIT, código)", scale=3)
                supplier_status_filter = gr.Dropdown(
                    choices=_STATUS_CHOICES, value="ACTIVO", label="Estado", scale=1
                )
            suppliers_grid = gr.Dataframe(
                headers=[
                    "ID",
                    "Código",
                    "Nombre",
                    "CUIT",
                    "Contacto",
                    "Teléfono",
                    "Margen",
                    "IVA",
                    "Estado",
                ],
                datatype=["number", "str", "str", "str", "str", "str", "str", "str", "str"],
                value=lambda: _suppliers_grid("", "ACTIVO"),
                label="Proveedores",
            )
            supplier_state = gr.State(value=0)
            with gr.Row():
                supplier_name = gr.Textbox(label="Razón social")
                supplier_code = gr.Textbox(
                    label="Código (3 letras — sugerido a partir del nombre)", placeholder="ABC"
                )
                supplier_cuit = gr.Textbox(label="CUIT", placeholder="30-12345678-1")
            with gr.Row():
                supplier_contact = gr.Textbox(label="Nombre de contacto")
                supplier_phone = gr.Textbox(label="Teléfono (E.164)", placeholder="+54 11 4321-5678")
                supplier_whatsapp = gr.Textbox(label="WhatsApp", placeholder="+54 9 11 4321-5678")
                supplier_email = gr.Textbox(label="Email", placeholder="nombre@empresa.com.ar")
            with gr.Row():
                supplier_address = gr.Textbox(label="Dirección", scale=2)
                supplier_iva = gr.Dropdown(choices=_IVA_CHOICES, value="", label="Condición IVA")
                supplier_margin = gr.Number(label="Margen por defecto %", value=0.0)
                supplier_moneda = gr.Textbox(
                    label="Moneda (ARS/USD)", placeholder="USD", max_length=3
                )
                supplier_terms = gr.Textbox(label="Condiciones")
            supplier_status = gr.Textbox(label="Estado", interactive=False)
            with gr.Row():
                supplier_save = gr.Button("Guardar proveedor", variant="primary")
                supplier_toggle = gr.Button("Cambiar estado", variant="stop")
                supplier_refresh = gr.Button(
                    "🔄", variant="secondary", size="sm", scale=0, min_width=48
                )
            supplier_search.change(
                _suppliers_grid,
                inputs=[supplier_search, supplier_status_filter],
                outputs=suppliers_grid,
            )
            supplier_status_filter.change(
                _suppliers_grid,
                inputs=[supplier_search, supplier_status_filter],
                outputs=suppliers_grid,
            )
            supplier_name.change(
                _supplier_code_suggestion, inputs=[supplier_name], outputs=supplier_code
            )
            suppliers_grid.select(
                _supplier_row_selected,
                inputs=[suppliers_grid],
                outputs=[
                    supplier_state,
                    supplier_code,
                    supplier_name,
                    supplier_cuit,
                    supplier_contact,
                    supplier_phone,
                    supplier_whatsapp,
                    supplier_email,
                    supplier_address,
                    supplier_iva,
                    supplier_margin,
                    supplier_terms,
                    supplier_moneda,
                ],
            )
            supplier_save.click(
                _save_supplier,
                inputs=[
                    supplier_state,
                    supplier_name,
                    supplier_code,
                    supplier_cuit,
                    supplier_contact,
                    supplier_phone,
                    supplier_whatsapp,
                    supplier_email,
                    supplier_address,
                    supplier_iva,
                    supplier_margin,
                    supplier_terms,
                    supplier_moneda,
                    supplier_status_filter,
                ],
                outputs=[
                    supplier_status,
                    suppliers_grid,
                    supplier_state,
                    supplier_name,
                    supplier_code,
                    supplier_cuit,
                    supplier_contact,
                    supplier_phone,
                    supplier_whatsapp,
                    supplier_email,
                    supplier_address,
                    supplier_iva,
                    supplier_margin,
                    supplier_terms,
                    supplier_moneda,
                ],
            )
            supplier_toggle.click(
                _supplier_toggle, inputs=[supplier_state], outputs=supplier_status
            )
            supplier_refresh.click(
                _suppliers_grid,
                inputs=[supplier_search, supplier_status_filter],
                outputs=suppliers_grid,
            )

        with gr.Tab("Pedidos de clientes"):
            gr.Markdown("### Pedidos de clientes y mantenimiento de conversión")
            # Nested sub-tabs: consultation/fulfillment on one side, the manual
            # entry + draft modification form on the other. ``customer_orders_tabs``
            # is an event target: the grid's edit-select listener switches to
            # "order-entry" by returning ``gr.Tabs(selected="order-entry")``.
            with gr.Tabs() as customer_orders_tabs:
                with gr.Tab("Consulta de pedidos", id="orders-consult"):
                    customer_orders_grid = gr.Dataframe(
                        headers=[
                            "Pedido",
                            "Cliente",
                            "Estado",
                            "Subtotal (ARS)",
                            "Total (ARS)",
                            "Conversión pendiente",
                            "Editar",
                        ],
                        datatype=["number", "str", "str", "str", "str", "bool", "str"],
                        value=_customer_orders_grid,
                        label="Pedidos de clientes",
                    )
                    with gr.Row():
                        customer_orders_refresh = gr.Button(
                            "🔄", variant="secondary", size="sm", scale=0, min_width=48
                        )
                    customer_orders_refresh.click(
                        _customer_orders_grid, outputs=customer_orders_grid
                    )
                    selected_order_id = gr.State(None)
                    gr.Markdown("### Progreso del estado del pedido")
                    order_state_html = gr.HTML(value=order_state_diagram(""))
                    customer_order_detail_grid = gr.Dataframe(
                        headers=[
                            "SKU",
                            "Código proveedor",
                            "Nombre producto",
                            "Cantidad",
                            "Moneda",
                            "Precio original",
                            "Margen %",
                            "Precio lista (AR$)",
                            "Precio final (AR$)",
                            "Total línea (AR$)",
                        ],
                        datatype=[
                            "str",
                            "str",
                            "str",
                            "number",
                            "str",
                            "str",
                            "str",
                            "str",
                            "str",
                            "str",
                        ],
                        label="Líneas del pedido",
                    )

                    gr.Markdown("### Acciones de preparación y entrega")
                    order_action_status = gr.Textbox(label="Estado de la acción", interactive=False)
                    order_action_label = gr.Textbox(
                        label="Acciones disponibles para el pedido seleccionado",
                        interactive=False,
                    )
                    with gr.Row():
                        action_start_picking = gr.Button(
                            "Iniciar preparación (Confirmed → Picking)"
                        )
                        action_complete_picking = gr.Button(
                            "Completar preparación (Picking → Ready)"
                        )
                        action_deliver = gr.Button("Entregar (Ready → Closed)")
                        action_confirm = gr.Button(
                            "Confirmar pedido (Draft → Confirmed)", variant="primary"
                        )
                        action_cancel = gr.Button("Cancelar pedido", variant="stop")
                    customer_orders_grid.select(
                        _order_row_selected,
                        None,
                        [
                            selected_order_id,
                            order_action_label,
                            order_state_html,
                            customer_order_detail_grid,
                        ],
                    )
                    action_start_picking.click(
                        _order_action_with_diagram(start_picking_action),
                        inputs=selected_order_id,
                        outputs=[order_action_status, order_state_html],
                    )
                    action_complete_picking.click(
                        _order_action_with_diagram(complete_picking_action),
                        inputs=selected_order_id,
                        outputs=[order_action_status, order_state_html],
                    )
                    action_deliver.click(
                        _order_action_with_diagram(deliver_order_action),
                        inputs=selected_order_id,
                        outputs=[order_action_status, order_state_html],
                    )
                    action_confirm.click(
                        _order_action_with_diagram(partial(confirm_order_action, sheets=_SHEETS)),
                        inputs=selected_order_id,
                        outputs=[order_action_status, order_state_html],
                    )
                    action_cancel.click(
                        _order_action_with_diagram(cancel_order_action),
                        inputs=selected_order_id,
                        outputs=[order_action_status, order_state_html],
                    )

                with gr.Tab("Alta / Modificación de pedido", id="order-entry"):
                    gr.Markdown("### Alta manual de pedido (borrador)")
                    with gr.Row():
                        manual_client = gr.Dropdown(
                            choices=_client_dropdown_choices(), label="Cliente", scale=3
                        )
                        manual_client_refresh = gr.Button(
                            "🔄", variant="secondary", size="sm", scale=0, min_width=48
                        )
                    manual_lines_state = gr.State([])
                    manual_order_id_state = gr.State(None)
                    with gr.Row():
                        manual_line_sku = gr.Textbox(label="SKU", placeholder="CLV-001")
                        manual_line_qty = gr.Number(label="Cantidad", precision=0, value=1)
                        manual_line_source = gr.Dropdown(
                            choices=["LOCAL", "RAG"], value="LOCAL", label="Origen"
                        )
                        manual_add_line = gr.Button("Agregar línea")
                    manual_lines_grid = gr.Dataframe(
                        headers=["SKU", "Cantidad", "Origen"],
                        datatype=["str", "number", "str"],
                        value=[],
                        label="Líneas del pedido nuevo",
                    )
                    manual_remove_line = gr.Button("Quitar línea seleccionada", variant="stop")
                    manual_create = gr.Button("Crear pedido (borrador)", variant="primary")
                    manual_status = gr.Textbox(label="Estado del alta manual", interactive=False)
                    manual_selected_line = gr.State(None)
                    # One-DRAFT-per-customer warning: shown as soon as the owner
                    # picks a client that already has a draft, with a direct
                    # path to load it into the form instead of failing later.
                    with gr.Group(visible=False) as manual_draft_banner:
                        manual_draft_banner_text = gr.Markdown("")
                        with gr.Row():
                            manual_edit_existing = gr.Button(
                                "Modificar la orden existente", scale=0, min_width=220
                            )
                            manual_ignore_draft = gr.Button("Ignorar", scale=0, min_width=110)
                    manual_client_refresh.click(_client_dropdown_choices, None, manual_client)
                    manual_client.input(
                        _manual_client_selected,
                        inputs=[manual_client],
                        outputs=[manual_draft_banner, manual_draft_banner_text],
                    )
                    manual_edit_existing.click(
                        _load_existing_draft,
                        inputs=[manual_client],
                        outputs=[
                            manual_draft_banner,
                            manual_draft_banner_text,
                            manual_lines_state,
                            manual_lines_grid,
                            manual_order_id_state,
                            manual_client,
                            manual_status,
                        ],
                    )
                    manual_ignore_draft.click(
                        _dismiss_draft_banner,
                        None,
                        [manual_draft_banner, manual_draft_banner_text],
                    )
                    manual_add_line.click(
                        _manual_order_add_line,
                        inputs=[manual_lines_state, manual_line_sku, manual_line_qty, manual_line_source],
                        outputs=[
                            manual_lines_state,
                            manual_lines_grid,
                            manual_line_sku,
                            manual_line_qty,
                            manual_status,
                        ],
                    )
                    manual_lines_grid.select(
                        _manual_line_selected,
                        None,
                        manual_selected_line,
                    )
                    manual_remove_line.click(
                        _manual_order_remove_line,
                        inputs=[manual_selected_line, manual_lines_state],
                        outputs=[manual_lines_state, manual_lines_grid, manual_status],
                    )
                    manual_create.click(
                        _create_manual_order,
                        inputs=[manual_client, manual_lines_state],
                        outputs=[
                            manual_status,
                            customer_orders_grid,
                            manual_lines_state,
                            manual_draft_banner,
                            manual_draft_banner_text,
                        ],
                    )

                    gr.Markdown(
                        "### Búsqueda de productos para el pedido (inventario LOCAL + catálogo RAG)"
                    )
                    with gr.Row():
                        msf_proveedor = gr.Textbox(label="Proveedor (código)", placeholder="SCO")
                        msf_marca = gr.Textbox(label="Marca", placeholder="Fischer")
                        msf_categoria = gr.Textbox(label="Categoría", placeholder="Griferías")
                        msf_codigo = gr.Textbox(label="Código", placeholder="483-8")
                    msf_texto = gr.Textbox(
                        label="Texto (LOCAL busca por nombre; RAG por descripción)",
                        placeholder="monocomando de cocina",
                    )
                    with gr.Row():
                        manual_search_btn = gr.Button("Buscar productos", variant="primary")
                        msf_limit = gr.Number(
                            label="Máx. resultados por origen", value=100, precision=0
                        )
                    manual_search_grid = gr.Dataframe(
                        headers=[
                            "Origen",
                            "Código",
                            "Nombre",
                            "Marca",
                            "Categoría",
                            "Proveedor",
                            "Precio",
                            "Moneda",
                            "Stock",
                        ],
                        datatype=["str", "str", "str", "str", "str", "str", "number", "str", "number"],
                        label="Resultados (LOCAL primero, RAG después)",
                    )
                    manual_search_state = gr.State([])
                    manual_selected_result = gr.State(None)
                    with gr.Row():
                        manual_result_qty = gr.Number(
                            label="Cantidad a agregar", precision=0, value=1
                        )
                        manual_add_selected = gr.Button("Agregar seleccionada al pedido")
                    manual_search_btn.click(
                        _manual_product_search,
                        inputs=[msf_proveedor, msf_marca, msf_categoria, msf_codigo, msf_texto, msf_limit],
                        outputs=[manual_search_grid, manual_search_state, manual_status],
                    )
                    manual_search_grid.select(
                        _manual_line_selected,
                        None,
                        manual_selected_result,
                    )
                    manual_add_selected.click(
                        _manual_order_add_selected,
                        inputs=[manual_selected_result, manual_search_state, manual_lines_state, manual_result_qty],
                        outputs=[manual_lines_state, manual_lines_grid, manual_result_qty, manual_status],
                    )

                    gr.Markdown("### Modificar pedido en borrador")
                    with gr.Row():
                        manual_edit_order_id = gr.Number(
                            label="Nº de pedido (borrador)", precision=0, value=None
                        )
                        manual_load_draft = gr.Button("Cargar borrador")
                        manual_save_draft = gr.Button(
                            "Guardar cambios del borrador", variant="secondary"
                        )
                    manual_load_draft.click(
                        _load_manual_draft,
                        inputs=[manual_edit_order_id],
                        outputs=[
                            manual_lines_state,
                            manual_lines_grid,
                            manual_order_id_state,
                            manual_client,
                            manual_status,
                        ],
                    )
                    manual_save_draft.click(
                        _save_manual_order_changes,
                        inputs=[manual_order_id_state, manual_lines_state],
                        outputs=[manual_status, customer_orders_grid, manual_lines_state],
                    )
                    # The edit-select listener needs the manual-form components
                    # above, so it is wired here — after both sub-tabs rendered.
                    # Both listeners fire on every grid selection; the no-op
                    # path returns gr.skip() for every output.
                    customer_orders_grid.select(
                        _order_edit_selected,
                        None,
                        [
                            customer_orders_tabs,
                            manual_lines_state,
                            manual_lines_grid,
                            manual_order_id_state,
                            manual_client,
                            manual_status,
                        ],
                    )

        with gr.Tab("Monitor de pedidos"):
            gr.Markdown("### Pedidos en vivo")
            orders_grid = gr.Dataframe(
                headers=["Pedido", "Cliente", "Estado", "Recotizar", "Reservas activas", "Sheets"],
                datatype=["number", "str", "str", "bool", "number", "bool"],
                value=_monitor_grid,
                label="Pedidos",
            )
            with gr.Row():
                monitor_refresh = gr.Button(
                    "🔄", variant="secondary", size="sm", scale=0, min_width=48
                )
            monitor_refresh.click(_monitor_grid, outputs=orders_grid)

        with gr.Tab("Órdenes de compra"):
            gr.Markdown("### Órdenes de compra a proveedores")
            po_grid = gr.Dataframe(
                headers=["PO", "Proveedor", "Estado", "Artículos", "Recibido"],
                datatype=["number", "str", "str", "str", "str"],
                value=_po_grid,
                label="Órdenes de compra",
            )
            with gr.Row():
                po_refresh = gr.Button(
                    "🔄", variant="secondary", size="sm", scale=0, min_width=48
                )
            po_refresh.click(_po_grid, outputs=po_grid)
            po_detail_grid = gr.Dataframe(
                headers=[
                    "SKU",
                    "Código proveedor",
                    "Nombre producto",
                    "Cantidad pedida",
                    "Cantidad recibida",
                ],
                datatype=["str", "str", "str", "number", "number"],
                label="Detalle de la orden",
            )
            po_grid.select(_po_row_selected, None, po_detail_grid)
            with gr.Row():
                po_id = gr.Number(label="ID de PO", precision=0, value=1)
                po_sku = gr.Textbox(label="SKU recibido", placeholder="CLV-001")
                po_qty = gr.Number(label="Cantidad recibida", precision=0, value=0)
            with gr.Row():
                po_send = gr.Button("Enviar a proveedor (OPEN → SENT)")
                po_receive = gr.Button("Registrar recepción (parcial/total)")
                po_cancel = gr.Button("Cancelar PO", variant="stop")
            po_status = gr.Textbox(label="Ejecución", interactive=False)
            po_send.click(_po_send, inputs=[po_id], outputs=po_status)
            po_receive.click(_po_receive, inputs=[po_id, po_sku, po_qty], outputs=po_status)
            po_cancel.click(_po_cancel, inputs=[po_id], outputs=po_status)

        with gr.Tab("Ingesta de remitos"):
            gr.Markdown("### Carga de remito / factura del proveedor (con RAG)")
            # Icon-only refresh beside the dropdown: scale=0 + sm keeps the
            # button compact instead of stretching to the container width.
            with gr.Row():
                # Placeholder first AND default: Gradio auto-selects the first
                # choice, and the literal maps to itself so the owner always
                # sees "seleccionar proveedor" until an explicit pick happens.
                supplier_selector = gr.Dropdown(
                    choices=_ingesta_supplier_choices(),
                    value=_SUPPLIER_PLACEHOLDER,
                    label="Proveedor (activo)",
                    scale=3,
                )
                supplier_refresh = gr.Button(
                    "🔄", variant="secondary", size="sm", scale=0, min_width=48
                )
            supplier_refresh.click(
                partial(_supplier_choices_update, include_placeholder=True),
                outputs=supplier_selector,
            )
            upload = gr.UploadButton("Subir documento", file_types=["image", ".pdf"])
            preview_grid = gr.Dataframe(
                headers=["Código", "Descripción", "Cantidad", "Costo", "Resolución"],
                datatype=["str", "str", "number", "str", "str"],
                label="Revisión (resueltas / pendientes)",
                interactive=False,
            )
            parsed_state = gr.State(())  # supplier-independent parsed ReceiptLine rows
            resolved_state = gr.State(())
            preview_status = gr.Textbox(label="Análisis", interactive=False)
            with gr.Row():
                pending_line_index = gr.Number(label="Línea pendiente (nº)", precision=0, value=1)
                manual_query = gr.Textbox(
                    label="Buscar producto en RAG (código)", scale=3
                )
                manual_search_btn = gr.Button("Buscar", variant="secondary")
            manual_results = gr.Dataframe(
                headers=["Código", "Nombre", "Marca", "Precio", "node_id"],
                datatype=["str", "str", "str", "number", "str"],
                label="Candidatos RAG",
            )
            manual_results_state = gr.State(())
            manual_candidate_index = gr.State(None)
            manual_status = gr.Textbox(label="Búsqueda manual", interactive=False)
            with gr.Row():
                assign_btn = gr.Button("Asignar seleccionado a la línea", variant="secondary")
                mark_new_btn = gr.Button("➕ Marcar como nuevo", variant="secondary")
            confirm_button = gr.Button("Confirmar e Ingresar a Inventario", variant="primary")
            confirm_status = gr.Textbox(label="Ingreso", interactive=False)
            upload.upload(
                _ingest_parse,
                inputs=[gr.State(_get_rag_client()), upload, supplier_selector],
                outputs=[parsed_state, preview_status],
            ).then(
                _ingest_resolve,
                inputs=[
                    gr.State(_get_rag_client()),
                    parsed_state,
                    supplier_selector,
                    resolved_state,
                    preview_status,
                ],
                outputs=[preview_grid, resolved_state, preview_status],
            )
            supplier_selector.change(
                _ingest_resolve,
                inputs=[
                    gr.State(_get_rag_client()),
                    parsed_state,
                    supplier_selector,
                    resolved_state,
                    preview_status,
                ],
                outputs=[preview_grid, resolved_state, preview_status],
            )
            preview_grid.select(
                _pending_row_selected,
                [resolved_state, pending_line_index],
                [manual_results, manual_results_state, manual_status, pending_line_index],
            )
            manual_search_btn.click(
                _ingest_manual_search,
                inputs=[
                    gr.State(_get_rag_client()),
                    pending_line_index,
                    manual_query,
                    supplier_selector,
                ],
                outputs=[manual_results, manual_results_state, manual_status],
            )
            manual_results.select(
                _manual_row_selected,
                None,
                [manual_candidate_index],
            )
            assign_btn.click(
                _ingest_assign,
                inputs=[
                    resolved_state,
                    pending_line_index,
                    manual_candidate_index,
                    manual_results_state,
                ],
                outputs=[resolved_state, preview_grid, manual_status],
            )
            mark_new_btn.click(
                _ingest_mark_new,
                inputs=[resolved_state, pending_line_index],
                outputs=[resolved_state, preview_grid, manual_status],
            )
            confirm_button.click(
                _ingest_confirm,
                inputs=[resolved_state, supplier_selector, gr.State(_get_embedder())],
                outputs=confirm_status,
            )

        with gr.Tab("Ingesta de catálogo"):
            gr.Markdown(
                "### Ingesta del catálogo PDF del proveedor al índice RAG\n\n"
                "⚠️ **Atención:** en modo **reemplazo total** (default) la ingesta "
                "**reemplaza TODAS las filas indexadas previamente** para el código del "
                "proveedor.\n\n"
                "Si el catálogo del proveedor está dividido en varios PDFs, usá la "
                "**ingesta incremental**: declará el mismo Documento / lista en cada "
                "archivo y solo se reemplazarán las filas de ese documento, preservando "
                "las de los demás.\n\n"
                "El campo **Documento / lista** muestra los documentos ya indexados del "
                "proveedor elegido. Si escribís un nombre nuevo, se declara un documento "
                "nuevo (usá el mismo valor en cada archivo del mismo documento). "
                "Si lo dejás vacío, la ingesta usa el default **LISTA GENERAL**."
            )
            with gr.Row():
                catalog_supplier_selector = gr.Dropdown(
                    choices=_active_supplier_choices(),
                    label="Proveedor (activo)",
                    scale=3,
                )
                catalog_supplier_refresh = gr.Button(
                    "🔄", variant="secondary", size="sm", scale=0, min_width=48
                )
            catalog_supplier_refresh.click(
                _supplier_choices_update, outputs=catalog_supplier_selector
            )
            catalog_upload = gr.File(label="Catálogo PDF", file_types=[".pdf"])
            catalog_documento_id = gr.Dropdown(
                label="Documento / lista",
                value=DEFAULT_DOCUMENTO_ID,
                allow_custom_value=True,
                interactive=True,
                multiselect=False,
                info=(
                    "Default: LISTA GENERAL si lo dejás vacío. Documentos ya indexados "
                    "del proveedor, o un nombre nuevo para declarar un documento. "
                    "Identidad lógica declarada por vos: usá el mismo valor en cada "
                    "archivo del mismo documento."
                ),
            )
            catalog_incremental = gr.Checkbox(
                label="Ingesta incremental (reemplaza solo este documento)",
                value=False,
            )
            with gr.Accordion("Opciones avanzadas", open=False):
                catalog_start_page = gr.Number(
                    label="Página inicial",
                    value=1,
                    precision=0,
                    minimum=1,
                    info="Página del PDF donde empieza la ingesta (1-indexed).",
                )
                catalog_max_pages = gr.Number(
                    label="Máximo de páginas",
                    value=None,
                    precision=0,
                    minimum=1,
                    info="Cantidad máxima de páginas a procesar. Dejálo vacío = sin límite.",
                )
                catalog_skip_pages = gr.Textbox(
                    label="Páginas a saltar",
                    placeholder="ej: 1-2,4",
                    info="Rangos o páginas sueltas separadas por coma. Se ignoran del catálogo.",
                )
                catalog_no_vision = gr.Checkbox(
                    label="Solo texto (sin visión)",
                    value=False,
                    info=(
                        "Desactiva la extracción multimodal: el PDF se procesa solo "
                        "como texto (más rápido, menos precisión en tablas e imágenes)."
                    ),
                )
                catalog_marca = gr.Textbox(
                    label="Forzar marca",
                    placeholder="ej: BULON",
                    info="Vacío = no forzar. Si se indica, se aplica a todos los productos extraídos.",
                )
            catalog_ingest_btn = gr.Button("Ingestar catálogo", variant="primary")
            catalog_job_state = gr.State(None)
            catalog_ingest_status = gr.Textbox(label="Ingesta", interactive=False)
            catalog_check_btn = gr.Button("Consultar estado", variant="secondary")
            catalog_job_status = gr.Textbox(label="Estado del job", interactive=False)
            catalog_supplier_selector.change(
                _load_provider_documents,
                inputs=[gr.State(_get_rag_client()), catalog_supplier_selector],
                outputs=catalog_documento_id,
            )
            catalog_ingest_btn.click(
                _catalog_ingest,
                inputs=[
                    gr.State(_get_rag_client()),
                    catalog_upload,
                    catalog_supplier_selector,
                    catalog_documento_id,
                    catalog_incremental,
                    catalog_start_page,
                    catalog_max_pages,
                    catalog_skip_pages,
                    catalog_no_vision,
                    catalog_marca,
                ],
                outputs=[catalog_job_state, catalog_ingest_status],
            )
            catalog_check_btn.click(
                _catalog_job_status,
                inputs=[gr.State(_get_rag_client()), catalog_job_state],
                outputs=catalog_job_status,
            )

        with gr.Tab("Adopción desde RAG"):
            gr.Markdown("### Buscar en el catálogo RAG del proveedor y adoptar al inventario")
            adoption_query = gr.Textbox(
                label="Búsqueda (nombre, código, marca)", placeholder="tornillo autoperforante"
            )
            adoption_search_btn = gr.Button("Buscar en RAG", variant="primary")
            adoption_results = gr.Dataframe(
                headers=["Código", "Nombre", "Marca", "Categoría", "Precio", "Moneda"],
                datatype=["str", "str", "str", "str", "number", "str"],
                label="Resultados RAG",
            )
            adoption_results_state = gr.State(())
            adoption_search_status = gr.Textbox(label="Búsqueda", interactive=False)
            with gr.Row():
                adoption_stock = gr.Number(label="Stock inicial", value=1, precision=0)
                adoption_confirm_btn = gr.Button("Adoptar seleccionado", variant="primary")
            adoption_confirm_status = gr.Textbox(label="Adopción", interactive=False)
            adoption_selected_index = gr.State(None)
            adoption_search_btn.click(
                _adoption_search,
                inputs=[gr.State(_get_rag_client()), adoption_query],
                outputs=[adoption_results, adoption_results_state, adoption_search_status],
            )
            adoption_results.select(
                _adoption_row_selected,
                None,
                [adoption_selected_index],
            )
            adoption_confirm_btn.click(
                _adoption_confirm,
                inputs=[
                    adoption_results_state,
                    adoption_selected_index,
                    adoption_stock,
                    gr.State(_get_embedder()),
                ],
                outputs=[adoption_confirm_status],
            )

        with gr.Tab("Configuración"):
            gr.Markdown("### Tipos de cambio")
            exchange_rates_grid = gr.Dataframe(
                headers=["Moneda", "Cotización a ARS", "Actualizado", "Editable"],
                datatype=["str", "str", "str", "bool"],
                value=_exchange_rates_grid,
                label="Tipos de cambio (solo lectura para ARS)",
                interactive=False,
            )
            with gr.Row():
                exchange_currency = gr.Textbox(label="Código de moneda")
                exchange_rate = gr.Number(label="Cotización a ARS", minimum=0)
                exchange_save = gr.Button("Guardar cotización", variant="primary")
            exchange_status = gr.Textbox(label="Estado de tipos de cambio", interactive=False)
            exchange_save.click(
                _save_exchange_rate,
                inputs=[exchange_currency, exchange_rate],
                outputs=[exchange_status, exchange_rates_grid, customer_orders_grid],
            )

            gr.Markdown("### Margen RAG por defecto")
            with gr.Row():
                default_margin = gr.Number(
                    label="Margen por defecto (%)", value=_default_margin_value
                )
                default_margin_save = gr.Button("Guardar margen por defecto")
            default_margin_status = gr.Textbox(
                label="Estado del margen por defecto", interactive=False
            )
            default_margin_save.click(
                _save_default_margin,
                inputs=default_margin,
                outputs=default_margin_status,
            )

            gr.Markdown("### Listas de precios")
            price_lists_grid = gr.Dataframe(
                headers=["ID", "Nombre", "Descuento %", "Clientes"],
                datatype=["number", "str", "number", "number"],
                value=lambda: _price_lists_grid(),
                label="Listas de precios (0 = sin descuento, negativo = recargo)",
                interactive=False,
            )
            price_list_state = gr.State(value=0)
            with gr.Row():
                price_list_nombre = gr.Textbox(label="Nombre")
                price_list_descuento = gr.Number(
                    label="Descuento % (negativo = recargo)", value=0.0
                )
            price_list_status = gr.Textbox(label="Estado de listas de precios", interactive=False)
            with gr.Row():
                price_list_save = gr.Button("Guardar lista", variant="primary")
                price_list_delete = gr.Button("Eliminar lista", variant="stop")
                price_list_refresh = gr.Button(
                    "🔄", variant="secondary", size="sm", scale=0, min_width=48
                )
            price_lists_grid.select(
                _price_list_row_selected,
                inputs=[price_lists_grid],
                outputs=[price_list_state, price_list_nombre, price_list_descuento],
            )
            price_list_save.click(
                _save_price_list,
                inputs=[price_list_state, price_list_nombre, price_list_descuento],
                outputs=[
                    price_list_status,
                    price_lists_grid,
                    price_list_state,
                    price_list_nombre,
                    price_list_descuento,
                    client_list,
                ],
            )
            price_list_delete.click(
                _delete_price_list,
                inputs=[price_list_state],
                outputs=[
                    price_list_status,
                    price_lists_grid,
                    price_list_state,
                    price_list_nombre,
                    price_list_descuento,
                    client_list,
                ],
            )
            price_list_refresh.click(_price_lists_grid, None, price_lists_grid)

        with gr.Tab("Sesiones de Telegram"):
            gr.Markdown("### Sesiones de Telegram y trazas")
            initial_sessions = list_sessions()
            initial_selected = initial_sessions[0] if initial_sessions else None
            with gr.Row():
                session_selector = gr.Dropdown(
                    label="Sesiones activas / recientes",
                    choices=initial_sessions,
                    value=initial_selected,
                    interactive=True,
                    scale=3,
                )
                refresh_sessions_btn = gr.Button(
                    "🔄", variant="secondary", size="sm", scale=0, min_width=48
                )
            session_trace_grid = gr.Dataframe(
                headers=["Hora", "Servicio", "Acción", "Nivel", "Detalles"],
                datatype=["str", "str", "str", "str", "str"],
                value=session_events_grid(initial_selected),
                label="Trazas de eventos de la sesión",
                interactive=False,
            )

            def _on_session_select(sid: str | None) -> list[list[object]]:
                return session_events_grid(sid)

            def _on_refresh_sessions() -> tuple[object, list[list[object]]]:
                sids = list_sessions()
                sel = sids[0] if sids else None
                return gr.update(choices=sids, value=sel), session_events_grid(sel)

            session_selector.change(
                _on_session_select, inputs=session_selector, outputs=session_trace_grid
            )
            refresh_sessions_btn.click(
                _on_refresh_sessions, outputs=[session_selector, session_trace_grid]
            )
    return cast(gr.Blocks, demo)


def launch(*, server_name: str = "127.0.0.1", port: int = 7860) -> None:
    """Launch the backoffice UI (only when run explicitly)."""
    build_app().launch(
        server_name=server_name,
        server_port=port,
        css=GRID_SELECTION_CSS,
    )


if __name__ == "__main__":
    launch()
