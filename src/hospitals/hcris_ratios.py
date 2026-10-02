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

Four AHD ratios are deliberately not here: EBITDAR, days cash on hand,
average payment period, and average age of plant all need Worksheet A-7
Part 3's depreciation/interest/lease lines, whose WKSHT_CD isn't confirmed
from a hospital-specific source yet (see hcris_crosswalk.py).
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

    return ratios
