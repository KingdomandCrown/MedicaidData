"""Worksheet/line/column -> named metric crosswalk for Form CMS-2552-10.

``hcris_numeric``/``hcris_alpha`` store CMS's own opaque (WKSHT_CD, LINE_NUM,
CLMN_NUM) coordinates verbatim -- deliberately undecoded, since guessing a
wrong line number would silently corrupt a financial figure. This crosswalk
is not a guess: it is carried over verbatim (fmt=10 rows only, since every
HOSP10FY<year>.ZIP release this project loads covers exclusively hospitals
filing the 2010 form version) from Adam Sacarny's ``hospital-cost-reports``
project -- https://github.com/asacarny/hospital-cost-reports, ``lookup.xlsx``
-- an openly published, actively maintained academic tool already used in
published health-economics research. Nothing here is re-derived or guessed;
a wrong mapping is a bug in that upstream project to fix there, not a
judgment call made here.

Each entry names one worksheet/column/line coordinate. When ``line_end`` is
set, the metric is the *sum* of every line from ``line_start`` to
``line_end`` inclusive (used for a few beds-by-type breakdowns); otherwise
it is the single value at ``line_start``. ``enabled=False`` entries are
carried over as disabled rather than dropped -- that is the upstream
project's own judgment about which lines it does not trust, not something to
silently drop or silently promote.

This is deliberately a narrower set than a complete HFMA-style ratio
dashboard: liquidity ratios (days cash on hand, current ratio, debt to
capitalization, average age of plant) need Worksheet G (the balance sheet)
and FTE/staffing ratios need Worksheet S-3 Part II, neither of which is in
this crosswalk yet pending a verified hospital-specific (not skilled-nursing)
source for those line numbers.
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
)
# fmt: on


CROSSWALK_BY_METRIC: dict[str, CrosswalkEntry] = {e.metric: e for e in CROSSWALK}
