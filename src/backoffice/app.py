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
from functools import lru_cache
from typing import cast

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
from src.backoffice.catalog import list_products, search_rag_products, update_margin, update_price, update_stock
from src.backoffice.clients import create_client, list_clients, list_price_lists
from src.backoffice.customer_orders import (
    cancel_order_action,
    complete_picking_action,
    deliver_order_action,
    get_default_margin,
    legal_actions,
    list_customer_orders,
    list_exchange_rates,
    order_detail,
    order_state_diagram,
    recompute_pending_conversion,
    set_default_margin,
    set_exchange_rate,
    start_picking_action,
)
from src.backoffice.ingestion import (
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


def _catalog_grid() -> list[list[object]]:
    with SessionLocal() as session:
        rows = list_products(session)
    return [
        [
            r["codigo_interno"],
            r["codigo_barras"],
            r["nombre_oficial"],
            r["costo_proveedor"],
            r["margen_aplicado_pct"],
            r["precio_lista_base"],
            r["stock_disponible"],
        ]
        for r in rows
    ]


def _rag_search(
    proveedor: str, marca: str, categoria: str, codigo: str, texto: str, limit: float | None
) -> tuple[list[list[object]], str]:
    """Consulta el catálogo RAG con filtros SQL directos (sin LLM)."""
    try:
        with SessionLocal() as session:
            rows = search_rag_products(
                session,
                codigo_proveedor=proveedor,
                marca=marca,
                categoria=categoria,
                codigo=codigo,
                texto=texto,
                limit=int(limit) if limit else 100,
            )
    except ValueError as exc:
        return [], f"Error: {exc}"
    if not rows:
        return [], "Sin resultados para los filtros indicados."
    grid = [
        [
            r["codigo"],
            r["codigo_orig"],
            r["proveedor"],
            r["marca"],
            r["categoria"],
            r["subcategoria"],
            r["precio"],
            r["moneda"],
            r["pagina"],
            r["archivo"],
        ]
        for r in rows
    ]
    return grid, f"{len(rows)} producto(s) encontrado(s)."


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


def _catalog_edit(sku: str, stock: int | None, price: float | None, margin: float | None) -> str:
    with SessionLocal() as session:
        try:
            if stock is not None:
                update_stock(session, sku, int(stock))
            if price is not None:
                update_price(session, sku, price)
            if margin is not None:
                update_margin(session, sku, margin)
            session.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            return f"Error: {exc}"
    return f"Guardado: {sku}"


# ------------------------------------------------ ingestion tab (RAG receipts)


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


def _supplier_choices_update() -> gr.Dropdown:
    """Re-query ACTIVO suppliers so the dropdown reflects additions at runtime.

    Gradio freezes ``choices=`` evaluated at Blocks build time; returning a
    component instance from a click handler updates only the given props.
    """
    return gr.Dropdown(choices=_active_supplier_choices())


def _resolved_grid(lines: Sequence[ResolvedLine]) -> list[list[object]]:
    """Render resolved/pending receipt lines for the review grid."""
    rows: list[list[object]] = []
    for resolved in lines:
        receipt = resolved.receipt
        if resolved.product is not None:
            resolution = f"{resolved.product.sku} — {resolved.product.name}"
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
) -> tuple[list[list[object]], tuple[ResolvedLine, ...], str]:
    """Upload → RAG parse → two-pass resolve → review grid. Zero writes.

    RAG/Luna unavailability surfaces as an honest error and writes nothing
    (spec: RAG down → no inventory/catalog write).
    """
    if upload is None:
        return [], (), "Subí un remito o factura (PDF o foto)."
    path = getattr(upload, "path", None) or str(upload)
    filename = os.path.basename(str(path))
    with open(str(path), "rb") as fh:
        content = fh.read()
    with SessionLocal() as session:
        try:
            supplier = ensure_active_supplier(session, int(str(supplier_id)))
        except (KeyError, SupplierInactiveError) as exc:
            return [], (), f"Error: {exc}"
        supplier_code = supplier.code
    try:
        document_lines = client.parse_document(
            filename=filename, content=content, codigo_proveedor=supplier_code
        )
    except RagProductError as exc:
        return [], (), f"Error: RAG no disponible ({exc})"
    receipt_lines = to_receipt_lines(document_lines)
    with SessionLocal() as session:
        resolved = resolve_lines(
            session, client, receipt_lines, supplier_id=int(str(supplier_id))
        )
    grid = _resolved_grid(resolved)
    pending = sum(1 for line in resolved if line.pending and line.receipt.cantidad > 0)
    message = (
        f"{len(grid)} líneas; {pending} pendiente(s) de resolver."
        if grid
        else "No se extrajeron líneas legibles."
    )
    return grid, resolved, message


def _ingest_manual_search(
    client: RagProductClient, line_index: object, query_text: object, supplier_id: object
) -> tuple[list[list[object]], tuple[RagProduct, ...], str]:
    """Per-line RAG product-code search for a pending line (supplier-scoped)."""
    line_idx = _as_index(line_index)
    if line_idx < 0:
        return [], (), "Seleccioná el número de línea pendiente (1-based)."
    text = str(query_text or "").strip()
    if not text:
        return [], (), "Escribí un código o término de búsqueda."
    with SessionLocal() as session:
        try:
            supplier = ensure_active_supplier(session, int(str(supplier_id)))
        except (KeyError, SupplierInactiveError) as exc:
            return [], (), f"Error: {exc}"
        supplier_code = supplier.code
    try:
        candidates = hybrid_candidates(client, supplier_code, text)
    except RagProductError as exc:
        return [], (), f"Error: RAG no disponible ({exc})"
    rows: list[list[object]] = [
        [
            product.sku,
            product.name,
            product.brand or "",
            product.price if product.price is not None else "",
            product.node_id or "",
        ]
        for product in candidates
    ]
    message = (
        f"{len(rows)} candidato(s) para la línea {line_idx + 1}. Seleccioná uno y asignalo."
        if rows
        else "Sin resultados: la línea sigue pendiente."
    )
    return rows, candidates, message


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
    line_idx = _as_index(line_index)
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
    lines[line_idx] = ResolvedLine(receipt=current.receipt, product=product)
    updated = tuple(lines)
    return updated, _resolved_grid(updated), f"Línea {line_idx + 1} asignada: {product.sku}"


def _ingest_confirm(state: object, supplier_id: object, embedder: Embedder) -> str:
    """Gated confirmation: blocked while any positive-qty line is unresolved."""
    lines = list(state) if isinstance(state, (tuple, list)) else []
    if not lines:
        return "Primero parseá un documento."
    pending = [line for line in lines if line.pending and line.receipt.cantidad > 0]
    if pending:
        detail = "; ".join(
            line.receipt.codigo_orig or line.receipt.descripcion for line in pending[:5]
        )
        return f"Ingreso bloqueado: líneas sin resolver: {detail}"
    with SessionLocal() as session:
        try:
            result = ingest_receipt_lines(
                session,
                int(str(supplier_id)),
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
        return (0, "", "", "", "", "", "", "", "", "", 0.0, "")
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
    )


def _supplier_code_suggestion(business_name: str) -> str:
    """Reactive code assistant: suggest a 3-char code from the business name."""
    return suggest_code(business_name)


# Form fields (name/code/cuit/contact/phone/whatsapp/email/address/iva/margin/terms)
# restored after a successful create so the next save starts a new supplier.
_CLEARED_SUPPLIER_FORM = ("", "", "", "", "", "", "", "", "", 0.0, "")


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
    status_filter: str = "ACTIVO",
) -> tuple[str, list[list[object]], tuple[object, ...], int]:
    """Save (create or update) a supplier and return the form state to render.

    Returns ``(message, grid, selected_id, *form_values)`` matching the
    ``supplier_save.click`` outputs (status, grid, state, then the 11 form
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


def _customer_orders_grid() -> list[list[object]]:
    """Render persisted customer orders for the seventh tab."""
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
            line["name"] or "—",
            line["cantidad"],
            line["precio_original"] or "—",
            line["margin_pct"] or "—",
            line["base_price"] or "—",
            line["line_total"] or "—",
        ]
        for line in cast(list[dict[str, object]], detail["lines"])
    ]


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
            gr.Markdown("### Catálogo y stock")
            catalog_grid = gr.Dataframe(
                headers=[
                    "SKU",
                    "Código barras",
                    "Nombre",
                    "Costo",
                    "Margen",
                    "Precio lista",
                    "Stock",
                ],
                datatype=["str", "str", "str", "str", "str", "str", "number"],
                value=_catalog_grid,
                label="Productos",
            )
            with gr.Row():
                edit_sku = gr.Textbox(label="SKU", placeholder="CLV-001")
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
            catalog_refresh = gr.Button("Refrescar")
            catalog_refresh.click(_catalog_grid, outputs=catalog_grid)

            gr.Markdown("### Consulta catálogo RAG (proveedores)")
            with gr.Row():
                ragf_proveedor = gr.Textbox(label="Proveedor (código)", placeholder="SCO")
                ragf_marca = gr.Textbox(label="Marca", placeholder="Fischer")
                ragf_categoria = gr.Textbox(label="Categoría", placeholder="Griferías")
                ragf_codigo = gr.Textbox(label="Código", placeholder="483-8")
            ragf_texto = gr.Textbox(
                label="Texto en descripción (substring)",
                placeholder="monocomando de cocina",
            )
            with gr.Row():
                rag_search_btn = gr.Button("Buscar en RAG", variant="primary")
                ragf_limit = gr.Number(label="Máx. resultados", value=100, precision=0)
            rag_grid = gr.Dataframe(
                headers=[
                    "Código",
                    "Cód. orig",
                    "Proveedor",
                    "Marca",
                    "Categoría",
                    "Subcategoría",
                    "Precio",
                    "Moneda",
                    "Pág.",
                    "Archivo",
                ],
                datatype=[
                    "str",
                    "str",
                    "str",
                    "str",
                    "str",
                    "str",
                    "number",
                    "str",
                    "number",
                    "str",
                ],
                label="Resultados RAG (Productos)",
            )
            rag_status = gr.Textbox(label="Estado RAG", interactive=False)
            rag_search_btn.click(
                _rag_search,
                inputs=[ragf_proveedor, ragf_marca, ragf_categoria, ragf_codigo, ragf_texto, ragf_limit],
                outputs=[rag_grid, rag_status],
            )

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
            client_refresh = gr.Button("Refrescar")
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
                supplier_terms = gr.Textbox(label="Condiciones")
            supplier_status = gr.Textbox(label="Estado", interactive=False)
            with gr.Row():
                supplier_save = gr.Button("Guardar proveedor", variant="primary")
                supplier_toggle = gr.Button("Cambiar estado", variant="stop")
                supplier_refresh = gr.Button("Refrescar")
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
            customer_orders_grid = gr.Dataframe(
                headers=[
                    "Pedido",
                    "Cliente",
                    "Estado",
                    "Subtotal (ARS)",
                    "Total (ARS)",
                    "Conversión pendiente",
                ],
                datatype=["number", "str", "str", "str", "str", "bool"],
                value=_customer_orders_grid,
                label="Pedidos de clientes",
            )
            customer_orders_refresh = gr.Button("Refrescar pedidos")
            customer_orders_refresh.click(_customer_orders_grid, outputs=customer_orders_grid)
            selected_order_id = gr.State(None)
            gr.Markdown("### Progreso del estado del pedido")
            order_state_html = gr.HTML(value=order_state_diagram(""))
            customer_order_detail_grid = gr.Dataframe(
                headers=[
                    "SKU",
                    "Nombre producto",
                    "Cantidad",
                    "Precio original",
                    "Margen %",
                    "Precio base/unit.",
                    "Total / producto",
                ],
                datatype=["str", "str", "number", "str", "str", "str", "str"],
                label="Líneas del pedido",
            )

            gr.Markdown("### Acciones de preparación y entrega")
            order_action_status = gr.Textbox(label="Estado de la acción", interactive=False)
            order_action_label = gr.Textbox(
                label="Acciones disponibles para el pedido seleccionado", interactive=False
            )
            with gr.Row():
                action_start_picking = gr.Button("Iniciar preparación (Confirmed → Picking)")
                action_complete_picking = gr.Button("Completar preparación (Picking → Ready)")
                action_deliver = gr.Button("Entregar (Ready → Closed)")
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
            action_cancel.click(
                _order_action_with_diagram(cancel_order_action),
                inputs=selected_order_id,
                outputs=[order_action_status, order_state_html],
            )

        with gr.Tab("Monitor de pedidos"):
            gr.Markdown("### Pedidos en vivo")
            orders_grid = gr.Dataframe(
                headers=["Pedido", "Cliente", "Estado", "Recotizar", "Reservas activas", "Sheets"],
                datatype=["number", "str", "str", "bool", "number", "bool"],
                value=_monitor_grid,
                label="Pedidos",
            )
            monitor_refresh = gr.Button("Refrescar")
            monitor_refresh.click(_monitor_grid, outputs=orders_grid)

        with gr.Tab("Órdenes de compra"):
            gr.Markdown("### Órdenes de compra a proveedores")
            po_grid = gr.Dataframe(
                headers=["PO", "Proveedor", "Estado", "Artículos", "Recibido"],
                datatype=["number", "str", "str", "str", "str"],
                value=_po_grid,
                label="Órdenes de compra",
            )
            po_refresh = gr.Button("Refrescar")
            po_refresh.click(_po_grid, outputs=po_grid)
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
            supplier_selector = gr.Dropdown(
                choices=_active_supplier_choices(),
                label="Proveedor (activo)",
            )
            supplier_refresh = gr.Button("Refrescar", variant="secondary")
            supplier_refresh.click(_supplier_choices_update, outputs=supplier_selector)
            upload = gr.UploadButton("Subir documento", file_types=["image", ".pdf"])
            preview_grid = gr.Dataframe(
                headers=["Código", "Descripción", "Cantidad", "Costo", "Resolución"],
                datatype=["str", "str", "number", "str", "str"],
                label="Revisión (resueltas / pendientes)",
                interactive=False,
            )
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
            assign_btn = gr.Button("Asignar seleccionado a la línea", variant="secondary")
            confirm_button = gr.Button("Confirmar e Ingresar a Inventario", variant="primary")
            confirm_status = gr.Textbox(label="Ingreso", interactive=False)
            upload.upload(
                _ingest_parse,
                inputs=[gr.State(_get_rag_client()), upload, supplier_selector],
                outputs=[preview_grid, resolved_state, preview_status],
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
            catalog_supplier_selector = gr.Dropdown(
                choices=_active_supplier_choices(),
                label="Proveedor (activo)",
            )
            catalog_supplier_refresh = gr.Button("Refrescar", variant="secondary")
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
                price_list_refresh = gr.Button("Refrescar")
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
                )
                refresh_sessions_btn = gr.Button("Refrescar sesiones")
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
