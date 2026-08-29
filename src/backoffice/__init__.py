"""Backoffice: Gradio UI for catalog, clients, monitor and ingestion."""

from src.backoffice.app import build_app, launch

__all__ = ["build_app", "launch"]
