"""Where a hospital's patients come from, and who else is taking them.

A price database answers "what does this hospital charge". It cannot answer
the question anyone actually asks next — *does it matter?* A hospital charging
double for a knee replacement is a footnote if it does eleven of them and the
system down the road does nine hundred. Price without volume and without
geography is a number nobody can act on.

CMS's **Hospital Service Area File** supplies the missing half. One row per
hospital per patient ZIP code, with the Medicare inpatient cases, days and
charges behind it. It is keyed by Medicare provider number, which is the CCN
this package already keys hospitals by, so it joins with no crosswalk in
between — unlike every other identifier in this domain, which is most of why
this dataset is worth reaching for first.

What it gives you, once loaded:

* the ZIPs a hospital actually draws from, ranked
* for any ZIP, every hospital competing for it and each one's share
* outmigration — where a rural county's patients go when they leave it
* the competitor set for a hospital, defined by shared patients rather than
  by distance, which is the definition that matches how patients behave

**Three limits, stated here because they are easy to forget downstream.**

*Medicare fee-for-service only.* Medicare Advantage is more than half of
Medicare enrollment and is absent from this file. The undercount is not
uniform: MA penetration swings widely by county, so a hospital in a high-MA
market looks smaller than an identical one in a low-MA market. Market shares
computed from this file are shares *of FFS*, and calling them anything else
is wrong.

*Inpatient only.* No outpatient, no commercial, no Medicaid.

*Small cells are suppressed, not zero.* CMS withholds cells below its
publication threshold. Those arrive as ``suppressed=True`` with a NULL count,
never as 0, because the difference between "too few to publish" and "none"
is the difference between a hospital's real rural draw and an empty map.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from sqlalchemy import delete, func, insert, select
from sqlalchemy.engine import Engine

from . import cms_pos
from .db import hospital_service_area, hospitals
from .logging_config import get_logger

log = get_logger(__name__)

HSA_DATASET_TITLE = "Hospital Service Area File"

# CMS renames and re-cases these between editions, so each field is looked up
# by any name it is known to travel under, compared after normalization.
_CCN_COLUMNS = (
    "MEDICARE_PROV_NUM",
    "MEDICARE_PROVIDER_NUMBER",
    "PROVIDER_ID",
    "PROVIDER_NUMBER",
    "PRVDR_NUM",
    "CCN",
)
_ZIP_COLUMNS = (
    "ZIP_CD_OF_RESIDENCE",
    "ZIP_CODE_OF_RESIDENCE",
    "ZIP_CD",
    "ZIP_CODE",
    "ZIP",
    "PATIENT_ZIP",
)
_CASES_COLUMNS = ("TOTAL_CASES", "TOTAL_DISCHARGES", "CASES", "DISCHARGES")
_DAYS_COLUMNS = ("TOTAL_DAYS_OF_CARE", "TOTAL_DAYS", "DAYS_OF_CARE", "DAYS")
_CHARGES_COLUMNS = ("TOTAL_CHARGES", "CHARGES")

#: What CMS writes where a cell was withheld. Never parsed as a number.
_SUPPRESSION_MARKERS = frozenset({"*", "**", ".", "-", "na", "n/a", "suppressed", ""})

_CCN_RE = re.compile(r"^[0-9]{2}[0-9A-Z]{4}$")


def _normalize_key(key: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (key or "").upper()).strip("_")


def _resolve_columns(row: dict) -> dict:
    """Map our field names to this file's actual column names."""

    present = {_normalize_key(k): k for k in row}

    def pick(candidates):
        for name in candidates:
            if name in present:
                return present[name]
        return None

    return {
        "ccn": pick(_CCN_COLUMNS),
        "zip5": pick(_ZIP_COLUMNS),
        "cases": pick(_CASES_COLUMNS),
        "days": pick(_DAYS_COLUMNS),
        "charges": pick(_CHARGES_COLUMNS),
    }


def normalize_ccn(raw) -> str | None:
    """A CCN as this package stores it, or None if it is not one.

    Provider numbers arrive from CSV readers as text, but a spreadsheet in the
    chain turns ``070027`` into ``70027``. Left-padding to six recovers those;
    anything that still does not look like a CCN is dropped rather than stored
    as a key that will never join.
    """

    text = str(raw or "").strip().upper()
    if not text:
        return None
    if text.isdigit() and len(text) < 6:
        text = text.zfill(6)
    return text if _CCN_RE.match(text) else None


def normalize_zip(raw) -> str | None:
    """The five-digit ZIP, or None. ZIP+4 is truncated; leading zeros restored."""

    digits = re.sub(r"[^0-9]", "", str(raw or ""))
    if not digits:
        return None
    if len(digits) == 9:  # ZIP+4 arriving unpunctuated
        digits = digits[:5]
    if len(digits) > 5:
        return None
    return digits.zfill(5)


def parse_count(raw) -> tuple[int | None, bool]:
    """Return ``(value, suppressed)``.

    The one rule this file demands: a withheld cell is not a zero. CMS marks
    them with an asterisk and reading that as 0 asserts the hospital served
    nobody in that ZIP — the opposite of what it means, and concentrated in
    exactly the small rural hospitals whose volumes are hardest to see.
    """

    text = str(raw or "").strip()
    if text.lower() in _SUPPRESSION_MARKERS:
        return None, True
    try:
        return int(float(text.replace(",", "").replace("$", ""))), False
    except (TypeError, ValueError):
        return None, True


def parse_amount(raw) -> float | None:
    text = str(raw or "").strip()
    if text.lower() in _SUPPRESSION_MARKERS:
        return None
    try:
        return float(text.replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


@dataclass
class ServiceAreaSummary:
    source_title: str | None = None
    source_modified: str | None = None
    edition: str | None = None
    rows_read: int = 0
    loaded: int = 0
    suppressed: int = 0
    unusable: int = 0
    unknown_ccns: set = field(default_factory=set)
    #: Distinct hospitals the load actually covered.
    hospitals_covered: int = 0


def write_rows(engine: Engine, rows: list[dict], batch_size: int = 5000) -> int:
    """Replace this edition's rows for the hospitals in ``rows``.

    Delete-then-insert per edition rather than upsert, so re-running a load
    after CMS revises a file leaves one edition's worth of rows rather than
    two overlapping ones.
    """

    if not rows:
        return 0

    editions = {r["edition"] for r in rows}
    ccns = {r["ccn"] for r in rows}

    with engine.begin() as conn:
        ccn_list = sorted(ccns)
        for start in range(0, len(ccn_list), 500):
            chunk = ccn_list[start : start + 500]
            conn.execute(
                delete(hospital_service_area).where(
                    hospital_service_area.c.edition.in_(editions),
                    hospital_service_area.c.ccn.in_(chunk),
                )
            )
        for start in range(0, len(rows), batch_size):
            conn.execute(insert(hospital_service_area), rows[start : start + batch_size])
    return len(rows)


def fetch_service_area(
    engine: Engine,
    *,
    dataset_title: str = HSA_DATASET_TITLE,
    session=None,
    source: str = "cms_hospital_service_area",
    known_ccns_only: bool = True,
    limit: int | None = None,
) -> ServiceAreaSummary:
    """Download the Hospital Service Area File and load it.

    ``known_ccns_only`` keeps only hospitals already in the POS roster. The
    file covers every Medicare-billing facility including ones this package
    does not track, and loading those would put rows in the table that no
    query can ever join to a hospital name.
    """

    distribution = cms_pos.discover_latest_distribution(
        session=session, dataset_title=dataset_title
    )
    log.info(
        "Loading patient origins from %s (modified=%s)",
        distribution.title,
        distribution.modified,
    )

    edition = str(distribution.modified or distribution.title)
    summary = ServiceAreaSummary(
        source_title=distribution.title,
        source_modified=distribution.modified,
        edition=edition,
    )
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    known: set[str] = set()
    if known_ccns_only:
        with engine.connect() as conn:
            known = {r.ccn for r in conn.execute(select(hospitals.c.ccn)) if r.ccn}
        if not known:
            raise LookupError(
                "No hospitals in this database to attach patient origins to. "
                "Run 'hospitals ingest --state ALL' first, or pass "
                "known_ccns_only=False to load every provider in the file."
            )

    columns: dict | None = None
    batch: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for raw in cms_pos.iter_distribution_records(
        distribution, hospitals_only=False, session=session
    ):
        summary.rows_read += 1
        if columns is None:
            columns = _resolve_columns(raw)
            if not columns["ccn"] or not columns["zip5"]:
                raise LookupError(
                    f"{distribution.title!r} has no provider/ZIP columns to read; "
                    f"saw {sorted(raw)[:15]}"
                )
            log.info(
                "Reading provider from %r and ZIP from %r",
                columns["ccn"],
                columns["zip5"],
            )

        ccn = normalize_ccn(raw.get(columns["ccn"]))
        zip5 = normalize_zip(raw.get(columns["zip5"]))
        if not ccn or not zip5:
            summary.unusable += 1
            continue
        if known and ccn not in known:
            summary.unknown_ccns.add(ccn)
            continue
        # One file has been seen listing a provider/ZIP pair twice across its
        # parts; the unique constraint would reject the whole batch for it.
        if (ccn, zip5) in seen:
            continue
        seen.add((ccn, zip5))

        cases, suppressed = parse_count(raw.get(columns["cases"]) if columns["cases"] else None)
        days, _ = parse_count(raw.get(columns["days"]) if columns["days"] else None)
        if suppressed:
            summary.suppressed += 1

        batch.append(
            {
                "ccn": ccn,
                "zip5": zip5,
                "edition": edition,
                "cases": cases,
                "days": days,
                "charges": parse_amount(
                    raw.get(columns["charges"]) if columns["charges"] else None
                ),
                "suppressed": suppressed,
                "source": source,
                "loaded_at": now,
            }
        )
        if limit and len(batch) >= limit:
            break

    summary.loaded = write_rows(engine, batch)
    summary.hospitals_covered = len({r["ccn"] for r in batch})
    log.info(
        "Service area: %d row(s) read, %d loaded for %d hospital(s), "
        "%d suppressed cell(s), %d unusable, %d provider(s) not in the roster",
        summary.rows_read,
        summary.loaded,
        summary.hospitals_covered,
        summary.suppressed,
        summary.unusable,
        len(summary.unknown_ccns),
    )
    return summary


# --- reading it back -------------------------------------------------------


@dataclass
class DrawZip:
    """One ZIP a hospital draws from, and how much of that ZIP it holds."""

    zip5: str
    cases: int
    zip_total: int
    #: Share of the ZIP's *fee-for-service Medicare inpatient* cases. Not share
    #: of the market — Medicare Advantage is absent from this file entirely.
    share: float


@dataclass
class Competitor:
    ccn: str
    name: str | None
    state: str | None
    #: Cases this hospital takes in ZIPs the subject also serves.
    overlap_cases: int
    shared_zips: int


def hospital_label(engine: Engine, ccn: str) -> str:
    """"NAME (City, ST)" for display, or the bare CCN if we do not know it."""

    with engine.connect() as conn:
        row = conn.execute(
            select(hospitals.c.name, hospitals.c.city, hospitals.c.state).where(
                hospitals.c.ccn == ccn
            )
        ).first()
    if not row or not row.name:
        return ccn
    where = ", ".join(p for p in (row.city, row.state) if p)
    return f"{row.name} ({where})" if where else row.name


def draw_area(engine: Engine, ccn: str, *, limit: int = 25) -> list[DrawZip]:
    """The ZIPs one hospital draws from, biggest first, with its share of each.

    Suppressed cells are excluded rather than counted as zero, so a share is
    computed only from ZIPs where both this hospital's and the ZIP's totals
    were actually published.
    """

    published = hospital_service_area.c.suppressed.is_(False)

    with engine.connect() as conn:
        mine = {
            r.zip5: r.cases
            for r in conn.execute(
                select(hospital_service_area.c.zip5, hospital_service_area.c.cases)
                .where(hospital_service_area.c.ccn == ccn, published)
                .where(hospital_service_area.c.cases.isnot(None))
            )
        }
        if not mine:
            return []

        totals = dict(
            conn.execute(
                select(
                    hospital_service_area.c.zip5,
                    func.sum(hospital_service_area.c.cases),
                )
                .where(hospital_service_area.c.zip5.in_(list(mine)), published)
                .group_by(hospital_service_area.c.zip5)
            ).all()
        )

    rows = [
        DrawZip(
            zip5=z,
            cases=c,
            zip_total=totals.get(z) or c,
            share=round(c / (totals.get(z) or c), 4),
        )
        for z, c in mine.items()
    ]
    rows.sort(key=lambda d: -d.cases)
    return rows[:limit]


def competitors(engine: Engine, ccn: str, *, limit: int = 15) -> list[Competitor]:
    """Who else serves this hospital's ZIPs, ranked by cases taken there.

    A competitor set defined by shared patients rather than by distance, which
    is the definition that matches how people actually choose a hospital: the
    system forty miles away with the cardiac program competes, and the one
    across the street that does only psychiatry does not.
    """

    published = hospital_service_area.c.suppressed.is_(False)

    with engine.connect() as conn:
        my_zips = [
            r.zip5
            for r in conn.execute(
                select(hospital_service_area.c.zip5).where(
                    hospital_service_area.c.ccn == ccn, published
                )
            )
        ]
        if not my_zips:
            return []

        rows = conn.execute(
            select(
                hospital_service_area.c.ccn,
                func.sum(hospital_service_area.c.cases).label("overlap"),
                func.count(hospital_service_area.c.zip5).label("zips"),
            )
            .where(
                hospital_service_area.c.zip5.in_(my_zips),
                hospital_service_area.c.ccn != ccn,
                published,
                hospital_service_area.c.cases.isnot(None),
            )
            .group_by(hospital_service_area.c.ccn)
            .order_by(func.sum(hospital_service_area.c.cases).desc())
            .limit(limit)
        ).all()

        names = {
            r.ccn: (r.name, r.state)
            for r in conn.execute(
                select(hospitals.c.ccn, hospitals.c.name, hospitals.c.state).where(
                    hospitals.c.ccn.in_([r.ccn for r in rows])
                )
            )
        }

    return [
        Competitor(
            ccn=r.ccn,
            name=names.get(r.ccn, (None, None))[0],
            state=names.get(r.ccn, (None, None))[1],
            overlap_cases=int(r.overlap or 0),
            shared_zips=int(r.zips or 0),
        )
        for r in rows
    ]
