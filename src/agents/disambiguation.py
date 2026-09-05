"""Catalog search agent: hybrid fuzzy + vector disambiguation.

Resolves free-form customer language (slang, misspellings, informal names) into
catalog SKUs. The fuzzy channel runs rapidfuzz token-sort similarity over the
official name and the owner-curated synonyms; when the perception pipeline
supplies an embedding, the pgvector channel adds cosine similarity over the
catalog's `vector(1536)` column. The hybrid confidence is the best of both
channels — either one reaching the auto-map threshold is enough to map without
prompting the customer.

Outcomes follow the catalog-search spec:
- exactly one candidate at or above the auto-map threshold → ``AUTO_MAPPED``;
- several plausible candidates, or a lone low-confidence one → ``AMBIGUOUS``
  (a numbered menu for the customer to pick from);
- nothing above the ambiguity floor → ``NOT_FOUND`` (ask for clarification).

Short or partial queries rely on synonym coverage for a strong score; the owner
extends synonyms when calibration shows precision below the 85% target.
"""

from __future__ import annotations

import enum
import math
import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.models import Catalogo

_WORD_RE = re.compile(r"[^\w\s]+")


@dataclass(frozen=True)
class SearchCandidate:
    """A catalog product candidate with hybrid confidence in [0, 1]."""

    sku: str
    nombre_oficial: str
    confidence: float


class ResolutionKind(str, enum.Enum):
    """Outcome of resolving one order item against the catalog."""

    AUTO_MAPPED = "AUTO_MAPPED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class Resolution:
    """Result of resolving one item: auto-mapped SKU, a menu, or nothing."""

    kind: ResolutionKind
    candidate: SearchCandidate | None = None
    candidates: tuple[SearchCandidate, ...] = ()


def normalize_text(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _WORD_RE.sub(" ", text).lower()
    return " ".join(text.split())


def _fuzzy_score(query: str, candidate: str) -> float:
    """Token-sort similarity in [0, 1], blended for short multi-token queries.

    `token_sort_ratio` compares every token of both strings (word order
    ignored), so it rewards real overlap and penalizes extra tokens in the
    candidate — a near-subset match like "clavos de 2 pulgadas" against
    "Clavos Espiralados 2 Pulgadas" scores well below the auto-map threshold.

    Calibration gate: a one-token query keeps `token_sort_ratio` only; a 2–3
    token query blends `token_set_ratio` and `partial_ratio` via max so a short
    query fully contained in a longer official name (e.g. "clavos paris" vs
    "Clavos Paris 2 Pulgadas (50mm)") clears the floor. Longer queries are
    unchanged. Global floors are never touched here.
    """
    normalized_query = normalize_text(query)
    normalized_candidate = normalize_text(candidate)
    if 2 <= len(query.split()) <= 3:
        return max(
            fuzz.token_sort_ratio(normalized_query, normalized_candidate),
            fuzz.token_set_ratio(normalized_query, normalized_candidate),
            fuzz.partial_ratio(normalized_query, normalized_candidate),
        ) / 100.0
    return fuzz.token_sort_ratio(normalized_query, normalized_candidate) / 100.0


def search_catalog(
    session: Session,
    query: str,
    *,
    embedding: list[float] | None = None,
    limit: int = 5,
) -> list[SearchCandidate]:
    """Rank catalog products by hybrid fuzzy + vector similarity, best first.

    ``embedding`` is the pre-computed query embedding (the OpenAI call lives in
    the perception pipeline, a later phase); when omitted the search degrades
    gracefully to pure fuzzy matching.
    """
    # Fuzzy channel: official name + every synonym, for all catalog rows.
    fuzzy: dict[str, float] = {}
    names: dict[str, str] = {}
    for sku, nombre_oficial, sinonimos in session.execute(
        select(Catalogo.codigo_interno, Catalogo.nombre_oficial, Catalogo.sinonimos)
    ):
        names[sku] = nombre_oficial
        fuzzy[sku] = max(
            (_fuzzy_score(query, name) for name in (nombre_oficial, *(sinonimos or []))),
            default=0.0,
        )

    # Vector channel: pgvector cosine similarity, only for rows that have one.
    vector: dict[str, float] = {}
    if embedding is not None:
        distance = Catalogo.embedding.cosine_distance(embedding)
        rows = session.execute(
            select(Catalogo.codigo_interno, distance.label("dist"))
            .where(Catalogo.embedding.is_not(None))
            .order_by(distance)
            .limit(limit * 2)
        )
        for sku, dist in rows:
            similarity = 1.0 - float(dist)
            if math.isnan(similarity):  # zero vectors yield NaN cosine distance
                similarity = 0.0
            vector[sku] = max(0.0, similarity)

    candidates = [
        SearchCandidate(
            sku=sku,
            nombre_oficial=names[sku],
            confidence=max(fuzzy.get(sku, 0.0), vector.get(sku, 0.0)),
        )
        for sku in set(fuzzy) | set(vector)
    ]
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates[:limit]


def resolve_item(
    session: Session,
    query: str,
    *,
    embedding: list[float] | None = None,
) -> Resolution:
    """Auto-map a high-confidence item, or return a menu / not-found outcome.

    Thresholds come from Settings (owner-configurable): candidates at or above
    the auto-map threshold map silently when there is exactly one; candidates at
    or above the ambiguity floor populate the numbered disambiguation menu.
    """
    settings = get_settings()
    candidates = search_catalog(session, query, embedding=embedding)
    above_floor = [c for c in candidates if c.confidence >= settings.search_ambiguity_floor]
    if not above_floor:
        return Resolution(kind=ResolutionKind.NOT_FOUND)
    auto_mapped = [c for c in above_floor if c.confidence >= settings.search_auto_map_threshold]
    if len(auto_mapped) == 1:
        return Resolution(kind=ResolutionKind.AUTO_MAPPED, candidate=auto_mapped[0])
    return Resolution(kind=ResolutionKind.AMBIGUOUS, candidates=tuple(above_floor))
