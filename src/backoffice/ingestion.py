"""Backoffice receipt ingestion: RAG-backed resolution + provenance persistence.

Receipt-flow rewrite of the supplier-document ingestion use case
(rag-document-ingestion, design D3/D4). ``catalogo_productos_rag`` is
authoritative: every receipt line resolves to an indexed RAG row carrying a
``node_id``, and stock is written ONLY for those matched rows — never from model
output alone.

Two-pass resolution (``resolve_lines``): exact ``codigo_orig`` lookup scoped to
the supplier (``UPPER(TRIM(...))`` both sides) resolves 1 hit directly and
flags >1 as pending (ambiguous — never silently picked); only an exact miss
falls back to a hybrid query scoped to the same supplier, which resolves on
exactly 1 candidate. Unresolved lines stay pending and gate confirmation.

Persistence (``ingest_receipt_lines``) is a single transaction
(session-in / caller-commits): a line whose SKU already exists in ``catalogo``
bumps stock and mirrors inventory with an audited ``StockAdjustment``, keeping
``origen`` write-once; a line that exists only in the RAG is adopted into
``catalogo`` with ``origen={"rag": {...}}`` provenance after a fail-closed
embedding call. Any failure rolls the whole confirmation back.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.disambiguation import normalize_text
from src.backoffice.adoption import (
    Embedder,
    EmbeddingUnavailableError,
    MissingProvenanceError,
    OwnerContext,
    build_sku,
)
from src.db.models import Catalogo, Inventory, StockAdjustment
from src.integrations.rag import DocumentLine, RagProduct, RagProductClient
from src.pricing.engine import compute_base
from src.supplier.guards import ensure_active_supplier

_CENT = Decimal("0.01")
_RECEIPT_REASON = "receipt_ingestion"
_EMBED_DIMS = 1536


@dataclass(frozen=True)
class ReceiptLine:
    """One parsed receipt line awaiting resolution against the RAG catalog.

    ``codigo_orig`` may be empty (the document line carried no code) — those
    lines skip the exact pass and go straight to hybrid/manual resolution.
    """

    codigo_orig: str | None
    descripcion: str
    cantidad: int
    costo: Decimal | None
    pagina: int = 1


@dataclass(frozen=True)
class ResolvedLine:
    """A receipt line with its attached RAG product; ``None`` means pending."""

    receipt: ReceiptLine
    product: RagProduct | None = None

    @property
    def pending(self) -> bool:
        return self.product is None


@dataclass(frozen=True)
class IngestResult:
    """Outcome of a receipt confirmation: existing updated vs new adopted."""

    updated: int
    created: int


class UnresolvedLineError(ValueError):
    """A positive-quantity line is unresolved — confirmation must not proceed."""


def to_receipt_lines(document_lines: Sequence[DocumentLine]) -> tuple[ReceiptLine, ...]:
    """Map typed RAG ``DocumentLine`` rows into money-safe ``ReceiptLine`` rows."""
    return tuple(
        ReceiptLine(
            codigo_orig=line.codigo_orig,
            descripcion=line.descripcion,
            cantidad=line.cantidad,
            costo=_coerce_money(line.costo),
            pagina=line.pagina,
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
      1 hit → resolved, >1 → pending (ambiguous, never silently pick);
    - exact miss (or no code) → hybrid ``query()`` scoped to the supplier:
      exactly 1 candidate → resolved, 0 or >1 → pending.

    Lines with ``cantidad <= 0`` are never ingested and never gate confirmation.
    """
    supplier = ensure_active_supplier(session, supplier_id)
    resolved: list[ResolvedLine] = []
    for line in lines:
        if line.cantidad <= 0:
            resolved.append(ResolvedLine(receipt=line))
            continue
        product = _auto_resolve(rag, line, supplier.code)
        resolved.append(ResolvedLine(receipt=line, product=product))
    return tuple(resolved)


def _auto_resolve(rag: RagProductClient, line: ReceiptLine, supplier_code: str) -> RagProduct | None:
    """Resolve one line: exact hit → product, exact miss → hybrid (1 candidate)."""
    key = (line.codigo_orig or "").strip().upper()
    if key:
        exact_matches = rag.exact_lookup(key, codigo_proveedor=supplier_code)
        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(exact_matches) > 1:
            return None  # ambiguous duplicate — stay pending, never silently pick
    query_text = f"{line.codigo_orig or ''} {line.descripcion}".strip()
    candidates = hybrid_candidates(rag, supplier_code, query_text)
    return candidates[0] if len(candidates) == 1 else None


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

    Fail-closed guard first: any positive-quantity line still unresolved raises
    ``UnresolvedLineError`` before a single write, so a caller that bypasses the
    UI gating can never persist partial data. Per line: exists in ``catalogo``
    → stock bump + inventory mirror + ``StockAdjustment`` (``origen`` untouched,
    write-once); only in RAG → fail-closed embed + adopt-new ``Catalogo`` with
    ``origen={"rag": {node_id, ...}}`` provenance. One ``flush``; the caller
    commits and rolls back on any exception.
    """
    supplier = ensure_active_supplier(session, supplier_id)
    pending = [
        line
        for line in lines
        if line.receipt.cantidad > 0 and line.product is None
    ]
    if pending:
        codes = ", ".join(
            f"{line.receipt.codigo_orig or line.receipt.descripcion!r}"
            for line in pending[:5]
        )
        raise UnresolvedLineError(f"unresolved lines block confirmation: {codes}")
    updated = 0
    created = 0
    actor = f"owner:{owner_ctx.owner_id}"
    for resolved in lines:
        receipt = resolved.receipt
        if receipt.cantidad <= 0 or resolved.product is None:
            continue
        product = resolved.product
        sku = build_sku(supplier.code, receipt.codigo_orig or product.sku)
        existing = session.scalar(select(Catalogo).where(Catalogo.codigo_interno == sku))
        if existing is not None:
            _bump_stock(session, existing, receipt.cantidad, actor)
            updated += 1
        else:
            _adopt_new(session, supplier.id, supplier.default_margin_pct, sku, resolved, actor, embedder)
            created += 1
    session.flush()
    return IngestResult(updated=updated, created=created)


def _bump_stock(session: Session, product: Catalogo, cantidad: int, actor: str) -> None:
    """Bump stock on an existing product, mirror inventory, audit the change.

    ``origen`` is write-once: the update path never overwrites it (provenance
    of the original adoption/creation stays intact).
    """
    product.stock_disponible += cantidad
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
        stock_disponible=receipt.cantidad,
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
