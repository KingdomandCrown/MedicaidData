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
    "othexp": Decimal("20000"),
    "cash_on_hand": Decimal("100000"),
    "market_securities": Decimal("50000"),
    "investments": Decimal("200000"),
    "depreciation_expense": Decimal("45000"),
    "lease_cost": Decimal("10000"),
    "interest_expense": Decimal("25000"),
    "salary_expense": Decimal("400000"),
    "fringe_benefits": Decimal("80000"),
    "contract_labor_addon": Decimal("5000"),
    "contract_labor_main": Decimal("15000"),
    "accum_depr_buildings": Decimal("300000"),
    "accum_depr_fixed_equipment": Decimal("60000"),
    "net_income_from_patients": Decimal("100000"),
    "ipbeddays_adultped": Decimal("18250"),
    "availbeddays_adultped": Decimal("36500"),
    "ipdischarges_adultped": Decimal("3650"),
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


def test_ebitdar_matches_the_ahd_formula():
    # net income + interest + depreciation + lease cost
    ratios = compute_ratios(FULL_METRICS)
    assert ratios["ebitdar"] == Decimal("150000") + Decimal("25000") + Decimal("45000") + Decimal("10000")


def test_days_cash_on_hand_and_all_sources_variant():
    ratios = compute_ratios(FULL_METRICS)
    oper_exp_less_depr = Decimal("900000") - Decimal("45000")
    assert ratios["days_cash_on_hand"] == (Decimal("100000") + Decimal("50000")) / (oper_exp_less_depr / 365)
    assert ratios["days_cash_on_hand_all_sources"] == (
        Decimal("100000") + Decimal("50000") + Decimal("200000")
    ) / (oper_exp_less_depr / 365)


def test_average_payment_period_matches_the_ahd_formula():
    ratios = compute_ratios(FULL_METRICS)
    denom = (Decimal("900000") + Decimal("20000") - Decimal("45000")) / 365
    assert ratios["average_payment_period_days"] == Decimal("250000") / denom


def test_average_age_of_plant_sums_the_accumulated_depreciation_lines_present():
    ratios = compute_ratios(FULL_METRICS)
    # Only accum_depr_buildings and accum_depr_fixed_equipment are set in
    # FULL_METRICS -- the other five accumulated-depreciation lines this
    # hospital never filed contribute nothing, not a zero that would be
    # indistinguishable from "filed and zero."
    assert ratios["average_age_of_plant"] == (Decimal("300000") + Decimal("60000")) / Decimal("45000")


def test_average_age_of_plant_is_absent_when_no_accumulated_depreciation_line_is_present():
    metrics = dict(FULL_METRICS)
    del metrics["accum_depr_buildings"]
    del metrics["accum_depr_fixed_equipment"]

    assert "average_age_of_plant" not in compute_ratios(metrics)


def test_personnel_expense_pct_sums_both_contract_labor_components():
    ratios = compute_ratios(FULL_METRICS)
    contract_labor = Decimal("5000") + Decimal("15000")
    expected = (Decimal("400000") + contract_labor + Decimal("80000")) / Decimal("1000000") * 100
    assert ratios["personnel_expense_pct"] == expected


def test_personnel_expense_pct_needs_both_contract_labor_components():
    metrics = dict(FULL_METRICS)
    del metrics["contract_labor_addon"]

    assert "personnel_expense_pct" not in compute_ratios(metrics)


def test_occupancy_and_average_length_of_stay():
    ratios = compute_ratios(FULL_METRICS)
    assert ratios["occupancy_pct"] == Decimal("18250") / Decimal("36500") * 100
    assert ratios["average_length_of_stay"] == Decimal("18250") / Decimal("3650")


def test_occupancy_is_absent_without_bed_day_data():
    metrics = dict(FULL_METRICS)
    del metrics["availbeddays_adultped"]

    ratios = compute_ratios(metrics)
    assert "occupancy_pct" not in ratios
    assert "average_length_of_stay" in ratios  # unaffected by the missing field


def test_ebitda_margin_uses_operating_net_income_not_total_net_income():
    """Distinct from EBITDAR: no lease add-back, and based on net income
    from patients (operating only), not AHD's total net income."""

    ratios = compute_ratios(FULL_METRICS)
    operating_ebitda = Decimal("100000") + Decimal("25000") + Decimal("45000")
    assert ratios["ebitda_margin_pct"] == operating_ebitda / Decimal("1000000") * 100
    assert ratios["ebitda_margin_pct"] != ratios["ebitdar"]  # not accidentally the same figure


def test_cash_to_debt_uses_long_term_liabilities_not_total_liabilities():
    ratios = compute_ratios(FULL_METRICS)
    liquid = Decimal("100000") + Decimal("50000") + Decimal("200000")
    assert ratios["cash_to_debt_pct"] == liquid / Decimal("600000") * 100


def test_cash_to_debt_is_absent_without_long_term_liabilities():
    metrics = dict(FULL_METRICS)
    del metrics["total_long_term_liabilities"]

    assert "cash_to_debt_pct" not in compute_ratios(metrics)


def test_alpha_valued_metrics_are_ignored_not_divided():
    """hospital_name/chain_name/typ_control are strings, not Decimals --
    a ratio referencing them (none do today) must never attempt to divide
    a string, and unrelated ratios must still compute."""

    metrics = dict(FULL_METRICS)
    metrics["hospital_name"] = "Example Hospital"

    ratios = compute_ratios(metrics)
    assert "operating_margin_pct" in ratios
