"""Normalization of raw CMS Provider of Services (POS) records.

The POS file is a wide, code-heavy fixed vocabulary. This module maps the raw
uppercase POS columns into a clean :class:`HospitalRecord`, normalizing
identifiers (CCN, ZIP, phone) and decoding the handful of coded fields we care
about (provider category, ownership, termination status).

Field names follow the CMS POS "Hospital & Non-Hospital Facilities" data
dictionary. Both the CMS data-api (JSON) and the downloadable CSV use these
same uppercase column names, so a single mapping serves both.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass

from .states import State, state_for_ssa_code

# --- Raw POS column names -------------------------------------------------

COL_PROVIDER_NUM = "PRVDR_NUM"  # CCN (CMS Certification Number)
COL_CATEGORY = "PRVDR_CTGRY_CD"  # provider category, "01" == Hospital
COL_SUBTYPE = "PRVDR_CTGRY_SBTYP_CD"  # provider category subtype
COL_NAME = "FAC_NAME"
COL_STREET = "ST_ADR"
COL_CITY = "CITY_NAME"
COL_STATE = "STATE_CD"  # two-letter USPS abbreviation
COL_ZIP = "ZIP_CD"
COL_COUNTY = "CNTY_NAME"
COL_PHONE = "TEL_NUM"
COL_TERMINATION = "PGM_TRMNTN_CD"  # "00" == active / not terminated
COL_CERT_DATE = "CRTFCTN_DT"
COL_OWNERSHIP = "GNRL_CNTL_TYPE_CD"
COL_BED_COUNT = "CRTFD_BED_CNT"
COL_SSA_STATE = "SSA_STATE_CD"
COL_FIPS_STATE = "FIPS_STATE_CD"

# Provider category code for hospitals in the POS file.
CATEGORY_HOSPITAL = "01"

# Program termination code meaning "active" (still participating).
TERMINATION_ACTIVE = "00"

# --- Code lookups ---------------------------------------------------------

# Provider category subtype for hospitals (PRVDR_CTGRY_SBTYP_CD when
# PRVDR_CTGRY_CD == "01"). Unmapped codes fall back to "Other/Unknown".
HOSPITAL_SUBTYPES: dict[str, str] = {
    "01": "Short-term (Acute Care)",
    "02": "Long-term",
    "03": "Religious Non-medical Health Care Institution",
    "04": "Psychiatric",
    "05": "Rehabilitation",
    "06": "Childrens",
    "07": "Distinct Part Psychiatric Unit",
    "08": "Distinct Part Rehabilitation Unit",
    "09": "Short-term (Critical Access Hospital)",
    "10": "Alcohol/Drug",
}

# General control (ownership) type. Codes per POS GNRL_CNTL_TYPE_CD.
OWNERSHIP_TYPES: dict[str, str] = {
    "01": "Voluntary Nonprofit - Church",
    "02": "Voluntary Nonprofit - Private",
    "03": "Voluntary Nonprofit - Other",
    "04": "Proprietary - Individual",
    "05": "Proprietary - Corporation",
    "06": "Proprietary - Partnership",
    "07": "Proprietary - Other",
    "08": "Government - Federal",
    "09": "Government - State",
    "10": "Government - County",
    "11": "Government - City",
    "12": "Government - City/County",
    "13": "Government - Hospital District",
    "14": "Government - Other",
}


@dataclass
class HospitalRecord:
    """A normalized hospital, ready to load into the database."""

    ccn: str
    name: str
    provider_category_code: str
    provider_subtype_code: str
    provider_subtype: str
    address: str | None
    city: str | None
    state: str | None
    zip5: str | None
    county: str | None
    phone: str | None
    ownership_code: str | None
    ownership_type: str | None
    certified_bed_count: int | None
    certification_date: dt.date | None
    termination_code: str | None
    is_active: bool
    ssa_state_code: str | None
    fips_state_code: str | None

    def as_dict(self) -> dict:
        return asdict(self)


# --- Field-level normalizers ---------------------------------------------


def clean_str(value) -> str | None:
    """Trim, collapse internal whitespace, and null out empties."""

    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def normalize_ccn(value) -> str | None:
    """Normalize a CMS Certification Number.

    CCNs are six characters. Numeric CCNs shorter than six characters (e.g.
    when a leading zero was dropped by a spreadsheet) are left-padded with
    zeros. Alphanumeric CCNs are upper-cased and returned as-is.
    """

    text = clean_str(value)
    if text is None:
        return None
    text = text.replace(" ", "").upper()
    if not text:
        return None
    if text.isdigit() and len(text) < 6:
        text = text.zfill(6)
    return text


def ccn_state(ccn: str | None) -> State | None:
    """Return the state implied by a CCN's leading SSA code, if resolvable."""

    if not ccn or len(ccn) < 2:
        return None
    return state_for_ssa_code(ccn[:2])


def normalize_zip(value) -> str | None:
    """Return the 5-digit ZIP, dropping any ZIP+4 suffix."""

    text = clean_str(value)
    if text is None:
        return None
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    return digits[:5].zfill(5) if len(digits) >= 5 else digits.zfill(5)


def normalize_phone(value) -> str | None:
    """Return a phone as digits only (10 digits when possible)."""

    text = clean_str(value)
    if text is None:
        return None
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    # Drop a leading US country code if present.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits or None


def normalize_int(value) -> int | None:
    text = clean_str(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except (ValueError, TypeError):
        return None


def parse_date(value) -> dt.date | None:
    """Parse a POS date, which appears as YYYYMMDD or an ISO date string."""

    text = clean_str(value)
    if text is None:
        return None
    text = text.split("T")[0]  # tolerate ISO datetimes
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# --- Record-level mapping -------------------------------------------------


def get(raw: dict, key: str) -> object:
    """Case-insensitive lookup against a raw POS record."""

    if key in raw:
        return raw[key]
    lowered = key.lower()
    for k, v in raw.items():
        if k.lower() == lowered:
            return v
    return None


def is_hospital(raw: dict) -> bool:
    """True when the raw record is a hospital (provider category 01)."""

    return clean_str(get(raw, COL_CATEGORY)) == CATEGORY_HOSPITAL


def is_active(raw: dict) -> bool:
    """True when the provider's termination code marks it active."""

    return clean_str(get(raw, COL_TERMINATION)) == TERMINATION_ACTIVE


def normalize_record(raw: dict) -> HospitalRecord:
    """Map a single raw POS record to a normalized :class:`HospitalRecord`."""

    ccn = normalize_ccn(get(raw, COL_PROVIDER_NUM))
    subtype_code = clean_str(get(raw, COL_SUBTYPE)) or ""
    ownership_code = clean_str(get(raw, COL_OWNERSHIP))
    termination_code = clean_str(get(raw, COL_TERMINATION))

    # Prefer the explicit state column; fall back to the CCN-derived state.
    state = clean_str(get(raw, COL_STATE))
    if state:
        state = state.upper()
    if not state:
        derived = ccn_state(ccn)
        state = derived.usps if derived else None

    return HospitalRecord(
        ccn=ccn or "",
        name=clean_str(get(raw, COL_NAME)) or "",
        provider_category_code=clean_str(get(raw, COL_CATEGORY)) or "",
        provider_subtype_code=subtype_code,
        provider_subtype=HOSPITAL_SUBTYPES.get(subtype_code, "Other/Unknown"),
        address=clean_str(get(raw, COL_STREET)),
        city=clean_str(get(raw, COL_CITY)),
        state=state,
        zip5=normalize_zip(get(raw, COL_ZIP)),
        county=clean_str(get(raw, COL_COUNTY)),
        phone=normalize_phone(get(raw, COL_PHONE)),
        ownership_code=ownership_code,
        ownership_type=OWNERSHIP_TYPES.get(ownership_code or "", None),
        certified_bed_count=normalize_int(get(raw, COL_BED_COUNT)),
        certification_date=parse_date(get(raw, COL_CERT_DATE)),
        termination_code=termination_code,
        is_active=termination_code == TERMINATION_ACTIVE,
        ssa_state_code=clean_str(get(raw, COL_SSA_STATE)) or (ccn[:2] if ccn else None),
        fips_state_code=clean_str(get(raw, COL_FIPS_STATE)),
    )
