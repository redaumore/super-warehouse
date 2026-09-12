"""Backoffice: Gradio UI for catalog, clients, monitor and ingestion.

``build_app``/``launch`` are resolved lazily: an eager import here makes every
``src.backoffice.*`` submodule import pull the whole Gradio app (and its
transitive imports, e.g. ``customer_orders`` → ``sourcing.draft_order``),
which deadlocks the import cycle when ``src.sourcing.product_search`` imports
``src.backoffice.sku_mappings`` from inside ``draft_order``'s own import.
"""

from typing import Any

__all__ = ["build_app", "launch"]


def __getattr__(name: str) -> Any:
    if name in ("build_app", "launch"):
        from src.backoffice import app as _app

        return getattr(_app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
