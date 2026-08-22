"""Unit tests for the pure pricing engine (tasks 2.1 / Phase 4.1).

Pricing is the highest-value unit test target of the MVP: a pure function with
no I/O, fixed by spec. This file must reach 100% coverage of
`src/pricing/engine.py`:

    .venv/bin/python -m pytest tests/test_pricing.py --cov=src.pricing --cov-report=term-missing
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.pricing.engine import compute_base, compute_final


@pytest.mark.parametrize(
    ("cost", "margin", "expected"),
    [
        # Spec scenario: cost 925.92, margin 35% → 925.92 × 1.35 = 1249.992.
        # NOTE: the spec example prints "1250.00" (rounding 1249.992 up to whole
        # pesos); standard HALF_UP yields 1249.99. Deviation flagged to verify.
        (Decimal("925.92"), Decimal("0.35"), Decimal("1249.99")),
        # Task RED acceptance: cost 1000, margin 0 → base 1000.
        (1000, 0, Decimal("1000.00")),
        (Decimal("1000.00"), Decimal("0.50"), Decimal("1500.00")),
        (Decimal("100.00"), None, Decimal("100.00")),  # absent margin → cost
        (1000.0, 0.0, Decimal("1000.00")),  # float inputs coerce cleanly
    ],
)
def test_compute_base(cost, margin, expected):
    """El precio base = costo × (1 + margen), redondeado HALF_UP."""
    assert compute_base(cost, margin) == expected


@pytest.mark.parametrize(
    ("base", "list_discount", "particular_discount", "expected"),
    [
        # Task RED acceptance: final = 1000 × 0.80 × 0.90 = 720.
        (1000, Decimal("0.20"), Decimal("0.10"), Decimal("720.00")),
        (Decimal("1250.00"), Decimal("0.10"), None, Decimal("1125.00")),  # only list
        (Decimal("1250.00"), None, Decimal("0.10"), Decimal("1125.00")),  # only particular
        (Decimal("1250.00"), None, None, Decimal("1250.00")),  # no discounts
        (Decimal("1250.00"), 0, 0, Decimal("1250.00")),  # explicit zero discounts
        (1000, 0.2, 0.1, Decimal("720.00")),  # float discounts
    ],
)
def test_compute_final(base, list_discount, particular_discount, expected):
    """El precio final = base × (1 − descuento lista) × (1 − descuento particular)."""
    assert compute_final(base, list_discount, particular_discount) == expected


def test_discounts_compound_multiplicatively_not_additively():
    """Los descuentos componen multiplicativamente, nunca se suman.

    20% + 10% compound to 720, NOT 700 (never summed).
    """
    assert compute_final(1000, Decimal("0.20"), Decimal("0.10")) == Decimal("720.00")
    assert compute_final(1000, Decimal("0.20"), Decimal("0.10")) != Decimal("700.00")


def test_final_matches_spec_formula_list_then_particular():
    """El precio final sigue la fórmula de la spec: lista y luego particular.

    Final = Base × (1 − list_discount) × (1 − particular_discount).
    """
    base, list_discount, particular_discount = (
        Decimal("1250.00"),
        Decimal("0.10"),
        Decimal("0.05"),
    )
    expected = (base * (Decimal(1) - list_discount) * (Decimal(1) - particular_discount)).quantize(
        Decimal("0.01")
    )
    assert compute_final(base, list_discount, particular_discount) == expected


def test_base_price_rounds_half_up_to_cent():
    """El precio base redondea HALF_UP al centavo.

    12.345 rounds HALF_UP to 12.35 (not 12.34, and never up to 12.35+).
    """
    assert compute_base(Decimal("12.345"), None) == Decimal("12.35")


def test_final_price_rounds_half_up_to_cent():
    """El precio final redondea HALF_UP al centavo.

    12.345 × 0.99 compounds to 12.22 (HALF_UP), not 12.21.
    """
    assert compute_final(Decimal("12.345"), Decimal("0.01"), None) == Decimal("12.22")
