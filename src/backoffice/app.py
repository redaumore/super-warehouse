"""Backoffice Gradio app (task 3.5): six tabs for the owner.

A lightweight web interface with six tabs — Catalog, Clients, Orders/Monitor,
Purchase Orders, Ingestion and Suppliers — wired to the pure DB operations in
``src.backoffice``. The build function only constructs the Blocks tree (no
server); ``launch()`` is guarded so importing the module never starts a server,
which keeps tests and CI safe.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import cast

import gradio as gr
import pandas as pd

from src.agents.perception import VisionAnalyzer
from src.backoffice.catalog import list_products, update_margin, update_price, update_stock
from src.backoffice.clients import create_client, list_clients, list_price_lists
from src.backoffice.ingestion import confirm_items, to_grid_rows
from src.backoffice.monitor import list_orders
from src.backoffice.po import (
    cancel_po_action,
    list_purchase_orders,
    receive_po_action,
    send_po_action,
)
from src.backoffice.suppliers import (
    create_supplier,
    list_suppliers,
    toggle_status,
    update_supplier,
)
from src.config import Settings, get_settings
from src.db.models import IvaCondition, Supplier, SupplierStatus
from src.db.session import SessionLocal
from src.integrations.openai import OpenAIVisionAnalyzer
from src.integrations.sheets import SheetsWriter
from src.supplier.validation import suggest_code

_SHEETS = SheetsWriter()  # append-only; quarantines internally when unconfigured

_IVA_CHOICES = [("—", "")] + [(c.value, c.value) for c in IvaCondition]
_STATUS_CHOICES = ["All"] + [s.value for s in SupplierStatus]


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


def _register_client(nombre: str, telefono: str, lista_id: object, descuento: float) -> str:
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
            return f"Error: {exc}"
    return "Cliente registrado"


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


def _ingest_preview(analyzer: VisionAnalyzer, image_path: object) -> tuple[list[list[str]], str]:
    """Analyze an uploaded supplier document and render the editable preview."""
    if not image_path:
        return [], "Subí una foto del remito o factura."
    # Gradio delivers the upload as a file path (or FileData wrapper).
    path = getattr(image_path, "path", None) or str(image_path)
    from src.backoffice.ingestion import extract_document_items

    try:
        extraction = extract_document_items(analyzer, path)
    except Exception as exc:  # noqa: BLE001 — surfaced in the UI
        return [], f"Error al extraer: {exc}"
    grid = to_grid_rows(extraction)
    message = (
        f"{len(grid)} filas extraídas. Revisá y corregí antes de confirmar."
        if grid
        else "No se pudieron extraer filas legibles."
    )
    return grid, message


def _ingest_confirm(rows: object, supplier_id: object) -> str:
    if hasattr(rows, "iloc"):  # Gradio hands a pandas DataFrame when headers are set
        rows = [
            [None if pd.isna(cell) else cell for cell in row]
            for row in rows.itertuples(index=False, name=None)
        ]
    with SessionLocal() as session:
        try:
            result = confirm_items(session, rows or [], int(str(supplier_id)))
            session.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            return f"Error al ingresar: {exc}"
    return f"Ingresado: {result.updated} actualizados, {result.created} creados."


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
) -> tuple[str, list[list[object]]]:
    with SessionLocal() as session:
        try:
            if int(str(supplier_id or 0)):
                update_supplier(
                    session,
                    int(str(supplier_id)),
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
            session.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            return f"Error: {exc}", _suppliers_grid("", "All")
    return message, _suppliers_grid("", "All")


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


def build_app(settings: Settings | None = None) -> gr.Blocks:
    """Construct the six-tab Blocks tree (no server is started).

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
        with gr.Tab("Catalog"):
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

        with gr.Tab("Clients"):
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
                    choices=[
                        (f"{l['nombre']} (ID {int(str(l['lista_id']))})", int(str(l["lista_id"])))
                        for l in _price_list_choices()
                    ],
                    label="Lista de precios",
                )
                client_discount = gr.Number(label="Descuento particular %", value=0)
            client_save = gr.Button("Registrar cliente", variant="primary")
            client_status = gr.Textbox(label="Estado", interactive=False)
            client_save.click(
                _register_client,
                inputs=[client_name, client_phone, client_list, client_discount],
                outputs=client_status,
            )
            client_refresh = gr.Button("Refrescar")
            client_refresh.click(_clients_grid, outputs=clients_grid)

        with gr.Tab("Orders/Monitor"):
            gr.Markdown("### Pedidos en vivo")
            orders_grid = gr.Dataframe(
                headers=["Pedido", "Cliente", "Estado", "Recotizar", "Reservas activas", "Sheets"],
                datatype=["number", "str", "str", "bool", "number", "bool"],
                value=_monitor_grid,
                label="Pedidos",
            )
            monitor_refresh = gr.Button("Refrescar")
            monitor_refresh.click(_monitor_grid, outputs=orders_grid)

        with gr.Tab("Purchase Orders"):
            gr.Markdown("### Purchase orders to suppliers")
            po_grid = gr.Dataframe(
                headers=["PO", "Supplier", "Estado", "Artículos", "Recibido"],
                datatype=["number", "str", "str", "str", "str"],
                value=_po_grid,
                label="Órdenes de compra",
            )
            po_refresh = gr.Button("Refrescar")
            po_refresh.click(_po_grid, outputs=po_grid)
            with gr.Row():
                po_id = gr.Number(label="PO ID", precision=0, value=1)
                po_sku = gr.Textbox(label="SKU recibido", placeholder="CLV-001")
                po_qty = gr.Number(label="Cantidad recibida", precision=0, value=0)
            with gr.Row():
                po_send = gr.Button("Send to supplier (OPEN → SENT)")
                po_receive = gr.Button("Registrar recepción (parcial/total)")
                po_cancel = gr.Button("Cancelar PO", variant="stop")
            po_status = gr.Textbox(label="Ejecución", interactive=False)
            po_send.click(_po_send, inputs=[po_id], outputs=po_status)
            po_receive.click(_po_receive, inputs=[po_id, po_sku, po_qty], outputs=po_status)
            po_cancel.click(_po_cancel, inputs=[po_id], outputs=po_status)

        with gr.Tab("Ingestion"):
            gr.Markdown("### Supplier remito / invoice entry")
            upload = gr.UploadButton("Subir documento", file_types=["image", ".pdf"])
            preview_grid = gr.Dataframe(
                headers=["Código", "Descripción", "Cantidad", "Supplier cost"],
                datatype=["str", "str", "number", "str"],
                label="Vista previa (editable)",
                interactive=True,
            )
            preview_status = gr.Textbox(label="Extracción", interactive=False)
            supplier_id = gr.Number(label="Supplier ID", precision=0, value=1)
            confirm_button = gr.Button("Confirmar e Ingresar a Inventario", variant="primary")
            confirm_status = gr.Textbox(label="Ingreso", interactive=False)
            upload.upload(
                _ingest_preview,
                inputs=[gr.State(_get_vision_analyzer()), upload],
                outputs=[preview_grid, preview_status],
            )
            confirm_button.click(
                _ingest_confirm,
                inputs=[preview_grid, supplier_id],
                outputs=confirm_status,
            )

        with gr.Tab("Suppliers"):
            gr.Markdown("### Supplier master data")
            with gr.Row():
                supplier_search = gr.Textbox(label="Search (name, CUIT, code)", scale=3)
                supplier_status_filter = gr.Dropdown(
                    choices=_STATUS_CHOICES, value="All", label="Status", scale=1
                )
            suppliers_grid = gr.Dataframe(
                headers=[
                    "ID",
                    "Code",
                    "Name",
                    "CUIT",
                    "Contact",
                    "Phone",
                    "Margin",
                    "IVA",
                    "Status",
                ],
                datatype=["number", "str", "str", "str", "str", "str", "str", "str", "str"],
                value=lambda: _suppliers_grid("", "All"),
                label="Suppliers",
            )
            supplier_state = gr.State(value=0)
            with gr.Row():
                supplier_name = gr.Textbox(label="Business name")
                supplier_code = gr.Textbox(label="Code (3 chars — suggested from name)")
                supplier_cuit = gr.Textbox(label="CUIT")
            with gr.Row():
                supplier_contact = gr.Textbox(label="Contact name")
                supplier_phone = gr.Textbox(label="Phone (E.164)")
                supplier_whatsapp = gr.Textbox(label="WhatsApp")
                supplier_email = gr.Textbox(label="Email")
            with gr.Row():
                supplier_address = gr.Textbox(label="Address", scale=2)
                supplier_iva = gr.Dropdown(choices=_IVA_CHOICES, value="", label="IVA condition")
                supplier_margin = gr.Number(label="Default margin %", value=0.0)
                supplier_terms = gr.Textbox(label="Terms")
            supplier_status = gr.Textbox(label="Status", interactive=False)
            with gr.Row():
                supplier_save = gr.Button("Save supplier", variant="primary")
                supplier_toggle = gr.Button("Toggle status", variant="stop")
                supplier_refresh = gr.Button("Refresh")
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
                ],
                outputs=[supplier_status, suppliers_grid],
            )
            supplier_toggle.click(
                _supplier_toggle, inputs=[supplier_state], outputs=supplier_status
            )
            supplier_refresh.click(
                _suppliers_grid,
                inputs=[supplier_search, supplier_status_filter],
                outputs=suppliers_grid,
            )
    return cast(gr.Blocks, demo)


@lru_cache
def _get_vision_analyzer() -> OpenAIVisionAnalyzer:
    """Lazily build the real vision analyzer (mockable in tests via settings)."""
    return OpenAIVisionAnalyzer()


def launch(*, server_name: str = "127.0.0.1", port: int = 7860) -> None:
    """Launch the backoffice UI (only when run explicitly)."""
    build_app().launch(server_name=server_name, server_port=port)


if __name__ == "__main__":
    launch()
