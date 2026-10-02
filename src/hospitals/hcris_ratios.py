"""Standard hospital financial ratios computed from already-decoded metrics.

Dividing two already-verified worksheet cells is arithmetic, not a worksheet
lookup, so these are kept out of ``hcris_crosswalk.py`` -- that module's
entries are exactly what one cell says, nothing else asserted. Every formula
here is quoted directly from American Hospital Directory's published
``FinIndWorksheetRef_2552-10.pdf`` (see ``hcris_crosswalk.py``'s docstring
for the full sourcing), not invented or approximated.

A ratio whose inputs aren't all present for a report is simply absent from
the result -- never computed with a missing value treated as zero, which
would misstate the ratio rather than honestly report that it can't be
computed from what this report filed.

Average age of plant uses only the General Fund column (column 1) of
Worksheet G's accumulated-depreciation lines, where AHD's own formula sums
all four fund columns (General/Specific Purpose/Endowment/Plant). Most
hospitals file the General Fund column only and leave the other three blank
-- per CMS's own instructions, a hospital completes the other fund columns
only if it keeps fund-type accounting records -- so this undercounts only
for that minority of filers rather than for a wrong reason.
"""

from __future__ import annotations

from decimal import Decimal

Metrics = dict[str, Decimal | str]


def _num(metrics: Metrics, key: str) -> Decimal | None:
    value = metrics.get(key)
    return value if isinstance(value, Decimal) else None


def _div(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def compute_ratios(metrics: Metrics) -> dict[str, Decimal]:
    """Standard ratios computable from ``compute_report_metrics``'s output."""

    ratios: dict[str, Decimal] = {}

    def add(name: str, value: Decimal | None) -> None:
        if value is not None:
            ratios[name] = value

    netpatrev = _num(metrics, "netpatrev")
    opexp = _num(metrics, "opexp")
    othinc = _num(metrics, "othinc")
    net_income = _num(metrics, "net_income")
    total_assets = _num(metrics, "total_assets")
    total_liabilities = _num(metrics, "total_liabilities")
    total_current_assets = _num(metrics, "total_current_assets")
    total_current_liabilities = _num(metrics, "total_current_liabilities")
    inventory = _num(metrics, "inventory")
    accounts_receivable = _num(metrics, "accounts_receivable")
    notes_receivable = _num(metrics, "notes_receivable")
    other_receivables = _num(metrics, "other_receivables")
    allow_uncollectible = _num(metrics, "allow_uncollectible")
    total_long_term_liabilities = _num(metrics, "total_long_term_liabilities")
    othexp = _num(metrics, "othexp")
    cash_on_hand = _num(metrics, "cash_on_hand")
    market_securities = _num(metrics, "market_securities")
    investments = _num(metrics, "investments")
    depreciation_expense = _num(metrics, "depreciation_expense")
    lease_cost = _num(metrics, "lease_cost")
    interest_expense = _num(metrics, "interest_expense")
    salary_expense = _num(metrics, "salary_expense")
    fringe_benefits = _num(metrics, "fringe_benefits")
    contract_labor_addon = _num(metrics, "contract_labor_addon")
    contract_labor_main = _num(metrics, "contract_labor_main")

    non_oper_rev = othinc  # AHD's "non-operating revenue" is G-3 line 25, same cell as othinc.
    net_assets = None
    if total_assets is not None and total_liabilities is not None:
        net_assets = total_assets - total_liabilities

    # Operating margin = (tot oper rev - tot oper exp) / tot oper rev * 100
    if netpatrev is not None and opexp is not None and netpatrev != 0:
        add("operating_margin_pct", (netpatrev - opexp) / netpatrev * 100)

    # Excess margin = (tot oper rev - tot oper exp + non-oper rev) / (tot oper rev + non-oper rev) * 100
    if netpatrev is not None and opexp is not None and non_oper_rev is not None:
        denom = netpatrev + non_oper_rev
        if denom != 0:
            add("excess_margin_pct", (netpatrev - opexp + non_oper_rev) / denom * 100)

    # Return on equity = net income / (total assets - total liabilities) * 100
    if net_income is not None and net_assets not in (None, Decimal(0)):
        add("return_on_equity_pct", net_income / net_assets * 100)

    # Return on assets = net income / total assets * 100
    if net_income is not None and total_assets not in (None, Decimal(0)):
        add("return_on_assets_pct", net_income / total_assets * 100)

    # Current ratio = total current assets / total current liabilities
    add("current_ratio", _div(total_current_assets, total_current_liabilities))

    # Quick ratio = (total current assets - inventory) / total current liabilities
    if total_current_assets is not None and inventory is not None:
        add("quick_ratio", _div(total_current_assets - inventory, total_current_liabilities))

    # Days in net patient AR = (AR - allow for uncollectible) / (tot oper rev / 365)
    if accounts_receivable is not None and allow_uncollectible is not None and netpatrev not in (None, Decimal(0)):
        add("days_in_net_patient_ar", (accounts_receivable - allow_uncollectible) / (netpatrev / 365))

    # Days in net total receivable = (AR + notes rec + other rec - allow) / (tot oper rev / 365)
    if (
        accounts_receivable is not None
        and notes_receivable is not None
        and other_receivables is not None
        and allow_uncollectible is not None
        and netpatrev not in (None, Decimal(0))
    ):
        total_receivable = accounts_receivable + notes_receivable + other_receivables - allow_uncollectible
        add("days_in_net_total_receivable", total_receivable / (netpatrev / 365))

    # Inventory turnover = (tot oper rev + non-oper rev) / inventory
    if netpatrev is not None and non_oper_rev is not None and inventory is not None:
        add("inventory_turnover", _div(netpatrev + non_oper_rev, inventory))

    # Total asset turnover = (tot oper rev + non-oper rev) / total assets
    if netpatrev is not None and non_oper_rev is not None and total_assets is not None:
        add("total_asset_turnover", _div(netpatrev + non_oper_rev, total_assets))

    # LT debt to net assets = total long term liabilities / (total assets - total liabilities)
    if total_long_term_liabilities is not None and net_assets not in (None, Decimal(0)):
        add("lt_debt_to_net_assets", total_long_term_liabilities / net_assets)

    # Total debt to net assets = total liabilities / (total assets - total liabilities)
    if total_liabilities is not None and net_assets not in (None, Decimal(0)):
        add("total_debt_to_net_assets", total_liabilities / net_assets)

    # EBITDAR = net income + interest + depreciation + lease cost
    if net_income is not None and interest_expense is not None and depreciation_expense is not None and lease_cost is not None:
        add("ebitdar", net_income + interest_expense + depreciation_expense + lease_cost)

    oper_exp_less_depr = None
    if opexp is not None and depreciation_expense is not None:
        oper_exp_less_depr = opexp - depreciation_expense

    # Days cash on hand = (cash + market securities) / ((tot oper exp - depreciation) / 365)
    if cash_on_hand is not None and market_securities is not None and oper_exp_less_depr not in (None, Decimal(0)):
        add("days_cash_on_hand", (cash_on_hand + market_securities) / (oper_exp_less_depr / 365))

    # Days cash on hand, all sources = (cash + market securities + investments) / ((tot oper exp - depreciation) / 365)
    if (
        cash_on_hand is not None
        and market_securities is not None
        and investments is not None
        and oper_exp_less_depr not in (None, Decimal(0))
    ):
        add(
            "days_cash_on_hand_all_sources",
            (cash_on_hand + market_securities + investments) / (oper_exp_less_depr / 365),
        )

    # Average payment period = total current liabilities / ((tot oper exp + tot other exp - depreciation) / 365)
    if total_current_liabilities is not None and opexp is not None and othexp is not None and depreciation_expense is not None:
        denom = (opexp + othexp - depreciation_expense) / 365
        if denom != 0:
            add("average_payment_period_days", total_current_liabilities / denom)

    # Average age of plant = accumulated depreciation / depreciation expense.
    # General Fund column (column 1) only -- see module docstring.
    accum_depr_fields = [
        metrics.get(key)
        for key in (
            "accum_depr_land_improvements", "accum_depr_buildings", "accum_depr_leasehold",
            "accum_depr_fixed_equipment", "accum_depr_autos_trucks", "accum_depr_major_movable",
            "accum_depr_minor_movable",
        )
    ]
    accum_depr_present = [v for v in accum_depr_fields if isinstance(v, Decimal)]
    if accum_depr_present and depreciation_expense not in (None, Decimal(0)):
        add("average_age_of_plant", sum(accum_depr_present) / depreciation_expense)

    # Personnel expense as % of total operating revenue
    # = (salary + contract labor + fringe benefits) / net patient revenue * 100
    if contract_labor_addon is not None and contract_labor_main is not None:
        contract_labor = contract_labor_addon + contract_labor_main
        if salary_expense is not None and fringe_benefits is not None and netpatrev not in (None, Decimal(0)):
            add(
                "personnel_expense_pct",
                (salary_expense + contract_labor + fringe_benefits) / netpatrev * 100,
            )

    return ratios
