"""Calibración del buscador local: gate de mezcla para queries cortas (work unit 0).

La regresión "clavos paris" (2 tokens) contra "Clavos Paris 2 Pulgadas (50mm)"
quedaba bajo el piso de 0.65 con `token_sort_ratio` puro; el gate por cantidad
de tokens mezcla token_set/partial para 2–3 tokens y deja el resto intacto.
Los negativos "tarugo"/"pintura" deben seguir bajo el piso, y un solo token
conserva el comportamiento token_sort original.
"""

from __future__ import annotations

import pytest

from src.agents.disambiguation import _fuzzy_score

FLOOR = 0.65

PRODUCT = "Clavos Paris 2 Pulgadas (50mm)"


def test_short_query_regression_matches_long_product():
    """La query de 2 tokens "clavos paris" supera el piso contra el producto largo."""
    assert _fuzzy_score("clavos paris", PRODUCT) >= FLOOR


@pytest.mark.parametrize("query", ["tarugo", "pintura"])
def test_unrelated_queries_stay_below_floor(query: str):
    """Las queries sin solapamiento real no false-positivan sobre el piso."""
    assert _fuzzy_score(query, PRODUCT) < FLOOR


def test_single_token_keeps_token_sort_only():
    """Un solo token conserva token_sort puro: el blend parcial solo aplica a 2–3 tokens."""
    # "clavos" está contenido en el nombre oficial y partial_ratio daría 1.0;
    # el gate de un token debe mantener el puntaje token_sort bajo el piso.
    assert _fuzzy_score("clavos", PRODUCT) < FLOOR