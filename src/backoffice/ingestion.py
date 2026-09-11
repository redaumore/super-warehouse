"""Backoffice receipt ingestion: RAG-backed resolution + provenance persistence.

Receipt-flow rewrite of the supplier-document ingestion use case
(rag-document-ingestion, design D3/D4), relaxed by ADR 0003: a receipt line
with NO candidate in the RAG index (0 exact hits and 0 hybrid candidates) is
NOT a blocker — at confirm time it enters directly as a DEFINITIVE catalog
product (``origen={"remito": {...}}`` document provenance), with no provisional
flag and no review screen. Rationale: the RAG index is rebuilt from supplier
price lists that are re-ingested over time, so a receipt line may legitimately
be absent from the current index even though the product is real. Only
AMBIGUOUS lines (the index holds 2+ candidates and OCR cannot disambiguate)
still gate confirmation via ``UnresolvedLineError``: auto-creating them would
duplicate products that already exist.

Two-pass resolution (``resolve_lines``): exact ``codigo_orig`` lookup scoped to
the supplier (``UPPER(TRIM(...))`` both sides) resolves 1 hit directly and
flags >1 as ambiguous (never silently picked); only an exact miss falls back
to a hybrid query scoped to the same supplier, which resolves on exactly 1
candidate. A hybrid miss with 0 candidates marks the line as ``NO_CANDIDATES``
(adopted at confirm); >1 candidates marks it ``AMBIGUOUS`` (manual assignment).

Persistence (``ingest_receipt_lines``) is a single transaction
(session-in / caller-commits): a line whose SKU already exists in ``catalogo``
bumps stock and mirrors inventory with an audited ``StockAdjustment``, keeping
``origen`` write-once; a line that exists only in the RAG is adopted into
``catalogo`` with ``origen={"rag": {...}}`` provenance after a fail-closed
embedding call; a line absent from the index is adopted with document-text
provenance and a best-effort embedding (an embedder failure still adopts, only
without a vector). Any failure of the RAG-resolved path rolls the whole
confirmation back.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.backoffice.adoption import (
    Embedder,
    EmbeddingUnavailableError,
    MissingProvenanceError,
    OwnerContext,
    build_sku,
)
from src.db.models import Catalogo, Inventory, StockAdjustment, Supplier
from src.integrations.rag import DocumentLine, RagProduct, RagProductClient
from src.pricing.engine import compute_base
from src.shared.text_normalization import normalize_text
from src.supplier.guards import ensure_active_supplier

_CENT = Decimal("0.01")
_RECEIPT_REASON = "receipt_ingestion"
_EMBED_DIMS = 1536


@dataclass(frozen=True)
class ReceiptLine:
    """One parsed receipt line awaiting resolution against the RAG catalog.

    ``codigo_orig`` may be empty (the document line carried no code) — those
    lines skip the exact pass and go straight to hybrid/manual resolution.
    ``source_file`` is the uploaded document's filename, carried so a
    no-candidate line can be adopted with document provenance.
    """

    codigo_orig: str | None
    descripcion: str
    cantidad: int
    costo: Decimal | None
    pagina: int = 1
    source_file: str | None = None


class PendingReason(StrEnum):
    """Why a positive-quantity line is unresolved (only set when product is None).

    - ``NO_CANDIDATES``: the RAG index returned nothing → adopted as a definitive
      new product at confirm time (ADR 0003).
    - ``AMBIGUOUS``: the index returned 2+ candidates → manual assignment
      required; confirmation stays blocked while unresolved.
    """

    NONE = "none"
    NO_CANDIDATES = "no_candidates"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ResolvedLine:
    """A receipt line with its attached RAG product; ``None`` means pending.

    ``pending_reason`` classifies the pending state (see ``PendingReason``);
    it is only meaningful when ``product`` is ``None``.
    """

    receipt: ReceiptLine
    product: RagProduct | None = None
    pending_reason: PendingReason = PendingReason.NONE

    @property
    def pending(self) -> bool:
        return self.product is None


@dataclass(frozen=True)
class IngestResult:
    """Outcome of a receipt confirmation: existing updated vs new adopted."""

    updated: int
    created: int


class UnresolvedLineError(ValueError):
    """A positive-quantity line is AMBIGUOUS — confirmation must not proceed.

    Since ADR 0003 only ambiguous lines (2+ index candidates) gate
    confirmation; a no-candidate line is adopted as a definitive product
    instead of blocking.
    """


def to_receipt_lines(
    document_lines: Sequence[DocumentLine], *, source_file: str | None = None
) -> tuple[ReceiptLine, ...]:
    """Map typed RAG ``DocumentLine`` rows into money-safe ``ReceiptLine`` rows.

    ``source_file`` tags every line with the parsed document's filename so the
    no-candidate adoption path can persist document provenance.
    """
    return tuple(
        ReceiptLine(
            codigo_orig=line.codigo_orig,
            descripcion=line.descripcion,
            cantidad=line.cantidad,
            costo=_coerce_money(line.costo),
            pagina=line.pagina,
            source_file=source_file,
        )
        for line in document_lines
    )


def resolve_lines(
    session: Session,
    rag: RagProductClient,
    lines: Sequence[ReceiptLine],
    supplier_id: int,
) -> tuple[ResolvedLine, ...]:
    """Two-pass resolution (exact first, hybrid only on miss); 1→resolved, else pending.

    - exact lookup on ``UPPER(TRIM(codigo_orig))`` scoped to the supplier:
      1 hit → resolved, >1 → ambiguous (never silently pick);
    - exact miss (or no code) → hybrid ``query()`` scoped to the supplier:
      exactly 1 candidate → resolved, 0 → ``NO_CANDIDATES``, >1 → ``AMBIGUOUS``.

    Lines with ``cantidad <= 0`` are never ingested and never gate confirmation.
    """
    supplier = ensure_active_supplier(session, supplier_id)
    resolved: list[ResolvedLine] = []
    for line in lines:
        if line.cantidad <= 0:
            resolved.append(ResolvedLine(receipt=line))
            continue
        product, reason = _auto_resolve(rag, line, supplier.code)
        resolved.append(
            ResolvedLine(
                receipt=line,
                product=product,
                pending_reason=PendingReason.NONE if product else reason,
            )
        )
    return tuple(resolved)


def _auto_resolve(
    rag: RagProductClient, line: ReceiptLine, supplier_code: str
) -> tuple[RagProduct | None, PendingReason]:
    """Resolve one line: exact hit → product, exact miss → hybrid (1 candidate).

    Returns the product (or ``None``) plus the pending reason: ``AMBIGUOUS``
    when the index holds 2+ candidates, ``NO_CANDIDATES`` when it returns
    none (adopted as a definitive product at confirm time, ADR 0003).
    """
    key = (line.codigo_orig or "").strip().upper()
    if key:
        exact_matches = rag.exact_lookup(key, codigo_proveedor=supplier_code)
        if len(exact_matches) == 1:
            return exact_matches[0], PendingReason.NONE
        if len(exact_matches) > 1:
            return None, PendingReason.AMBIGUOUS  # duplicate codes — manual assignment
    query_text = f"{line.codigo_orig or ''} {line.descripcion}".strip()
    candidates = hybrid_candidates(rag, supplier_code, query_text)
    if len(candidates) == 1:
        return candidates[0], PendingReason.NONE
    if len(candidates) > 1:
        return None, PendingReason.AMBIGUOUS
    return None, PendingReason.NO_CANDIDATES


def hybrid_candidates(
    rag: RagProductClient, supplier_code: str, query_text: str
) -> tuple[RagProduct, ...]:
    """Hybrid RAG candidates for a query, filtered to the supplier's products.

    The hybrid endpoint is not supplier-scoped, so results are filtered to rows
    whose ``codigo_proveedor`` equals the selected supplier (design D4). Used by
    the automatic fallback (exactly 1 → resolved) and by the per-line manual
    search (owner picks from the candidates).
    """
    query_text = query_text.strip()
    if not query_text:
        return ()
    products = rag.query(query_text)
    wanted = supplier_code.strip().upper()
    return tuple(
        p
        for p in products
        if p.codigo_proveedor and p.codigo_proveedor.strip().upper() == wanted
    )


def ingest_receipt_lines(
    session: Session,
    supplier_id: int,
    lines: Sequence[ResolvedLine],
    owner_ctx: OwnerContext,
    embedder: Embedder,
) -> IngestResult:
    """Persist resolved receipt lines in ONE transaction; the caller commits.

    Fail-closed guard first: any positive-quantity line still AMBIGUOUS raises
    ``UnresolvedLineError`` before a single write, so a caller that bypasses the
    UI gating can never persist partial data. A ``NO_CANDIDATES`` line does NOT
    block (ADR 0003): it is adopted as a definitive product during this same
    transaction. Per line: exists in ``catalogo`` → stock bump + inventory
    mirror + ``StockAdjustment`` (``origen`` untouched, write-once); only in
    RAG → fail-closed embed + adopt-new ``Catalogo`` with
    ``origen={"rag": {node_id, ...}}`` provenance; absent from the index →
    adopt-new with document provenance and a best-effort embedding (embedder
    failure adopts without a vector, never raises). One ``flush``; the caller
    commits and rolls back on any exception.
    """
    supplier = ensure_active_supplier(session, supplier_id)
    ambiguous = [
        line
        for line in lines
        if line.receipt.cantidad > 0
        and line.pending
        and line.pending_reason is PendingReason.AMBIGUOUS
    ]
    if ambiguous:
        codes = ", ".join(
            f"{line.receipt.codigo_orig or line.receipt.descripcion!r}"
            for line in ambiguous[:5]
        )
        raise UnresolvedLineError(f"ambiguous lines require manual assignment: {codes}")
    updated = 0
    created = 0
    actor = f"owner:{owner_ctx.owner_id}"
    for resolved in lines:
        receipt = resolved.receipt
        if receipt.cantidad <= 0:
            continue
        if resolved.product is not None:
            sku = build_sku(supplier.code, receipt.codigo_orig or resolved.product.sku)
        else:
            sku = _sku_from_document(supplier.code, receipt)
        existing = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == sku))
        if existing is not None:
            _bump_stock(session, existing, receipt.cantidad, actor)
            updated += 1
        elif resolved.product is not None:
            _adopt_new(session, supplier.id, supplier.default_margin_pct, sku, resolved, actor, embedder)
            created += 1
        else:
            _adopt_from_document(session, supplier, sku, receipt, actor, embedder)
            created += 1
    session.flush()
    return IngestResult(updated=updated, created=created)


def _sku_from_document(supplier_code: str, receipt: ReceiptLine) -> str:
    """Deterministic SKU for a no-candidate line: ``codigo_orig`` or description.

    A line with neither a code nor a description cannot be identified at all —
    fail closed instead of creating an unnamed product.
    """
    base = receipt.codigo_orig or receipt.descripcion
    if not base or not base.strip():
        raise UnresolvedLineError(
            f"line has no codigo_orig or description to derive a SKU: {receipt.descripcion!r}"
        )
    return build_sku(supplier_code, base)


def _bump_stock(session: Session, product: Catalogo, cantidad: int, actor: str) -> None:
    """Bump the canonical on-hand stock of an existing product, audit the change.

    ``origen`` is write-once: the update path never overwrites it (provenance
    of the original adoption/creation stays intact).
    """
    inventory_row = session.scalar(
        select(Inventory).where(Inventory.sku_id == product.codigo_interno)
    )
    if inventory_row is not None:
        inventory_row.quantity_on_hand += cantidad
        inventory_row.updated_at = datetime.now(UTC)
    else:
        session.add(Inventory(sku_id=product.codigo_interno, quantity_on_hand=cantidad))
    session.add(
        StockAdjustment(
            sku=product.codigo_interno,
            delta=cantidad,
            reason=_RECEIPT_REASON,
            actor=actor,
        )
    )


def _adopt_new(
    session: Session,
    supplier_id: int,
    default_margin_pct: Decimal,
    sku: str,
    resolved: ResolvedLine,
    actor: str,
    embedder: Embedder,
) -> None:
    """Adopt a RAG-only product into ``catalogo`` with provenance (no creation
    from model output: identity and provenance come from the RAG row).

    The embedding runs BEFORE any write and fails closed: on error the caller
    rolls back and nothing is persisted (mirrors ``adopt_product``).
    """
    product = resolved.product
    receipt = resolved.receipt
    assert product is not None
    if not product.node_id:
        raise MissingProvenanceError("node_id is required (fail closed)")
    text = _compose_embedding_text(product)
    try:
        vectors = embedder.embed([text])
        embedding = vectors[0]
    except Exception as exc:  # any embedder failure closes the transaction
        raise EmbeddingUnavailableError(f"embedding failed: {exc}") from exc
    if len(embedding) != _EMBED_DIMS:
        raise EmbeddingUnavailableError(
            f"embedding has {len(embedding)} dims, expected {_EMBED_DIMS}"
        )
    costo = receipt.costo if receipt.costo is not None else (_coerce_money(product.price) or Decimal("0.00"))
    origen: dict[str, Any] = {
        "rag": {
            "node_id": product.node_id,
            "archivo_origen": product.source_file,
            "pagina_origen": product.page,
        }
    }
    new_product = Catalogo(
        codigo_interno=sku,
        supplier_id=supplier_id,
        nombre_oficial=product.name,
        costo_proveedor=costo,
        margen_aplicado_pct=default_margin_pct,
        precio_lista_base=compute_base(costo, default_margin_pct),
        sinonimos=[receipt.descripcion] if receipt.descripcion else [product.name],
        marca=product.brand,
        categoria=product.categoria,
        subcategoria=product.subcategoria,
        moneda=product.currency,
        origen=origen,
        embedding=embedding,
    )
    session.add(new_product)
    session.add(Inventory(sku_id=sku, quantity_on_hand=receipt.cantidad))
    session.add(
        StockAdjustment(
            sku=sku,
            delta=receipt.cantidad,
            reason=_RECEIPT_REASON,
            actor=actor,
        )
    )


def _adopt_from_document(
    session: Session,
    supplier: Supplier,
    sku: str,
    receipt: ReceiptLine,
    actor: str,
    embedder: Embedder,
) -> None:
    """Adopt a NO_CANDIDATES receipt line as a NEW DEFINITIVE product (ADR 0003).

    Identity comes from the document itself (``codigo_orig`` — or the
    description when the line carries no code), never from a fabricated
    ``node_id``: provenance is ``origen={"remito": {...}}`` with the parsed
    line snapshot. The embedding is best-effort: any embedder failure (or a
    wrong-dimension vector) adopts the product WITHOUT a vector — the nullable
    ``embedding`` column exists for exactly this — and must never block
    ingestion.
    """
    costo = receipt.costo if receipt.costo is not None else Decimal("0.00")
    text = normalize_text(receipt.descripcion)
    embedding: list[float] | None = None
    try:
        vectors = embedder.embed([text])
        if len(vectors[0]) == _EMBED_DIMS:
            embedding = vectors[0]
    except Exception:  # noqa: BLE001 — embedding must not block receipt ingestion
        embedding = None
    origen: dict[str, Any] = {
        "remito": {
            "archivo_origen": receipt.source_file,
            "codigo_proveedor": supplier.code,
            "fecha_ingesta": datetime.now(UTC).isoformat(),
            "pagina_origen": receipt.pagina,
            "linea": {
                "codigo_orig": receipt.codigo_orig,
                "descripcion": receipt.descripcion,
                "cantidad": receipt.cantidad,
                # str: Decimal is not JSON-serializable for the JSONB column.
                "costo": str(costo),
            },
        }
    }
    new_product = Catalogo(
        codigo_interno=sku,
        supplier_id=supplier.id,
        nombre_oficial=receipt.descripcion,
        costo_proveedor=costo,
        margen_aplicado_pct=supplier.default_margin_pct,
        precio_lista_base=compute_base(costo, supplier.default_margin_pct),
        sinonimos=[receipt.descripcion]
        if receipt.descripcion
        else ([receipt.codigo_orig] if receipt.codigo_orig else []),
        origen=origen,
        embedding=embedding,
    )
    session.add(new_product)
    _bump_stock(session, new_product, receipt.cantidad, actor)


def _compose_embedding_text(product: RagProduct) -> str:
    """Embedding text: ``nombre + marca + categoria + subcategoria`` normalized."""
    parts = [
        normalize_text(part)
        for part in (product.name, product.brand, product.categoria, product.subcategoria)
        if part
    ]
    return " ".join(parts)


def _coerce_money(raw: object) -> Decimal | None:
    """Coerce a float/Decimal/str cost cell to cents; None when blank."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return Decimal(str(raw)).quantize(_CENT)
    except Exception:  # noqa: BLE001 — the owner can review the line in the grid
        return None
