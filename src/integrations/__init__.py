"""Integrations: real external-service adapters (OpenAI, Google Sheets).

Submodule exports are loaded lazily on first access: eager re-exports here
would create an import cycle, because ``src.orchestrator.approval`` imports
``src.integrations.sheets`` while ``src.agents.customer`` — imported by
``src.integrations.openai`` — is still initializing via ``src.orchestrator``.
The public names remain the same (see ``__all__``).
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "OpenAIEmbedder",
    "OpenAIResponder",
    "OpenAITranscriber",
    "OpenAIVisionAnalyzer",
    "SheetsWriteStatus",
    "SheetsWriter",
]

_LAZY: dict[str, tuple[str, str]] = {
    "OpenAIEmbedder": ("src.integrations.openai", "OpenAIEmbedder"),
    "OpenAIResponder": ("src.integrations.openai", "OpenAIResponder"),
    "OpenAITranscriber": ("src.integrations.openai", "OpenAITranscriber"),
    "OpenAIVisionAnalyzer": ("src.integrations.openai", "OpenAIVisionAnalyzer"),
    "SheetsWriter": ("src.integrations.sheets", "SheetsWriter"),
    "SheetsWriteStatus": ("src.integrations.sheets", "SheetsWriteStatus"),
}


def __getattr__(name: str) -> Any:
    """Resolve a re-exported adapter on first access, then cache it on the module."""
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = _LAZY[name]
    value = getattr(import_module(module_name), attr)
    globals()[name] = value
    return value
