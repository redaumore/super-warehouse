"""Unit tests for the documento_id default ("LISTA GENERAL") in the orchestrator.

Patrón: sin base de datos y sin PDF. ``_normalize_documento_id`` es la fuente
única de verdad del default; el caso ``delete_scope='documento'`` sin
documento se cubre a través de ``ingest_catalog_pdf`` con un PDF inexistente
(retorno temprano ERROR, antes de cualquier fase del pipeline), para probar
que el ValueError defensivo ya no dispara mientras el default esté vigente.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.core.orchestrator import RAGOrchestrator, _normalize_documento_id


# ------------------------------------------------------------------- default


def test_normalize_documento_id_applies_default_when_missing():
    """documento_id ausente (None o solo espacios) → default del settings."""
    assert _normalize_documento_id(None) == "LISTA GENERAL"
    assert _normalize_documento_id("   ") == "LISTA GENERAL"


def test_normalize_documento_id_declared_value_wins_and_is_normalized():
    """El valor declarado por el operador gana y se normaliza (strip/mayúsculas)."""
    assert _normalize_documento_id("  lista   general ") == "LISTA GENERAL"
    assert _normalize_documento_id("Ofertas") == "OFERTAS"


def test_normalize_documento_id_empty_setting_falls_back_to_none(monkeypatch):
    """Con DEFAULT_DOCUMENTO_ID vacío en settings, no hay default → None.

    Este es el único caso en el que el fallback defensivo de
    ``delete_scope='documento'`` puede disparar el ValueError.
    """
    monkeypatch.setattr(settings, "DEFAULT_DOCUMENTO_ID", "")
    assert _normalize_documento_id(None) is None


# ------------------------------------------------- delete_scope='documento'


def test_ingest_incremental_without_documento_id_uses_default_instead_of_raising(tmp_path):
    """delete_scope='documento' + documento_id=None ya NO corta: usa el default."""
    orchestrator = RAGOrchestrator()
    res = orchestrator.ingest_catalog_pdf(
        pdf_path=str(tmp_path / "no-existe.pdf"),
        codigo_proveedor="MSA",
        documento_id=None,
        delete_scope="documento",
    )
    assert res.status == "ERROR"
    # Llega hasta el chequeo de PDF (retorna ERROR temprano); el ValueError
    # defensivo de documento_id no dispara mientras el default está vigente.
    assert "documento_id declarado" not in (res.error or "")
    assert "no existe" in (res.error or "")
