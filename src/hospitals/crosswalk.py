"""Build the NPI -> CCN crosswalk from CMS's own hospital enrollment file.

Linking price-transparency files to hospitals needs a bridge: the MRF schema
carries an organizational **NPI**, the Provider of Services file is keyed by
**CCN**, and neither contains the other. Without that bridge the linker falls
back to matching hospital names, which fails on exactly the files that hold the
most data — 549 of 835 sources, 632 million charge rows, published under names
like ``dignity-health`` or ``atrium-health-hospitals-inc``. There is no hospital
called Dignity Health; there are twenty. No amount of name matching resolves
that, and the NPI resolves it exactly.

CMS publishes the bridge as **Hospital Enrollments**, which lists both
identifiers for every enrolled hospital. This fetches it through the same
catalog machinery as the POS file and loads it into ``npi_ccn_crosswalk``.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from sqlalchemy.engine import Engine

from . import cms_pos
from .link import write_crosswalk_rows
from .logging_config import get_logger

log = get_logger(__name__)

HOSPITAL_ENROLLMENT_TITLE = "Hospital Enrollments"

# CMS is not consistent about these across files and revisions, so each is
# looked up by any of the names it is known to travel under. Compared after
# normalization, so "Organization Name" and "ORGANIZATION_NAME" are one key.
_NPI_COLUMNS = ("NPI", "NPI_NUM", "NPI_NUMBER", "ORG_NPI")
_CCN_COLUMNS = ("CCN", "CCN_NUM", "MEDICARE_CCN", "PRVDR_NUM", "PROVIDER_NUMBER")
_NAME_COLUMNS = (
    "ORGANIZATION_NAME",
    "ORG_NAME",
    "DOING_BUSINESS_AS_NAME",
    "PROVIDER_NAME",
    "FAC_NAME",
)


def _normalize_key(key: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (key or "").upper()).strip("_")


def _resolve_columns(row: dict) -> tuple[str | None, str | None, str | None]:
    """Find the NPI, CCN and name columns in a row, by any known spelling."""

    lookup = {_normalize_key(k): k for k in row}

    def pick(candidates):
        for candidate in candidates:
            if candidate in lookup:
                return lookup[candidate]
        return None

    return pick(_NPI_COLUMNS), pick(_CCN_COLUMNS), pick(_NAME_COLUMNS)


@dataclass
class CrosswalkSummary:
    rows_read: int = 0
    loaded: int = 0
    conflicts: list[str] = field(default_factory=list)
    source_title: str | None = None
    source_modified: str | None = None

    @property
    def conflict_count(self) -> int:
        return len(self.conflicts)


def fetch_crosswalk(
    engine: Engine,
    *,
    dataset_title: str = HOSPITAL_ENROLLMENT_TITLE,
    session=None,
    source: str = "cms_hospital_enrollments",
) -> CrosswalkSummary:
    """Download CMS Hospital Enrollments and load NPI -> CCN into the database.

    An NPI can appear more than once — a hospital may hold several enrollments
    — so the first CCN seen for an NPI wins and any disagreement is counted and
    reported. Silently keeping the last would make the result depend on row
    order, which is not a property anyone should have to reason about.
    """

    distribution = cms_pos.discover_latest_distribution(
        session=session, dataset_title=dataset_title
    )
    log.info(
        "Building the NPI->CCN crosswalk from %s (modified=%s)",
        distribution.title,
        distribution.modified,
    )

    summary = CrosswalkSummary(
        source_title=distribution.title, source_modified=distribution.modified
    )
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    npi_col = ccn_col = name_col = None
    by_npi: dict[str, dict] = {}

    for raw in cms_pos.iter_distribution_records(distribution, session=session):
        summary.rows_read += 1
        if npi_col is None:
            npi_col, ccn_col, name_col = _resolve_columns(raw)
            if not npi_col or not ccn_col:
                raise LookupError(
                    f"{distribution.title!r} has no NPI/CCN columns to build a "
                    f"crosswalk from; saw {sorted(raw)[:15]}"
                )
            log.info("Reading NPI from %r and CCN from %r", npi_col, ccn_col)

        npi = (raw.get(npi_col) or "").strip()
        ccn = (raw.get(ccn_col) or "").strip().upper()
        if not npi or not ccn:
            continue

        existing = by_npi.get(npi)
        if existing is None:
            by_npi[npi] = {
                "npi": npi,
                "ccn": ccn,
                "name": ((raw.get(name_col) or "").strip() or None) if name_col else None,
                "source": source,
                "loaded_at": now,
            }
        elif existing["ccn"] != ccn:
            summary.conflicts.append(npi)

    summary.loaded = write_crosswalk_rows(engine, list(by_npi.values()))
    log.info(
        "Crosswalk: %d row(s) read, %d NPI->CCN pairs loaded, %d NPI(s) mapped to "
        "more than one CCN (first kept)",
        summary.rows_read,
        summary.loaded,
        summary.conflict_count,
    )
    return summary
