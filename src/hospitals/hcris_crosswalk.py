"""Worksheet/line/column -> named metric crosswalk for Form CMS-2552-10.

``hcris_numeric``/``hcris_alpha`` store CMS's own opaque (WKSHT_CD, LINE_NUM,
CLMN_NUM) coordinates verbatim -- deliberately undecoded, since guessing a
wrong line number would silently corrupt a financial figure. Two independent,
published, hospital-specific sources back every entry here, not a guess:

* The ``netpatrev``/``opexp``/beds/discharges/uncompensated-care entries are
  carried over verbatim (fmt=10 rows only, since every HOSP10FY<year>.ZIP
  release this project loads covers exclusively hospitals filing the 2010
  form version) from Adam Sacarny's ``hospital-cost-reports`` project --
  https://github.com/asacarny/hospital-cost-reports, ``lookup.xlsx`` -- an
  openly published, actively maintained academic tool already used in
  published health-economics research.
* The Worksheet G (balance sheet), additional Worksheet G-3 entries, and the
  Worksheet A / A-7 Part III / S-3 Part II entries are carried over from
  American Hospital Directory's ``FinIndWorksheetRef_2552-10.pdf`` and the
  AHA DataQuery Cost Report Fields Dictionary -- both name the exact
  worksheet/line/column for the 2010 hospital form specifically (not
  skilled-nursing, which uses the same worksheet letters for different
  content). Several lines overlap and agree with Sacarny's entries
  (donations at G-3 line 6, investment income at line 7, net patient revenue
  at line 3, total operating expense at line 4), which is corroborating
  cross-confirmation between the two independent sources, not a coincidence.

Neither source states the raw WKSHT_CD for Worksheet A-7 Part III or
Worksheet S-3 Part II (they cite worksheets by name only), so those two were
confirmed directly against this project's own stored data: querying the
distinct (wksht_cd, column range, line range) actually present turned up
``A700003`` (columns 1-15, lines 1-3 -- exactly matching AHD's "A-7, part 3,
line 3, col 9/10/11", and following the identical ``A700001``/``A700002``
Part I/II pattern already visible alongside it) and ``S300002`` (columns
1-6, lines 1-43 -- matching AHD's "S-3, part 2, lines 7.01/11-16, col 4",
alongside the already-confirmed ``S300001`` for Part I). Every line/column
this crosswalk uses from those two codes falls inside the observed range,
which would be a remarkable coincidence if either code were wrong.

S300002's own column layout -- also not stated by either source beyond "col
4" for contract labor -- was likewise confirmed from the data: column 1 is
not a data column at all but a cross-reference to the corresponding
Worksheet A line (constant ``200`` wherever line 1 appears, matching AHD's
own "Salary Expense - A, line 200" citation), column 2 is the dollar amount
as reported, column 3 is a rare reclassification adjustment, column 4 is the
reclassified amount (column 2 adjusted by column 3 -- identical to column 2
whenever there is no reclassification, which is why AHD's contract-labor
citation and the real data agree exactly), and column 5 is the paid hours
tied to that dollar amount. There is no literal "FTE" field anywhere on this
worksheet; hcris_ratios.py derives one from total_paid_hours using the
standard 2080-hour FTE-year convention.

A wrong mapping is a bug in one of those upstream sources (or in this
project's own data) to fix at the source, not a judgment call made here.

Each entry names one worksheet/column/line coordinate. When ``line_end`` is
set, the metric is the *sum* of every line from ``line_start`` to
``line_end`` inclusive (used for a few beds-by-type breakdowns); otherwise
it is the single value at ``line_start``. ``enabled=False`` entries are
carried over as disabled rather than dropped -- that is the upstream
project's own judgment about which lines it does not trust, not something to
silently drop or silently promote.

Ratios built from these base figures (current ratio, operating margin, EBITDAR,
days cash on hand, etc.) are computed in ``hcris_ratios.py``, not asserted as
their own crosswalk entries -- dividing two already-verified cells is
arithmetic, not a worksheet lookup.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CrosswalkEntry:
    metric: str
    label: str
    metric_type: str  # dollar_flow | flow | stock | alpha
    wksht_cd: str
    clmn_num: str
    line_start: str
    line_end: str | None
    enabled: bool


# fmt: off
CROSSWALK: tuple[CrosswalkEntry, ...] = (
    CrosswalkEntry('netpatrev', 'net patient revenues (total revenues minus allowances & discounts)', 'dollar_flow', 'G300000', '00100', '00300', None, True),
    CrosswalkEntry('othinc', 'other income', 'dollar_flow', 'G300000', '00100', '02500', None, True),
    CrosswalkEntry('opexp', 'total operating expenses', 'dollar_flow', 'G300000', '00100', '00400', None, True),
    CrosswalkEntry('othexp', 'total other expenses', 'dollar_flow', 'G300000', '00100', '02800', None, True),
    CrosswalkEntry('donations', 'donations', 'dollar_flow', 'G300000', '00100', '00600', None, True),
    CrosswalkEntry('invinc', 'investment income', 'dollar_flow', 'G300000', '00100', '00700', None, True),
    CrosswalkEntry('iphosprev', 'inpatient hospital revenue', 'dollar_flow', 'G200000', '00100', '00100', None, True),
    CrosswalkEntry('ipgenrev', 'inpatient general revenue (total of hosp, ipf, irf, snf, etc.)', 'dollar_flow', 'G200000', '00100', '01000', None, True),
    CrosswalkEntry('ipicrev', 'inpatient intensive care type revenue (total of icu, ccu, etc.)', 'dollar_flow', 'G200000', '00100', '01600', None, True),
    CrosswalkEntry('iprcrev', 'inpatient routine care revenue (sum of ipgenrev and ipicrev)', 'dollar_flow', 'G200000', '00100', '01700', None, True),
    CrosswalkEntry('ipancrev', 'inpatient ancillary services revenue', 'dollar_flow', 'G200000', '00100', '01800', None, True),
    CrosswalkEntry('ipoprev', 'inpatient outpatient services revenue', 'dollar_flow', 'G200000', '00100', '01900', None, True),
    CrosswalkEntry('iptotrev', 'inpatient total patient revenue', 'dollar_flow', 'G200000', '00100', '02800', None, True),
    CrosswalkEntry('opancrev', 'outpatient ancillary services revenue', 'dollar_flow', 'G200000', '00200', '01800', None, True),
    CrosswalkEntry('opoprev', 'outpatient outpatient services revenue', 'dollar_flow', 'G200000', '00200', '01900', None, True),
    CrosswalkEntry('optotrev', 'outpatient total patient revenues', 'dollar_flow', 'G200000', '00200', '02800', None, True),
    CrosswalkEntry('tottotrev', 'total patient revenue (sum of iptotrev and optotrev)', 'dollar_flow', 'G200000', '00300', '02800', None, True),
    CrosswalkEntry('ccr', 'cost to charge ratio', 'stock', 'S100000', '00100', '00100', None, True),
    CrosswalkEntry('totinitchcare', 'total initial obligation of patients for charity care (2010 format only)', 'dollar_flow', 'S100000', '00300', '02000', None, True),
    CrosswalkEntry('ppaychcare', 'partial payment by patients approved for charity care (2010 format only)', 'dollar_flow', 'S100000', '00300', '02200', None, True),
    CrosswalkEntry('nonmcbaddebt', 'non-medicare bad debt expense (2010 format only)', 'dollar_flow', 'S100000', '00100', '02800', None, True),
    CrosswalkEntry('costuccare_v2010', 'cost of uncompensated care (2010 format only)', 'dollar_flow', 'S100000', '00100', '03000', None, True),
    CrosswalkEntry('beds_adultped', 'beds - adults & peds', 'stock', 'S300001', '00200', '00100', None, True),
    CrosswalkEntry('availbeddays_adultped', 'bed days available in rpt period', 'flow', 'S300001', '00300', '00100', None, True),
    CrosswalkEntry('ipbeddays_adultped', 'inpatient bed days utilized', 'flow', 'S300001', '00800', '00100', None, True),
    CrosswalkEntry('ipdischarges_adultped', 'inpatient discharges', 'flow', 'S300001', '01500', '00100', None, True),
    CrosswalkEntry('beds_totadultped', 'beds - total adults & peds incl swing beds', 'stock', 'S300001', '00200', '00700', None, True),
    CrosswalkEntry('icu_beds', 'intensive care unit beds', 'stock', 'S300001', '00200', '00800', '00899', True),
    CrosswalkEntry('ccu_beds', 'coronary care unit beds', 'stock', 'S300001', '00200', '00900', '00999', True),
    CrosswalkEntry('bicu_beds', 'burn intensive care unit beds', 'stock', 'S300001', '00200', '01000', '01099', True),
    CrosswalkEntry('sicu_beds', 'surgical intensive care unit beds', 'stock', 'S300001', '00200', '01100', '01199', True),
    CrosswalkEntry('othspec_beds', 'other special care beds', 'stock', 'S300001', '00200', '01200', '01299', True),
    CrosswalkEntry('beds_total', 'beds - total (inc swing + spec care beds e.g. icu, ccu, nicu)', 'stock', 'S300001', '00200', '01400', None, True),
    CrosswalkEntry('beds_grandtotal', 'beds - grand total (total + subprovider/snf/nf/hospice)', 'stock', 'S300001', '00200', '02700', None, True),
    CrosswalkEntry('costinitchcare', 'cost of patients approved for charity care and uninsured discounts (2010 format only)', 'dollar_flow', 'S100000', '00300', '02100', None, False),
    CrosswalkEntry('costchcare', 'cost of charity care (2010 format only)', 'dollar_flow', 'S100000', '00300', '02300', None, True),
    CrosswalkEntry('totbaddebt', 'total bad debt expense (2010 format only)', 'dollar_flow', 'S100000', '00100', '02600', None, False),
    CrosswalkEntry('mcbaddebt', 'medicare reimbursable bad debts (2010 format only)', 'dollar_flow', 'S100000', '00100', '02700', None, False),
    CrosswalkEntry('baddebt', 'cost of non-Medicare and non-reimbursable Medicare bad debt expense (2010 format only)', 'dollar_flow', 'S100000', '00100', '02900', None, False),
    CrosswalkEntry('prog_nursery_cost', 'medicare inpatient program nursery cost', 'dollar_flow', 'D10A181', '00500', '04200', None, False),
    CrosswalkEntry('prog_op_cost', 'medicare inpatient program operating cost', 'dollar_flow', 'D10A181', '00100', '05300', None, True),
    CrosswalkEntry('prog_rt_chg', 'medicare inpatient program routine service charges', 'dollar_flow', 'D30A180', '00200', '03000', '03599', True),
    CrosswalkEntry('prog_net_chg', 'medicare inpatient program ancillary service net charges', 'dollar_flow', 'D30A180', '00200', '20200', None, True),
    CrosswalkEntry('typ_control', 'type of control', 'alpha', 'S200001', '00100', '02100', None, True),
    CrosswalkEntry('hospital_name', 'name of hospital', 'alpha', 'S200001', '00100', '00300', None, True),
    CrosswalkEntry('chain_name', 'name of chain organization', 'alpha', 'S200001', '00100', '14100', None, True),

    # Worksheet G (balance sheet) and additional Worksheet G-3 lines, sourced
    # from AHD's FinIndWorksheetRef_2552-10.pdf and the AHA DataQuery Cost
    # Report Fields Dictionary (see module docstring).
    CrosswalkEntry('cash_on_hand', 'cash on hand and in banks', 'stock', 'G000000', '00100', '00100', None, True),
    CrosswalkEntry('market_securities', 'temporary investments / marketable securities', 'stock', 'G000000', '00100', '00200', None, True),
    CrosswalkEntry('notes_receivable', 'notes receivable', 'stock', 'G000000', '00100', '00300', None, True),
    CrosswalkEntry('accounts_receivable', 'accounts receivable', 'stock', 'G000000', '00100', '00400', None, True),
    CrosswalkEntry('other_receivables', 'other receivables', 'stock', 'G000000', '00100', '00500', None, True),
    CrosswalkEntry('allow_uncollectible', 'allowances for uncollectible notes and accounts receivable (contra-asset)', 'stock', 'G000000', '00100', '00600', None, True),
    CrosswalkEntry('inventory', 'inventory', 'stock', 'G000000', '00100', '00700', None, True),
    CrosswalkEntry('prepaid_expenses', 'prepaid expenses', 'stock', 'G000000', '00100', '00800', None, True),
    CrosswalkEntry('other_current_assets', 'other current assets', 'stock', 'G000000', '00100', '00900', None, True),
    CrosswalkEntry('due_from_other_funds', 'due from other funds', 'stock', 'G000000', '00100', '01000', None, True),
    CrosswalkEntry('total_current_assets', 'total current assets', 'stock', 'G000000', '00100', '01100', None, True),
    CrosswalkEntry('land', 'land', 'stock', 'G000000', '00100', '01200', None, True),
    CrosswalkEntry('land_improvements', 'land improvements', 'stock', 'G000000', '00100', '01300', None, True),
    CrosswalkEntry('accum_depr_land_improvements', 'accumulated depreciation - land improvements', 'stock', 'G000000', '00100', '01400', None, True),
    CrosswalkEntry('buildings', 'buildings', 'stock', 'G000000', '00100', '01500', None, True),
    CrosswalkEntry('accum_depr_buildings', 'accumulated depreciation - buildings', 'stock', 'G000000', '00100', '01600', None, True),
    CrosswalkEntry('leasehold_improvements', 'leasehold improvements', 'stock', 'G000000', '00100', '01700', None, True),
    CrosswalkEntry('accum_depr_leasehold', 'accumulated depreciation - leasehold improvements', 'stock', 'G000000', '00100', '01800', None, True),
    CrosswalkEntry('fixed_equipment', 'fixed equipment', 'stock', 'G000000', '00100', '01900', None, True),
    CrosswalkEntry('accum_depr_fixed_equipment', 'accumulated depreciation - fixed equipment', 'stock', 'G000000', '00100', '02000', None, True),
    CrosswalkEntry('autos_trucks', 'automobiles and trucks', 'stock', 'G000000', '00100', '02100', None, True),
    CrosswalkEntry('accum_depr_autos_trucks', 'accumulated depreciation - automobiles and trucks', 'stock', 'G000000', '00100', '02200', None, True),
    CrosswalkEntry('major_movable_equipment', 'major movable equipment', 'stock', 'G000000', '00100', '02300', None, True),
    CrosswalkEntry('accum_depr_major_movable', 'accumulated depreciation - major movable equipment', 'stock', 'G000000', '00100', '02400', None, True),
    CrosswalkEntry('minor_movable_equipment_depreciable', 'minor movable equipment (depreciable)', 'stock', 'G000000', '00100', '02500', None, True),
    CrosswalkEntry('accum_depr_minor_movable', 'accumulated depreciation - minor movable equipment', 'stock', 'G000000', '00100', '02600', None, True),
    CrosswalkEntry('hit_designated_assets', 'health information technology (HIT) designated assets', 'stock', 'G000000', '00100', '02700', None, True),
    CrosswalkEntry('accum_depr_hit', 'accumulated depreciation - HIT designated assets', 'stock', 'G000000', '00100', '02800', None, True),
    CrosswalkEntry('minor_equipment_nondepreciable', 'minor equipment (nondepreciable)', 'stock', 'G000000', '00100', '02900', None, True),
    CrosswalkEntry('total_fixed_assets', 'total fixed assets', 'stock', 'G000000', '00100', '03000', None, True),
    CrosswalkEntry('investments', 'investments', 'stock', 'G000000', '00100', '03100', None, True),
    CrosswalkEntry('deposits_on_leases', 'deposits on leases', 'stock', 'G000000', '00100', '03200', None, True),
    CrosswalkEntry('due_from_owners_officers', 'due from owners/officers', 'stock', 'G000000', '00100', '03300', None, True),
    CrosswalkEntry('total_other_assets', 'total other assets', 'stock', 'G000000', '00100', '03500', None, True),
    CrosswalkEntry('total_assets', 'total assets', 'stock', 'G000000', '00100', '03600', None, True),
    CrosswalkEntry('accounts_payable', 'accounts payable', 'stock', 'G000000', '00100', '03700', None, True),
    CrosswalkEntry('salaries_wages_fees_payable', 'salaries, wages, and fees payable', 'stock', 'G000000', '00100', '03800', None, True),
    CrosswalkEntry('payroll_taxes_payable', 'payroll taxes payable', 'stock', 'G000000', '00100', '03900', None, True),
    CrosswalkEntry('notes_loans_payable_st', 'notes and loans payable (short term)', 'stock', 'G000000', '00100', '04000', None, True),
    CrosswalkEntry('deferred_income', 'deferred income', 'stock', 'G000000', '00100', '04100', None, True),
    CrosswalkEntry('accelerated_payments', 'accelerated payments', 'stock', 'G000000', '00100', '04200', None, True),
    CrosswalkEntry('due_to_other_funds', 'due to other funds', 'stock', 'G000000', '00100', '04300', None, True),
    CrosswalkEntry('other_current_liabilities', 'other current liabilities', 'stock', 'G000000', '00100', '04400', None, True),
    CrosswalkEntry('total_current_liabilities', 'total current liabilities', 'stock', 'G000000', '00100', '04500', None, True),
    CrosswalkEntry('mortgage_payable', 'mortgage payable', 'stock', 'G000000', '00100', '04600', None, True),
    CrosswalkEntry('notes_payable_lt', 'notes payable (long term)', 'stock', 'G000000', '00100', '04700', None, True),
    CrosswalkEntry('unsecured_loans', 'unsecured loans', 'stock', 'G000000', '00100', '04800', None, True),
    CrosswalkEntry('other_long_term_liabilities', 'other long term liabilities', 'stock', 'G000000', '00100', '04900', None, True),
    CrosswalkEntry('total_long_term_liabilities', 'total long term liabilities', 'stock', 'G000000', '00100', '05000', None, True),
    CrosswalkEntry('total_liabilities', 'total liabilities', 'stock', 'G000000', '00100', '05100', None, True),
    CrosswalkEntry('general_fund_balance', 'general fund balance', 'stock', 'G000000', '00100', '05200', None, True),
    CrosswalkEntry('specific_purpose_fund_balance', 'specific purpose fund balance', 'stock', 'G000000', '00100', '05300', None, True),
    CrosswalkEntry('donor_restricted_endowment_fund_balance', 'donor-created restricted endowment fund balance', 'stock', 'G000000', '00100', '05400', None, True),
    CrosswalkEntry('donor_unrestricted_endowment_fund_balance', 'donor-created unrestricted endowment fund balance', 'stock', 'G000000', '00100', '05500', None, True),
    CrosswalkEntry('governing_body_endowment_fund_balance', 'governing body created endowment fund balance', 'stock', 'G000000', '00100', '05600', None, True),
    CrosswalkEntry('plant_fund_balance_invested', 'plant fund balance invested in plant', 'stock', 'G000000', '00100', '05700', None, True),
    CrosswalkEntry('plant_fund_balance_reserve', 'plant fund balance reserve for plant improvement', 'stock', 'G000000', '00100', '05800', None, True),
    CrosswalkEntry('total_fund_balances', 'total fund balances', 'stock', 'G000000', '00100', '05900', None, True),
    CrosswalkEntry('total_liabilities_and_fund_balances', 'total liabilities and fund balances', 'stock', 'G000000', '00100', '06000', None, True),
    CrosswalkEntry('contractual_allowances', 'contractual allowances and discounts on patient accounts', 'dollar_flow', 'G300000', '00100', '00200', None, True),
    CrosswalkEntry('net_income_from_patients', 'net income from service to patients (net patient revenue less total operating expenses)', 'dollar_flow', 'G300000', '00100', '00500', None, True),
    CrosswalkEntry('total_income', 'total income (net income from patients plus total other income)', 'dollar_flow', 'G300000', '00100', '02600', None, True),
    CrosswalkEntry('net_income', 'net income (or loss) for the period', 'dollar_flow', 'G300000', '00100', '02900', None, True),

    # Worksheet A (A000000), A-7 Part III (A700003), and S-3 Part II
    # (S300002) -- see module docstring for how the latter two codes were
    # confirmed, since neither AHD nor AHA names them directly.
    CrosswalkEntry('salary_expense', 'total salaries (worksheet A, line 200, col 1)', 'dollar_flow', 'A000000', '00100', '20000', None, True),
    CrosswalkEntry('fringe_benefits', 'employee benefits (worksheet A, line 4, col 2)', 'dollar_flow', 'A000000', '00200', '00400', None, True),
    CrosswalkEntry('depreciation_expense', 'depreciation and amortization expense (worksheet A-7 part III, line 3, col 9)', 'dollar_flow', 'A700003', '00900', '00300', None, True),
    CrosswalkEntry('lease_cost', 'lease cost (worksheet A-7 part III, line 3, col 10)', 'dollar_flow', 'A700003', '01000', '00300', None, True),
    CrosswalkEntry('interest_expense', 'interest expense (worksheet A-7 part III, line 3, col 11)', 'dollar_flow', 'A700003', '01100', '00300', None, True),
    CrosswalkEntry('contract_labor_addon', 'contract labor, add-on line 7.01 (worksheet S-3 part II, col 4)', 'dollar_flow', 'S300002', '00400', '00701', None, True),
    CrosswalkEntry('contract_labor_main', 'contract labor, lines 11-16 (worksheet S-3 part II, col 4)', 'dollar_flow', 'S300002', '00400', '01100', '01600', True),
    # S-3 Part II has no literal "FTE" field -- line 1 (which cross-references
    # Worksheet A line 200, "Total salaries," confirmed via column 1's own
    # content) is the hospital-wide total row, and column 5 there is total
    # paid hours. FTEs are computed from this via the standard 2080-hour
    # FTE-year convention in hcris_ratios.py, not asserted as a raw field.
    CrosswalkEntry('total_paid_hours', 'total hospital paid hours, all employees (worksheet S-3 part II, line 1, col 5)', 'flow', 'S300002', '00500', '00100', None, True),
)
# fmt: on


CROSSWALK_BY_METRIC: dict[str, CrosswalkEntry] = {e.metric: e for e in CROSSWALK}
