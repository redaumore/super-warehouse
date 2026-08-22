"""Barcode decoder (task 3.8): photos -> values -> catalog lookups.

Decodes barcode images (EAN-13, UPC, QR, internal codes) with pyzbar and
resolves the value against the catalog. Per the barcode-stock-ops spec:

- one catalog SKU for the barcode → ``SINGLE``;
- several SKUs sharing the value → ``DUPLICATE`` — never silently pick one,
  present the candidates for the owner to choose (design: flag to owner);
- no match → ``UNKNOWN`` — surface to the owner for manual resolution.

The pyzbar C library must be installed on the host (``brew install zbar`` on
macOS); a missing library or an unreadable image raises ``BarcodeDecodeError``
so the caller can ask the owner to retake the photo.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from pyzbar.pyzbar import decode  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Catalogo


class BarcodeDecodeError(Exception):
    """The barcode image could not be decoded (blurred, corrupted, missing zbar)."""


@dataclass(frozen=True)
class Barcode:
    """One decoded barcode value and its symbology."""

    data: str
    symbology: str


class BarcodeLookupKind(str, enum.Enum):
    """Outcome of resolving a barcode value against the catalog."""

    SINGLE = "SINGLE"
    DUPLICATE = "DUPLICATE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class BarcodeLookup:
    """Resolved barcode: one SKU, several (owner must choose), or none."""

    kind: BarcodeLookupKind
    candidates: tuple[Catalogo, ...] = ()


def decode_image(image_path: str | Path) -> list[Barcode]:
    """Decode every barcode in the image file.

    Returns an empty list when the image is valid but carries no barcode (the
    caller asks the owner to retake the photo); raises ``BarcodeDecodeError``
    when the image cannot be opened or the decoder fails.
    """
    try:
        with Image.open(image_path) as image:
            results = decode(image)
    except Exception as exc:
        raise BarcodeDecodeError(f"could not decode barcode image {image_path}: {exc}") from exc
    return [
        Barcode(data=result.data.decode("utf-8", errors="replace"), symbology=str(result.type))
        for result in results
    ]


def lookup_barcode(session: Session, data: str) -> BarcodeLookup:
    """Resolve a decoded barcode value to catalog SKUs (never silent on dupes)."""
    candidates = tuple(
        session.scalars(select(Catalogo).where(Catalogo.codigo_barras == data)).all()
    )
    if not candidates:
        return BarcodeLookup(kind=BarcodeLookupKind.UNKNOWN)
    if len(candidates) == 1:
        return BarcodeLookup(kind=BarcodeLookupKind.SINGLE, candidates=candidates)
    return BarcodeLookup(kind=BarcodeLookupKind.DUPLICATE, candidates=candidates)