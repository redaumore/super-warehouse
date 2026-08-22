"""Backoffice Gradio app (task 3.5): four tabs for the owner.

A lightweight web interface with four tabs — Catalog, Clients, Orders/Monitor
and Ingestion — wired to the pure DB operations in ``src.backoffice``. The
build function only constructs the Blocks tree (no server); ``launch()`` is
guarded so importing the module never starts a server, which keeps tests and
CI safe.
"""

from __future__ import annotations

from functools import lru_cache
from typing import cast

import gradio as gr

from src.agents.perception import VisionAnalyzer
from src.backoffice.catalog import list_products, update_margin, update_price, update_stock
from src.backoffice.clients import create_client, list_clients, list_price_lists
from src.backoffice.ingestion import confirm_items, to_grid_rows
from src.backoffice.monitor import list_orders
from src.config import get_settings
from src.db.session import SessionLocal
from src.integrations.openai import OpenAIVisionAnalyzer
from src.integrations.sheets import SheetsWriter

_SHEETS = SheetsWriter()  # append-only; quarantines internally when unconfigured


def _catalog_grid() -> list[list[object]]:
    with SessionLocal() as session:
        rows = list_products(session)
    return [
        [r["codigo_interno"], r["codigo_barras"], r["nombre_oficial"], r["costo_proveedor"],
         r["margen_aplicado_pct"], r["precio_lista_base"], r["stock_disponible"]]
        for r in rows
    ]


def _clients_grid() -> list[list[object]]:
    with SessionLocal() as session:
        rows = list_clients(session)
    return [
        [r["customer_id"], r["nombre_comercial"], r["telefono_norm"],
         r["lista_precios_id"], r["descuento_particular_pct"]]
        for r in rows
    ]


def _monitor_grid() -> list[list[object]]:
    with SessionLocal() as session:
        rows = list_orders(session, sheets=_SHEETS)
    return [
        [r["order_id"], r["customer"], r["estado"], r["needs_requote"],
         r["active_reservations"], r["sheets_synced"]]
        for r in rows
    ]


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


def _ingest_confirm(rows: list[list[object]], proveedor_id: object) -> str:
    with SessionLocal() as session:
        try:
            result = confirm_items(session, rows or [], int(str(proveedor_id)))
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            return f"Error al ingresar: {exc}"
    return f"Ingresado: {result.updated} actualizados, {result.created} creados."


def _price_list_choices() -> list[dict[str, object]]:
    with SessionLocal() as session:
        return list_price_lists(session)


def build_app() -> gr.Blocks:
    """Construct the four-tab Blocks tree (no server is started)."""
    settings = get_settings()
    with gr.Blocks(title="Ferretería — Backoffice") as demo:
        gr.Markdown(
            f"# Backoffice Ferretería\n"
            f"Fase 4 habilitada: {settings.fase4_enabled}. "
            "Los datos van a la base local (Postgres + pgvector)."
        )
        with gr.Tab("Catalog"):
            gr.Markdown("### Catálogo y stock")
            catalog_grid = gr.Dataframe(
                headers=["SKU", "Código barras", "Nombre", "Costo", "Margen", "Precio lista", "Stock"],
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

        with gr.Tab("Ingestion"):
            gr.Markdown("### Ingreso de remitos / facturas de proveedor")
            upload = gr.UploadButton("Subir documento", file_types=["image", ".pdf"])
            preview_grid = gr.Dataframe(
                headers=["Código", "Descripción", "Cantidad", "Costo proveedor"],
                datatype=["str", "str", "number", "str"],
                label="Vista previa (editable)",
                interactive=True,
            )
            preview_status = gr.Textbox(label="Extracción", interactive=False)
            proveedor_id = gr.Number(label="Proveedor ID", precision=0, value=1)
            confirm_button = gr.Button("Confirmar e Ingresar a Inventario", variant="primary")
            confirm_status = gr.Textbox(label="Ingreso", interactive=False)
            upload.upload(
                _ingest_preview,
                inputs=[gr.State(_get_vision_analyzer()), upload],
                outputs=[preview_grid, preview_status],
            )
            confirm_button.click(
                _ingest_confirm,
                inputs=[preview_grid, proveedor_id],
                outputs=confirm_status,
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