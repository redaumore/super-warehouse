"""Per-Fase feature flags (task 5.2): safe stop points at Fase boundaries.

Each phase of the MVP can be switched off at its boundary via
``FASE1..4_ENABLED``. A disabled fase refuses to RUN its features with a clear
``FeatureDisabledError`` instead of half-working — the system stops cleanly at
the boundary and the operator re-enables the fase when ready.

Mapping (design: per-Fase flags; Fase boundaries are safe stop points):

- Fase 1 — foundation: API, DB, channels;
- Fase 2 — core: the six agents + orchestrator pipeline (webhook dispatch);
- Fase 3 — integrations: WhatsApp/OpenAI/Sheets, approval, backoffice,
  barcode, OCR;
- Fase 4 — backoffice + testing/cleanup surface.
"""

from __future__ import annotations

from src.config import Settings, get_settings

_FASE_ATTRS: dict[int, str] = {
    1: "fase1_enabled",
    2: "fase2_enabled",
    3: "fase3_enabled",
    4: "fase4_enabled",
}


class FeatureDisabledError(Exception):
    """A per-Fase feature flag disabled this feature at the boundary."""


def fase_enabled(fase: int, settings: Settings | None = None) -> bool:
    """True when the fase's features may run."""
    attr = _FASE_ATTRS.get(fase)
    if attr is None:
        raise ValueError(f"unknown fase: {fase}")
    cfg = settings or get_settings()
    return bool(getattr(cfg, attr))


def require_fase(fase: int, settings: Settings | None = None) -> None:
    """Raise ``FeatureDisabledError`` when the fase is disabled (stop at boundary)."""
    if not fase_enabled(fase, settings):
        raise FeatureDisabledError(f"fase {fase} is disabled; stopping at the boundary")
