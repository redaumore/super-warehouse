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

from src.db.models import Catalogo, StockAdjustment


class BarcodeDecodeError(Exception):
    """The barcode image could not be decoded (blurred, corrupted, missing zbar)."""


class BarcodeAdjustmentErrorKind(str, enum.Enum):
    """Precise cause of a failed stock adjustment (assertable in tests)."""

    DUPLICATE = "DUPLICATE"
    UNKNOWN = "UNKNOWN"
    NEGATIVE = "NEGATIVE"


class BarcodeAdjustmentError(Exception):
    """A stock adjustment could not be applied; ``kind`` carries the cause."""

    def __init__(self, kind: BarcodeAdjustmentErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


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


def adjust_stock_by_barcode(
    session: Session,
    data: str,
    delta: int,
    reason: str,
    actor: str,
) -> StockAdjustment:
    """Adjust stock by barcode, recording an audited ``StockAdjustment`` row.

    ``delta`` is positive for increases and negative for decreases. Duplicate
    barcodes and unknown barcodes raise (never silently pick); a result below
    zero raises. The caller owns the transaction — we only ``flush`` so the row
    gets an id.
    """
    lookup = lookup_barcode(session, data)
    if lookup.kind is BarcodeLookupKind.DUPLICATE:
        raise BarcodeAdjustmentError(
            BarcodeAdjustmentErrorKind.DUPLICATE,
            f"barcode {data!r} maps to {len(lookup.candidates)} SKUs; choose one before adjusting",
        )
    if lookup.kind is BarcodeLookupKind.UNKNOWN:
        raise BarcodeAdjustmentError(
            BarcodeAdjustmentErrorKind.UNKNOWN,
            f"barcode {data!r} is not recognized",
        )
    item = lookup.candidates[0]
    new_stock = item.stock_disponible + delta
    if new_stock < 0:
        raise BarcodeAdjustmentError(
            BarcodeAdjustmentErrorKind.NEGATIVE,
            f"cannot adjust {item.codigo_interno} below zero (stock "
            f"{item.stock_disponible}, delta {delta})",
        )
    item.stock_disponible = new_stock
    adjustment = StockAdjustment(
        sku=item.codigo_interno,
        delta=delta,
        reason=reason,
        actor=actor,
    )
    session.add(adjustment)
    session.flush()
    return adjustment