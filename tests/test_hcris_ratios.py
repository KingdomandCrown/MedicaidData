"""Ratios computed from already-decoded HCRIS metrics.

Every formula is quoted directly from American Hospital Directory's
FinIndWorksheetRef_2552-10.pdf (see hcris_crosswalk.py for full sourcing).
These tests check the arithmetic and the "missing input -> absent, not
zero-filled" behavior, not the crosswalk itself.
"""

from decimal import Decimal

from hospitals.hcris_ratios import compute_ratios

FULL_METRICS = {
    "netpatrev": Decimal("1000000"),
    "opexp": Decimal("900000"),
    "othinc": Decimal("50000"),
    "net_income": Decimal("150000"),
    "total_assets": Decimal("2000000"),
    "total_liabilities": Decimal("800000"),
    "total_current_assets": Decimal("500000"),
    "total_current_liabilities": Decimal("250000"),
    "inventory": Decimal("50000"),
    "accounts_receivable": Decimal("300000"),
    "notes_receivable": Decimal("10000"),
    "other_receivables": Decimal("5000"),
    "allow_uncollectible": Decimal("20000"),
    "total_long_term_liabilities": Decimal("600000"),
}


def test_operating_margin_matches_the_ahd_formula():
    # (tot oper rev - tot oper exp) / tot oper rev * 100
    ratios = compute_ratios(FULL_METRICS)
    assert ratios["operating_margin_pct"] == (Decimal("1000000") - Decimal("900000")) / Decimal("1000000") * 100


def test_current_and_quick_ratio():
    ratios = compute_ratios(FULL_METRICS)
    assert ratios["current_ratio"] == Decimal("500000") / Decimal("250000")
    assert ratios["quick_ratio"] == (Decimal("500000") - Decimal("50000")) / Decimal("250000")


def test_return_on_equity_and_assets():
    ratios = compute_ratios(FULL_METRICS)
    net_assets = Decimal("2000000") - Decimal("800000")
    assert ratios["return_on_equity_pct"] == Decimal("150000") / net_assets * 100
    assert ratios["return_on_assets_pct"] == Decimal("150000") / Decimal("2000000") * 100


def test_debt_to_net_assets_ratios():
    ratios = compute_ratios(FULL_METRICS)
    net_assets = Decimal("2000000") - Decimal("800000")
    assert ratios["lt_debt_to_net_assets"] == Decimal("600000") / net_assets
    assert ratios["total_debt_to_net_assets"] == Decimal("800000") / net_assets


def test_a_ratio_missing_one_input_is_absent_not_computed_with_zero():
    partial = dict(FULL_METRICS)
    del partial["total_current_liabilities"]

    ratios = compute_ratios(partial)

    assert "current_ratio" not in ratios
    assert "quick_ratio" not in ratios
    # Unrelated ratios still compute fine.
    assert "operating_margin_pct" in ratios
    assert "return_on_equity_pct" in ratios


def test_a_zero_denominator_is_absent_not_a_zero_division_error():
    metrics = dict(FULL_METRICS)
    metrics["total_current_liabilities"] = Decimal("0")

    ratios = compute_ratios(metrics)

    assert "current_ratio" not in ratios
    assert "quick_ratio" not in ratios


def test_no_metrics_at_all_yields_no_ratios():
    assert compute_ratios({}) == {}


def test_alpha_valued_metrics_are_ignored_not_divided():
    """hospital_name/chain_name/typ_control are strings, not Decimals --
    a ratio referencing them (none do today) must never attempt to divide
    a string, and unrelated ratios must still compute."""

    metrics = dict(FULL_METRICS)
    metrics["hospital_name"] = "Example Hospital"

    ratios = compute_ratios(metrics)
    assert "operating_margin_pct" in ratios
