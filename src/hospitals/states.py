"""State reference data used across the pipeline.

The CMS CCN (CMS Certification Number) encodes an SSA (Social Security
Administration) state code in its first two characters, which differs from the
FIPS code and the USPS abbreviation. We keep all three so we can filter,
validate, and cross-check identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class State:
    """A U.S. state / territory and its various codes."""

    name: str
    usps: str  # two-letter postal abbreviation, e.g. "KS"
    ssa_code: str  # two-digit SSA state code used as the CCN prefix, e.g. "17"
    fips_code: str  # two-digit FIPS state code, e.g. "20"


# SSA state codes as used in the leading two digits of a CMS Certification
# Number. Source: CMS Provider of Services file data dictionary / SSA state
# code table. Only the 50 states + DC are enumerated here; extend as needed.
STATES: dict[str, State] = {
    "AL": State("Alabama", "AL", "01", "01"),
    "AK": State("Alaska", "AK", "02", "02"),
    "AZ": State("Arizona", "AZ", "03", "04"),
    "AR": State("Arkansas", "AR", "04", "05"),
    "CA": State("California", "CA", "05", "06"),
    "CO": State("Colorado", "CO", "06", "08"),
    "CT": State("Connecticut", "CT", "07", "09"),
    "DE": State("Delaware", "DE", "08", "10"),
    "DC": State("District of Columbia", "DC", "09", "11"),
    "FL": State("Florida", "FL", "10", "12"),
    "GA": State("Georgia", "GA", "11", "13"),
    "HI": State("Hawaii", "HI", "12", "15"),
    "ID": State("Idaho", "ID", "13", "16"),
    "IL": State("Illinois", "IL", "14", "17"),
    "IN": State("Indiana", "IN", "15", "18"),
    "IA": State("Iowa", "IA", "16", "19"),
    "KS": State("Kansas", "KS", "17", "20"),
    "KY": State("Kentucky", "KY", "18", "21"),
    "LA": State("Louisiana", "LA", "19", "22"),
    "ME": State("Maine", "ME", "20", "23"),
    "MD": State("Maryland", "MD", "21", "24"),
    "MA": State("Massachusetts", "MA", "22", "25"),
    "MI": State("Michigan", "MI", "23", "26"),
    "MN": State("Minnesota", "MN", "24", "27"),
    "MS": State("Mississippi", "MS", "25", "28"),
    "MO": State("Missouri", "MO", "26", "29"),
    "MT": State("Montana", "MT", "27", "30"),
    "NE": State("Nebraska", "NE", "28", "31"),
    "NV": State("Nevada", "NV", "29", "32"),
    "NH": State("New Hampshire", "NH", "30", "33"),
    "NJ": State("New Jersey", "NJ", "31", "34"),
    "NM": State("New Mexico", "NM", "32", "35"),
    "NY": State("New York", "NY", "33", "36"),
    "NC": State("North Carolina", "NC", "34", "37"),
    "ND": State("North Dakota", "ND", "35", "38"),
    "OH": State("Ohio", "OH", "36", "39"),
    "OK": State("Oklahoma", "OK", "37", "40"),
    "OR": State("Oregon", "OR", "38", "41"),
    "PA": State("Pennsylvania", "PA", "39", "42"),
    "RI": State("Rhode Island", "RI", "41", "44"),
    "SC": State("South Carolina", "SC", "42", "45"),
    "SD": State("South Dakota", "SD", "43", "46"),
    "TN": State("Tennessee", "TN", "44", "47"),
    "TX": State("Texas", "TX", "45", "48"),
    "UT": State("Utah", "UT", "46", "49"),
    "VT": State("Vermont", "VT", "47", "50"),
    "VA": State("Virginia", "VA", "48", "51"),
    "WA": State("Washington", "WA", "49", "53"),
    "WV": State("West Virginia", "WV", "50", "54"),
    "WI": State("Wisconsin", "WI", "51", "55"),
    "WY": State("Wyoming", "WY", "52", "56"),
}

# Build reverse lookups once.
_BY_SSA = {s.ssa_code: s for s in STATES.values()}


def resolve_state(value: str) -> State:
    """Resolve a state from a USPS abbreviation or full name.

    Raises ``KeyError`` if the state is unknown.
    """

    if not value:
        raise KeyError("empty state value")
    key = value.strip().upper()
    if key in STATES:
        return STATES[key]
    # Fall back to matching on the full name.
    for state in STATES.values():
        if state.name.upper() == key:
            return state
    raise KeyError(f"unknown state: {value!r}")


def state_for_ssa_code(ssa_code: str) -> State | None:
    """Return the state whose CCN prefix matches ``ssa_code`` (or None)."""

    return _BY_SSA.get((ssa_code or "").strip())
